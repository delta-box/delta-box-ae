"""Epoch-tagged replay requests with one deadline for open, write and reply.

Only one driver sends workload commands. Workload requests are never retried: a
timed-out write or reply may already have executed. The cold replay_ready probe
alone may reconnect on EPIPE during epoch endpoint reopening. The record
separator recovers an incomplete pre-restore frame without replaying its tail.
"""
from __future__ import annotations

import errno
import json
import os
import select
import sys
import time


class AgentProbe:
    def __init__(self, pipe_in="/tmp/agent.in", pipe_out="/tmp/agent.out",
                 epoch_file=None):
        self.pipe_in = pipe_in
        self.pipe_out = pipe_out
        self.epoch_file = epoch_file or os.environ.get(
            "NPD_EPOCH_FILE", "/tmp/npd_current_epoch")
        self._in_fd = None
        self._out_fd = None
        self._out_buf = b""
        self._ctrl_seq = 0

    def _epoch(self):
        with open(self.epoch_file) as stream:
            epoch = int(stream.read().strip())
        if epoch < 0:
            raise ValueError("negative replay epoch")
        return epoch

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("agent channel deadline expired")
        return remaining

    def _connect_until(self, deadline):
        if self._out_fd is None:
            self._out_fd = os.open(self.pipe_out, os.O_RDWR | os.O_NONBLOCK)
        while self._in_fd is None:
            remaining = self._remaining(deadline)
            try:
                self._in_fd = os.open(self.pipe_in, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                time.sleep(min(0.005, remaining))

    def connect(self, timeout=5.0):
        try:
            self._connect_until(time.monotonic() + timeout)
        except BaseException:
            self.reset()
            raise

    def reset(self):
        for name in ("_in_fd", "_out_fd"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
        self._out_buf = b""

    def _send(self, payload, deadline):
        self._connect_until(deadline)
        frame = memoryview(b"\x1e" + json.dumps(payload).encode() + b"\n")
        while frame:
            self._remaining(deadline)
            try:
                written = os.write(self._in_fd, frame)
            except BlockingIOError:
                select.select([], [self._in_fd], [], self._remaining(deadline))
                continue
            if written <= 0:
                raise BrokenPipeError("agent request write made no progress")
            frame = frame[written:]

    def _reply(self, deadline, counters):
        while b"\n" not in self._out_buf:
            ready, _, _ = select.select([self._out_fd], [], [], self._remaining(deadline))
            if not ready:
                continue
            try:
                chunk = os.read(self._out_fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                counters["chunks_read"] += 1
                counters["bytes_read"] += len(chunk)
                self._out_buf += chunk
        line, self._out_buf = self._out_buf.split(b"\n", 1)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("agent reply must be an object")
        return reply

    def control(self, payload, timeout=60.0):
        started = time.monotonic()
        deadline = started + timeout
        counters = {"chunks_read": 0, "bytes_read": 0}
        skipped = []
        request = dict(payload)
        try:
            epoch = self._epoch()
            self._ctrl_seq += 1
            request["ctrl_id"] = f"ep{epoch}.ctrl-{self._ctrl_seq}"
            request["_replay_epoch"] = epoch
            self._send(request, deadline)
            while True:
                self._remaining(deadline)
                reply = self._reply(deadline, counters)
                if (reply.get("ctrl_id") != request["ctrl_id"]
                        or reply.get("ctrl") != request.get("ctrl")):
                    skipped.append(reply.get("ctrl_id"))
                    continue
                reply.setdefault("wall_ms", (time.monotonic() - started) * 1000)
                return reply
        except (OSError, ValueError) as error:
            failure = {"ok": False, "err": "timeout" if isinstance(error, TimeoutError)
                       else type(error).__name__, "msg": str(error),
                       "ctrl": request.get("ctrl"), "ctrl_id": request.get("ctrl_id"),
                       "epoch": request.get("_replay_epoch"), **counters,
                       "out_buf_len": len(self._out_buf), "skipped_ctrl_ids": skipped[-16:],
                       "wall_ms": (time.monotonic() - started) * 1000}
            self.reset()
            return failure

    def control_fresh(self, payload, timeout=60.0):
        self.reset()
        try:
            return self.control(payload, timeout)
        finally:
            self.reset()

    def request(self, step_idx, phase="ckpt", timeout=5.0):
        started = time.monotonic()
        deadline = started + timeout
        token = f"replay probe {phase} step={step_idx}"
        try:
            epoch = self._epoch()
            token += f" epoch={epoch}"
            self._send({"messages": [{"role": "user", "content": token}],
                        "temperature": 0.0, "_replay_epoch": epoch}, deadline)
            reply = self._reply(deadline, {"chunks_read": 0, "bytes_read": 0})
            response_text = json.dumps(reply, sort_keys=True)
            return {"ok": True, "response": reply, "expected_token": token,
                    "phase": phase, "response_text": response_text,
                    "mismatch": token not in response_text,
                    "wall_ms": (time.monotonic() - started) * 1000}
        except (OSError, ValueError) as error:
            self.reset()
            return {"ok": False, "err": type(error).__name__, "msg": str(error),
                    "expected_token": token, "phase": phase, "mismatch": True,
                    "wall_ms": (time.monotonic() - started) * 1000}


def restore_with_ready(controller, target_id, channel, resources=None):
    """Include the cold worker activation handshake in the measured API call."""
    previous_namespace = controller.ns_init_pid
    try:
        result = controller.restore_action(target_id)
    finally:
        # Cold restore can publish the new root and then fail bookkeeping or
        # activation. Register it before any ready request can fail. Warm hits
        # keep their namespace and avoid another pidfd/proc lookup entirely.
        if resources is not None and controller.ns_init_pid != previous_namespace:
            primary_error = sys.exc_info()[1]
            try:
                resources.own_namespace(controller.ns_init_pid)
            except Exception as ownership_error:
                if primary_error is None:
                    raise
                print(f"[replay] namespace ownership after restore failure: "
                      f"{ownership_error}", file=sys.stderr, flush=True)
    if channel is not None and result.get("path") != "warm-template":
        started = time.monotonic()
        deadline = started + 5.0
        attempts = 0
        ready = {"ok": False, "err": "timeout"}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready = channel.control_fresh({"ctrl": "replay_ready"}, timeout=remaining)
            attempts += 1
            if ready.get("err") != "BrokenPipeError":
                break
            # Epoch synchronization can close the agent's FIFO reader after
            # our WRONLY open but before write. Only this idempotent readiness
            # handshake may reconnect; never resend workload/control actions.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.005, remaining))
        if not ready.get("ok") or ready.get("epoch") != channel._epoch():
            raise RuntimeError(f"restored agent activation failed: {ready}")
        result["restore_replay_ready_ms"] = (time.monotonic() - started) * 1000
        result["restore_replay_ready_retries"] = max(0, attempts - 1)
    return result
