#!/usr/bin/env python3
"""Execute one DeltaBox-style worker_ops replay step inside an E2B sandbox.

This 62-local runner mirrors the DeltaBox P-EAGLE experiment's
worker_exec_action mode: execute the recorded worker_ops against the live repo,
then return the recorded Moatless observation/file_context so the controller
follows the same recorded MCTS tree.  E2B still performs the real resume/pause
transition around every step.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path


BASE = Path(os.environ.get("E2B_FINALBENCH_BASE", "/opt/finalbench"))
PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", "/opt/spr_payload"))

for p in (
    str(BASE / "slim_shims"),
    str(BASE),
    str(PAYLOAD),
    str(PAYLOAD / "moatless-det-src"),
):
    if p not in sys.path:
        sys.path.insert(0, p)


def _short_text(text: str | bytes | None, limit: int = 65536) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return text[:limit]


def _path_aliases(rel: str) -> list[str]:
    rel = str(rel).lstrip("/")
    aliases = [rel]
    for prefix in ("lib/", "src/"):
        if rel.startswith(prefix):
            aliases.append(rel[len(prefix) :])
    return list(dict.fromkeys(aliases))


def _resolve_repo_file(repo_path: str, rel: str, *, for_write: bool = False) -> tuple[Path, str]:
    rel = str(rel).lstrip("/")
    candidates = [rel]
    if not rel.startswith("lib/"):
        candidates.append(f"lib/{rel}")
    if not rel.startswith("src/"):
        candidates.append(f"src/{rel}")

    root = Path(repo_path)
    for candidate in candidates:
        full = root / candidate
        if full.exists():
            return full, candidate
    if for_write:
        for candidate in candidates:
            full = root / candidate
            if full.parent.exists():
                return full, candidate
    return root / rel, rel


def _match_paths(repo_path: str, file_pattern: str | None) -> list[str]:
    import fnmatch

    root = Path(repo_path)
    out: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace(os.sep, "/")
        parts = set(rel.split("/"))
        if parts & {".git", "__pycache__", ".tox", ".venv", "node_modules"}:
            continue
        if (
            not file_pattern
            or any(
                fnmatch.fnmatch(alias, file_pattern)
                or fnmatch.fnmatch("/" + alias, file_pattern)
                for alias in _path_aliases(rel)
            )
        ):
            if file_pattern:
                out.append(next(alias for alias in _path_aliases(rel) if fnmatch.fnmatch(alias, file_pattern) or fnmatch.fnmatch("/" + alias, file_pattern)))
            else:
                out.append(rel)
    return sorted(out)


def _worker_exec_one(op: dict, repo_path: str) -> dict:
    typ = op.get("type") or "noop"
    t0 = time.perf_counter()
    out: dict = {
        "type": typ,
        "action_class": op.get("action_class"),
        "path": op.get("path"),
    }
    try:
        if typ == "noop":
            out.update({"ok": True, "note": op.get("note", "")})
        elif typ == "view_file":
            rel = str(op.get("path") or "")
            full, resolved = _resolve_repo_file(repo_path, rel)
            out.update({
                "ok": full.is_file(),
                "resolved_path": resolved,
                "preview": _short_text(full.read_text(errors="replace") if full.is_file() else ""),
            })
        elif typ == "find_file_pattern":
            pattern = op.get("file_pattern") or op.get("path") or "*"
            matches = _match_paths(repo_path, pattern)[:64]
            out.update({"ok": True, "n_matches": len(matches), "matches": matches, "from_index": True})
        elif typ == "grep":
            pattern = op.get("pattern") or ""
            try:
                regex = re.compile(pattern)
                literal_fallback = False
            except re.error:
                regex = None
                literal_fallback = True
            hits = []
            for rel in _match_paths(repo_path, op.get("file_pattern") or "**/*"):
                full, resolved = _resolve_repo_file(repo_path, rel)
                try:
                    text = full.read_text(errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    matched = regex.search(line) if regex is not None else pattern in line
                    if matched:
                        hits.append({"path": rel, "resolved_path": resolved, "line": lineno, "text": line.rstrip()[:300]})
                        if len(hits) >= 64:
                            break
                if len(hits) >= 64:
                    break
            out.update({"ok": True, "from_index": True, "literal_fallback": literal_fallback, "n_hits": len(hits), "hits": hits})
        elif typ == "find_symbol":
            name = op.get("name") or ""
            file_pattern = op.get("file_pattern") or "**/*.py"
            symbol_kind = op.get("symbol_kind")
            hits = []
            if name:
                if symbol_kind == "class":
                    regex = re.compile(r"^\s*class\s+" + re.escape(name) + r"\b")
                else:
                    regex = re.compile(r"^\s*(?:async\s+)?def\s+" + re.escape(name) + r"\b")
                for rel in _match_paths(repo_path, file_pattern):
                    full, resolved = _resolve_repo_file(repo_path, rel)
                    try:
                        text = full.read_text(errors="ignore")
                    except OSError:
                        continue
                    if any(regex.search(line) for line in text.splitlines()):
                        hits.append({"path": rel, "resolved_path": resolved})
                        if len(hits) >= 64:
                            break
            out.update({
                "ok": True,
                "from_index": True,
                "symbol": name,
                "symbol_kind": symbol_kind,
                "n_hits": len(hits),
                "hits": hits,
            })
        elif typ == "replace":
            rel = str(op.get("path") or "")
            full, resolved = _resolve_repo_file(repo_path, rel)
            data = full.read_text(errors="replace")
            old = op.get("old_str", "")
            if old not in data:
                out.update({"ok": False, "err": "old_str_not_found"})
            else:
                full.write_text(data.replace(old, op.get("new_str", ""), 1))
                out.update({"ok": True, "resolved_path": resolved})
        elif typ == "write_file":
            rel = str(op.get("path") or "")
            full, resolved = _resolve_repo_file(repo_path, rel, for_write=True)
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(op.get("content", ""))
            out.update({"ok": True, "resolved_path": resolved})
        elif typ == "append_file":
            rel = str(op.get("path") or "")
            full, resolved = _resolve_repo_file(repo_path, rel, for_write=True)
            full.parent.mkdir(parents=True, exist_ok=True)
            with full.open("a") as f:
                f.write(op.get("content", ""))
            out.update({"ok": True, "resolved_path": resolved})
        elif typ == "run_tests":
            test_files = op.get("test_files") or []
            quoted = " ".join(subprocess.list2cmdline([str(p)]) for p in test_files)
            cmd = op.get("command") or (f"python3 -m pytest -q {quoted}" if quoted else "true")
            cp = subprocess.run(
                cmd,
                shell=True,
                cwd=repo_path,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=float(op.get("timeout", 120.0)),
                executable="/bin/bash",
            )
            out.update({
                "ok": True,
                "command": cmd,
                "rc": cp.returncode,
                "stdout": _short_text(cp.stdout),
                "stderr": _short_text(cp.stderr),
            })
        else:
            out.update({"ok": True, "note": f"unhandled_worker_op:{typ}"})
    except subprocess.TimeoutExpired as e:
        out.update({
            "ok": False,
            "err": "TimeoutExpired",
            "timeout": e.timeout,
            "stdout": _short_text(e.stdout),
            "stderr": _short_text(e.stderr),
        })
    except Exception as e:  # noqa: BLE001
        out.update({"ok": False, "err": type(e).__name__, "msg": str(e)})
    out["wall_ms"] = (time.perf_counter() - t0) * 1000.0
    return out


def run(req: dict) -> dict:
    t0 = time.perf_counter()
    worker_ops = req.get("worker_ops") or []
    worker_results = [_worker_exec_one(op, req["repo_path"]) for op in worker_ops]
    worker_ok = all(r.get("ok") for r in worker_results)

    observation = req.get("recorded_observation") or {
        "message": "No recorded observation supplied",
        "terminal": False,
        "properties": {"worker_results": worker_results},
        "execution_completion": None,
    }
    file_context = req.get("recorded_file_context") or req["file_context"]

    materialized: list[str] = []
    materialize_enabled = (
        os.environ.get("E2B_MATERIALIZE_FILE_CONTEXT", "0") == "1"
        or bool(req.get("materialize_file_context"))
    )
    if materialize_enabled:
        for context_file in file_context.get("files") or []:
            file_path = context_file.get("file_path") or context_file.get("path")
            if not (
                context_file.get("was_edited")
                or context_file.get("patch")
                or context_file.get("is_new")
            ):
                continue
            if context_file.get("content") is None or not file_path:
                materialized.append(f"skipped_missing_content:{file_path}")
                continue
            full = Path(req["repo_path"]) / file_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(context_file["content"])
            materialized.append(file_path)

    action = req.get("action") or {}
    action_name = (
        action.get("name")
        or action.get("action")
        or action.get("action_name")
        or action.get("action_args_class", "").rsplit(".", 1)[-1]
        or action.get("type")
        or "unknown"
    )

    return {
        "ok": worker_ok,
        "observation": observation,
        "file_context": file_context,
        "event": {
            "event_type": "e2b_slim_worker_ops_replay",
            "action": action_name,
            "node_id": req.get("node_id"),
            "action_wall_ms": (time.perf_counter() - t0) * 1000.0,
            "helper_pid": os.getpid(),
            "worker_ops_n": len(worker_ops),
            "worker_ops_ok": worker_ok,
            "worker_results": worker_results,
            "materialize_file_context": materialize_enabled,
            "materialized_files": materialized,
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: e2b_slim_action_runner.py <request.json> <response.json>", file=sys.stderr)
        return 2
    req_path = Path(argv[1])
    resp_path = Path(argv[2])
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
        out = run(req)
    except BaseException as e:
        out = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }
    tmp = resp_path.with_suffix(resp_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    tmp.replace(resp_path)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
