#!/usr/bin/env python3
"""Read-only, best-effort evidence from one failed Cube action.

The caller must impose a process timeout (20 seconds in the AE driver). Every
finished step is persisted atomically so a killed probe retains partial evidence.
This program never resumes, pauses, snapshots, kills, or re-executes an action.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from typing import Any, Callable

IO_TIMEOUT = 2.0
COMMAND_TIMEOUT = 4.0
TEXT_LIMIT = 128 * 1024
FILE_PATHS = (
    "/tmp/finalbench/action_worker.log",
    "/tmp/finalbench/action.req.json",
    "/tmp/finalbench/action.resp.json",
    "/tmp/finalbench/action.resp.json.tmp",
    "/tmp/action.req.json",
)

# This executes inside the addressed sandbox, never against the host's /proc.
# Only known action processes, their descendants, and holders of our FIFO are
# included. No environment, credentials, or unrelated file contents are read.
PROCESS_PROBE = r'''
import json, os, pathlib, stat
root = pathlib.Path("/tmp/finalbench")
fifo = str(root / "action_worker.in")
mine = {os.getpid(), os.getppid()}
info = {}
def read(path, limit=16384):
    try:
        with open(path, "rb") as stream:
            value = stream.read(limit + 1)
        return {"text": value[:limit].decode("utf-8", "replace"),
                "truncated": len(value) > limit}
    except OSError as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)}
for path in pathlib.Path("/proc").iterdir():
    if not path.name.isdigit() or int(path.name) in mine:
        continue
    try:
        cmd = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        status = (path / "status").read_text()
        ppid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:")))
        fds = {}
        for fd in (path / "fd").iterdir():
            try:
                fds[fd.name] = os.readlink(fd)
            except OSError:
                pass
        match = ("/opt/finalbench/e2b_slim_action_worker.py" in cmd
                 or "/tmp/finalbench/action_worker.in" in cmd
                 or "/tmp/finalbench/action.req.json" in cmd
                 or fifo in fds.values())
        info[int(path.name)] = {"ppid": ppid, "cmdline": cmd[:4096], "fds": fds, "match": match}
    except (OSError, StopIteration, ValueError):
        continue
selected = {pid for pid, row in info.items() if row["match"]}
for _ in range(8):
    children = {pid for pid, row in info.items() if row["ppid"] in selected}
    if children <= selected:
        break
    selected |= children
processes = []
for pid in sorted(selected)[:128]:
    row = info[pid]
    path = pathlib.Path("/proc") / str(pid)
    row = {"pid": pid, "ppid": row["ppid"], "cmdline": row["cmdline"], "fds": row["fds"]}
    for name in ("status", "wchan", "syscall", "stack"):
        row[name] = read(path / name)
    row["fifo_fdinfo"] = {
        fd: read(path / "fdinfo" / fd)
        for fd, target in row["fds"].items() if target == fifo
    }
    processes.append(row)
files = {}
for path in (root / "action_worker.in", root / "action.req.json",
             root / "action.resp.json", root / "action.resp.json.tmp",
             root / "action_worker.log", pathlib.Path("/tmp/action.req.json")):
    try:
        value = path.lstat()
        files[str(path)] = {"mode": stat.filemode(value.st_mode), "inode": value.st_ino,
                           "size": value.st_size, "mtime_ns": value.st_mtime_ns}
    except OSError as exc:
        files[str(path)] = {"error": type(exc).__name__ + ": " + str(exc)}
print(json.dumps({"processes": processes, "process_limit_reached": len(selected) > 128,
                  "file_metadata": files}, separators=(",", ":")))
'''


def _save(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)


def _error(exc: BaseException) -> str:
    # Do not serialize exception objects, HTTP headers, or sandbox metadata.
    number = getattr(exc, "errno", None)
    return type(exc).__name__ + (f" (errno={number})" if number is not None else "")


def _text_record(text: str) -> dict[str, Any]:
    raw = text.encode("utf-8")
    return {
        "text": raw[:TEXT_LIMIT].decode("utf-8", "replace"),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "truncated": len(raw) > TEXT_LIMIT,
    }


def connect_readonly(sandbox_id: str):
    """GET existing metadata and build a bounded, private SDK instance.

    Sandbox.connect() is intentionally avoided: it can resume a paused sandbox
    and extend its TTL, and the installed SDK's connect POST has no timeout.
    """
    sdk = Path(os.environ.get("CUBE_SDK_PATH", "/mnt/disk2/dyp/cubesandbox/sdk/python"))
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    import httpx
    import requests
    from cubesandbox import Config, Sandbox

    config = Config(request_timeout=IO_TIMEOUT)
    response = requests.get(
        f"{config.api_url}/sandboxes/{sandbox_id}",
        timeout=(IO_TIMEOUT, IO_TIMEOUT),
    )
    response.raise_for_status()
    data = response.json()
    if data.get("sandboxID") != sandbox_id:
        raise ValueError("Cube metadata sandbox identity differs from requested sandbox")
    state = str(data.get("state", "")).lower()
    if state and state not in ("running", "ready"):
        raise ValueError(f"Refusing to probe a sandbox in state {state!r}")
    sandbox = Sandbox(data, config=config)
    try:
        # The normal SDK data client defaults to read=None. Bound this private
        # client's connect/read/write/pool timeouts without touching SDK code.
        sandbox._client = sandbox._build_data_client()
        sandbox._client.timeout = httpx.Timeout(IO_TIMEOUT)
    except Exception:
        sandbox.close()
        raise
    return sandbox


def collect(sandbox_id: str, output: Path,
            connect: Callable[[str], Any] = connect_readonly) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sandbox_id):
        raise ValueError("Invalid sandbox identifier")
    report: dict[str, Any] = {
        "schema": 1,
        "sandbox_id": sandbox_id,
        "purpose": "failed-action-read-only-diagnostic",
        "status": "running",
        "started_unix_ns": time.time_ns(),
        "timeouts_seconds": {"request": IO_TIMEOUT, "command": COMMAND_TIMEOUT,
                             "required_parent_process_timeout": 20},
        "steps": {},
    }
    _save(output, report)
    sandbox = None

    def record(name: str, operation: Callable[[], Any]) -> None:
        started = time.monotonic()
        try:
            value = operation()
            ok = not (isinstance(value, dict) and value.get("exit_code", 0) != 0)
            report["steps"][name] = {"ok": ok, "result": value}
        except Exception as exc:
            report["steps"][name] = {"ok": False, "error": _error(exc)}
        report["steps"][name]["elapsed_ms"] = (time.monotonic() - started) * 1000
        _save(output, report)

    try:
        started = time.monotonic()
        try:
            sandbox = connect(sandbox_id)
            report["steps"]["connect"] = {"ok": True}
        except Exception as exc:
            report["steps"]["connect"] = {"ok": False, "error": _error(exc)}
        report["steps"]["connect"]["elapsed_ms"] = (time.monotonic() - started) * 1000
        _save(output, report)
        if sandbox is not None:
            def process_sample() -> dict[str, Any]:
                result = sandbox.commands.run(
                    "python3 -c " + shlex.quote(PROCESS_PROBE),
                    timeout=COMMAND_TIMEOUT,
                )
                return {
                    "exit_code": result.exit_code,
                    "stdout": _text_record(result.stdout),
                    "stderr": _text_record(result.stderr),
                }
            record("processes", process_sample)
            for path in FILE_PATHS:
                record("file:" + path, lambda path=path: _text_record(sandbox.files.read(path)))
    finally:
        if sandbox is not None:
            record("close_client", lambda: sandbox.close())
        report["status"] = (
            "complete" if all(step["ok"] for step in report["steps"].values()) else "partial"
        )
        report["finished_unix_ns"] = time.time_ns()
        _save(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Refusing to overwrite existing diagnostic evidence")
    report = collect(args.sandbox_id, args.output)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
