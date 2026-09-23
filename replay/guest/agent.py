"""agent.py — Rollback-safe SWE-bench agent.

Architecture (post-2026-04-18 rewrite):

  Agent is a single-threaded event-driven state machine. It owns NO
  outward-facing socket at any point in its lifetime, including while
  it waits for an LLM response. Concretely, the main loop does:

      select(pipe_in, npd_notify_fifo, template_ctrl_fifo)
        → pipe_in readable:       new task from runner
        → npd_notify readable:    LLM response ready for some rid
        → template_ctrl readable: warm-template fork command from GSD

  When a task arrives, the agent writes the request payload to a file
  in NPD_REQ_DIR, then writes the rid to NPD_REQ_FIFO. All outgoing
  communication with NPD uses files + FIFOs; no UDS, no TCP. NPD runs
  the HTTPS call in a background thread and eventually writes the
  response to NPD_RESP_DIR and a notify line to NPD_NOTIFY_FIFO.

  This decouples LLM wait time from agent's checkpointable state. CRIU
  can dump the agent at any main-loop quiescent point (which is almost
  always, since there's no HTTPS client or UDS peer in the agent's
  address space) without "Can't dump half of stream unix connection".

Trace events kept for continuity: agent_step_begin, llm_req, llm_resp,
agent_step_end, agent_parse_error. pending[rid] carries the per-step
context (start_ts, n_msgs, temperature, prompt_chars) so llm_resp
latency can still be reported in ms.

Stale notify handling: after warm-template fork + rollback, the
restored agent inherits its parent's `pending` snapshot. By invariant
(save_step only runs between steps, i.e. when pending is empty) this
dict is empty on restore. Any notify that arrives for an unknown rid
— typically from an abandoned MCTS branch whose LLM call completed
after the branch was killed — is silently dropped and the orphan
response file is unlinked.
"""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
import traceback
import uuid
import ast
import fnmatch
import hashlib
import shlex
from urllib.error import URLError
from urllib.request import Request, urlopen

if os.environ.get('DELTABOX_RESTORE_DIAGNOSTICS') == '1':
    from restore_diagnostics import install_agent
    install_agent()

if os.environ.get('DELTABOX_DUMP_DIAGNOSTICS') == '1':
    from dump_diagnostics import install_agent
    install_agent()

MODEL_NAME = os.environ.get("MODEL_NAME", "claude-sonnet-4-6")

# Warm-template fork driver (set by AGENT_WARM_TEMPLATE=1).
WARM_TEMPLATE = os.environ.get("AGENT_WARM_TEMPLATE", "0") == "1"

# NPD channel. All four paths are required when NPD is on.
NPD_REQ_FIFO    = os.environ.get("NPD_REQ_FIFO",    "/tmp/npd_req.fifo")
NPD_NOTIFY_FIFO = os.environ.get("NPD_NOTIFY_FIFO", "/tmp/npd_notify.fifo")
NPD_REQ_DIR     = os.environ.get("NPD_REQ_DIR",     "/tmp/npd_requests")
NPD_RESP_DIR    = os.environ.get("NPD_RESP_DIR",    "/tmp/npd_responses")
NPD_EPOCH_FILE  = os.environ.get("NPD_EPOCH_FILE",  "/tmp/npd_current_epoch")

# Per-agent in-flight LLM cap. Gateway ai.prism.uno throttles above ~3
# concurrent on sonnet-4-6; 2 is enough for agent_step ⫣ value overlap
# without bursting. Enforced with a plain int counter under a single-
# threaded event loop (no locking needed).
LLM_INFLIGHT_CAP = int(os.environ.get("AGENT_LLM_CAP", "2"))

PIPE_IN  = "/tmp/agent.in"   # Runner → Agent
PIPE_OUT = "/tmp/agent.out"  # Agent → Runner
LOG_FILE = "/tmp/agent_trace.log"

TRACE_PATH = os.environ.get("AGENT_TRACE_PATH", "/tmp/agent_trace.jsonl")
TRACE_CTX = {
    "instance_id": os.environ.get("AGENT_INSTANCE_ID", ""),
    "run_id":      os.environ.get("AGENT_RUN_ID", ""),
    "strategy":    os.environ.get("AGENT_STRATEGY", ""),
    "agent_idx":   os.environ.get("AGENT_IDX", "0"),
}

ACTIVE_WORKER_STATE: dict = {}
WORKER_INDEX_STATE: dict = {}
WORKER_ROOT = os.environ.get("AGENT_WORKER_ROOT", "/testbed")
WORKER_PYTHON = os.environ.get("AGENT_WORKER_PYTHON", "python3")
WORKER_TEST_RUNNER = os.environ.get("AGENT_WORKER_TEST_RUNNER", "pytest")
WORKER_CMD_TIMEOUT = float(os.environ.get("AGENT_WORKER_CMD_TIMEOUT", "30"))
WORKER_MAX_OUTPUT = int(os.environ.get("AGENT_WORKER_MAX_OUTPUT", "65536"))
WORKER_INDEX_MAX_FILE_BYTES = int(os.environ.get(
    "AGENT_WORKER_INDEX_MAX_FILE_BYTES", str(2 << 20)))
WORKER_INDEX_MAX_TOTAL_BYTES = int(os.environ.get(
    "AGENT_WORKER_INDEX_MAX_TOTAL_BYTES", str(512 << 20)))
WORKER_INDEX_SIDECAR_URL = os.environ.get(
    "AGENT_WORKER_INDEX_SIDECAR_URL", "").rstrip("/")
WORKER_INDEX_SIDECAR_TIMEOUT = float(os.environ.get(
    "AGENT_WORKER_INDEX_SIDECAR_TIMEOUT", "30"))


def _extract_first_json(text):
    """Find the first valid JSON object in arbitrary model output.

    Handles ```json fences, leading prose, multiple back-to-back objects.
    """
    s = text.strip()
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip()
    if s.endswith("```"):
        s = s[:-3].rstrip()
    dec = json.JSONDecoder()
    i = 0
    while i < len(s):
        b = s.find("{", i)
        if b == -1:
            return None
        try:
            obj, _end = dec.raw_decode(s[b:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = b + 1
    return None


def log_message(message: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            ts = time.strftime("%H:%M:%S", time.localtime())
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def trace_event(kind: str, **fields) -> None:
    try:
        ev = {"ts": time.time(), "kind": kind, **TRACE_CTX, **fields}
        with open(TRACE_PATH, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception as e:
        log_message(f"[trace] write failed: {e}")


# Kept for import compatibility with value_agent / discriminator.
def _classify_error(code, body):
    if code == 429:
        return "rate_limit_429"
    low = (body or "").lower()
    if any(tok in low for tok in ("rate limit", "rate_limit",
                                  "too many requests", "rpm")):
        return "rate_limit_body"
    if code == 500 and "bad_response_status_code" in low:
        return "gateway_500_overload"
    if code and 400 <= code < 500:
        return f"client_{code}"
    if code and code >= 500:
        return f"server_{code}"
    return "unknown"


def _is_overload(code, body):
    return _classify_error(code, body) != "unknown"


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, path)


def _read_current_epoch() -> int:
    try:
        with open(NPD_EPOCH_FILE) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _send_llm_request(req_fd: int, messages, temperature: float,
                      max_tokens: int = 4000) -> str:
    """Write the request payload file and signal NPD. Returns the rid.

    rid format `ep<N>.<uuid>`: N is the epoch read from NPD_EPOCH_FILE at
    submit time. GSD bumps this file on every CRIU restore; NPD compares
    the rid's epoch against the current one at response-emit time and
    drops responses from rolled-away trajectories. See overlap_design.md.
    """
    epoch = _read_current_epoch()
    rid = f"ep{epoch}.{uuid.uuid4().hex}"
    req_path = os.path.join(NPD_REQ_DIR, f"{rid}.json")
    _atomic_write_json(req_path, {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
    os.write(req_fd, (rid + "\n").encode())
    return rid


def _read_response_file(rid: str) -> dict | None:
    resp_path = os.path.join(NPD_RESP_DIR, f"{rid}.json")
    try:
        with open(resp_path) as f:
            resp = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log_message(f"[npd] resp read failed rid={rid}: {e}")
        return None
    try:
        os.unlink(resp_path)
    except OSError:
        pass
    return resp


def _proc_footprint() -> dict:
    out = {
        "pid": os.getpid(),
        "rss_kb": None,
        "pss_kb": None,
        "private_dirty_kb": None,
        "vm_size_kb": None,
        "threads": None,
    }
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                out["rss_kb"] = int(line.split()[1])
            elif line.startswith("VmSize:"):
                out["vm_size_kb"] = int(line.split()[1])
            elif line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
    except OSError as e:
        out["err"] = str(e)
        return out
    try:
        pss = private_dirty = 0
        for line in open("/proc/self/smaps"):
            if line.startswith("Pss:"):
                pss += int(line.split()[1])
            elif line.startswith("Private_Dirty:"):
                private_dirty += int(line.split()[1])
        out["pss_kb"] = pss
        out["private_dirty_kb"] = private_dirty
    except OSError as e:
        out["smaps_err"] = str(e)
    return out


def _worker_index_use_sidecar() -> bool:
    return bool(WORKER_INDEX_SIDECAR_URL)


def _worker_index_sidecar_call(method: str, **kwargs) -> dict:
    if not WORKER_INDEX_SIDECAR_URL:
        raise RuntimeError("worker index sidecar URL is not configured")
    payload = json.dumps(
        {"method": method, "kwargs": kwargs},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    req = Request(
        WORKER_INDEX_SIDECAR_URL + "/call",
        data=payload,
        headers={"Content-Type": "application/json", "Connection": "close"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=WORKER_INDEX_SIDECAR_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        raise RuntimeError(f"index sidecar unavailable: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"index sidecar returned non-object: {type(data).__name__}")
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or f"index sidecar failed: {data}")
    result = data.get("result")
    return result if isinstance(result, dict) else {"value": result}


def _worker_abspath(path: str, root: str = WORKER_ROOT) -> str:
    if not path or os.path.isabs(path):
        rel = (path or "").lstrip("/")
    else:
        rel = path
    full = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise ValueError(f"path escapes worker root: {path!r}")
    return full


def _worker_resolve_rel(path: str, root: str = WORKER_ROOT) -> tuple[str, str]:
    """Resolve recorded paths against the staged repo and indexed files."""
    if _worker_index_use_sidecar() and WORKER_INDEX_STATE:
        out = _worker_index_sidecar_call("resolve_rel", path=path, root=root)
        return str(out["full"]), str(out["rel"])
    full = _worker_abspath(path, root)
    rel = os.path.relpath(full, os.path.abspath(root))
    if os.path.exists(full) or rel in WORKER_INDEX_STATE.get("files", {}):
        return full, rel
    suffix = rel.lstrip("/")
    matches = [
        indexed
        for indexed in WORKER_INDEX_STATE.get("files", {})
        if indexed == suffix or indexed.endswith("/" + suffix)
    ]
    if len(matches) == 1:
        rel = matches[0]
        return _worker_abspath(rel, root), rel
    if len(matches) > 1:
        raise ValueError(f"ambiguous indexed path {path!r}: {matches[:8]}")
    return full, rel


def _short_text(text: str | bytes | None, limit: int = WORKER_MAX_OUTPUT) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _file_digest(path: str) -> dict:
    import hashlib

    try:
        st = os.stat(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return {
            "exists": True,
            "size": st.st_size,
            "sha256": h.hexdigest(),
        }
    except FileNotFoundError:
        return {"exists": False}


def _worker_relpath(path: str, root: str = WORKER_ROOT) -> str:
    return os.path.relpath(_worker_abspath(path, root), os.path.abspath(root))


def _index_should_skip(path: str) -> bool:
    parts = set(path.split(os.sep))
    if parts & {".git", "__pycache__", ".tox", ".venv", "node_modules"}:
        return True
    return path.endswith((".pyc", ".pyo", ".so", ".o", ".a", ".png", ".jpg",
                          ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz"))


def _extract_python_symbols(rel: str, text: str) -> tuple[list[str], list[str]]:
    classes: list[str] = []
    functions: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return classes, functions
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
    return classes, functions


def _worker_index_remove(rel: str) -> None:
    state = WORKER_INDEX_STATE
    entry = state.get("files", {}).pop(rel, None)
    if not entry:
        return
    for cls in entry.get("classes", []):
        rows = state.get("classes", {}).get(cls, [])
        rows = [r for r in rows if r.get("path") != rel]
        if rows:
            state["classes"][cls] = rows
        else:
            state.get("classes", {}).pop(cls, None)
    for fn in entry.get("functions", []):
        rows = state.get("functions", {}).get(fn, [])
        rows = [r for r in rows if r.get("path") != rel]
        if rows:
            state["functions"][fn] = rows
        else:
            state.get("functions", {}).pop(fn, None)


def _worker_index_one(path: str, root: str = WORKER_ROOT) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or st.st_size > WORKER_INDEX_MAX_FILE_BYTES:
        return None
    rel = os.path.relpath(path, root)
    if _index_should_skip(rel):
        return None
    try:
        with open(path, "r", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    classes, functions = _extract_python_symbols(rel, text) if rel.endswith(".py") else ([], [])
    text_sha256 = hashlib.sha256(text.encode(errors="ignore")).hexdigest()
    entry = {
        "path": rel,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "text_sha256": text_sha256,
        "text": text,
        "classes": classes,
        "functions": functions,
    }
    state = WORKER_INDEX_STATE
    _worker_index_remove(rel)
    state.setdefault("files", {})[rel] = entry
    for cls in classes:
        state.setdefault("classes", {}).setdefault(cls, []).append({"path": rel})
    for fn in functions:
        state.setdefault("functions", {}).setdefault(fn, []).append({"path": rel})
    return entry


def _worker_index_build(req: dict | None = None) -> dict:
    global WORKER_INDEX_STATE
    root = (req or {}).get("root") or WORKER_ROOT
    if _worker_index_use_sidecar():
        out = _worker_index_sidecar_call("build", root=root)
        WORKER_INDEX_STATE = {
            "sidecar": True,
            "root": root,
            "status": out,
        }
        out = dict(out)
        out["sidecar"] = True
        out["footprint"] = _proc_footprint()
        trace_event("worker_index_build", **{
            k: out.get(k)
            for k in ("n_files", "n_classes", "n_functions",
                      "total_bytes", "build_ms", "truncated", "sidecar")
        })
        return out
    t0 = time.time()
    files: dict = {}
    WORKER_INDEX_STATE = {
        "root": root,
        "files": files,
        "classes": {},
        "functions": {},
        "built_at": time.time(),
        "build_ms": None,
        "total_bytes": 0,
        "truncated": False,
    }
    total = 0
    n_seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in (".git", "__pycache__", ".tox", ".venv", "node_modules")
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if _index_should_skip(rel):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if total + size > WORKER_INDEX_MAX_TOTAL_BYTES:
                WORKER_INDEX_STATE["truncated"] = True
                break
            entry = _worker_index_one(path, root)
            if entry is not None:
                total += entry["size"]
                n_seen += 1
        if WORKER_INDEX_STATE["truncated"]:
            break
    WORKER_INDEX_STATE["total_bytes"] = total
    WORKER_INDEX_STATE["build_ms"] = (time.time() - t0) * 1000
    out = _worker_index_status()
    out["ok"] = True
    trace_event("worker_index_build", **{
        k: out.get(k)
        for k in ("n_files", "n_classes", "n_functions",
                  "total_bytes", "build_ms", "truncated")
    })
    return out


def _worker_index_status() -> dict:
    if _worker_index_use_sidecar():
        try:
            out = _worker_index_sidecar_call("status")
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "loaded": False,
                "sidecar": True,
                "err": type(e).__name__,
                "msg": str(e),
                "footprint": _proc_footprint(),
            }
        out = dict(out)
        out["sidecar"] = True
        out["footprint"] = _proc_footprint()
        return out
    state = WORKER_INDEX_STATE
    fingerprint = None
    metadata_fingerprint = None
    sample_query = None
    root = state.get("root") if state else WORKER_ROOT
    root_entries: list[str] = []
    try:
        root_entries = sorted(os.listdir(root))[:16]
    except OSError:
        root_entries = []
    if state:
        h = hashlib.sha256()
        mh = hashlib.sha256()
        for rel in sorted(state.get("files", {})):
            entry = state["files"][rel]
            parts = (
                rel.encode(),
                str(entry.get("size", 0)).encode(),
                (entry.get("text_sha256") or "").encode(),
            )
            for part in parts:
                h.update(part)
                mh.update(part)
            mh.update(str(entry.get("mtime_ns", 0)).encode())
            for cls in sorted(entry.get("classes", [])):
                h.update(b"C")
                h.update(cls.encode())
                mh.update(b"C")
                mh.update(cls.encode())
            for fn in sorted(entry.get("functions", [])):
                h.update(b"F")
                h.update(fn.encode())
                mh.update(b"F")
                mh.update(fn.encode())
        fingerprint = h.hexdigest()
        metadata_fingerprint = mh.hexdigest()
        if state.get("classes"):
            name = sorted(state["classes"])[0]
            sample_query = {
                "kind": "class",
                "name": name,
                "hits": state["classes"].get(name, [])[:8],
            }
        elif state.get("functions"):
            name = sorted(state["functions"])[0]
            sample_query = {
                "kind": "function",
                "name": name,
                "hits": state["functions"].get(name, [])[:8],
            }
    return {
        "ok": True,
        "loaded": bool(state),
        "root": root,
        "root_exists": os.path.isdir(root),
        "root_is_mount": os.path.ismount(root),
        "root_entries": root_entries,
        "n_files": len(state.get("files", {})),
        "n_classes": len(state.get("classes", {})),
        "n_functions": len(state.get("functions", {})),
        "total_bytes": state.get("total_bytes", 0),
        "build_ms": state.get("build_ms"),
        "truncated": state.get("truncated", False),
        "fingerprint": fingerprint,
        "metadata_fingerprint": metadata_fingerprint,
        "sample_query": sample_query,
        "footprint": _proc_footprint(),
    }


def _worker_index_match_paths(file_pattern: str | None) -> list[str]:
    if _worker_index_use_sidecar() and WORKER_INDEX_STATE:
        out = _worker_index_sidecar_call(
            "match_paths", file_pattern=file_pattern)
        value = out.get("matches")
        return value if isinstance(value, list) else []
    state = WORKER_INDEX_STATE
    files = state.get("files", {})
    if not file_pattern:
        return list(files)
    pat = file_pattern
    return [
        rel for rel in files
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch("/" + rel, pat)
    ]


def _worker_index_refresh_path(path: str, root: str = WORKER_ROOT) -> None:
    if _worker_index_use_sidecar() and WORKER_INDEX_STATE:
        _worker_index_sidecar_call("refresh_path", path=path, root=root)
        return
    rel = _worker_relpath(path, root)
    _worker_index_remove(rel)
    if os.path.exists(_worker_abspath(path, root)):
        _worker_index_one(_worker_abspath(path, root), root)


def _worker_index_snapshot(snapshot_id: str) -> dict:
    if not _worker_index_use_sidecar():
        return {"ok": False, "err": "sidecar_not_enabled"}
    out = _worker_index_sidecar_call("snapshot", snapshot_id=snapshot_id)
    out = dict(out)
    out["sidecar"] = True
    out["footprint"] = _proc_footprint()
    return out


def _worker_index_restore_snapshot(snapshot_id: str) -> dict:
    if not _worker_index_use_sidecar():
        return {"ok": False, "err": "sidecar_not_enabled"}
    out = _worker_index_sidecar_call(
        "restore_snapshot", snapshot_id=snapshot_id)
    out = dict(out)
    out["sidecar"] = True
    out["footprint"] = _proc_footprint()
    return out


def _read_file(path: str, limit: int = WORKER_MAX_OUTPUT) -> str:
    with open(path, "r", errors="replace") as f:
        return f.read(limit)


def _worker_index_has_file(rel: str) -> bool:
    if _worker_index_use_sidecar() and WORKER_INDEX_STATE:
        out = _worker_index_sidecar_call("has_file", rel=rel)
        return bool(out.get("exists"))
    return rel in WORKER_INDEX_STATE.get("files", {})


def _worker_index_grep(pattern: str, file_pattern: str | None) -> dict:
    if _worker_index_use_sidecar() and WORKER_INDEX_STATE:
        return _worker_index_sidecar_call(
            "grep", pattern=pattern, file_pattern=file_pattern)
    pattern_mode = "regex"
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = None
        pattern_mode = "literal"
    hits = []
    for rel in _worker_index_match_paths(file_pattern):
        entry = WORKER_INDEX_STATE.get("files", {}).get(rel) or {}
        text = entry.get("text", "")
        for lineno, line in enumerate(text.splitlines(), 1):
            matched = (
                regex.search(line) if regex is not None
                else pattern in line
            )
            if matched:
                hits.append({
                    "path": rel,
                    "line": lineno,
                    "text": line.rstrip()[:300],
                })
                if len(hits) >= 64:
                    break
        if len(hits) >= 64:
            break
    return {
        "ok": True,
        "from_index": True,
        "pattern_mode": pattern_mode,
        "n_hits": len(hits),
        "hits": hits,
    }


def _worker_index_find_symbol(name: str, symbol_kind: str | None,
                              file_pattern: str | None) -> dict:
    if _worker_index_use_sidecar() and WORKER_INDEX_STATE:
        return _worker_index_sidecar_call(
            "find_symbol",
            name=name,
            symbol_kind=symbol_kind,
            file_pattern=file_pattern,
        )
    table = "classes" if symbol_kind == "class" else "functions"
    rows = WORKER_INDEX_STATE.get(table, {}).get(name, [])
    if file_pattern:
        allowed = set(_worker_index_match_paths(file_pattern))
        rows = [r for r in rows if r.get("path") in allowed]
    return {
        "ok": True,
        "from_index": True,
        "symbol": name,
        "symbol_kind": symbol_kind,
        "n_hits": len(rows),
        "hits": rows[:64],
    }


def _worker_shell_command_for_tests(op: dict) -> str:
    test_files = op.get("test_files") or []
    if not isinstance(test_files, list):
        test_files = [str(test_files)]
    if not test_files:
        return "true"
    # Keep the narrowly recorded missing-file probe on its existing pytest path.
    if WORKER_TEST_RUNNER == "django" and not op.get("expected_outcome"):
        labels = []
        for name in test_files:
            name = str(name)
            if name.startswith("tests/"):
                name = name[len("tests/"):]
            if name.endswith(".py"):
                name = name[:-3]
            labels.append(name.rstrip("/").replace("/", "."))
        return shlex.join([WORKER_PYTHON, "tests/runtests.py", "--settings=test_sqlite",
                           "--parallel=1", "--noinput", *labels])
    return shlex.join([WORKER_PYTHON, "-m", "pytest", "-q", *map(str, test_files)])


def _test_command_outcome(op: dict, returncode: int, stdout: str, stderr: str) -> str:
    if WORKER_TEST_RUNNER != "django" or op.get("command"):
        return "completed" if returncode in (0, 1) else "infrastructure-error"
    output = stdout + "\n" + stderr
    if (returncode in (0, 1)
            and re.search(r"^Ran [1-9][0-9]* tests? in ", output, re.MULTILINE)
            and re.search(r"^(?:OK(?: \(.*\))?|FAILED \(.*\))$", output, re.MULTILINE)):
        return "completed"
    # The native runner's settings/apps were validated before agent startup.
    # A model declared by the requested test without an app_label is a code
    # error in that test, not a missing Django environment. Preserve it as an
    # explicit collection failure: no tests passed or ran in this case.
    model_error = re.search(
        r"^RuntimeError: Model class ([A-Za-z_][\w.]*) doesn't declare an explicit app_label "
        r"and isn't in an application in INSTALLED_APPS\.$", stderr, re.MULTILINE)
    if returncode == 1 and model_error:
        for name in op.get("test_files") or []:
            name = str(name)
            if name.startswith("tests/") and name.endswith(".py"):
                module = name[len("tests/"):-3].replace("/", ".")
                if model_error.group(1).startswith(module + "."):
                    return "workload-collection-error"
    return "infrastructure-error"


def _matches_recorded_missing_tests(op: dict, root: str, returncode: int, stderr: str) -> bool:
    """Match an actually executed pytest failure to the recorded observation.

    No arbitrary expected return codes, command overrides, or new missing files
    are accepted. The original invalid action is still executed and timed.
    """
    expected = op.get("expected_outcome") or {}
    files = op.get("test_files")
    if (returncode != 4 or op.get("command") or expected.get("kind") != "missing_test_files"
            or not isinstance(files, list) or not files
            or not all(isinstance(name, str) and name for name in files)
            or expected.get("test_files") != files
            or expected.get("recorded_fail_reason") != "no_test_files"
            or expected.get("recorded_message") != "Unable to run tests: Files not found: " + ", ".join(files)):
        return False
    for name in files:
        path, _ = _worker_resolve_rel(name, root)
        if os.path.exists(path):
            return False
    prefix = "ERROR: file or directory not found: "
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith(prefix) and line[len(prefix):] in files for line in lines)


def _matches_recorded_test_directories(op: dict, root: str) -> bool:
    expected = op.get("expected_outcome") or {}
    files = op.get("test_files")
    if (op.get("command") or expected.get("kind") != "test_directories"
            or not isinstance(files, list) or not files
            or not all(isinstance(name, str) and name for name in files)
            or expected.get("test_files") != files
            or expected.get("recorded_fail_reason") != "no_test_files"
            or expected.get("recorded_message") != "Unable to run tests: Directories provided instead of files: " + ", ".join(files)):
        return False
    return all(os.path.isdir(_worker_resolve_rel(name, root)[0]) for name in files)


def _worker_exec_one(op: dict, root: str = WORKER_ROOT) -> dict:
    typ = op.get("type") or "noop"
    t0 = time.time()
    out: dict = {
        "type": typ,
        "action_class": op.get("action_class"),
        "path": op.get("path"),
    }
    try:
        if typ == "noop":
            out.update({"ok": True, "note": op.get("note", "")})
        elif typ == "view_file":
            path, rel = _worker_resolve_rel(op.get("path", ""), root)
            indexed = _worker_index_has_file(rel)
            out.update({
                "ok": True,
                "digest": _file_digest(path),
                "from_index": indexed,
                "resolved_path": rel,
                "preview": _short_text(_read_file(path)),
            })
        elif typ == "find_file_pattern":
            if not WORKER_INDEX_STATE:
                return {"ok": False, "type": typ,
                        "err": "worker_index_not_loaded"}
            pattern = op.get("file_pattern") or op.get("path") or "*"
            matches = _worker_index_match_paths(pattern)[:64]
            out.update({"ok": True, "n_matches": len(matches),
                        "matches": matches, "from_index": True})
        elif typ == "grep":
            if not WORKER_INDEX_STATE:
                return {"ok": False, "type": typ,
                        "err": "worker_index_not_loaded"}
            pattern = op.get("pattern") or ""
            file_pattern = op.get("file_pattern") or "**/*"
            out.update(_worker_index_grep(pattern, file_pattern))
        elif typ == "find_symbol":
            if not WORKER_INDEX_STATE:
                return {"ok": False, "type": typ,
                        "err": "worker_index_not_loaded"}
            name = op.get("name") or ""
            file_pattern = op.get("file_pattern")
            out.update(_worker_index_find_symbol(
                name, op.get("symbol_kind"), file_pattern))
        elif typ == "apply_recorded_diff":
            diff = op.get("diff", "")
            if not diff or "@@" not in diff:
                raise ValueError("Missing recorded unified diff")
            validation = op.get("recorded_file_validation")
            validated_path = None
            if validation is not None:
                if set(validation) != {"path", "before_sha256", "after_sha256"}:
                    raise ValueError("Invalid recorded file validation fields")
                validated_path, _ = _worker_resolve_rel(validation["path"], root)
                with open(validated_path, "rb") as stream:
                    before_hash = hashlib.sha256(stream.read()).hexdigest()
                if before_hash != validation["before_sha256"]:
                    raise ValueError("Recorded repaired diff: live before-file hash differs from snapshot evidence")
            strip = "1" if diff.startswith("diff --git a/") or diff.startswith("--- a/") else "0"
            command = ["git", "-C", root, "apply", "--recount", "--whitespace=nowarn", "-p" + strip]
            # A unified-diff transport line must end in LF. Several legacy
            # recordings stripped the final LF; restoring it does not add a
            # newline to the target file (the diff's no-newline marker governs
            # target content). Keep the original diff in the schedule.
            transport_newline_added = not diff.endswith("\n")
            patch_text = diff + "\n" if transport_newline_added else diff
            try:
                subprocess.run(command + ["--check", "-"], input=patch_text, text=True, check=True, capture_output=True)
                subprocess.run(command + ["-"], input=patch_text, text=True, check=True, capture_output=True)
            except subprocess.CalledProcessError as exc:
                raise ValueError("Recorded diff validation/application failed: " + (exc.stderr or str(exc))[:2000]) from exc
            if validation is not None:
                with open(validated_path, "rb") as stream:
                    after_hash = hashlib.sha256(stream.read()).hexdigest()
                if after_hash != validation["after_sha256"]:
                    raise ValueError("Recorded repaired diff: after-file hash differs from snapshot evidence")
                out["recorded_file_validation"] = dict(validation, matched=True)
            paths = []
            for line in diff.splitlines():
                if line.startswith("+++ "):
                    rel = line[4:].split("\t", 1)[0]
                    if rel == "/dev/null": continue
                    if strip == "1": rel = rel.split("/", 1)[1]
                    _, rel = _worker_resolve_rel(rel, root)
                    _worker_index_refresh_path(rel, root)
                    paths.append(rel)
            out.update({"ok": True, "recorded_diff": True, "updated_paths": paths,
                        "patch_transport_newline_added": transport_newline_added})
        elif typ == "replace":
            path, rel = _worker_resolve_rel(op.get("path", ""), root)
            old = op.get("old_str", "")
            new = op.get("new_str", "")
            with open(path, "r", errors="replace") as f:
                data = f.read()
            if old not in data:
                out.update({"ok": False, "err": "old_str_not_found",
                            "digest_before": _file_digest(path)})
            else:
                data2 = data.replace(old, new, 1)
                with open(path, "w") as f:
                    f.write(data2)
                _worker_index_refresh_path(rel, root)
                out.update({"ok": True, "resolved_path": rel,
                            "digest_after": _file_digest(path)})
        elif typ == "write_file":
            path, rel = _worker_resolve_rel(op.get("path", ""), root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(op.get("content", ""))
            _worker_index_refresh_path(rel, root)
            out.update({"ok": True, "resolved_path": rel,
                        "digest_after": _file_digest(path)})
        elif typ == "append_file":
            path, rel = _worker_resolve_rel(op.get("path", ""), root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(op.get("content", ""))
            _worker_index_refresh_path(rel, root)
            out.update({"ok": True, "resolved_path": rel,
                        "digest_after": _file_digest(path)})
        elif typ == "shell":
            cmd = op.get("command") or ""
            if not cmd:
                out.update({"ok": False, "err": "missing_command"})
            else:
                timeout = float(op.get("timeout", WORKER_CMD_TIMEOUT))
                out.update({"command": cmd, "timeout": timeout})
                cp = subprocess.run(
                    cmd, shell=True, cwd=root, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=timeout, executable="/bin/bash")
                out.update({
                    "ok": True,
                    "rc": cp.returncode,
                    "stdout": _short_text(cp.stdout),
                    "stderr": _short_text(cp.stderr),
                })
        elif typ == "run_tests" and (op.get("expected_outcome") or {}).get("kind") == "test_directories":
            # Moatless rejects directory-only selections before calling a test
            # subprocess. Repeat that filesystem validation on the restored tree.
            matched = _matches_recorded_test_directories(op, root)
            out.update({"ok": matched, "expected_failure_matched": matched,
                        "err": None if matched else "expected_outcome_mismatch",
                        "test_passed": False, "test_outcome": "invalid-test-selection",
                        "test_subprocess_started": False, "rc": None,
                        "stdout": "", "stderr": op["expected_outcome"].get("recorded_message", "")})
        elif typ == "run_tests":
            cmd = op.get("command") or _worker_shell_command_for_tests(op)
            timeout = float(op.get("timeout", WORKER_CMD_TIMEOUT))
            out.update({"command": cmd, "timeout": timeout})
            cp = subprocess.run(
                cmd, shell=True, cwd=root, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, executable="/bin/bash")
            expected_failure = _matches_recorded_missing_tests(op, root, cp.returncode, cp.stderr)
            has_expectation = "expected_outcome" in op
            test_outcome = _test_command_outcome(op, cp.returncode, cp.stdout, cp.stderr)
            operation_ok = expected_failure if has_expectation else test_outcome != "infrastructure-error"
            out.update({
                "ok": operation_ok,
                "err": None if operation_ok else "expected_outcome_mismatch" if has_expectation else "test_runner_infrastructure_failure",
                "expected_failure_matched": expected_failure,
                "test_passed": cp.returncode == 0 and test_outcome == "completed",
                "test_outcome": test_outcome,
                "command": cmd,
                "rc": cp.returncode,
                "stdout": _short_text(cp.stdout),
                "stderr": _short_text(cp.stderr),
            })
        else:
            out.update({"ok": False, "err": f"unknown_worker_op:{typ}"})
    except subprocess.TimeoutExpired as e:
        out.update({
            "ok": False,
            "err": "TimeoutExpired",
            "timeout": e.timeout,
            "stdout": _short_text(e.stdout),
            "stderr": _short_text(e.stderr),
        })
    except Exception as e:  # noqa: BLE001
        out.update({"ok": False, "err": type(e).__name__,
                    "msg": str(e)})
    out["wall_ms"] = (time.time() - t0) * 1000
    return out


def _clip_text_field(value, limit: int = 2048, preserve_tail: bool = False):
    if value is None:
        return value
    text = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    if len(text) <= limit:
        return text
    marker = f"...<truncated {len(text) - limit} chars>"
    if preserve_tail:
        tail = limit // 3
        return text[:limit - tail] + marker + text[-tail:]
    return text[:limit] + marker


def _clip_worker_result_for_control(result: dict) -> dict:
    """Return a small control-channel summary of a worker op result.

    Full grep/find/view outputs can exceed the FIFO capacity. The replay gates
    only need success/failure, timing, counts, digests, and enough failure
    context to classify run_tests timeouts.
    """
    if not isinstance(result, dict):
        return {"ok": False, "err": "invalid_worker_result"}
    keep_keys = (
        "type", "action_class", "path", "ok", "err", "msg", "wall_ms",
        "from_index", "pattern_mode", "n_hits", "n_matches", "symbol",
        "symbol_kind", "resolved_path", "digest", "digest_before",
        "digest_after", "rc", "timeout", "command",
        "expected_failure_matched", "test_passed", "test_outcome", "test_subprocess_started",
    )
    out = {k: result[k] for k in keep_keys if k in result}
    for key in ("stdout", "stderr", "preview"):
        if key in result:
            # A traceback's cause is at the end. Keep it on actual failures
            # without enlarging the FIFO payload or adding hot-path log I/O.
            out[key] = _clip_text_field(result.get(key), 2048,
                                        preserve_tail=result.get("ok") is False or result.get("test_passed") is False)
    if "hits" in result:
        hits = result.get("hits") or []
        out["hits_n"] = len(hits) if isinstance(hits, list) else None
        if isinstance(hits, list):
            clipped_hits = []
            for hit in hits[:4]:
                if isinstance(hit, dict):
                    clipped = {
                        k: hit.get(k)
                        for k in ("path", "line", "name", "kind")
                        if k in hit
                    }
                    if "text" in hit:
                        clipped["text"] = _clip_text_field(hit.get("text"), 160)
                    clipped_hits.append(clipped)
                else:
                    clipped_hits.append(_clip_text_field(hit, 160))
            out["hits_sample"] = clipped_hits
    if "matches" in result:
        matches = result.get("matches") or []
        out["matches_n"] = len(matches) if isinstance(matches, list) else None
        if isinstance(matches, list):
            out["matches_sample"] = [
                _clip_text_field(m, 200) for m in matches[:8]
            ]
    return out


def _worker_exec(req: dict) -> dict:
    ops = req.get("ops")
    if not isinstance(ops, list):
        return {"ok": False, "err": "missing_ops",
                "footprint": _proc_footprint()}
    root = req.get("root") or WORKER_ROOT
    op_summaries = [
        {
            "type": op.get("type"),
            "action_class": op.get("action_class"),
            "file_pattern": op.get("file_pattern"),
            "path": op.get("path"),
            "name": op.get("name"),
            "symbol_kind": op.get("symbol_kind"),
        }
        for op in ops
        if isinstance(op, dict)
    ]
    trace_event("worker_exec_begin",
                ctrl_id=req.get("ctrl_id"),
                n_ops=len(ops),
                ops=op_summaries[:8])
    full_results = [_worker_exec_one(op, root) for op in ops]
    ok = all(r.get("ok") for r in full_results)
    results = [_clip_worker_result_for_control(r) for r in full_results]
    payload = {
        "ok": ok,
        "n_ops": len(ops),
        "n_failed": sum(1 for r in full_results if not r.get("ok")),
        "results": results,
        "footprint": _proc_footprint(),
    }
    trace_event("worker_exec", ctrl_id=req.get("ctrl_id"),
                ok=ok, n_ops=len(ops),
                n_failed=payload["n_failed"])
    return payload


def _strip_recorded_tree(data: dict) -> dict:
    data = json.loads(json.dumps(data))
    root = data.get("root") or {}
    first_expansion_node_id = None
    stack = [root]
    while stack:
        node = stack.pop()
        comps = node.get("completions") or {}
        if comps.get("build_action"):
            first_expansion_node_id = node.get("node_id")
            break
        stack.extend(reversed(node.get("children") or []))
    root["children"] = []
    root["action_steps"] = []
    root["completions"] = {}
    for key in ("assistant_message", "output", "reward", "value",
                "error", "feedback_data"):
        if key in root:
            root[key] = None
    if "visits" in root:
        root["visits"] = 0
    if first_expansion_node_id is not None:
        data["unique_id"] = int(first_expansion_node_id) - 1
    else:
        data["unique_id"] = root.get("node_id", 0)
    data["root"] = root
    return data


def _rewrite_model_base_url(data: dict, new_url: str,
                            api_key: str = "dummy") -> int:
    n = 0
    agent = data.get("agent", {}) or {}
    if isinstance(agent.get("completion"), dict):
        agent["completion"]["model_base_url"] = new_url
        agent["completion"]["model_api_key"] = api_key
        n += 1
    for action in agent.get("actions", []) or []:
        cm = action.get("completion_model")
        if isinstance(cm, dict):
            cm["model_base_url"] = new_url
            cm["model_api_key"] = api_key
            n += 1
    return n


def _active_worker_status() -> dict:
    state = ACTIVE_WORKER_STATE
    if not state:
        return {
            "ok": True,
            "loaded": False,
            "footprint": _proc_footprint(),
        }
    tree = state.get("tree")
    code_index = state.get("code_index")
    root_nodes = None
    try:
        root_nodes = len(tree.root.get_all_nodes()) if tree is not None else None
    except Exception:
        root_nodes = None
    classes = functions = None
    try:
        classes = len(code_index._blocks_by_class_name)
        functions = len(code_index._blocks_by_function_name)
    except Exception:
        pass
    return {
        "ok": True,
        "loaded": True,
        "instance_id": state.get("instance_id"),
        "trajectory_path": state.get("trajectory_path"),
        "repo_path": state.get("repo_path"),
        "index_store_dir": state.get("index_store_dir"),
        "tree_object_id": id(tree) if tree is not None else None,
        "repo_object_id": id(state.get("repo")),
        "index_object_id": id(code_index) if code_index is not None else None,
        "root_node_count": root_nodes,
        "index_classes": classes,
        "index_functions": functions,
        "model_rewrites": state.get("model_rewrites"),
        "load_ms": state.get("load_ms"),
        "footprint": _proc_footprint(),
    }


def _active_worker_load(req: dict) -> dict:
    """Load real moatless replay state into this checkpointed agent process.

    This is deliberately fail-hard.  Report runs must not silently fall back to
    the idle FIFO shell: if moatless deps, repo, index, or trajectory are
    missing, the caller gets ok=false and the replay fails.
    """
    global ACTIVE_WORKER_STATE
    t0 = time.time()
    payload_root = req.get("spr_payload_root") or os.environ.get(
        "SPR_PAYLOAD_ROOT", "/tmp/spr_payload")
    moatless_src = req.get("moatless_src") or os.path.join(
        payload_root, "moatless-det-src")
    instance_id = req.get("instance_id")
    trajectory_path = req.get("trajectory_path") or os.path.join(
        payload_root, "trajectory.json")
    repo_path = req.get("repo_path") or os.path.join(
        payload_root, "repos", f"swe-bench_{instance_id}")
    index_store_dir = req.get("index_store_dir") or os.path.join(
        payload_root, "index_store")
    mock_url = req.get("mock_url", "http://127.0.0.1:9/v1")
    if not instance_id:
        return {"ok": False, "err": "missing_instance_id"}
    required_paths = {
        "moatless_src": moatless_src,
        "trajectory_path": trajectory_path,
        "repo_path": repo_path,
        "index_dir": os.path.join(index_store_dir, instance_id),
    }
    missing = {k: v for k, v in required_paths.items()
               if not os.path.exists(v)}
    if missing:
        return {"ok": False, "err": "missing_active_worker_paths",
                "missing": missing}
    try:
        for path in (payload_root, moatless_src):
            if path not in sys.path:
                sys.path.insert(0, path)
        os.environ.setdefault("PYTHONHASHSEED", "0")
        os.environ.setdefault("OPENAI_API_KEY", "dummy")
        os.environ.setdefault("CUSTOM_LLM_API_KEY", "dummy")
        os.environ.setdefault("LITELLM_LOG", "ERROR")
        os.environ.setdefault("FAISS_OPT_LEVEL", "generic")
        os.environ.setdefault("FAISS_DISABLE_CPU_FEATURES",
                              "AVX512_SPR,AVX512,AVX2")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OMP_THREAD_LIMIT", "1")
        os.environ.setdefault("MALLOC_ARENA_MAX", "1")

        from moatless.search_tree import SearchTree
        from moatless.repository.file import FileRepository
        from moatless.index.code_index import CodeIndex

        with open(trajectory_path) as f:
            data = json.load(f)
        data = _strip_recorded_tree(data)
        rewrites = _rewrite_model_base_url(data, mock_url)
        repo = FileRepository(repo_path=repo_path)
        code_index = CodeIndex.from_index_name(
            instance_id, file_repo=repo, index_store_dir=index_store_dir)
        tree = SearchTree.from_dict(
            data, repository=repo, code_index=code_index)
        ACTIVE_WORKER_STATE = {
            "instance_id": instance_id,
            "trajectory_path": trajectory_path,
            "repo_path": repo_path,
            "index_store_dir": index_store_dir,
            "repo": repo,
            "code_index": code_index,
            "tree": tree,
            "model_rewrites": rewrites,
            "load_ms": (time.time() - t0) * 1000,
        }
        status = _active_worker_status()
        status["ok"] = True
        status["load_wall_ms"] = (time.time() - t0) * 1000
        trace_event("active_worker_load", **{
            k: status.get(k)
            for k in ("instance_id", "load_wall_ms", "index_classes",
                      "index_functions", "root_node_count")
        })
        return status
    except Exception as e:  # noqa: BLE001
        ACTIVE_WORKER_STATE = {}
        return {
            "ok": False,
            "err": type(e).__name__,
            "msg": str(e),
            "traceback": traceback.format_exc()[-4000:],
            "footprint": _proc_footprint(),
        }


def main():
    cooperative_prewarm = None
    if os.environ.get("DELTABOX_PAPER_COOPERATIVE_PREWARM") == "1":
        from cooperative_prewarm import CooperativePrewarm
        cooperative_prewarm = CooperativePrewarm()
    if os.environ.get("DELTABOX_ASYNC_INCREMENTAL_DUMP") == "1":
        # Replay resolves file operations under WORKER_ROOT and launches test
        # subprocesses with cwd=root. Keep the idle controller outside that
        # mutable mount so an independent dump has no task-filesystem cwd ref.
        if not os.path.isabs(WORKER_ROOT):
            raise ValueError("asynchronous replay requires an absolute worker root")
        os.chdir("/")
    os.makedirs(NPD_REQ_DIR, exist_ok=True)
    os.makedirs(NPD_RESP_DIR, exist_ok=True)

    # Template / warm-fork endpoint (optional).
    template_fd = None
    template_write_path = None
    if WARM_TEMPLATE:
        try:
            import template_fork
            template_fd, template_write_path = template_fork.install_template_endpoint()
            log_message("[template] control FIFO endpoint installed")
        except Exception as e:
            log_message(f"[template] install failed: {e}")

    # NPD endpoints — mkfifo if missing (NPD may have created them first).
    for p in (NPD_REQ_FIFO, NPD_NOTIFY_FIFO):
        if not os.path.exists(p):
            os.mkfifo(p, 0o600)
    # O_RDWR keeps the FIFO alive across agent fork churn (parent SIGSTOP'd,
    # child becomes active) without the kernel tearing down readers.
    req_fd    = os.open(NPD_REQ_FIFO,    os.O_RDWR | os.O_NONBLOCK)
    notify_fd = os.open(NPD_NOTIFY_FIFO, os.O_RDWR | os.O_NONBLOCK)

    log_message(f"Agent Process Started (model={MODEL_NAME}, "
                f"npd=on, warm_template={'on' if template_fd is not None else 'off'})")

    if not os.path.exists(PIPE_IN):
        os.mkfifo(PIPE_IN)
    if not os.path.exists(PIPE_OUT):
        os.mkfifo(PIPE_OUT)
    f_in_fd = os.open(PIPE_IN, os.O_RDWR | os.O_NONBLOCK)
    f_in_buf = b""
    f_out_fd = os.open(PIPE_OUT, os.O_RDWR | os.O_NONBLOCK)
    f_out = os.fdopen(f_out_fd, "w", buffering=1)
    reopen_fifos_on_epoch = os.environ.get(
        "DELTABOX_REOPEN_AGENT_FIFOS_ON_EPOCH") == "1"
    seen_epoch = _read_current_epoch()

    def _reopen_runner_fifos(reason: str) -> None:
        nonlocal f_in_fd, f_out
        try:
            os.close(f_in_fd)
        except Exception:
            pass
        for f in (f_out,):
            try:
                f.close()
            except Exception:
                pass
        trace_event("agent_runner_fifos_reopen_begin",
                    reason=reason, epoch=_read_current_epoch())
        f_in_fd = os.open(PIPE_IN, os.O_RDWR | os.O_NONBLOCK)
        # A read may contain abort plus a new-epoch request. Keep those bytes;
        # epoch validation below discards stale requests individually.
        new_out_fd = os.open(PIPE_OUT, os.O_RDWR | os.O_NONBLOCK)
        f_out = os.fdopen(new_out_fd, "w", buffering=1)
        trace_event("agent_runner_fifos_reopen_end",
                    reason=reason, epoch=_read_current_epoch())

    # rid → {start_ts, n_msgs, temperature, prompt_chars}
    pending: dict[str, dict] = {}
    notify_buf = b""

    def sync_epoch(current_epoch: int, reason: str) -> None:
        nonlocal seen_epoch
        if current_epoch == seen_epoch:
            return
        # Restored pending entries can fill the cap forever: their old NPD
        # replies have already been invalidated. Clear only old epochs so a
        # request admitted in the new epoch is never cancelled by late cleanup.
        stale = [rid for rid in pending if not rid.startswith(f"ep{current_epoch}.")]
        for rid in stale:
            pending.pop(rid, None)
            try:
                os.unlink(os.path.join(NPD_RESP_DIR, f"{rid}.json"))
            except OSError:
                pass
        trace_event("agent_epoch_pending_cleanup", epoch=current_epoch, n_stale=len(stale))
        seen_epoch = current_epoch
        if reopen_fifos_on_epoch:
            _reopen_runner_fifos(reason)
        if cooperative_prewarm is not None:
            # Only restore changes the epoch. Checkpoint's fork must not
            # trigger this Figure 6-only policy.
            cooperative_prewarm.start_epoch(current_epoch)

    while True:
        try:
            if cooperative_prewarm is not None:
                cooperative_prewarm.check()
            sync_epoch(_read_current_epoch(), "epoch_change")
            watch = [f_in_fd, notify_fd]
            template_pending = False
            if template_fd is not None:
                watch.append(template_fd)
                try:
                    import template_fork
                    template_pending = template_fork.has_pending_ctrl_message(
                        template_fd)
                except Exception:
                    template_pending = False
            if b"\n" in f_in_buf:
                # Buffered runner work must not starve NPD completions. They
                # free the admission cap even while another frame is queued.
                ready, _, _ = select.select(watch, [], [], 0)
                if f_in_fd not in ready:
                    ready.append(f_in_fd)
            else:
                ready, _, _ = select.select(watch, [], [], 1.0)
                if not ready and not template_pending:
                    continue

            # 1. Warm-template fork (quickest, time-sensitive).
            if (template_fd is not None
                    and (template_pending or template_fd in ready)):
                import template_fork
                if cooperative_prewarm is not None:
                    # The endpoint's existing single-threaded guard remains
                    # authoritative after cancellation and a complete join.
                    cooperative_prewarm.quiesce()
                role = template_fork.handle_template_message(
                    template_fd, template_write_path)
                if role is not None:
                    # child: just became active agent, keep looping
                    # parent_resumed: stopped template woke up, keep looping
                    continue

            # 2. New task from runner.
            #
            # In-flight cap: if we already have LLM_INFLIGHT_CAP outstanding
            # rids in `pending`, defer reading from pipe_in this tick —
            # select() fires again next round when a response arrives. This
            # preserves the "pending = pure data" invariant (#3 in
            # overlap_design.md) and the "≤2 in-flight per agent" invariant
            # (#4). Under linear mode len(pending) ≤ 1 so this is a no-op.
            if f_in_fd in ready and len(pending) >= LLM_INFLIGHT_CAP:
                trace_event("agent_inflight_defer", inflight=len(pending))
                ready = [fd for fd in ready if fd != f_in_fd]

            if f_in_fd in ready:
                if b"\n" not in f_in_buf:
                    try:
                        chunk = os.read(f_in_fd, 65536)
                    except BlockingIOError:
                        chunk = b""
                    if not chunk:
                        time.sleep(0.01)
                        continue
                    f_in_buf += chunk
                if b"\n" not in f_in_buf:
                    continue
                line_b, f_in_buf = f_in_buf.split(b"\n", 1)
                # Resynchronize after an incomplete frame captured by restore.
                # JSON escapes literal record separators inside string values.
                line = line_b.rsplit(b"\x1e", 1)[-1].decode(errors="replace")
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    log_message(f"bad runner request: {e} raw={line!r}")
                    continue

                if not isinstance(req, dict):
                    log_message("bad runner request: expected object")
                    continue
                request_epoch = req.get("_replay_epoch")
                current_epoch = _read_current_epoch()
                sync_epoch(current_epoch, "request_epoch_change")
                if (request_epoch != current_epoch
                        and (request_epoch is not None or os.environ.get(
                            "DELTABOX_REPLAY_STRICT_EPOCH") == "1")):
                    trace_event("agent_stale_runner_request", ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"), request_epoch=request_epoch,
                                current_epoch=current_epoch)
                    continue
                if req.get("ctrl") == "replay_ready":
                    # Complete endpoint activation before acknowledging. This
                    # may be reached from the pre-restore select/read frame.
                    sync_epoch(current_epoch, "replay_ready")
                    f_out.write(json.dumps({"ok": True, "ctrl": "replay_ready",
                                            "ctrl_id": req.get("ctrl_id"),
                                            "epoch": current_epoch}) + "\n")
                    f_out.flush()
                    continue

                if req.get("ctrl") == "abort_pending_all":
                    # Rollback in progress: drop every in-flight rid and
                    # clean up any resp files we own. Idempotent — the
                    # controller has already bumped epoch and unlinked
                    # resp files in resp_dir; we re-unlink any stragglers
                    # for rids we submitted (memory: agent only touches
                    # its own resp files).
                    aborted = list(pending.keys())
                    for rid in aborted:
                        resp_path = os.path.join(NPD_RESP_DIR, f"{rid}.json")
                        try:
                            os.unlink(resp_path)
                        except OSError:
                            pass
                    pending.clear()
                    trace_event("agent_abort_pending_all",
                                n_aborted=len(aborted))
                    log_message(f"[Agent] abort_pending_all: dropped {len(aborted)} rid(s)")
                    continue

                if req.get("ctrl") == "active_worker_load":
                    out = _active_worker_load(req)
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    trace_event("agent_ctrl_write_begin",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    trace_event("agent_ctrl_write_end",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    continue

                if req.get("ctrl") == "active_worker_status":
                    out = _active_worker_status()
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    trace_event("agent_ctrl_write_begin",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    trace_event("agent_ctrl_write_end",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    continue

                if req.get("ctrl") == "worker_index_build":
                    out = _worker_index_build(req)
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    trace_event("agent_ctrl_write_begin",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    trace_event("agent_ctrl_write_end",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    continue

                if req.get("ctrl") == "worker_index_status":
                    out = _worker_index_status()
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    trace_event("agent_ctrl_write_begin",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    trace_event("agent_ctrl_write_end",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    continue

                if req.get("ctrl") == "worker_index_snapshot":
                    out = _worker_index_snapshot(str(req.get("snapshot_id") or ""))
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    trace_event("agent_ctrl_write_begin",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    trace_event("agent_ctrl_write_end",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    continue

                if req.get("ctrl") == "worker_index_restore_snapshot":
                    out = _worker_index_restore_snapshot(
                        str(req.get("snapshot_id") or ""))
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    trace_event("agent_ctrl_write_begin",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    trace_event("agent_ctrl_write_end",
                                ctrl=req.get("ctrl"),
                                ctrl_id=req.get("ctrl_id"))
                    continue

                if req.get("ctrl") == "worker_exec":
                    out = _worker_exec(req)
                    out["ctrl"] = req.get("ctrl")
                    out["ctrl_id"] = req.get("ctrl_id")
                    line_out = json.dumps(out) + "\n"
                    trace_event("worker_exec_write_begin",
                                ctrl_id=req.get("ctrl_id"),
                                bytes=len(line_out))
                    f_out.write(line_out)
                    f_out.flush()
                    trace_event("worker_exec_write_end",
                                ctrl_id=req.get("ctrl_id"))
                    continue

                messages = req.get("messages", [])
                temp = float(req.get("temperature", 0.0))
                prompt_chars = sum(len(str(m.get("content", "")))
                                   for m in messages)
                trace_event("agent_step_begin",
                            n_msgs=len(messages), temperature=temp)
                trace_event("llm_req", model=MODEL_NAME,
                            n_msgs=len(messages), temperature=temp,
                            prompt_chars=prompt_chars)

                if not messages:
                    # Degenerate task — reply synchronously, no NPD call.
                    out = {"action": "run",
                           "thought": "Error: Empty message history received.",
                           "command": "ls"}
                    f_out.write(json.dumps(out) + "\n")
                    f_out.flush()
                    continue

                rid = _send_llm_request(req_fd, messages, temp)
                pending[rid] = {
                    "start_ts":    time.time(),
                    "n_msgs":      len(messages),
                    "temperature": temp,
                    "prompt_chars": prompt_chars,
                }
                log_message(f"[Agent] dispatched rid={rid} msgs={len(messages)}")

            # 3. NPD response notifications.
            if notify_fd in ready:
                try:
                    chunk = os.read(notify_fd, 65536)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    notify_buf += chunk
                while b"\n" in notify_buf:
                    nline, notify_buf = notify_buf.split(b"\n", 1)
                    rid = nline.decode(errors="replace").strip()
                    if not rid:
                        continue

                    if rid not in pending:
                        # Not ours. Two cases:
                        #   (a) truly stale — a rid whose epoch is < current
                        #       epoch, left behind across a CRIU restore;
                        #   (b) another in-process caller (e.g. value_agent
                        #       via npd_client) that submitted through the
                        #       same req_fifo and is polling resp_dir for
                        #       its file to appear.
                        # DO NOT unlink the resp file here: in case (b) we
                        # would starve the other caller (the file vanishes
                        # before its poll sees it, and it hangs to timeout).
                        # In case (a) the file is bounded in count (≤ cap on
                        # in-flight LLM calls at the moment of restore) and
                        # will be removed when the caller is restored with
                        # a new rid, or by a future resp_dir GC sweep.
                        trace_event("agent_stale_notify_drop", rid=rid)
                        continue

                    resp = _read_response_file(rid)
                    ctx = pending.pop(rid)
                    latency_ms = int((time.time() - ctx["start_ts"]) * 1000)

                    if resp is None or not resp.get("ok"):
                        err = (resp or {}).get("error", "resp unavailable")
                        trace_event("llm_error", rid=rid, err=err,
                                    latency_ms=latency_ms)
                        out = {"action": "run",
                               "thought": f"LLM response unavailable: {err}",
                               "command": "echo 'llm resp err'"}
                        f_out.write(json.dumps(out) + "\n")
                        f_out.flush()
                        continue

                    content = resp.get("content", "") or ""
                    usage = resp.get("usage", {})
                    trace_event("llm_resp", latency_ms=latency_ms,
                                content_chars=len(content),
                                prompt_tokens=usage.get("prompt_tokens"),
                                completion_tokens=usage.get("completion_tokens"),
                                finish_reason=resp.get("finish_reason"))
                    trace_event("agent_step_end", response_chars=len(content))

                    parsed = _extract_first_json(content)
                    if parsed is None:
                        log_message(f"JSON Parse Error\nRaw: {content[:200]}")
                        trace_event("agent_parse_error",
                                    raw_snippet=content[:400])
                        parsed = {
                            "action":  "run",
                            "thought": "Failed to parse JSON response from model.",
                            "command": "echo 'JSON Parsing Error'",
                        }
                    f_out.write(json.dumps(parsed) + "\n")
                    f_out.flush()

        except Exception as e:
            if (cooperative_prewarm is not None
                    and isinstance(e, cooperative_prewarm.error_type)):
                cooperative_prewarm.close()
                raise
            log_message(f"Main Loop Error: {type(e).__name__}: {e}")
            time.sleep(1)
        except BaseException:
            if cooperative_prewarm is not None:
                cooperative_prewarm.close()
            raise


if __name__ == "__main__":
    main()
