"""worker_client.py — host-side control client for the in-sandbox agent_worker.

The host (MCTS strategy) talks to the checkpointable worker over a JSON-line FIFO
pair (/tmp/agent.in host→worker, /tmp/agent.out worker→host). One request is in
flight at a time; the worker echoes each request's ctrl_id.

After a warm-fork restore the worker is a fresh process that inherited the FIFO
read/write ends from the frozen template, so the FIFO files persist — but the
host re-opens its own ends (`reopen`) to drop any half-read buffer from the
abandoned branch.
"""
from __future__ import annotations

import errno
import json
import os
import select
import time
import uuid


class WorkerError(RuntimeError):
    pass


class WorkerClient:
    def __init__(self, pipe_in: str = "/tmp/agent.in",
                 pipe_out: str = "/tmp/agent.out",
                 default_timeout: float = 300.0):
        self.pipe_in = pipe_in
        self.pipe_out = pipe_out
        self.default_timeout = default_timeout
        self._fin = None          # unbuffered nonblocking writer to pipe_in
        self._fout_fd: int | None = None  # raw fd reader of pipe_out
        self._buf = b""
        self._seq = 0

    # ── connection lifecycle ──
    def connect(self, wait_s: float = 10.0) -> None:
        deadline = time.monotonic() + wait_s
        try:
            while self._fin is None:
                remaining = self._remaining(deadline)
                try:
                    if self._fout_fd is None:
                        self._fout_fd = os.open(self.pipe_out, os.O_RDWR | os.O_NONBLOCK)
                    fd = os.open(self.pipe_in, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as error:
                    if error.errno not in (errno.ENOENT, errno.ENXIO):
                        raise
                    # A restored reader may not have opened the FIFO yet. Do
                    # not block in open() beyond the connection deadline.
                    time.sleep(min(.005, remaining))
                    continue
                try:
                    self._fin = os.fdopen(fd, "wb", buffering=0)
                except BaseException:
                    os.close(fd)
                    raise
            self._buf = b""
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerError("worker connection/ready deadline expired")
        return remaining

    def reopen(self, timeout: float = 10.0) -> None:
        """Discard the old branch's channel data and await the restored worker.

        A matching state reply is the readiness barrier. Fixed sleeps neither
        establish readiness nor distinguish an abandoned branch's response.
        """
        deadline = time.monotonic() + timeout
        self.close()
        try:
            self.connect(wait_s=self._remaining(deadline))
            self._drain(deadline)
            ready = self.state(timeout=self._remaining(deadline))
            if ready.get("ok") is not True:
                raise WorkerError(f"worker ready handshake failed: {ready}")
        except BaseException:
            self.close()
            raise

    def _drain(self, deadline: float | None = None) -> None:
        if self._fout_fd is None:
            return
        while True:
            if deadline is not None:
                self._remaining(deadline)
            try:
                if not os.read(self._fout_fd, 65536):
                    break
            except BlockingIOError:
                break
        self._buf = b""

    def close(self) -> None:
        try:
            if self._fin is not None:
                self._fin.close()
        except OSError:
            pass
        try:
            if self._fout_fd is not None:
                os.close(self._fout_fd)
        except OSError:
            pass
        self._fin = None
        self._fout_fd = None
        self._buf = b""

    # ── one request / one reply ──
    def call(self, payload: dict, timeout: float | None = None) -> dict:
        if self._fin is None or self._fout_fd is None:
            raise WorkerError("worker client not connected")
        self._seq += 1
        ctrl_id = payload.get("ctrl_id") or f"h{self._seq}.{uuid.uuid4().hex[:8]}"
        payload = dict(payload)
        payload["ctrl_id"] = ctrl_id
        timeout_s = self.default_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout_s
        frame = memoryview((json.dumps(payload) + "\n").encode())
        while frame:
            remaining = self._remaining(deadline)
            try:
                written = os.write(self._fin.fileno(), frame)
            except BlockingIOError:
                select.select([], [self._fin.fileno()], [], remaining)
                continue
            if written <= 0:
                raise WorkerError("worker request write made no progress")
            frame = frame[written:]
        while time.monotonic() < deadline:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                try:
                    obj = json.loads(line.decode(errors="replace"))
                except json.JSONDecodeError:
                    continue
                # Tolerate (skip) a stale reply from an abandoned branch.
                if not isinstance(obj, dict) or obj.get("ctrl_id") != ctrl_id:
                    continue
                return obj
            r, _, _ = select.select([self._fout_fd], [], [],
                                    max(0.0, deadline - time.monotonic()))
            if not r:
                continue
            try:
                chunk = os.read(self._fout_fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                self._buf += chunk
        raise WorkerError(f"worker timeout after "
                          f"{timeout_s}s for {payload.get('ctrl')}")

    # ── typed ops ──
    def init(self, repo_path: str, task: str, timeout: float = 60.0) -> dict:
        return self.call({"ctrl": "init", "repo_path": repo_path, "task": task},
                         timeout=timeout)

    def step(self, temperature: float = 0.0, observation: str | None = None,
             timeout: float | None = None) -> dict:
        req = {"ctrl": "step", "temperature": temperature}
        if observation:
            req["observation"] = observation
        return self.call(req, timeout=timeout)

    def state(self, timeout: float = 15.0) -> dict:
        return self.call({"ctrl": "state"}, timeout=timeout)
