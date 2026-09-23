"""guest/rl/batched_fork.py — batched warm-template fork for RL fan-out.

This module extends guest/template_fork.py's protocol (which only supports
1:1 fork at a time via op="fork") with a new op "fork_n" that performs N
forks in a single SIGCONT/SIGSTOP cycle:

    Existing 1:1 protocol (template_fork.handle_template_message):
      harness:  SIGCONT donor → write {"op":"fork"} → read 1 response line
                → wait_until_stopped(donor)
      donor:    select() readable → handle_template_message → fork once
                → write 1 response line → SIGSTOP

    New batched protocol (handle_batched_message in this file):
      harness:  SIGCONT donor → write {"op":"fork_n", "n":N} → read N
                response lines → wait_until_stopped(donor)
      donor:    select() readable → handle_batched_message → fork N times
                → write N response lines → SIGSTOP

For RL fan-out (Tree-GRPO style), batching saves (N-1) × SIGCONT/SIGSTOP
context-switch round-trips per "RL step". Empirically each SIGCONT/SIGSTOP
round-trip is ~1-2 ms of elapsed time across the harness↔donor boundary; on
N=64 fan-out this can compress the fan-out time by ~60-120 ms.

DESIGN PRINCIPLE: this module **does not modify** guest/template_fork.py.
It only IMPORTS helpers from it (`install_template_endpoint`,
`_find_child_host_pid`, `_translate_ns_to_host`, the FIFO path constants),
defines a new handler `handle_batched_message`, and exposes a new pool
class `BatchedTemplatePool` that talks to a donor running the new handler.
Existing 1:1 callers of `template_fork.handle_template_message` are
unaffected.

Usage in a donor process:
    from guest.rl import batched_fork
    read_fd, write_path = batched_fork.install_template_endpoint()
    while True:
        ready = select.select([read_fd], [], [], 1.0)
        if not ready[0]:
            continue
        role = batched_fork.handle_batched_message(read_fd, write_path)
        if role == "child":
            # child-specific work (e.g. trigger-file wait + rollout)
            ...

Usage in a harness:
    pool = batched_fork.BatchedTemplatePool(ctrl_in=..., ctrl_out=...)
    pool.dispatch_fork_batch(source_pid, n=N)
    children = pool.await_fork_batch(source_pid, n=N, timeout=10.0)
    # children = [(parent_host_pid, child_host_pid), ...] length N
"""
from __future__ import annotations

import json
import os
import signal
import time

# Re-use the existing protocol primitives. We do NOT modify these.
from template_fork import (  # noqa: E402 (parent guest/ added to sys.path)
    install_template_endpoint,
    _find_child_host_pid,
    _translate_ns_to_host,
    _assert_single_threaded,
    _write_response,
    _log,
)


# ─────────────────────── Donor side (handler) ───────────────────────

def handle_batched_message(read_fd: int, write_path: str) -> str | None:
    """Read one message; dispatch to single-fork or batched fork.

    Behaves identically to `template_fork.handle_template_message` when
    op="fork" so a donor running this handler is a drop-in replacement.
    When op="fork_n", forks N children and writes N response lines before
    the single SIGSTOP.

    Returns "child" if THIS process is one of the freshly-forked children
    (caller should break out and do its child-side work); "parent_resumed"
    if this is the template parent finishing the dispatch; None on a
    malformed message or empty read.
    """
    try:
        buf = os.read(read_fd, 4096)
    except BlockingIOError:
        return None
    if not buf:
        return None
    line = buf.decode().strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        _log(f"batched: bad ctrl message: {line!r}")
        return None

    op = msg.get("op")

    # --- 1:1 fork path: identical to template_fork.handle_template_message ---
    if op == "fork":
        n_threads = _assert_single_threaded()
        if n_threads != 1:
            _write_response(write_path,
                            {"ok": False,
                             "error": f"multithread ({n_threads})"})
            return None
        pid = os.fork()
        if pid == 0:
            return "child"
        _write_response(write_path,
                        {"ok": True, "parent": os.getpid(), "child": pid})
        _log(f"batched (single): forked parent={os.getpid()} child={pid}")
        os.kill(os.getpid(), signal.SIGSTOP)
        return "parent_resumed"

    # --- N:1 batched path: new ---
    if op == "fork_n":
        try:
            n = int(msg.get("n", 0))
        except (TypeError, ValueError):
            _write_response(write_path,
                            {"ok": False, "error": "bad n field"})
            return None
        if n <= 0 or n > 1024:
            _write_response(write_path,
                            {"ok": False, "error": f"n out of range: {n}"})
            return None

        n_threads = _assert_single_threaded()
        if n_threads != 1:
            _write_response(write_path,
                            {"ok": False,
                             "error": f"multithread ({n_threads})"})
            return None

        my_pid_ns = os.getpid()
        children_ns_pids = []
        for i in range(n):
            pid = os.fork()
            if pid == 0:
                # Child: return immediately; caller's outer loop sees "child".
                return "child"
            children_ns_pids.append(pid)

        # Parent: write N response lines, one per fork.
        for child_pid in children_ns_pids:
            _write_response(write_path,
                            {"ok": True,
                             "parent": my_pid_ns,
                             "child": child_pid})
        _log(f"batched (fork_n={n}): forked parent={my_pid_ns} "
             f"children={children_ns_pids[:4]}...")
        os.kill(os.getpid(), signal.SIGSTOP)
        return "parent_resumed"

    # Unknown op
    _log(f"batched: unknown op {op!r}")
    return None


# ─────────────────────── Harness side (pool) ───────────────────────

CTRL_IN_FIFO_DEFAULT  = "/tmp/agentfs_template_in.fifo"
CTRL_OUT_FIFO_DEFAULT = "/tmp/agentfs_template_out.fifo"


class BatchedTemplatePool:
    """Harness-side pool that talks to a donor running handle_batched_message.

    Single-fork path (`dispatch_fork`/`await_fork`) is wire-compatible with
    `template_fork.TemplatePool` — the donor's handle_batched_message
    recognizes the same op="fork" message. Batched path adds
    dispatch_fork_batch / await_fork_batch.
    """

    def __init__(self,
                 ctrl_in: str = CTRL_IN_FIFO_DEFAULT,
                 ctrl_out: str = CTRL_OUT_FIFO_DEFAULT):
        self.ctrl_in_path = ctrl_in
        self.ctrl_out_path = ctrl_out
        self._out_fd: int | None = None

    def _ensure_out_fd(self) -> int:
        if self._out_fd is None:
            self._out_fd = os.open(self.ctrl_out_path,
                                   os.O_RDWR | os.O_NONBLOCK)
        return self._out_fd

    def _drain_pending(self) -> None:
        fd = self._ensure_out_fd()
        try:
            while True:
                buf = os.read(fd, 4096)
                if not buf:
                    return
        except BlockingIOError:
            return

    # ---- Single-fork path (compatibility with template_fork.TemplatePool) ----

    def dispatch_fork(self, source_pid: int) -> bool:
        self._drain_pending()
        cmd = (json.dumps({"op": "fork"}) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"batched pool: open ctrl_in failed: {e}")
            return False
        try:
            os.write(wfd, cmd)
        finally:
            os.close(wfd)
        try:
            os.kill(source_pid, signal.SIGCONT)
        except ProcessLookupError:
            return False
        return True

    def await_fork(self, source_pid: int, timeout: float = 5.0):
        children = self._read_n_responses(1, timeout)
        if not children:
            return None, None
        ns_parent, ns_child = children[0]
        host_parent = source_pid
        host_child = _find_child_host_pid(source_pid, ns_child)
        if host_child is None:
            host_child = _translate_ns_to_host(ns_child, source_pid)
        if host_child is None:
            return None, None
        return host_parent, host_child

    # ---- Batched path: NEW ----

    def dispatch_fork_batch(self, source_pid: int, n: int) -> bool:
        """Send a single fork_n command. Donor forks N times, replies N
        response lines, then SIGSTOPs. Returns True on successful dispatch.
        """
        self._drain_pending()
        cmd = (json.dumps({"op": "fork_n", "n": int(n)}) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"batched pool: open ctrl_in failed: {e}")
            return False
        try:
            os.write(wfd, cmd)
        finally:
            os.close(wfd)
        try:
            os.kill(source_pid, signal.SIGCONT)
        except ProcessLookupError:
            return False
        return True

    def await_fork_batch(self, source_pid: int, n: int,
                         timeout: float = 10.0) -> list[tuple[int, int]]:
        """Read N response lines, translate each ns_child → host_child,
        return list of (host_parent_pid, host_child_pid). Parent host PID
        is always source_pid (fork doesn't change parent's host PID).

        Returns empty list on any read/parse failure; caller treats this as
        a fan-out failure and falls back to slow path / aborts.
        """
        ns_pairs = self._read_n_responses(n, timeout)
        if len(ns_pairs) < n:
            return []
        host_parent = source_pid
        result: list[tuple[int, int]] = []
        for ns_parent, ns_child in ns_pairs:
            host_child = _find_child_host_pid(source_pid, ns_child)
            if host_child is None:
                host_child = _translate_ns_to_host(ns_child, source_pid)
            if host_child is None:
                _log(f"batched pool: ns_child={ns_child} ns→host failed")
                return []
            result.append((host_parent, host_child))
        return result

    # ---- Shared response reader (line-oriented) ----

    def _read_n_responses(self, n: int,
                          timeout: float) -> list[tuple[int, int]]:
        """Block until N response lines have been read, or timeout."""
        import select as _select
        fd = self._ensure_out_fd()
        deadline = time.time() + timeout
        buf = b""
        pairs: list[tuple[int, int]] = []
        while time.time() < deadline and len(pairs) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            r, _, _ = _select.select([fd], [], [], remaining)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf and len(pairs) < n:
                line, buf = buf.split(b"\n", 1)
                try:
                    resp = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    _log(f"batched pool: bad response: {e} raw={line!r}")
                    continue
                if not resp.get("ok"):
                    _log(f"batched pool: donor refused: {resp!r}")
                    continue
                try:
                    pairs.append((int(resp["parent"]), int(resp["child"])))
                except (KeyError, ValueError):
                    _log(f"batched pool: malformed pair: {resp!r}")
                    continue
        if len(pairs) < n:
            _log(f"batched pool: read {len(pairs)}/{n} responses before timeout")
        return pairs


__all__ = [
    "install_template_endpoint",
    "handle_batched_message",
    "BatchedTemplatePool",
]
