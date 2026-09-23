#!/usr/bin/env python3
"""Persistent in-sandbox action worker for E2B warm-command experiments."""
from __future__ import annotations

import argparse
import json
import os
import socket
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


def _prewarm_imports() -> None:
    from moatless.actions.action import Action  # noqa: F401
    from moatless.actions.model import ActionArguments  # noqa: F401
    from moatless.file_context import FileContext  # noqa: F401
    from moatless.repository.file import FileRepository  # noqa: F401
    from slim_index_proxy import SlimIndexProxy  # noqa: F401
    from moatless.codeblocks import get_parser_by_path
    # Parser/tokenizer initialization belongs to warm setup, before snapshots
    # and measured action execution, just like the imports above.
    get_parser_by_path("ae_warmup.py")


def _recv_all(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def serve(sock_path: Path) -> int:
    from e2b_slim_action_runner import run

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sock_path.unlink()
    except FileNotFoundError:
        pass

    t0 = time.perf_counter()
    _prewarm_imports()
    prewarm_ms = (time.perf_counter() - t0) * 1000.0
    print(f"[warm-worker] prewarmed imports in {prewarm_ms:.2f}ms", flush=True)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o666)
    srv.listen(8)
    print(f"[warm-worker] listening on {sock_path}", flush=True)

    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                req = json.loads(_recv_all(conn).decode("utf-8"))
                out = run(req)
            except BaseException as e:
                out = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(),
                }
            conn.sendall(json.dumps(out, separators=(",", ":")).encode("utf-8"))
            conn.shutdown(socket.SHUT_WR)


def _write_json_atomic(path: Path, obj: dict) -> None:
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def serve_fifo(input_fifo: Path, response_path: Path, ready_path: Path | None) -> int:
    """Serve one JSON request per line over a FIFO.

    The hot-path client is plain shell: write the uploaded request JSON into
    the FIFO, then wait for response_path to appear. Keeping the client out of
    Python is the point of this warm-worker experiment.
    """
    from e2b_slim_action_runner import run

    input_fifo.parent.mkdir(parents=True, exist_ok=True)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (input_fifo, response_path, response_path.with_suffix(response_path.suffix + ".tmp")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    t0 = time.perf_counter()
    _prewarm_imports()
    prewarm_ms = (time.perf_counter() - t0) * 1000.0
    print(f"[warm-worker] prewarmed imports in {prewarm_ms:.2f}ms", flush=True)

    os.mkfifo(input_fifo, 0o666)
    os.chmod(input_fifo, 0o666)
    if ready_path is not None:
        ready_path.write_text("ready\n", encoding="utf-8")
    print(f"[warm-worker] fifo={input_fifo} response={response_path}", flush=True)

    while True:
        # Reopen after each writer closes; this avoids EOF spinning and keeps
        # one request == one shell command.
        with input_fifo.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                    out = run(req)
                except BaseException as e:
                    out = {
                        "ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc(),
                    }
                _write_json_atomic(response_path, out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("socket", "fifo"), default="socket")
    ap.add_argument("--socket", default="/tmp/finalbench/action_worker.sock")
    ap.add_argument("--fifo", default="/tmp/finalbench/action_worker.in")
    ap.add_argument("--response", default="/tmp/finalbench/action.resp.json")
    ap.add_argument("--ready", default="/tmp/finalbench/action_worker.ready")
    args = ap.parse_args(argv[1:])
    if args.mode == "fifo":
        return serve_fifo(Path(args.fifo), Path(args.response), Path(args.ready) if args.ready else None)
    return serve(Path(args.socket))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
