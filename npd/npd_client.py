"""npd_client — polling NPD client for in-process callers (e.g. value_agent).

Rationale: the agent process owns the single reader of NPD_NOTIFY_FIFO. Other
in-process LLM callers (value_agent, future evaluators) can't also read that
FIFO without racing. Instead we use the same NPD_REQ_FIFO + resp_dir files
but poll for the response file's existence — a few ms of polling cost per
request is trivial against 6-14 s LLM round trips.

Key properties:
  - rid carries the submit-time epoch ("ep<N>.<uuid>"); NPD drops responses
    for stale epochs, so after a CRIU restore the poll simply times out
    (or returns when the agent finally sends new requests — caller should
    abandon old rids post-restore).
  - submit() is non-blocking; caller holds the rid, may poll later.
  - wait() blocks with a deadline; returns None on timeout or cancellation.
  - A shared global req fd is opened once per process; O_RDWR so a stopped
    NPD doesn't torpedo us at open time.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid

NPD_REQ_FIFO   = os.environ.get("NPD_REQ_FIFO",   "/tmp/npd_req.fifo")
NPD_REQ_DIR    = os.environ.get("NPD_REQ_DIR",    "/tmp/npd_requests")
NPD_RESP_DIR   = os.environ.get("NPD_RESP_DIR",   "/tmp/npd_responses")
NPD_EPOCH_FILE = os.environ.get("NPD_EPOCH_FILE", "/tmp/npd_current_epoch")

_req_fd: int = -1
_fd_lock = threading.Lock()
_write_lock = threading.Lock()


def _ensure_fd() -> int:
    global _req_fd
    with _fd_lock:
        if _req_fd < 0 or not _is_fd_alive(_req_fd):
            if _req_fd >= 0:
                try:
                    os.close(_req_fd)
                except OSError:
                    pass
            _req_fd = os.open(NPD_REQ_FIFO, os.O_RDWR | os.O_NONBLOCK)
        return _req_fd


def _is_fd_alive(fd: int) -> bool:
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def _read_current_epoch() -> int:
    try:
        with open(NPD_EPOCH_FILE) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, path)


def submit(messages, temperature: float = 0.0, max_tokens: int = 400,
           epoch_exempt: bool = False) -> tuple[str, int]:
    """Submit an LLM request. Returns (rid, submit_epoch).

    epoch_exempt=True generates an rid WITHOUT the "ep<N>." prefix, so
    NPD's epoch filter (npd.py L150-161) does not drop the response
    after a CRIU restore bumps the epoch. Use this from in-process
    callers that run OUTSIDE the CRIU-checkpointed process tree (e.g.
    runner-side value_agent): the caller's state persists across
    restore, and the evaluated tree node is still alive in MCTS, so
    the response is still meaningful. submit_epoch returned as -1
    signals "no epoch to check" to wait() below.
    """
    os.makedirs(NPD_REQ_DIR, exist_ok=True)
    if epoch_exempt:
        epoch = -1
        rid = uuid.uuid4().hex
    else:
        epoch = _read_current_epoch()
        rid = f"ep{epoch}.{uuid.uuid4().hex}"
    _atomic_write_json(
        os.path.join(NPD_REQ_DIR, f"{rid}.json"),
        {"messages": messages, "temperature": temperature, "max_tokens": max_tokens},
    )
    line = (rid + "\n").encode()
    fd = _ensure_fd()
    with _write_lock:
        os.write(fd, line)
    return rid, epoch


def wait(rid: str, submit_epoch: int, *,
         timeout_s: float = 180.0,
         poll_interval_s: float = 0.05) -> dict | None:
    """Block until <rid>.json appears in resp_dir, or return None if the
    current epoch has moved past `submit_epoch` (our request was cancelled
    by a restore — NPD will never write a response). Also returns None on
    deadline expiry."""
    resp_path = os.path.join(NPD_RESP_DIR, f"{rid}.json")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(resp_path):
            try:
                with open(resp_path) as f:
                    resp = json.load(f)
                try:
                    os.unlink(resp_path)
                except OSError:
                    pass
                return resp
            except (OSError, json.JSONDecodeError):
                # Partial write race shouldn't happen (NPD uses rename), but
                # stay defensive.
                time.sleep(poll_interval_s)
                continue
        # Cancellation check: if the current epoch has advanced past ours,
        # NPD's filter will drop the response. No point polling further.
        # submit_epoch == -1 signals an epoch-exempt rid (see submit());
        # such calls never cancel and we poll until timeout or response.
        if submit_epoch >= 0 and _read_current_epoch() > submit_epoch:
            return None
        time.sleep(poll_interval_s)
    return None


def call(messages, temperature: float = 0.0, max_tokens: int = 400,
         timeout_s: float = 180.0) -> dict | None:
    """Convenience: submit + wait. Returns resp dict or None on
    cancel/timeout."""
    rid, ep = submit(messages, temperature=temperature, max_tokens=max_tokens)
    return wait(rid, ep, timeout_s=timeout_s)
