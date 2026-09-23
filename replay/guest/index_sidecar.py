#!/usr/bin/env python3
"""Checkpoint-external lightweight worker index sidecar.

The replay agent can keep this process outside the CRIU dump tree and query it
over loopback. It deliberately mirrors guest/agent.py's worker index behavior so
the experiment changes memory ownership, not the worker action semantics.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import time
import traceback
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from typing import Any


INDEX_STATE: dict[str, Any] = {}
INDEX_SNAPSHOTS: dict[str, dict[str, Any]] = {}
MAX_FILE_BYTES = int(os.environ.get(
    "AGENT_WORKER_INDEX_MAX_FILE_BYTES", str(2 << 20)))
MAX_TOTAL_BYTES = int(os.environ.get(
    "AGENT_WORKER_INDEX_MAX_TOTAL_BYTES", str(512 << 20)))


def _proc_footprint() -> dict[str, Any]:
    out: dict[str, Any] = {
        "pid": os.getpid(),
        "rss_kb": None,
        "pss_kb": None,
        "private_dirty_kb": None,
        "vm_size_kb": None,
        "threads": None,
    }
    try:
        for line in open("/proc/self/status", encoding="utf-8"):
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
        for line in open("/proc/self/smaps", encoding="utf-8"):
            if line.startswith("Pss:"):
                pss += int(line.split()[1])
            elif line.startswith("Private_Dirty:"):
                private_dirty += int(line.split()[1])
        out["pss_kb"] = pss
        out["private_dirty_kb"] = private_dirty
    except OSError as e:
        out["smaps_err"] = str(e)
    return out


def _abspath(root: str, path: str) -> str:
    rel = (path or "").lstrip("/") if os.path.isabs(path or "") else (path or "")
    full = os.path.abspath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    if full != root_abs and not full.startswith(root_abs + os.sep):
        raise ValueError(f"path escapes worker root: {path!r}")
    return full


def _resolve_rel(path: str, root: str | None = None) -> tuple[str, str]:
    root = root or INDEX_STATE.get("root") or "/testbed"
    full = _abspath(root, path)
    rel = os.path.relpath(full, os.path.abspath(root))
    files = INDEX_STATE.get("files", {})
    if os.path.exists(full) or rel in files:
        return full, rel
    suffix = rel.lstrip("/")
    matches = [
        indexed for indexed in files
        if indexed == suffix or indexed.endswith("/" + suffix)
    ]
    if len(matches) == 1:
        rel = matches[0]
        return _abspath(root, rel), rel
    if len(matches) > 1:
        raise ValueError(f"ambiguous indexed path {path!r}: {matches[:8]}")
    return full, rel


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


def _remove(rel: str) -> None:
    entry = INDEX_STATE.get("files", {}).pop(rel, None)
    if not entry:
        return
    for cls in entry.get("classes", []):
        rows = INDEX_STATE.get("classes", {}).get(cls, [])
        rows = [r for r in rows if r.get("path") != rel]
        if rows:
            INDEX_STATE["classes"][cls] = rows
        else:
            INDEX_STATE.get("classes", {}).pop(cls, None)
    for fn in entry.get("functions", []):
        rows = INDEX_STATE.get("functions", {}).get(fn, [])
        rows = [r for r in rows if r.get("path") != rel]
        if rows:
            INDEX_STATE["functions"][fn] = rows
        else:
            INDEX_STATE.get("functions", {}).pop(fn, None)


def _recompute_total_bytes() -> int:
    total = sum(
        int(entry.get("size", 0))
        for entry in INDEX_STATE.get("files", {}).values()
    )
    INDEX_STATE["total_bytes"] = total
    return total


def _cow_for_update() -> None:
    """Detach mutable index tables before changing this checkpoint state."""
    if not INDEX_STATE:
        return
    INDEX_STATE["files"] = dict(INDEX_STATE.get("files", {}))
    INDEX_STATE["classes"] = {
        name: list(rows)
        for name, rows in INDEX_STATE.get("classes", {}).items()
    }
    INDEX_STATE["functions"] = {
        name: list(rows)
        for name, rows in INDEX_STATE.get("functions", {}).items()
    }


def _index_one(path: str, root: str) -> dict[str, Any] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or st.st_size > MAX_FILE_BYTES:
        return None
    rel = os.path.relpath(path, root)
    if _index_should_skip(rel):
        return None
    try:
        with open(path, "r", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    classes, functions = (
        _extract_python_symbols(rel, text) if rel.endswith(".py") else ([], [])
    )
    entry = {
        "path": rel,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "text_sha256": hashlib.sha256(text.encode(errors="ignore")).hexdigest(),
        "text": text,
        "classes": classes,
        "functions": functions,
    }
    _remove(rel)
    INDEX_STATE.setdefault("files", {})[rel] = entry
    for cls in classes:
        INDEX_STATE.setdefault("classes", {}).setdefault(cls, []).append({"path": rel})
    for fn in functions:
        INDEX_STATE.setdefault("functions", {}).setdefault(fn, []).append({"path": rel})
    return entry


def _status() -> dict[str, Any]:
    state = INDEX_STATE
    root = state.get("root") if state else "/testbed"
    root_entries: list[str] = []
    try:
        root_entries = sorted(os.listdir(root))[:16]
    except OSError:
        root_entries = []
    fingerprint = None
    metadata_fingerprint = None
    sample_query = None
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
        "sidecar_footprint": _proc_footprint(),
    }


def _build(root: str) -> dict[str, Any]:
    global INDEX_STATE
    t0 = time.time()
    files: dict[str, Any] = {}
    INDEX_STATE = {
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
            if total + size > MAX_TOTAL_BYTES:
                INDEX_STATE["truncated"] = True
                break
            entry = _index_one(path, root)
            if entry is not None:
                total += entry["size"]
        if INDEX_STATE["truncated"]:
            break
    INDEX_STATE["total_bytes"] = total
    INDEX_STATE["build_ms"] = (time.time() - t0) * 1000
    out = _status()
    out["ok"] = True
    return out


def _snapshot(snapshot_id: str) -> dict[str, Any]:
    if not snapshot_id:
        raise ValueError("snapshot_id is required")
    if not INDEX_STATE:
        raise RuntimeError("cannot snapshot unloaded index")
    INDEX_SNAPSHOTS[snapshot_id] = dict(INDEX_STATE)
    out = _status()
    out["snapshot_id"] = snapshot_id
    out["snapshot_count"] = len(INDEX_SNAPSHOTS)
    return out


def _restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    global INDEX_STATE
    if not snapshot_id:
        raise ValueError("snapshot_id is required")
    snap = INDEX_SNAPSHOTS.get(snapshot_id)
    if snap is None:
        return {
            "ok": False,
            "missing_snapshot": True,
            "snapshot_id": snapshot_id,
            "snapshot_count": len(INDEX_SNAPSHOTS),
        }
    INDEX_STATE = dict(snap)
    out = _status()
    out["snapshot_id"] = snapshot_id
    out["snapshot_count"] = len(INDEX_SNAPSHOTS)
    return out


def _match_paths(file_pattern: str | None) -> list[str]:
    files = INDEX_STATE.get("files", {})
    if not file_pattern:
        return list(files)
    return [
        rel for rel in files
        if fnmatch.fnmatch(rel, file_pattern)
        or fnmatch.fnmatch("/" + rel, file_pattern)
    ]


def _refresh_path(path: str, root: str | None = None) -> dict[str, Any]:
    root = root or INDEX_STATE.get("root") or "/testbed"
    _cow_for_update()
    full, rel = _resolve_rel(path, root)
    _remove(rel)
    entry = _index_one(full, root) if os.path.exists(full) else None
    total = _recompute_total_bytes()
    return {
        "ok": True,
        "path": rel,
        "indexed": entry is not None,
        "total_bytes": total,
    }


def _grep(pattern: str, file_pattern: str | None) -> dict[str, Any]:
    pattern_mode = "regex"
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = None
        pattern_mode = "literal"
    hits = []
    for rel in _match_paths(file_pattern or "**/*"):
        text = (INDEX_STATE.get("files", {}).get(rel) or {}).get("text", "")
        for lineno, line in enumerate(text.splitlines(), 1):
            matched = regex.search(line) if regex is not None else pattern in line
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


def _find_symbol(name: str, symbol_kind: str | None,
                 file_pattern: str | None) -> dict[str, Any]:
    table = "classes" if symbol_kind == "class" else "functions"
    rows = INDEX_STATE.get(table, {}).get(name or "", [])
    if file_pattern:
        allowed = set(_match_paths(file_pattern))
        rows = [r for r in rows if r.get("path") in allowed]
    return {
        "ok": True,
        "from_index": True,
        "symbol": name or "",
        "symbol_kind": symbol_kind,
        "n_hits": len(rows),
        "hits": rows[:64],
    }


def _call(method: str, kwargs: dict[str, Any]) -> Any:
    if method == "build":
        return _build(kwargs.get("root") or "/testbed")
    if method == "status":
        return _status()
    if method == "snapshot":
        return _snapshot(kwargs.get("snapshot_id") or "")
    if method == "restore_snapshot":
        return _restore_snapshot(kwargs.get("snapshot_id") or "")
    if method == "resolve_rel":
        full, rel = _resolve_rel(kwargs.get("path") or "", kwargs.get("root"))
        return {"full": full, "rel": rel}
    if method == "match_paths":
        return {"matches": _match_paths(kwargs.get("file_pattern"))}
    if method == "has_file":
        return {"exists": kwargs.get("rel") in INDEX_STATE.get("files", {})}
    if method == "refresh_path":
        return _refresh_path(kwargs.get("path") or "", kwargs.get("root"))
    if method == "grep":
        return _grep(kwargs.get("pattern") or "", kwargs.get("file_pattern"))
    if method == "find_symbol":
        return _find_symbol(
            kwargs.get("name") or "",
            kwargs.get("symbol_kind"),
            kwargs.get("file_pattern"),
        )
    raise ValueError(f"unsupported method: {method}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        elif self.path == "/status":
            self._send(200, {"ok": True, "result": _status()})
        else:
            self._send(404, {"ok": False, "error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/call":
            self._send(404, {"ok": False, "error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            result = _call(str(req["method"]), req.get("kwargs") or {})
            self._send(200, {"ok": True, "result": result})
        except Exception as e:  # noqa: BLE001
            self._send(500, {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-4000:],
            })


class Server(ThreadingTCPServer):
    allow_reuse_address = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18765)
    args = ap.parse_args()
    srv = Server((args.host, args.port), Handler)
    print(f"[index-sidecar] listening http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
