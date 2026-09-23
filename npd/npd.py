#!/usr/bin/env python3
"""
npd.py — Network Proxy Daemon, async file+FIFO edition.

Owns the LLM HTTP socket. Agent↔NPD channel is now completely socket-free on
the agent side:

    /tmp/npd_req.fifo     agent writes one rid per line
    /tmp/npd_requests/    agent writes <rid>.json payload before signalling
    /tmp/npd_responses/   NPD writes <rid>.json payload before notifying
    /tmp/npd_notify.fifo  NPD writes one rid per line

FIFOs carry ≤33-byte lines (hex uuid + "\\n"), well under PIPE_BUF=4096 so
writes are atomic — no UDS half-open state can ever exist inside the agent's
address space. CRIU-dumping the agent mid-LLM-call is trivially safe; all
NPD-side state (HTTPS socket, response buffers) lives outside the dump tree.

Concurrency: a ThreadPoolExecutor handles the upstream HTTPS calls. _chat
uses bounded retries for transient failures. Request-dispatch is serialised
through the main thread's select loop over the request FIFO.

File-write atomicity: write-tmp + rename. Any reader opening the final
path sees either "not yet there" (ENOENT) or the full file — never a
partial byte stream.
"""
from __future__ import annotations
import json
import os
import random
import select
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

NPD_REQ_FIFO    = os.environ.get("NPD_REQ_FIFO",    "/tmp/npd_req.fifo")
NPD_NOTIFY_FIFO = os.environ.get("NPD_NOTIFY_FIFO", "/tmp/npd_notify.fifo")
NPD_REQ_DIR     = os.environ.get("NPD_REQ_DIR",     "/tmp/npd_requests")
NPD_RESP_DIR    = os.environ.get("NPD_RESP_DIR",    "/tmp/npd_responses")
NPD_EPOCH_FILE  = os.environ.get("NPD_EPOCH_FILE",  "/tmp/npd_current_epoch")
API_KEY   = os.environ.get("API_KEY", "")
API_BASE  = os.environ.get("API_BASE", "")
MODEL     = os.environ.get("MODEL_NAME", "claude-sonnet-4-6")
LOG_PATH  = os.environ.get("NPD_LOG", "/tmp/npd.log")
N_WORKERS = int(os.environ.get("NPD_WORKERS", "8"))


_notify_lock = threading.Lock()
_notify_fd: int = -1


def _log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _chat(messages, temperature: float, max_tokens: int = 4000) -> dict:
    """Use an OpenAI-compatible endpoint; never log response bodies or AKs."""
    if not API_KEY or API_KEY == "EMPTY" or not API_BASE:
        raise ValueError("API_KEY and API_BASE must be configured")
    attempts = int(os.environ.get("NPD_MAX_ATTEMPTS", "3"))
    timeout = float(os.environ.get("NPD_HTTP_TIMEOUT_S", "60"))
    if not 1 <= attempts <= 10 or not 0 < timeout <= 180:
        raise ValueError("invalid NPD retry/timeout configuration")
    url = API_BASE.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": MODEL, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}",
               "Connection": "close"}
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.load(resp)
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("LLM response has no text content")
            return {"ok": True, "content": content, "usage": result.get("usage", {}),
                    "finish_reason": choice.get("finish_reason")}
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status not in (408, 429) and status < 500:
                raise RuntimeError(f"LLM HTTP {status}; request rejected") from None
            reason = f"HTTP {status}"
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            reason = "transport failure"
        except (ValueError, KeyError, IndexError, TypeError):
            raise RuntimeError("LLM returned an invalid chat-completions response") from None
        if attempt + 1 == attempts:
            raise RuntimeError(f"LLM unavailable after {attempts} attempts ({reason})")
        delay = min(2 ** attempt, 10) + random.uniform(0, 0.25)
        _log(f"chat retry {attempt + 1}/{attempts}: {reason}")
        time.sleep(delay)


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, path)


def _parse_rid_epoch(rid: str) -> int | None:
    # rid format "ep<N>.<uuid>" carries its submission-time epoch. Rids
    # without the prefix predate the overlap feature and are exempt.
    if not rid.startswith("ep"):
        return None
    dot = rid.find(".")
    if dot < 3:
        return None
    try:
        return int(rid[2:dot])
    except ValueError:
        return None


def _read_current_epoch() -> int:
    try:
        with open(NPD_EPOCH_FILE) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _process(rid: str) -> None:
    """Worker-thread: load <rid>.json, call upstream, write resp, notify."""
    req_path = os.path.join(NPD_REQ_DIR, f"{rid}.json")
    try:
        with open(req_path) as f:
            req = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log(f"req load failed rid={rid}: {e}")
        return
    try:
        os.unlink(req_path)
    except OSError:
        pass

    try:
        resp = _chat(req.get("messages", []),
                     float(req.get("temperature", 0.0)),
                     int(req.get("max_tokens", 4000)))
    except Exception as e:
        resp = {"ok": False, "content": "", "error": f"{type(e).__name__}: {e}"}
        _log(f"chat raised rid={rid}: {e}")

    # Epoch filter: if the caller's epoch is strictly older than the
    # current epoch (bumped by GSD on every CRIU restore), the response
    # belongs to a trajectory that's already been rolled away. Drop it —
    # no resp file, no notify. This makes post-restore stale responses
    # invisible to the agent regardless of pending-dict state. Rids
    # without an ep<N>. prefix are exempt (old call sites).
    sub_epoch = _parse_rid_epoch(rid)
    if sub_epoch is not None:
        cur_epoch = _read_current_epoch()
        if sub_epoch < cur_epoch:
            _log(f"epoch-drop rid={rid} sub_ep={sub_epoch} cur_ep={cur_epoch}")
            return

    resp_path = os.path.join(NPD_RESP_DIR, f"{rid}.json")
    try:
        _atomic_write_json(resp_path, resp)
    except OSError as e:
        _log(f"resp write failed rid={rid}: {e}")
        return

    # Optional full-content trace for replay benches. Set NPD_REPLAY_CAPTURE
    # to a JSONL path; we'll append {rid, req, resp} one entry per LLM round.
    capture_path = os.environ.get("NPD_REPLAY_CAPTURE")
    if capture_path:
        try:
            entry = {"ts": time.time(), "rid": rid, "req": req, "resp": resp}
            with open(capture_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
        except OSError as e:
            _log(f"replay-capture failed rid={rid}: {e}")

    line = (rid + "\n").encode()
    with _notify_lock:
        try:
            os.write(_notify_fd, line)
        except OSError as e:
            _log(f"notify write failed rid={rid}: {e}")


def main() -> int:
    os.makedirs(NPD_REQ_DIR, exist_ok=True)
    os.makedirs(NPD_RESP_DIR, exist_ok=True)
    for p in (NPD_REQ_FIFO, NPD_NOTIFY_FIFO):
        if not os.path.exists(p):
            os.mkfifo(p, 0o600)

    # O_RDWR keeps the FIFO readable even when every writer has (briefly)
    # disappeared — e.g. between agent fork/SIGSTOP transitions.
    req_fd = os.open(NPD_REQ_FIFO, os.O_RDWR | os.O_NONBLOCK)
    global _notify_fd
    _notify_fd = os.open(NPD_NOTIFY_FIFO, os.O_RDWR | os.O_NONBLOCK)

    pool = ThreadPoolExecutor(max_workers=N_WORKERS, thread_name_prefix="npd-http")
    _log(f"NPD ready: req={NPD_REQ_FIFO} notify={NPD_NOTIFY_FIFO} "
         f"req_dir={NPD_REQ_DIR} resp_dir={NPD_RESP_DIR} "
         f"workers={N_WORKERS} model={MODEL} api_base={API_BASE}")

    buf = b""
    while True:
        try:
            ready, _, _ = select.select([req_fd], [], [], 1.0)
        except InterruptedError:
            continue
        if not ready:
            continue
        try:
            chunk = os.read(req_fd, 65536)
        except BlockingIOError:
            continue
        if not chunk:
            time.sleep(0.01)
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            rid = line.decode(errors="replace").strip()
            if rid:
                pool.submit(_process, rid)


if __name__ == "__main__":
    sys.exit(main() or 0)
