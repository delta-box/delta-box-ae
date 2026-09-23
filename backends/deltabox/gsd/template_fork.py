"""
template_fork.py — Warm-template restore plumbing.

Design (revised after multi-thread fork-safety audit):

  Fork is driven by a *control FIFO pair* between GSD (sandbox controller)
  and the agent. The agent's main loop polls the control FIFO at every
  iteration of its select(); when a {"op":"fork"} command arrives, the
  agent is in a known quiescent state (between turns, no in-flight NPD
  call, no half-written file) and calls os.fork() inline. The parent
  responds with both PIDs and self-suspends with SIGSTOP, becoming a
  frozen template. The child returns to the main loop and becomes the
  new active agent.

  This replaces an earlier SIGUSR1-handler design that was unsafe in
  the presence of any second thread (Python GC arena, our own prewarm
  thread, etc.) — fork() inside an async signal handler can deadlock
  on glibc malloc arena locks. The FIFO-driven path eliminates that
  class of bugs entirely.

FIFO ownership invariant (critical):

  Every agent process (active + templates) inherits the FIFO read fd
  across os.fork(). Correctness requires that at any moment at most
  ONE of them is runnable: the "active" agent. All other descendants
  are either SIGSTOP'd (templates) or dead (rolled-back chain). The
  SandboxController enforces this by:
    - Always SIGSTOP'ing the parent immediately after os.fork()
      returns (handle_template_message below).
    - Always SIGKILL + waitpid'ing the current active before
      SIGCONT'ing any template (restore_action in sandbox_controller).
    - Serializing all fork requests through TemplatePool.request_fork,
      which blocks on the response before sending the next command.

  Violating any of these would let two runnable readers race on a
  single FIFO message; one would get the "fork" command and the other
  would spuriously loop. handle_template_message's _assert_owner
  check below is a cheap runtime tripwire.

Files:
  CTRL_IN_FIFO  — GSD writes commands ("fork"), agent reads.
  CTRL_OUT_FIFO — agent writes responses, GSD reads.

Both are created by install_template_endpoint() at agent startup. The
GSD's TemplatePool opens them lazily on first use.
"""
from __future__ import annotations

import json
import os
import signal
import time
import ctypes
import select

# Import once before the checkpointed event loop starts. Loading pathlib and
# resource helpers separately in every disposable child adds avoidable latency
# to the foreground freeze handshake.
_freeze_async_resources = None
if os.environ.get("DELTABOX_ASYNC_INCREMENTAL_DUMP") == "1":
    try:
        from .async_resources import freeze_dump_view as _freeze_async_resources
    except ImportError:
        from async_resources import freeze_dump_view as _freeze_async_resources

CTRL_IN_FIFO  = os.environ.get("TEMPLATE_CTRL_IN",  "/tmp/template_ctrl.in")
CTRL_OUT_FIFO = os.environ.get("TEMPLATE_CTRL_OUT", "/tmp/template_ctrl.out")
LOG_FILE      = os.environ.get("TEMPLATE_LOG",      "/tmp/template_fork.log")
CLONE_NEWPID = 0x20000000
CLONE_PARENT = 0x00008000
CLONE_PIDFD = 0x00001000
SYS_CLONE3_X86_64 = 435
SYS_CLONE_X86_64 = 56
_CTRL_IN_BUFS: dict[int, bytes] = {}


class _CloneArgs(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_ulonglong),
        ("pidfd", ctypes.c_ulonglong),
        ("child_tid", ctypes.c_ulonglong),
        ("parent_tid", ctypes.c_ulonglong),
        ("exit_signal", ctypes.c_ulonglong),
        ("stack", ctypes.c_ulonglong),
        ("stack_size", ctypes.c_ulonglong),
        ("tls", ctypes.c_ulonglong),
        ("set_tid", ctypes.c_ulonglong),
        ("set_tid_size", ctypes.c_ulonglong),
        ("cgroup", ctypes.c_ulonglong),
    ]


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _fresh_pidns_enabled(msg: dict) -> bool:
    return (msg.get("fresh_pidns") is True
            or os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1")


def _fixed_active_pid(msg: dict) -> int | None:
    raw = msg.get("fixed_active_pid")
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def _unshare_newpid() -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.unshare(CLONE_NEWPID) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _clear_self_soft_dirty() -> bool:
    try:
        with open("/proc/self/clear_refs", "w") as f:
            f.write("4")
        return True
    except OSError as e:
        _log(f"self clear_refs failed: {e}")
        return False


def _clone3_newpid_pidfd(*, clone_parent: bool = False) -> tuple[int, int]:
    """clone3(CLONE_NEWPID|CLONE_PIDFD) and return (pid, pidfd).

    Parent gets child's pid as seen in the parent's PID namespace; child gets
    pid == 0. The pidfd lives in the caller process, so it is useful for local
    liveness/cleanup only; the controller still receives the numeric child pid
    and can translate it through /proc like the ordinary fork path.
    """
    pidfd = ctypes.c_int(-1)
    args = _CloneArgs()
    args.flags = CLONE_NEWPID | CLONE_PIDFD
    if clone_parent:
        args.flags |= CLONE_PARENT
    args.pidfd = ctypes.addressof(pidfd)
    # clone3 rejects CLONE_PARENT with an explicit exit signal; the child
    # inherits the parent's exit signal in that mode.
    args.exit_signal = 0 if clone_parent else signal.SIGCHLD
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ret = libc.syscall(
        ctypes.c_long(SYS_CLONE3_X86_64),
        ctypes.byref(args),
        ctypes.c_size_t(ctypes.sizeof(args)),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(ret), int(pidfd.value)


def _clone3_same_pidns_settid(set_tid: int) -> int:
    """clone3(CLONE_PARENT, set_tid=X) inside the current PID namespace.

    This is the non-nesting warm-restore primitive: a frozen template creates
    a new active in the same pidns, pinned to the CRIU-visible PID X.
    """
    tid = ctypes.c_ulonglong(set_tid)
    args = _CloneArgs()
    args.flags = CLONE_PARENT
    args.exit_signal = 0
    args.set_tid = ctypes.addressof(tid)
    args.set_tid_size = 1
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ret = libc.syscall(
        ctypes.c_long(SYS_CLONE3_X86_64),
        ctypes.byref(args),
        ctypes.c_size_t(ctypes.sizeof(args)),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(ret)


def _clone_newpid_parent() -> int:
    """Legacy clone(CLONE_NEWPID|CLONE_PARENT).

    clone3 rejects this flag combination in the nested guest/agent topology on
    some kernels, while legacy clone is exactly what CRIU's capability probe
    uses. The child is detached from the frozen template's subtree and becomes
    PID 1 in its own fresh PID namespace.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ret = libc.syscall(
        ctypes.c_long(SYS_CLONE_X86_64),
        ctypes.c_ulong(CLONE_NEWPID | CLONE_PARENT),
        ctypes.c_void_p(0),
        ctypes.c_void_p(0),
        ctypes.c_void_p(0),
        ctypes.c_ulong(0),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(ret)


def _pidfd_nspids(pidfd: int) -> list[int]:
    try:
        with open(f"/proc/self/fdinfo/{pidfd}") as f:
            for line in f:
                if line.startswith("NSpid:"):
                    return [int(x) for x in line.split()[1:]]
                if line.startswith("Pid:"):
                    pid = int(line.split()[1])
                    if pid > 0:
                        fallback = [pid]
    except (OSError, ValueError):
        return []
    return locals().get("fallback", [])


def _nspids_for_pid(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("NSpid:"):
                    return [int(x) for x in line.split()[1:]]
    except (OSError, ValueError):
        pass
    return []


def _self_nspids() -> list[int]:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("NSpid:"):
                    return [int(x) for x in line.split()[1:]]
    except (OSError, ValueError):
        pass
    return []


def _pid_state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    fields = line.split()
                    return fields[1] if len(fields) > 1 else None
    except OSError:
        return None
    return None


def _ppid_for_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _find_own_child_host_pid(target_ns_pid: int) -> int | None:
    try:
        tids = os.listdir("/proc/self/task")
    except OSError:
        return None
    candidates: list[str] = []
    for tid in tids:
        try:
            with open(f"/proc/self/task/{tid}/children") as f:
                candidates.extend(f.read().split())
        except OSError:
            continue
    for child in candidates:
        try:
            child_int = int(child)
            if _pid_state(child_int) == "Z":
                continue
            with open(f"/proc/{child}/status") as f:
                for line in f:
                    if line.startswith("NSpid:"):
                        nspids = [int(x) for x in line.split()[1:]]
                        if nspids and nspids[-1] == target_ns_pid:
                            return child_int
                        break
        except (OSError, ValueError):
            continue
    return None


# ─────────────────────── Agent-side ───────────────────────

def install_template_endpoint() -> tuple:
    """Create the control FIFO pair and return (read_fd, write_fd_path).

    Agent's main loop is expected to add `read_fd` to its select() set
    and call handle_template_message() when it becomes readable. We
    return raw fds (not file objects) so the caller can use os.read /
    os.write — non-blocking and signal-safe.
    """
    for path in (CTRL_IN_FIFO, CTRL_OUT_FIFO):
        if not os.path.exists(path):
            os.mkfifo(path, 0o600)

    # Open read end nonblocking; opening read-write keeps the FIFO open
    # even when the GSD writer is briefly absent (avoids EOF storms).
    read_fd = os.open(CTRL_IN_FIFO, os.O_RDWR | os.O_NONBLOCK)
    return read_fd, CTRL_OUT_FIFO


def has_pending_ctrl_message(read_fd: int) -> bool:
    return b"\n" in _CTRL_IN_BUFS.get(read_fd, b"")


def _read_ctrl_line(read_fd: int) -> str | None:
    buf = _CTRL_IN_BUFS.get(read_fd, b"")
    if b"\n" not in buf:
        try:
            chunk = os.read(read_fd, 4096)
        except BlockingIOError:
            _CTRL_IN_BUFS[read_fd] = buf
            return None
        if not chunk:
            _CTRL_IN_BUFS[read_fd] = buf
            return None
        buf += chunk
    if b"\n" not in buf:
        _CTRL_IN_BUFS[read_fd] = buf
        return None
    line_b, rest = buf.split(b"\n", 1)
    _CTRL_IN_BUFS[read_fd] = rest
    line = line_b.decode(errors="replace").strip()
    return line or None


def _assert_single_threaded() -> int:
    """Return the thread count, or -1 when it cannot be verified safely.

    Uses /proc/self/task rather than /proc/<os.getpid()>/task because the
    agent runs in its own PID ns (via namespace_launcher) while /proc is
    still bound to init PID ns — so os.getpid() returns the ns-local PID
    (typically 1) which would resolve to init's task set under init-ns
    /proc. /proc/self is ns-invariant.
    """
    try:
        n = len(os.listdir("/proc/self/task"))
    except OSError as exc:
        _log(f"WARN: cannot verify single-threaded fork safety — refusing: {exc}")
        return -1
    if n != 1:
        names = []
        try:
            import threading
            names = [t.name for t in threading.enumerate()]
        except Exception:
            pass
        _log(f"WARN: fork requested but {n} threads alive — refusing; names={names}")
    return n


def handle_template_message(read_fd: int, write_path: str,
                            probe_payload: bytes | bytearray | None = None
                            ) -> str | None:
    """Handle queued commands while a stopped template is being reused.

    The controller queues a command before SIGCONT. A resumed template can
    read it here without unwinding through the agent's application loop and
    rebuilding its select set. The active parent and new child still return
    to that loop immediately. An unsolicited SIGCONT with no complete command
    also returns, preserving the existing nonblocking endpoint contract.
    """
    resumed = False
    while True:
        role = _handle_template_message(read_fd, write_path, probe_payload)
        if role != "parent_resumed":
            return "parent_resumed" if resumed and role is None else role
        resumed = True


def _handle_template_message(read_fd: int, write_path: str,
                             probe_payload: bytes | bytearray | None = None
                             ) -> str | None:
    """Read one message from the control FIFO and act on it.

    Called from the agent's main loop after select() reports the FIFO
    readable. Returns the role this process took: "child" if a fork
    happened and we are the new active agent (caller should continue
    its main loop); "parent_resumed" if we are a previously-stashed
    template that just got SIGCONT'd and is preparing to fork again
    (caller should continue waiting for the next message); None if no
    actionable message was available.
    """
    line = _read_ctrl_line(read_fd)
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        _log(f"bad ctrl message: {line!r}")
        return None

    op = msg.get("op")
    if op not in ("fork", "stash_template", "reap_children",
                  "reap_pid", "probe_digest"):
        return None

    n_threads = _assert_single_threaded()
    if n_threads != 1:
        _write_response(write_path, {"ok": False,
                                     "error": (
                                         "thread count unavailable; refusing fork"
                                         if n_threads < 0
                                         else f"multithread ({n_threads})")})
        return None

    if op == "reap_children":
        reaped: list[int] = []
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            except OSError as e:
                _write_response(write_path, {
                    "ok": False,
                    "mode": "reap_children",
                    "error": str(e),
                    "reaped": reaped,
                })
                os.kill(os.getpid(), signal.SIGSTOP)
                return "parent_resumed"
            if pid <= 0:
                break
            reaped.append(pid)
        _write_response(write_path, {
            "ok": True,
            "mode": "reap_children",
            "pid": os.getpid(),
            "reaped": reaped,
        })
        _log(f"reaped children: pid={os.getpid()} reaped={reaped}")
        os.kill(os.getpid(), signal.SIGSTOP)
        return "parent_resumed"

    if op == "reap_pid":
        target = msg.get("pid")
        reaped: list[int] = []
        try:
            target_pid = int(target)
            deadline = time.time() + 0.25
            while True:
                try:
                    pid, _status = os.waitpid(target_pid, os.WNOHANG)
                except ChildProcessError:
                    break
                except OSError as e:
                    _write_response(write_path, {
                        "ok": False,
                        "mode": "reap_pid",
                        "pid": os.getpid(),
                        "target": target_pid,
                        "error": str(e),
                        "reaped": reaped,
                    })
                    os.kill(os.getpid(), signal.SIGSTOP)
                    return "parent_resumed"
                if pid > 0:
                    reaped.append(pid)
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.001)
            _write_response(write_path, {
                "ok": True,
                "mode": "reap_pid",
                "pid": os.getpid(),
                "target": target_pid,
                "reaped": reaped,
            })
            _log(f"reaped pid: pid={os.getpid()} target={target_pid} "
                 f"reaped={reaped}")
        except (TypeError, ValueError):
            _write_response(write_path, {
                "ok": False,
                "mode": "reap_pid",
                "pid": os.getpid(),
                "target": target,
                "error": "bad pid",
                "reaped": reaped,
            })
        os.kill(os.getpid(), signal.SIGSTOP)
        return "parent_resumed"

    if op == "probe_digest":
        import hashlib

        digest = hashlib.sha256()
        heap = probe_payload or b""
        if isinstance(heap, (bytearray, bytes)):
            digest.update(heap)
        _write_response(write_path, {
            "ok": True,
            "mode": "probe_digest",
            "pid": os.getpid(),
            "heap_sha256": digest.hexdigest(),
            "heap_len": len(heap) if isinstance(heap, (bytearray, bytes)) else 0,
        })
        return "parent_active"

    if op == "stash_template":
        fixed_template_pid = _fixed_active_pid(msg)
        if fixed_template_pid is not None:
            t0_ns = time.perf_counter_ns()
            t0 = t0_ns / 1e9
            try:
                pid = _clone3_same_pidns_settid(fixed_template_pid)
            except OSError as e:
                _write_response(write_path, {
                    "ok": False,
                    "mode": "stash_settid_template",
                    "fixed_template_pid": fixed_template_pid,
                    "error": f"clone3(CLONE_PARENT,set_tid={fixed_template_pid}): {e}",
                })
                _log(f"stash settid template failed fixed_pid={fixed_template_pid}: {e}")
                return None
            if pid == 0:
                t_child = time.perf_counter()
                try:
                    os.setsid()
                except OSError as e:
                    _log(f"stash settid child setsid failed fixed_pid={fixed_template_pid}: {e}")
                _clear_ok = None
                if msg.get("clear_soft_dirty_template") is True:
                    _clear_ok = _clear_self_soft_dirty()
                _log("stash settid child timing: "
                     f"child_entry={(t_child - t0) * 1000:.3f}ms "
                     f"fixed_pid={fixed_template_pid} "
                     f"clear_refs_ok={_clear_ok}")
                os.kill(os.getpid(), signal.SIGSTOP)
                return "parent_resumed"
            t_parent = time.perf_counter()
            child_nspids = _nspids_for_pid(pid)
            t_nspids = time.perf_counter()
            child_host_pid = child_nspids[0] if len(child_nspids) >= 2 else None
            timing = {
                "clone3_settid_parent": (t_parent - t0) * 1000,
                "settid_parent_nspids": (t_nspids - t_parent) * 1000,
            }
            _write_response(write_path, {
                "ok": True,
                "mode": "stash_settid_template",
                "parent": os.getpid(),
                "child": fixed_template_pid,
                "child_host_pid": child_host_pid,
                "child_nspids": child_nspids,
                "fixed_template_pid": fixed_template_pid,
                "same_pidns_set_tid": True,
                "template_self_clear_refs": (
                    msg.get("clear_soft_dirty_template") is True),
                "timing_ms": timing,
            })
            t_resp = time.perf_counter()
            _log("stash settid parent timing: "
                 f"clone3_settid_parent={(t_parent - t0) * 1000:.3f}ms "
                 f"nspids={(t_nspids - t_parent) * 1000:.3f}ms "
                 f"response={(t_resp - t_nspids) * 1000:.3f}ms "
                 f"fixed_pid={fixed_template_pid} child_ns_return={pid} "
                 f"child_nspids={child_nspids}")
            return "parent_active"

        if _fresh_pidns_enabled(msg):
            t0 = time.perf_counter()
            rfd, wfd = os.pipe()
            try:
                pid, pidfd = _clone3_newpid_pidfd()
            except OSError as e:
                os.close(rfd)
                os.close(wfd)
                _write_response(write_path, {
                    "ok": False,
                    "mode": "stash_fresh_pidns",
                    "error": f"clone3(CLONE_NEWPID|CLONE_PIDFD): {e}",
                })
                return None
            if pid == 0:
                t_child = time.perf_counter()
                os.close(rfd)
                nspids = _self_nspids()
                payload = {
                    "ok": True,
                    "mode": "stash_fresh_pidns",
                    "child": nspids[-1] if nspids else os.getpid(),
                    "child_host_pid": nspids[0] if nspids else None,
                    "child_nspids": nspids,
                    "timing_ms": {
                        "clone3_child_entry": (t_child - t0) * 1000,
                    },
                }
                try:
                    if msg.get("async_resources") is not None:
                        if _freeze_async_resources is None:
                            raise RuntimeError("async resources were not initialized before the agent loop")
                        resource_view = msg["async_resources"]
                        payload["frozen_resources"] = _freeze_async_resources(
                            resource_view["contract"], lower_layers=resource_view["lower_layers"],
                            workspace=resource_view["workspace"])
                except BaseException as error:
                    payload.update(ok=False, error=f"frozen resources: {type(error).__name__}: {error}")
                    os.write(wfd, (json.dumps(payload) + "\n").encode())
                    os.close(wfd)
                    os._exit(1)
                try:
                    # PID 1 ignores self-directed stop signals on some kernel
                    # paths. Its ancestor stops it after this ready packet.
                    # Until SIGCONT on cold restore it cannot reach the shared
                    # control FIFO and compete with the original active.
                    os.setsid()
                    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCONT})
                    os.write(wfd, (json.dumps(payload) + "\n").encode())
                except OSError as e:
                    _log(f"stash fresh pidns child report failed: {e}")
                    os._exit(1)
                os.close(wfd)
                signal.sigwait({signal.SIGCONT})
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                return "parent_resumed"

            t_parent = time.perf_counter()
            os.close(wfd)
            chunks: list[bytes] = []
            chunk = None
            report_deadline = time.monotonic() + 5.0
            while True:
                try:
                    remaining = report_deadline - time.monotonic()
                    if remaining <= 0 or not select.select([rfd], [], [], remaining)[0]:
                        break
                    chunk = os.read(rfd, 4096)
                except InterruptedError:
                    continue
                if not chunk:
                    break
                chunks.append(chunk)
                # EOF certifies that the child closed its anonymous handshake
                # pipe before its ancestor freezes the CRIU snapshot.
            t_read = time.perf_counter()
            os.close(rfd)
            try:
                if chunk is None or chunk:
                    raise ValueError("dump child readiness pipe timed out before EOF")
                child_resp = json.loads(
                    b"".join(chunks).split(b"\n", 1)[0].decode())
            except (IndexError, ValueError) as e:
                child_resp = {
                    "ok": False,
                    "error": f"stash fresh child response: {e}",
                }
            if child_resp.get("ok"):
                try:
                    os.kill(pid, signal.SIGSTOP)
                    deadline = time.monotonic() + 2.0
                    while True:
                        stopped_pid, status = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
                        if stopped_pid:
                            if not os.WIFSTOPPED(status):
                                raise RuntimeError("dump child exited before stop")
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError("dump child did not stop")
                        time.sleep(0.0001)
                except BaseException as error:
                    child_resp.update(ok=False, error=f"stop dump child: {error}")
            if not child_resp.get("ok"):
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
            if pidfd >= 0:
                try:
                    os.close(pidfd)
                except OSError:
                    pass
            if not child_resp.get("ok"):
                _write_response(write_path, {
                    "ok": False,
                    "mode": "stash_fresh_pidns",
                    "error": child_resp.get("error", "child response failed"),
                })
                return None
            timing = child_resp.get("timing_ms", {})
            timing.update({
                "clone3_parent": (t_parent - t0) * 1000,
                "parent_read_child_report": (t_read - t_parent) * 1000,
            })
            _write_response(write_path, {
                "ok": True,
                "mode": "stash_fresh_pidns",
                "parent": os.getpid(),
                "child": int(child_resp["child"]),
                "child_parent_pid": int(pid),
                "child_host_pid": child_resp.get("child_host_pid"),
                "child_nspids": child_resp.get("child_nspids"),
                "fresh_pidns": True,
                "clone3": True,
                "timing_ms": timing,
            })
            t_resp = time.perf_counter()
            _log("stash fresh pidns parent timing: "
                 f"clone3_parent={(t_parent - t0) * 1000:.3f}ms "
                 f"parent_read_child_report={(t_read - t_parent) * 1000:.3f}ms "
                 f"response={(t_resp - t_parent) * 1000:.3f}ms "
                 f"child={child_resp.get('child')} "
                 f"child_host={child_resp.get('child_host_pid')} "
                 f"child_nspids={child_resp.get('child_nspids')}")
            return "parent_active"

        helper = os.fork()
        if helper == 0:
            template_pid = os.fork()
            if template_pid == 0:
                # Detached template: quiescent copy of current active state.
                # It is reparented when the short-lived helper exits, so CRIU
                # dumps of the active process do not include prior templates.
                if (msg.get("setsid_template") is True
                        or _fixed_active_pid(msg) is not None):
                    try:
                        os.setsid()
                    except OSError as e:
                        _log(f"stash template setsid failed: {e}")
                if msg.get("clear_soft_dirty_template") is True:
                    _clear_self_soft_dirty()
                os.kill(os.getpid(), signal.SIGSTOP)
                return "parent_resumed"
            _write_response(write_path, {
                "ok": True,
                "mode": "stash_template",
                "parent": os.getppid(),
                "child": template_pid,
                "helper": os.getpid(),
                "template_self_clear_refs": (
                    msg.get("clear_soft_dirty_template") is True),
            })
            _log(f"stashed template: active={os.getppid()} "
                 f"template={template_pid} clear_refs="
                 f"{msg.get('clear_soft_dirty_template') is True}")
            os._exit(0)
        try:
            os.waitpid(helper, 0)
        except ChildProcessError:
            pass
        return "parent_active"

    fixed_pid = _fixed_active_pid(msg)
    if fixed_pid is not None:
        t0_ns = time.perf_counter_ns()
        t0 = t0_ns / 1e9
        try:
            pid = _clone3_same_pidns_settid(fixed_pid)
        except OSError as e:
            _write_response(write_path, {
                "ok": False,
                "mode": "settid_fork",
                "fixed_active_pid": fixed_pid,
                "error": f"clone3(CLONE_PARENT,set_tid={fixed_pid}): {e}",
            })
            _log(f"settid fork failed fixed_pid={fixed_pid}: {e}")
            return None
        if pid == 0:
            t_child = time.perf_counter()
            try:
                os.setsid()
            except OSError as e:
                _log(f"settid child setsid failed fixed_pid={fixed_pid}: {e}")
            _log("settid child timing: "
                 f"child_entry={(t_child - t0) * 1000:.3f}ms "
                 f"preserve_soft_dirty=1 fixed_pid={fixed_pid}")
            return "child"
        t_parent = time.perf_counter()
        child_nspids = _nspids_for_pid(pid)
        t_nspids = time.perf_counter()
        # Only expose a host PID when /proc shows a cross-namespace NSpid
        # chain. Some PID-namespace proc mounts expose only the inner PID
        # (e.g. [100]); treating that as a host PID is unsafe.
        child_host_pid = child_nspids[0] if len(child_nspids) >= 2 else None
        timing = {
            "clone3_settid_parent": (t_parent - t0) * 1000,
            "settid_parent_nspids": (t_nspids - t_parent) * 1000,
        }
        dispatch_ns = msg.get("_dispatch_mono_ns")
        if isinstance(dispatch_ns, int):
            timing["pool_dispatch_to_agent_start"] = (
                (t0_ns - dispatch_ns) / 1_000_000)
        _write_response(write_path, {
            "ok": True,
            "mode": "settid_fork",
            "parent": os.getpid(),
            "child": fixed_pid,
            # clone3 returns the child PID as seen from this pidns, not the
            # controller's host namespace. Let TemplatePool translate it via
            # /proc NSpid instead of accidentally treating X as a host PID.
            "child_host_pid": child_host_pid,
            "child_nspids": child_nspids,
            "fixed_active_pid": fixed_pid,
            "same_pidns_set_tid": True,
            "preserve_soft_dirty_after_settid": True,
            "timing_ms": timing,
        })
        t_resp = time.perf_counter()
        _log("settid parent timing: "
             f"clone3_settid_parent={(t_parent - t0) * 1000:.3f}ms "
             f"nspids={(t_nspids - t_parent) * 1000:.3f}ms "
             f"response={(t_resp - t_nspids) * 1000:.3f}ms "
             f"fixed_pid={fixed_pid} child_ns_return={pid} "
             f"child_nspids={child_nspids}")
        os.kill(os.getpid(), signal.SIGSTOP)
        return "parent_resumed"

    if _fresh_pidns_enabled(msg):
        clone_parent_active = msg.get("clone_parent_active") is True
        if clone_parent_active:
            t0 = time.perf_counter()
            try:
                pid = _clone_newpid_parent()
            except OSError as e:
                _write_response(write_path, {
                    "ok": False,
                    "mode": "fresh_pidns_clone_parent",
                    "error": f"clone(CLONE_NEWPID|CLONE_PARENT): {e}",
                })
                _log(f"fresh pidns clone-parent failed: {e}")
                return None
            if pid == 0:
                t_child = time.perf_counter()
                clear_ok = _clear_self_soft_dirty()
                t_clear = time.perf_counter()
                try:
                    os.setsid()
                except OSError as e:
                    _log(f"fresh pidns clone-parent child setsid failed: {e}")
                _log("fresh pidns clone-parent child timing: "
                     f"child_entry={(t_child - t0) * 1000:.3f}ms "
                     f"self_clear={(t_clear - t_child) * 1000:.3f}ms "
                     f"clear_ok={clear_ok}")
                return "child"
            t_parent = time.perf_counter()
            child_nspids = _nspids_for_pid(pid)
            _write_response(write_path, {
                "ok": True,
                "parent": os.getpid(),
                "child": child_nspids[-1] if child_nspids else int(pid),
                "child_host_pid": int(pid),
                "child_nspids": child_nspids,
                "fresh_pidns": True,
                "clone_parent_active": True,
                "clone_parent_child_self_clear_refs": True,
                "timing_ms": {
                    "clone_parent": (t_parent - t0) * 1000,
                },
            })
            t_resp = time.perf_counter()
            _log("fresh pidns clone-parent parent timing: "
                 f"clone_parent={(t_parent - t0) * 1000:.3f}ms "
                 f"response={(t_resp - t_parent) * 1000:.3f}ms "
                 f"child_host={pid} child_nspids={child_nspids}")
            os.kill(os.getpid(), signal.SIGSTOP)
            return "parent_resumed"

        if os.environ.get("DELTABOX_FRESH_PIDNS_CLONE3") == "1":
            t0 = time.perf_counter()
            try:
                pid, pidfd = _clone3_newpid_pidfd()
            except OSError as e:
                _log(f"fresh pidns clone3 failed: {e}; falling back to double fork")
                pid = -1
                pidfd = -1
            if pid == 0:
                t_child = time.perf_counter()
                clear_ok = _clear_self_soft_dirty()
                t_clear = time.perf_counter()
                try:
                    os.setsid()
                except OSError as e:
                    _log(f"fresh pidns clone3 child setsid failed: {e}")
                if os.environ.get("DELTABOX_CLONE3_CHILD_TIMING") == "1":
                    _log("fresh pidns clone3 child timing: "
                         f"child_entry={(t_child - t0) * 1000:.3f}ms "
                         f"self_clear={(t_clear - t_child) * 1000:.3f}ms "
                         f"clear_ok={clear_ok}")
                return "child"
            if pid > 0:
                t_parent = time.perf_counter()
                child_ns_pid = int(pid)
                child_nspids = _pidfd_nspids(pidfd) if pidfd >= 0 else []
                child_host_pid = child_nspids[0] if child_nspids else None
                if child_host_pid is None:
                    child_host_pid = _find_own_child_host_pid(child_ns_pid)
                t_child_lookup = time.perf_counter()
                timing = {
                    "clone3_parent": (t_parent - t0) * 1000,
                    "parent_child_host_lookup": (t_child_lookup - t_parent) * 1000,
                    "pidfd": pidfd,
                }
                _write_response(write_path, {
                    "ok": True,
                    "parent": os.getpid(),
                    "child": child_ns_pid,
                    "child_host_pid": child_host_pid,
                    "child_nspids": child_nspids,
                    "fresh_pidns": True,
                    "clone3": True,
                    "clone3_child_self_clear_refs": True,
                    "timing_ms": timing,
                })
                t_resp = time.perf_counter()
                _log("fresh pidns clone3 parent timing: "
                     f"clone3_parent={(t_parent - t0) * 1000:.3f}ms "
                     f"child_lookup={(t_child_lookup - t_parent) * 1000:.3f}ms "
                     f"response={(t_resp - t_parent) * 1000:.3f}ms "
                     f"child={child_ns_pid} child_host={child_host_pid} "
                     f"child_nspids={child_nspids} "
                     f"pidfd={pidfd}")
                if pidfd >= 0:
                    try:
                        os.close(pidfd)
                    except OSError:
                        pass
                os.kill(os.getpid(), signal.SIGSTOP)
                return "parent_resumed"

        t_fresh0 = time.perf_counter()
        rfd, wfd = os.pipe()
        t_pipe = time.perf_counter()
        helper = os.fork()
        t_helper_fork_parent = time.perf_counter()
        if helper == 0:
            t_child0 = time.perf_counter()
            os.close(rfd)
            try:
                t_unshare0 = time.perf_counter()
                _unshare_newpid()
                t_unshare1 = time.perf_counter()
                pid = os.fork()
                t_pid1_fork_parent = time.perf_counter()
            except OSError as e:
                os.write(wfd, (json.dumps({
                    "ok": False,
                    "mode": "fresh_pidns_fork",
                    "error": f"unshare/fork(CLONE_NEWPID): {e}",
                }) + "\n").encode())
                os.close(wfd)
                os._exit(1)
            if pid == 0:
                t_pid1_child0 = time.perf_counter()
                nspids = _self_nspids()
                parent_ns_pid = nspids[-2] if len(nspids) >= 2 else os.getpid()
                payload = {
                    "ok": True,
                    "mode": "fresh_pidns_fork",
                    "child": parent_ns_pid,
                    "child_host_pid": nspids[0] if nspids else None,
                    "child_nspids": nspids,
                    "timing_ms": {
                        "helper_child_to_unshare_done":
                            (t_unshare1 - t_child0) * 1000,
                        "unshare":
                            (t_unshare1 - t_unshare0) * 1000,
                        "pid1_fork_child_entry":
                            (t_pid1_child0 - t_unshare1) * 1000,
                    },
                }
                try:
                    os.write(wfd, (json.dumps(payload) + "\n").encode())
                    t_report = time.perf_counter()
                    _log("fresh pidns child timing: "
                         f"helper_child_to_unshare_done="
                         f"{payload['timing_ms']['helper_child_to_unshare_done']:.3f}ms "
                         f"unshare={payload['timing_ms']['unshare']:.3f}ms "
                         f"pid1_fork_child_entry="
                         f"{payload['timing_ms']['pid1_fork_child_entry']:.3f}ms "
                         f"child_report={(t_report - t_pid1_child0) * 1000:.3f}ms")
                except OSError as e:
                    _log(f"fresh pidns child host-pid report failed: {e}")
                os.close(wfd)
                try:
                    os.setsid()
                except OSError as e:
                    _log(f"fresh pidns child setsid failed: {e}")
                return "child"
            _log("fresh pidns helper timing: "
                 f"child_to_unshare_done={(t_unshare1 - t_child0) * 1000:.3f}ms "
                 f"unshare={(t_unshare1 - t_unshare0) * 1000:.3f}ms "
                 f"pid1_fork_parent={(t_pid1_fork_parent - t_unshare1) * 1000:.3f}ms")
            os.close(wfd)
            os._exit(0)

        os.close(wfd)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(rfd, 4096)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        t_read_done = time.perf_counter()
        os.close(rfd)
        try:
            os.waitpid(helper, 0)
        except ChildProcessError:
            pass
        t_wait_done = time.perf_counter()
        try:
            helper_resp = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode())
        except (IndexError, json.JSONDecodeError) as e:
            _write_response(write_path, {
                "ok": False,
                "mode": "fresh_pidns_fork",
                "error": f"helper response: {e}",
            })
            return None
        if not helper_resp.get("ok"):
            _write_response(write_path, helper_resp)
            return None
        timing = helper_resp.get("timing_ms", {})
        timing.update({
            "pipe": (t_pipe - t_fresh0) * 1000,
            "helper_fork_parent": (t_helper_fork_parent - t_pipe) * 1000,
            "parent_read_response": (t_read_done - t_helper_fork_parent) * 1000,
            "helper_wait": (t_wait_done - t_read_done) * 1000,
        })
        _write_response(write_path, {
            "ok": True,
            "parent": os.getpid(),
            "child": int(helper_resp["child"]),
            "child_host_pid": helper_resp.get("child_host_pid"),
            "child_nspids": helper_resp.get("child_nspids"),
            "fresh_pidns": True,
            "timing_ms": timing,
        })
        t_pool_resp = time.perf_counter()
        _log(f"fresh pidns forked: parent={os.getpid()} "
             f"child={helper_resp['child']} "
             f"child_host={helper_resp.get('child_host_pid')}")
        _log("fresh pidns parent timing: "
             f"pipe={timing['pipe']:.3f}ms "
             f"helper_fork_parent={timing['helper_fork_parent']:.3f}ms "
             f"parent_read_response={timing['parent_read_response']:.3f}ms "
             f"helper_wait={timing['helper_wait']:.3f}ms "
             f"pool_response={(t_pool_resp - t_wait_done) * 1000:.3f}ms "
             f"child_unshare={float(timing.get('unshare', 0.0)):.3f}ms "
             f"pid1_fork_child_entry="
             f"{float(timing.get('pid1_fork_child_entry', 0.0)):.3f}ms")
        os.kill(os.getpid(), signal.SIGSTOP)
        return "parent_resumed"

    t_fork0 = time.perf_counter()
    pid = os.fork()
    t_fork1 = time.perf_counter()
    if pid == 0:
        # Child: new active agent. Do not write a response — parent owns it.
        return "child"
    timing = {
        "fork_parent_ms": (t_fork1 - t_fork0) * 1000,
    }
    # Parent: write response (we know both PIDs), then SIGSTOP.
    _write_response(write_path, {
        "ok": True,
        "mode": "fork",
        "parent": os.getpid(),
        "child": pid,
        "fresh_pidns": _fresh_pidns_enabled(msg),
        "timing_ms": timing,
    })
    _log(f"forked: parent={os.getpid()} child={pid} "
         f"fresh_pidns={_fresh_pidns_enabled(msg)} "
         f"fork_parent={timing['fork_parent_ms']:.3f}ms")
    os.kill(os.getpid(), signal.SIGSTOP)
    # When SIGCONT'd later (re-fork from this template), execution resumes
    # here and we return; caller's main loop iterates and waits for the
    # next "fork" command, which will hit the same fork() path above.
    return "parent_resumed"


def _write_response(path: str, payload: dict) -> None:
    line = (json.dumps(payload) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, line)
    except (BlockingIOError, BrokenPipeError):
        pass
    finally:
        os.close(fd)


# ─────────────────────── GSD-side ───────────────────────

def _write_all(fd: int, data: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        except BlockingIOError:
            delay = deadline - time.monotonic()
            if delay <= 0 or not select.select([], [fd], [], delay)[1]:
                raise TimeoutError("template command FIFO write timed out")
            continue
        if written <= 0:
            raise BrokenPipeError("template command FIFO made no progress")
        remaining = remaining[written:]


def _find_fresh_stash_pid(source_pid: int, parent_visible_pid: int) -> int | None:
    """Resolve a direct child using the source's namespace depth, not a hint.

    A child procfs mount can hide outer PIDs. Its NSpid[0] is therefore never
    accepted as the GSD PID without proving ancestry and the extra namespace.
    """
    source_ids = _nspids_for_pid(source_pid)
    if not source_ids or parent_visible_pid <= 1:
        return None
    try:
        with open(f"/proc/{source_pid}/task/{source_pid}/children") as stream:
            children = [int(value) for value in stream.read().split()]
        source_ns = os.readlink(f"/proc/{source_pid}/ns/pid")
        for candidate in children:
            ids = _nspids_for_pid(candidate)
            if (len(ids) == len(source_ids) + 1 and ids[-1] == 1
                    and ids[len(source_ids) - 1] == parent_visible_pid
                    and _ppid_for_pid(candidate) == source_pid
                    and os.readlink(f"/proc/{candidate}/ns/pid") != source_ns):
                return candidate
    except (OSError, ValueError):
        pass
    return None

def _translate_ns_to_host(target_ns_pid: int, source_host_pid: int) -> int | None:
    """Find the host PID of a process whose inner-ns PID is `target_ns_pid`
    and which lives in the same PID namespace as `source_host_pid`.

    Agent runs as PID 1 in its own PID ns (via namespace_launcher). fork()
    inside the agent returns ns-local PIDs, but GSD (the caller) must act on
    host PIDs to SIGCONT/SIGKILL across the ns boundary.

    Fast path: if source is in the SAME PID ns as the caller (no
    namespace_launcher in the deployment, or unit-test/bench scenario), the
    ns-local PID IS the host PID — no translation needed.

    Slow path: source is in a different ns. Walk /proc looking for an entry
    whose ns/pid readlink matches source's, then check NSpid for the inner-
    PID match. ~40 ms inside a VM with ~200 /proc entries — should NOT be
    on the critical path. Prefer `_find_child_host_pid` for the fork-reply
    case where the target is a child of source.
    """
    try:
        src_ns = os.readlink(f"/proc/{source_host_pid}/ns/pid")
        self_ns = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    if src_ns == self_ns:
        # Same PID ns as caller — ns-local PID == host PID.
        return target_ns_pid
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{entry}/ns/pid") != src_ns:
                continue
            with open(f"/proc/{entry}/status") as f:
                for line in f:
                    if line.startswith("NSpid:"):
                        fields = line.split()
                        nspids = [int(x) for x in fields[1:]]
                        if nspids and nspids[-1] == target_ns_pid:
                            return int(entry)
                        break
        except (OSError, ValueError):
            continue
    return None


def _valid_stash_host_pid(candidate: int, child_ns_pid: int,
                         source_pid: int) -> bool:
    """Validate an ordinary stash hint in GSD's procfs view.

    An inner PID must never be mistaken for an unrelated outer PID. The
    detached child stays in the source's PID namespace even after its helper
    exits; checking the parent relationship here would race with reparenting.
    """
    if candidate == source_pid:
        return False
    try:
        nspids = _nspids_for_pid(candidate)
        return (bool(nspids) and nspids[-1] == child_ns_pid
                and os.readlink(f"/proc/{candidate}/ns/pid")
                == os.readlink(f"/proc/{source_pid}/ns/pid"))
    except OSError:
        return False


def _find_child_host_pid(source_host_pid: int, target_ns_pid: int,
                         exclude_pids: set[int] | None = None) -> int | None:
    """Find the host PID of a child of `source_host_pid` whose ns-local PID
    is `target_ns_pid`.

    Use this on the warm-template fork reply path: the just-forked process
    is by definition a child of the template, so we can read
    `/proc/<source>/task/<tid>/children` (CONFIG_PROC_CHILDREN=y) — a tiny
    list — instead of walking all of /proc. Typical cost ~200 µs vs ~40 ms
    for the full /proc walk. Falls back to None on missing files; caller
    can retry via `_translate_ns_to_host`. Fast path covers 80-96% of
    events; remaining miss rate is the re-parented-to-ns_init case that
    falls back to the legacy slow walk.
    """
    task_dir = f"/proc/{source_host_pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return None
    candidates: list[str] = []
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children") as f:
                candidates.extend(f.read().split())
        except OSError:
            continue
    exclude_pids = exclude_pids or set()
    for child in candidates:
        try:
            child_int = int(child)
            if child_int in exclude_pids:
                continue
            if _pid_state(child_int) == "Z":
                continue
            with open(f"/proc/{child}/status") as f:
                for line in f:
                    if line.startswith("NSpid:"):
                        fields = line.split()
                        nspids = [int(x) for x in fields[1:]]
                        if nspids and nspids[-1] == target_ns_pid:
                            return child_int
                        break
        except (OSError, ValueError):
            continue
    return None


class TemplatePool:
    """GSD-side registry: snapshot_id → frozen template PID.

    Talks to the agent via the control FIFO pair. request_fork() blocks
    until the agent (or template) replies with both PIDs or the timeout
    expires.
    """

    def __init__(self,
                 ctrl_in: str = CTRL_IN_FIFO,
                 ctrl_out: str = CTRL_OUT_FIFO,
                 reaper_pid: int | None = None):
        self.templates: dict[str, int] = {}
        self.reaper_pid = reaper_pid
        self.ctrl_in_path = ctrl_in     # we write here
        self.ctrl_out_path = ctrl_out   # we read here
        self._out_fd: int | None = None  # opened lazily
        self._out_buf = b""
        self.last_fork_meta: dict = {}

    def _ensure_out_fd(self) -> int:
        if self._out_fd is None:
            self._out_fd = os.open(self.ctrl_out_path,
                                   os.O_RDWR | os.O_NONBLOCK)
        return self._out_fd

    def reset_channels(self) -> None:
        """Drop cached FIFO fds after a full CRIU restore recreates the agent."""
        if self._out_fd is not None:
            try:
                os.close(self._out_fd)
            except OSError:
                pass
            self._out_fd = None
        self._out_buf = b""

    def _drain_pending(self) -> None:
        """Discard any stale response left by a previous timed-out request."""
        self._out_buf = b""
        fd = self._ensure_out_fd()
        try:
            while True:
                buf = os.read(fd, 4096)
                if not buf:
                    return
        except BlockingIOError:
            return

    def _drain_pending_commands(self) -> None:
        """Discard stale control commands left after a timed-out request.

        The control FIFO has a single shared reader set (active + stopped
        templates).  If a request times out, its command may still be buffered
        in CTRL_IN_FIFO; sending the next command can make an agent read two
        JSON lines in one wakeup.  That poisoned the async-full-dump smoke
        with '{"op":"stash_template"}\\n{"op":"fork"}'.  Drain before each
        host-side request so old commands cannot attach to the next one.
        """
        try:
            rfd = os.open(self.ctrl_in_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            while True:
                try:
                    chunk = os.read(rfd, 4096)
                except BlockingIOError:
                    break
                if not chunk:
                    break
        finally:
            os.close(rfd)

    def _read_response_line(self, timeout: float,
                            expected_modes: set[str] | None = None) -> bytes | None:
        import select as _select

        ofd = self._ensure_out_fd()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if b"\n" in self._out_buf:
                line, self._out_buf = self._out_buf.split(b"\n", 1)
                if expected_modes:
                    try:
                        resp = json.loads(line.decode())
                        mode = resp.get("mode")
                    except Exception:
                        resp = None
                        mode = None
                    if mode not in expected_modes:
                        if isinstance(resp, dict) and resp.get("ok") is False:
                            _log(f"pool response passthrough error: "
                                 f"expected={sorted(expected_modes)} "
                                 f"mode={mode!r} raw={line[:200]!r}")
                            return line
                        _log(f"pool response skip: expected={sorted(expected_modes)} "
                             f"mode={mode!r} raw={line[:200]!r}")
                        continue
                return line
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            r, _, _ = _select.select([ofd], [], [], remaining)
            if not r:
                continue
            try:
                chunk = os.read(ofd, 4096)
            except BlockingIOError:
                continue
            if chunk:
                self._out_buf += chunk
        return None

    def dispatch_fork(self, source_pid: int,
                      clone_parent_active: bool = False,
                      fixed_active_pid: int | None = None) -> bool:
        """Send the fork command without waiting for the response.

        Used by restore_action to overlap fork (in template's address space)
        with the overlayfs layer-switch ioctl (in GSD's address space): we
        kick off the fork here, run ioctl in the main thread, then call
        await_fork to reap. Cost is dominated by the FIFO write (~µs), the
        actual fork() runs in the SIGCONT'd template concurrently.

        ORDERING NOTE (2026-05-11 fix): we write the fork command BEFORE
        sending SIGCONT. If we SIGCONT'd first, there is a race where the
        template wakes faster than this thread can open+write the FIFO,
        enters select() on the still-empty FIFO, and blocks for ~60 ms
        until the kernel re-wakes it on the write. By queueing the
        command first (the template's read_fd is still open while
        STOPPED, so the FIFO buffer accepts the write), the template's
        first post-SIGCONT select() returns immediately with data.

        Returns True if the command was successfully dispatched. False
        means the source is gone or the FIFO write failed; caller should
        skip await_fork and fall back to the slow path.
        """
        # 0. Drain any stale response from a previous timed-out request.
        self._drain_pending()
        self._drain_pending_commands()

        # 1. Write the fork command FIRST (template's read_fd is still
        #    open while it's SIGSTOPped, so the FIFO buffer accepts).
        payload = {"op": "fork"}
        if os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1":
            payload["fresh_pidns"] = True
        if clone_parent_active:
            payload["clone_parent_active"] = True
        if fixed_active_pid and fixed_active_pid > 1:
            payload["fixed_active_pid"] = int(fixed_active_pid)
        payload["_dispatch_mono_ns"] = time.perf_counter_ns()
        cmd = (json.dumps(payload) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"pool dispatch fail: open ctrl_in for write failed: {e}")
            return False
        try:
            os.write(wfd, cmd)
        finally:
            os.close(wfd)

        # 2. Now wake the template — its first select() iteration after
        #    SIGCONT sees the FIFO already readable.
        try:
            os.kill(source_pid, signal.SIGCONT)
        except ProcessLookupError:
            _log(f"pool dispatch fail: source_pid {source_pid} gone")
            return False
        return True

    def await_fork(self, source_pid: int, snapshot_id: str | None,
                   timeout: float = 2.0) -> tuple[int | None, int | None]:
        """Reap the response from a previously dispatched fork command.

        Pairs with dispatch_fork. Blocks via select() until the response
        arrives on ctrl_out or `timeout` expires. By the time GSD calls
        this, the template has typically already finished the fork() and
        written its response, so the first read drains it. select() is
        used (not 1ms busy-poll) so the wake-up is kernel-level event-
        driven — sub-µs latency once the FIFO has data.
        Returns (parent_host_pid, child_host_pid) on success, (None, None)
        on any failure path.
        """
        t_await0 = time.perf_counter()
        line_b = self._read_response_line(
            timeout, expected_modes={"fork", "fresh_pidns_fork",
                                     "fresh_pidns_clone_parent",
                                     "settid_fork"})
        t_after_read = time.perf_counter()
        if line_b is None:
            self.last_fork_meta = {}
            alive = os.path.isdir(f"/proc/{source_pid}")
            _log(f"pool await fail: timeout after {timeout}s waiting for "
                 f"response (source_pid={source_pid}, alive={alive}, "
                 f"buf={self._out_buf[:200]!r})")
            return None, None
        line = line_b.decode()
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            self.last_fork_meta = {}
            _log(f"pool await fail: bad json from agent: {e} raw={line!r}")
            return None, None
        if not resp.get("ok"):
            self.last_fork_meta = resp
            _log(f"pool await fail: agent refused: {resp!r}")
            return None, None
        self.last_fork_meta = resp
        timing = resp.get("timing_ms")
        if not isinstance(timing, dict):
            timing = {}
            resp["timing_ms"] = timing
        timing["pool_await_read"] = (t_after_read - t_await0) * 1000
        parent_ns_pid = int(resp["parent"])
        child_ns_pid  = int(resp["child"])
        # parent_ns_pid is the responder's own ns-PID; its host PID is exactly
        # source_pid (the process we SIGCONT'd). No /proc lookup needed.
        parent_pid = source_pid
        # Belt-and-suspenders freeze for the PID namespace init template only.
        #
        # The agent-side parent normally calls SIGSTOP on itself immediately
        # after writing the fork response. That works for ordinary templates.
        # The first template, however, is ns-local PID 1; namespace init has
        # special signal semantics and a self-SIGSTOP can fail to quiesce it.
        # If it keeps running, it steals subsequent command FIFO reads and
        # pollutes the checkpoint it is supposed to represent.
        #
        # Do NOT do this for non-init templates: the host-side SIGSTOP can
        # arrive before the parent reaches its own self-SIGSTOP. On the next
        # SIGCONT, it resumes into that old self-SIGSTOP and times out instead
        # of reading the new fork command.
        if parent_ns_pid == 1:
            try:
                os.kill(parent_pid, signal.SIGSTOP)
            except ProcessLookupError:
                _log(f"pool await warn: parent_pid {parent_pid} gone before SIGSTOP")
            except OSError as e:
                _log(f"pool await warn: parent_pid {parent_pid} SIGSTOP failed: {e}")
        template_pids = set(self.templates.values())
        child_pid = None
        child_host_pid = resp.get("child_host_pid")
        t_translate0 = time.perf_counter()
        translate_path = None
        if child_host_pid is not None:
            t0 = time.perf_counter()
            try:
                candidate = int(child_host_pid)
                if (candidate not in template_pids
                        and os.path.isdir(f"/proc/{candidate}")):
                    child_pid = candidate
                    translate_path = "child_host_pid"
                else:
                    _log(f"pool await warn: child_host_pid {candidate} "
                         f"invalid/template for source_pid={source_pid}")
            except (TypeError, ValueError):
                _log(f"pool await warn: bad child_host_pid={child_host_pid!r}")
            timing["pool_child_host_pid_check"] = (time.perf_counter() - t0) * 1000
        # child_ns_pid is a fresh child of source_pid; walk only that
        # subtree (typically 1-3 entries) instead of all of /proc.
        if child_pid is None:
            t0 = time.perf_counter()
            child_pid = _find_child_host_pid(
                source_pid, child_ns_pid, exclude_pids=template_pids)
            timing["pool_find_source_child"] = (time.perf_counter() - t0) * 1000
            if child_pid in template_pids:
                _log(f"pool await warn: translated child {child_pid} is an "
                     f"existing template; ignoring")
                child_pid = None
            elif child_pid is not None:
                translate_path = "source_child"
        if child_pid is None and resp.get("same_pidns_set_tid") is True:
            t0 = time.perf_counter()
            reaper_pid = _ppid_for_pid(source_pid)
            timing["pool_reaper_ppid_lookup"] = (time.perf_counter() - t0) * 1000
            if reaper_pid:
                t0 = time.perf_counter()
                child_pid = _find_child_host_pid(
                    reaper_pid, child_ns_pid, exclude_pids=template_pids)
                timing["pool_find_reaper_child"] = (time.perf_counter() - t0) * 1000
                if child_pid in template_pids:
                    _log(f"pool await warn: reaper child {child_pid} is an "
                         f"existing template; ignoring")
                    child_pid = None
                elif child_pid is not None:
                    translate_path = "reaper_child"
        if child_pid is None:
            # Fall back to the slow /proc walk in case child has a thread-
            # group leader we didn't see in the immediate children list.
            t0 = time.perf_counter()
            child_pid = _translate_ns_to_host(child_ns_pid, source_pid)
            timing["pool_translate_fallback"] = (time.perf_counter() - t0) * 1000
            if child_pid in template_pids:
                _log(f"pool await warn: fallback child {child_pid} is an "
                     f"existing template; ignoring")
                child_pid = None
            elif child_pid is not None:
                translate_path = "fallback"
        timing["pool_translate_total"] = (time.perf_counter() - t_translate0) * 1000
        resp["translate_path"] = translate_path or "failed"
        if parent_pid is None or child_pid is None:
            _log(f"pool await fail: ns->host translate failed "
                 f"parent_ns={parent_ns_pid}->{parent_pid} "
                 f"child_ns={child_ns_pid}->{child_pid} "
                 f"(source_pid={source_pid})")
            return None, None
        if snapshot_id is not None:
            self.templates[snapshot_id] = parent_pid
        return parent_pid, child_pid

    def request_fork(self, source_pid: int, snapshot_id: str | None,
                     timeout: float = 2.0,
                     clone_parent_active: bool = False,
                     fixed_active_pid: int | None = None
                     ) -> tuple[int | None, int | None]:
        """Synchronous dispatch+await wrapper. Used by checkpoint path
        (no concurrent ioctl to overlap with). Restore path uses the split
        dispatch_fork / await_fork pair so ioctl can run in parallel.
        """
        if not self.dispatch_fork(
                source_pid, clone_parent_active=clone_parent_active,
                fixed_active_pid=fixed_active_pid):
            return None, None
        return self.await_fork(source_pid, snapshot_id, timeout)

    def request_stash_template(self, source_pid: int, snapshot_id: str | None,
                               timeout: float = 2.0,
                               fresh_pidns: bool | None = None,
                               setsid_template: bool = False,
                               clear_soft_dirty_template: bool = False,
                               fixed_template_pid: int | None = None,
                               async_resources: dict | None = None
                               ) -> int | None:
        """Ask the active process to create a detached SIGSTOP'd child
        template while the active process keeps running with the same PID.

        This is the checkpoint-side companion to fork-based restore. It keeps
        the CRIU incremental dump chain valid during linear execution because
        the active tree PID does not change after each checkpoint.
        """
        self._drain_pending()
        self._drain_pending_commands()
        payload = {"op": "stash_template"}
        if async_resources is not None:
            if fresh_pidns is not True:
                raise ValueError("frozen dump resources require a fresh PID namespace")
            payload["async_resources"] = async_resources
        if fresh_pidns is None:
            fresh_pidns = os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1"
        if fresh_pidns:
            payload["fresh_pidns"] = True
        if setsid_template:
            payload["setsid_template"] = True
        if clear_soft_dirty_template:
            payload["clear_soft_dirty_template"] = True
        if fixed_template_pid and fixed_template_pid > 1:
            payload["fixed_active_pid"] = int(fixed_template_pid)
        cmd = (json.dumps(payload) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"pool stash fail: open ctrl_in for write failed: {e}")
            return None
        try:
            _write_all(wfd, cmd, timeout)
        finally:
            os.close(wfd)

        line_b = self._read_response_line(
            timeout, expected_modes={"stash_template", "stash_fresh_pidns",
                                     "stash_settid_template"})
        if line_b is None:
            alive = os.path.isdir(f"/proc/{source_pid}")
            _log(f"pool stash fail: timeout after {timeout}s waiting for "
                 f"response (source_pid={source_pid}, alive={alive}, "
                 f"buf={self._out_buf[:200]!r})")
            return None
        line = line_b.decode()
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"pool stash fail: bad json from agent: {e} raw={line!r}")
            return None
        if not resp.get("ok"):
            _log(f"pool stash fail: agent refused: {resp!r}")
            return None
        template_ns_pid = int(resp["child"])
        template_pid = None
        if resp.get("mode") == "stash_fresh_pidns":
            parent_visible_pid = resp.get("child_parent_pid")
            if isinstance(parent_visible_pid, int) and template_ns_pid == 1:
                template_pid = _find_fresh_stash_pid(source_pid, parent_visible_pid)
            if template_pid is None:
                raise RuntimeError("cannot establish fresh dump child's namespace identity")
            if snapshot_id is not None:
                self.templates[snapshot_id] = template_pid
            return template_pid
        child_host_pid = resp.get("child_host_pid")
        if child_host_pid is not None:
            try:
                candidate = int(child_host_pid)
                if candidate == source_pid:
                    _log(f"pool stash warn: child_host_pid {candidate} "
                         f"is source_pid; ignoring")
                elif (resp.get("mode") == "stash_template"
                      and not _valid_stash_host_pid(
                          candidate, template_ns_pid, source_pid)):
                    _log(f"pool stash warn: child_host_pid {candidate} "
                         "namespace identity mismatch; using translation")
                elif os.path.isdir(f"/proc/{candidate}"):
                    template_pid = candidate
                else:
                    _log(f"pool stash warn: child_host_pid {candidate} gone")
            except (TypeError, ValueError):
                _log(f"pool stash warn: bad child_host_pid={child_host_pid!r}")
        if template_pid is None and resp.get("mode") == "stash_template":
            # The reply precedes helper exit. Locate the child under that
            # helper first; if it exits during lookup, the reaper path below
            # covers the one-way reparenting transition without waiting.
            helper_ns_pid = resp.get("helper")
            if isinstance(helper_ns_pid, int) and helper_ns_pid > 1:
                helper_pid = _find_child_host_pid(source_pid, helper_ns_pid)
                if helper_pid is not None and _valid_stash_host_pid(
                        helper_pid, helper_ns_pid, source_pid):
                    candidate = _find_child_host_pid(helper_pid, template_ns_pid)
                    if candidate is not None and _valid_stash_host_pid(
                            candidate, template_ns_pid, source_pid):
                        template_pid = candidate
        if (template_pid is None and resp.get("mode") == "stash_template"
                and getattr(self, "reaper_pid", None) is not None):
            # Agent procfs can be mounted inside its PID namespace, so it
            # cannot report an outer PID. Detached templates normally become
            # children of the namespace init already known to GSD. Search
            # that children list, skipping registered templates, then validate
            # namespace identity. A still-live helper or unavailable procfs
            # simply falls through to the original global translation.
            candidate = _find_child_host_pid(
                self.reaper_pid, template_ns_pid,
                exclude_pids=set(self.templates.values()) | {source_pid})
            if candidate is not None and _valid_stash_host_pid(
                    candidate, template_ns_pid, source_pid):
                template_pid = candidate
        if template_pid is None:
            template_pid = _translate_ns_to_host(template_ns_pid, source_pid)
        if template_pid == source_pid:
            _log(f"pool stash fail: translated child_ns={template_ns_pid} "
                 f"to source_pid={source_pid}; refusing to stash pidns init")
            return None
        if template_pid is None:
            _log(f"pool stash fail: ns->host translate failed "
                 f"child_ns={template_ns_pid} (source_pid={source_pid})")
            return None
        if snapshot_id is not None:
            self.templates[snapshot_id] = template_pid
        return template_pid

    def request_reap_children(self, source_pid: int,
                              timeout: float = 1.0) -> list[int] | None:
        """Ask a stopped template/ns-init to reap dead children, then stop again."""
        self._drain_pending()
        self._drain_pending_commands()
        cmd = (json.dumps({"op": "reap_children"}) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"pool reap fail: open ctrl_in for write failed: {e}")
            return None
        try:
            os.write(wfd, cmd)
        finally:
            os.close(wfd)

        try:
            os.kill(source_pid, signal.SIGCONT)
        except ProcessLookupError:
            _log(f"pool reap fail: source_pid {source_pid} gone")
            return None

        line_b = self._read_response_line(
            timeout, expected_modes={"reap_children"})

        self._host_stop_init_template(source_pid, "reap")

        if line_b is None:
            alive = os.path.isdir(f"/proc/{source_pid}")
            _log(f"pool reap fail: timeout after {timeout}s waiting for "
                 f"response (source_pid={source_pid}, alive={alive}, "
                 f"buf={self._out_buf[:200]!r})")
            return None
        line = line_b.decode()
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"pool reap fail: bad json from agent: {e} raw={line!r}")
            return None
        if not resp.get("ok"):
            _log(f"pool reap fail: agent refused: {resp!r}")
            return None
        return list(resp.get("reaped", []))

    def request_reap_pid(self, source_pid: int, target_pid: int,
                         timeout: float = 1.0) -> list[int] | None:
        """Ask a stopped parent template to reap one known dead child."""
        self._drain_pending()
        self._drain_pending_commands()
        cmd = (json.dumps({"op": "reap_pid", "pid": target_pid}) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"pool reap-pid fail: open ctrl_in for write failed: {e}")
            return None
        try:
            os.write(wfd, cmd)
        finally:
            os.close(wfd)

        try:
            os.kill(source_pid, signal.SIGCONT)
        except ProcessLookupError:
            _log(f"pool reap-pid fail: source_pid {source_pid} gone")
            return None

        line_b = self._read_response_line(
            timeout, expected_modes={"reap_pid"})

        self._host_stop_init_template(source_pid, "reap-pid")

        if line_b is None:
            alive = os.path.isdir(f"/proc/{source_pid}")
            _log(f"pool reap-pid fail: timeout after {timeout}s waiting for "
                 f"response (source_pid={source_pid}, target={target_pid}, "
                 f"alive={alive}, buf={self._out_buf[:200]!r})")
            return None
        line = line_b.decode()
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"pool reap-pid fail: bad json from agent: {e} raw={line!r}")
            return None
        if not resp.get("ok"):
            _log(f"pool reap-pid fail: agent refused: {resp!r}")
            return None
        return list(resp.get("reaped", []))

    def _host_stop_init_template(self, source_pid: int, label: str) -> None:
        """Extra host SIGSTOP only for PID-namespace init templates.

        The agent-side reap handlers self-stop before returning to the template
        loop. Sending a host SIGSTOP to an ordinary template can race with that
        self-stop and poison the next SIGCONT: it resumes into the old SIGSTOP
        instead of reading the next command. PID namespace init is special, so
        keep the same belt-and-suspenders stop used by await_fork for ns PID 1.
        """
        try:
            nspids = _nspids_for_pid(source_pid)
        except Exception:
            nspids = []
        ns_pid = nspids[-1] if nspids else None
        if ns_pid != 1:
            return
        try:
            os.kill(source_pid, signal.SIGSTOP)
        except ProcessLookupError:
            _log(f"pool {label} warn: source_pid {source_pid} gone before SIGSTOP")
        except OSError as e:
            _log(f"pool {label} warn: source_pid {source_pid} SIGSTOP failed: {e}")

    def request_probe_digest(self, timeout: float = 2.0) -> dict | None:
        """Ask the currently runnable active agent for its probe digest."""
        import select as _select

        self._drain_pending()
        cmd = (json.dumps({"op": "probe_digest"}) + "\n").encode()
        try:
            wfd = os.open(self.ctrl_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            _log(f"pool probe-digest fail: open ctrl_in for write failed: {e}")
            return None
        try:
            os.write(wfd, cmd)
        finally:
            os.close(wfd)

        ofd = self._ensure_out_fd()
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            r, _, _ = _select.select([ofd], [], [], remaining)
            if not r:
                continue
            try:
                chunk = os.read(ofd, 4096)
            except BlockingIOError:
                continue
            if chunk:
                buf += chunk
                if b"\n" in buf:
                    break

        if b"\n" not in buf:
            _log(f"pool probe-digest fail: timeout waiting for response "
                 f"buf={buf[:200]!r}")
            return None
        line = buf.split(b"\n", 1)[0].decode()
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"pool probe-digest fail: bad json from agent: {e} raw={line!r}")
            return None
        if not resp.get("ok"):
            _log(f"pool probe-digest fail: agent refused: {resp!r}")
            return None
        return resp

    def get(self, snapshot_id: str) -> int | None:
        pid = self.templates.get(snapshot_id)
        if pid is None:
            return None
        if not os.path.isdir(f"/proc/{pid}"):
            self.templates.pop(snapshot_id, None)
            return None
        return pid

    def discard(self, snapshot_id: str) -> None:
        pid = self.templates.pop(snapshot_id, None)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        if self._out_fd is not None:
            try: os.close(self._out_fd)
            except OSError: pass
            self._out_fd = None
