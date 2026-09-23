#!/usr/bin/env python3
"""mock_npd.py — minimal NPD stub for the real-agent CoW bench.

Reads rids from NPD_REQ_FIFO, returns a canned response (recorded from
a previous real \\sys MCTS run) into NPD_RESP_DIR/{rid}.json, and notifies
on NPD_NOTIFY_FIFO.

Use canonical agent.py paths via env (matching agent.py's defaults) so
the agent's protocol works unchanged.

Canned response is loaded from BENCH_CANNED_RESPONSE_PATH (a JSON file
holding the response body that agent.py expects). Default: a benign
{"action":"run","thought":"...", "command":"true"} payload that agent
will parse + emit, leaving the sandbox state untouched.
"""
from __future__ import annotations

import json
import os
import select
import sys
import time
import uuid

NPD_REQ_FIFO    = os.environ.get("NPD_REQ_FIFO",    "/tmp/npd_req.fifo")
NPD_NOTIFY_FIFO = os.environ.get("NPD_NOTIFY_FIFO", "/tmp/npd_notify.fifo")
NPD_REQ_DIR     = os.environ.get("NPD_REQ_DIR",     "/tmp/npd_requests")
NPD_RESP_DIR    = os.environ.get("NPD_RESP_DIR",    "/tmp/npd_responses")
EPOCH_FILE      = os.environ.get("NPD_EPOCH_FILE",  "/tmp/npd_current_epoch")
CANNED          = os.environ.get("BENCH_CANNED_RESPONSE_PATH", "")
# Simulated LLM round-trip latency (ms). Real \sys agent.py blocks on this
# delay between dispatching a request and receiving the response —
# precisely the window during which async-warm can complete in the
# background. Defaults to 1000 ms, matching observed median LLM RTT.
LLM_RTT_MS      = float(os.environ.get("BENCH_LLM_RTT_MS", "1000"))

DEFAULT_PAYLOAD = json.dumps({
    "action": "run",
    "thought": "mock LLM response from canned-NPD bench harness.",
    "command": "echo bench-mock-noop",
})


def main() -> int:
    os.makedirs(NPD_REQ_DIR,  exist_ok=True)
    os.makedirs(NPD_RESP_DIR, exist_ok=True)
    for p in (NPD_REQ_FIFO, NPD_NOTIFY_FIFO):
        if not os.path.exists(p):
            os.mkfifo(p, 0o600)

    # Load canned content (or default).
    if CANNED and os.path.exists(CANNED):
        canned_content = open(CANNED).read()
    else:
        canned_content = DEFAULT_PAYLOAD

    # O_RDWR keeps FIFO open across writer churn.
    req_fd    = os.open(NPD_REQ_FIFO,    os.O_RDWR | os.O_NONBLOCK)
    notify_fd = os.open(NPD_NOTIFY_FIFO, os.O_RDWR | os.O_NONBLOCK)

    sys.stdout.write(f"[mock-npd] pid={os.getpid()} ready\n")
    sys.stdout.flush()

    buf = b""
    while True:
        try:
            r, _, _ = select.select([req_fd], [], [], 60.0)
        except (OSError, InterruptedError):
            continue
        if req_fd not in r:
            continue
        try:
            chunk = os.read(req_fd, 4096)
        except BlockingIOError:
            continue
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            rid = line.decode(errors="replace").strip()
            if not rid:
                continue
            # Read the request payload (agent already wrote it before
            # writing the rid).  In replay validation mode, echo the last
            # user message into the canned response so the harness can prove
            # the restored agent consumed the expected post-rollback request.
            req_path = os.path.join(NPD_REQ_DIR, f"{rid}.json")
            req_payload = {}
            try:
                with open(req_path) as f:
                    req_payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                req_payload = {}
            try:
                os.unlink(req_path)
            except OSError:
                pass
            # Simulate LLM round-trip latency. This is the idle window
            # async-warm overlaps with on real \sys.
            if LLM_RTT_MS > 0:
                time.sleep(LLM_RTT_MS / 1000.0)
            content = canned_content
            messages = req_payload.get("messages") if isinstance(req_payload, dict) else None
            if isinstance(messages, list) and messages:
                last = messages[-1]
                if isinstance(last, dict):
                    token = str(last.get("content", ""))
                    content = json.dumps({
                        "action": "run",
                        "thought": f"mock replay response for {token}",
                        "command": "echo bench-mock-noop",
                    })
            resp = {
                "ok": True,
                "content": content,
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "finish_reason": "stop",
            }
            resp_path = os.path.join(NPD_RESP_DIR, f"{rid}.json")
            tmp_path = resp_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(resp, f)
            os.rename(tmp_path, resp_path)
            os.write(notify_fd, (rid + "\n").encode())


if __name__ == "__main__":
    sys.exit(main())
