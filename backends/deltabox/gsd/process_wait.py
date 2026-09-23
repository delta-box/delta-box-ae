"""Wait for process exit without adding a polling interval to normal exits."""

import errno
import ctypes
from functools import lru_cache
import math
import os
import select
import sys
import time
from typing import Callable, Iterable


_PROBE_INTERVAL = 0.01


@lru_cache(maxsize=1)
def _libc_pidfd_opener():
    """Support Python builds lacking os.pidfd_open on a capable Linux kernel.

    Prefer libc's wrapper. Older glibc also lacks it, so use the Linux syscall
    on explicitly supported 64-bit ABIs; unknown ABIs retain the probe fallback.
    A native API error is never bypassed (e.g. EPERM or ENOSYS).
    """
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        wrapper = getattr(libc, "pidfd_open", None)
        if wrapper is not None:
            wrapper.argtypes = [ctypes.c_int, ctypes.c_uint]
            wrapper.restype = ctypes.c_int
            call = lambda pid: wrapper(pid, 0)
        elif (os.uname().machine in ("x86_64", "aarch64")
              and ctypes.sizeof(ctypes.c_void_p) == 8):
            syscall = libc.syscall
            syscall.restype = ctypes.c_long
            # __NR_pidfd_open is 434 in Linux x86_64 and asm-generic ABIs.
            call = lambda pid: syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0))
        else:
            return None
    except (OSError, AttributeError):
        return None

    def open_pidfd(pid):
        fd = call(pid)
        if fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return fd
    return open_pidfd


def _pidfd_opener():
    native = getattr(os, "pidfd_open", None)
    return native if callable(native) else _libc_pidfd_opener()


def wait_for_process_exit(pids: Iterable[int], timeout: float,
                          is_done: Callable[[int], bool]) -> set[int]:
    """Return unresolved PIDs, preserving the caller's gone-or-zombie probe.

    pidfds wake immediately on exit. Older kernels, restricted environments,
    and targets without a usable pidfd retain the 10 ms probe cadence. Even
    with pidfds, probe after a poll timeout: a thread-group leader can become
    a zombie before its remaining threads exit and make the pidfd readable.
    """
    deadline = time.monotonic() + timeout
    remaining = {pid for pid in set(pids) if not is_done(pid)}
    if not remaining:
        return remaining
    next_probe = time.monotonic() + _PROBE_INTERVAL
    fallback = set(remaining)
    pidfds: dict[int, int] = {}
    poller = None
    pidfd_open = _pidfd_opener()
    poll_factory = getattr(select, "poll", None)

    def release(fd: int) -> None:
        try:
            poller.unregister(fd)
        except (KeyError, OSError):
            pass
        finally:
            del pidfds[fd]
            try:
                os.close(fd)
            except OSError:
                pass

    try:
        if remaining and callable(pidfd_open) and callable(poll_factory):
            try:
                poller = poll_factory()
            except OSError:
                pass
        if poller is not None:
            for pid in list(remaining):
                if time.monotonic() >= deadline:
                    break
                try:
                    fd = pidfd_open(pid)
                except OSError as error:
                    if error.errno == errno.ESRCH:
                        remaining.remove(pid)
                        fallback.remove(pid)
                    continue
                # Own the descriptor before registration, which can also fail.
                pidfds[fd] = pid
                try:
                    poller.register(fd, select.POLLIN)
                except OSError:
                    release(fd)
                else:
                    fallback.remove(pid)

        while remaining:
            now = time.monotonic()
            time_left = deadline - now
            if time_left <= 0:
                remaining = {pid for pid in remaining if not is_done(pid)}
                break
            interval = min(time_left, max(0.0, next_probe - now))
            events = []
            if pidfds:
                try:
                    # poll uses integral milliseconds; round up to avoid a
                    # busy loop during the last fraction of a millisecond.
                    events = poller.poll(math.ceil(interval * 1000))
                except InterruptedError:
                    # Still probe: repeated interruptions must not defer
                    # zombie or fallback checks until the overall deadline.
                    pass
                except OSError:
                    fallback.update(pidfds.values())
                    for fd in list(pidfds):
                        release(fd)
            else:
                time.sleep(interval)

            for fd, event in events:
                pid = pidfds.get(fd)
                if pid is None:
                    continue
                if event & (select.POLLIN | select.POLLHUP):
                    remaining.remove(pid)
                    release(fd)
                elif event & (select.POLLERR | select.POLLNVAL):
                    fallback.add(pid)
                    release(fd)

            # Readiness itself proves exit. Keep the probe deadline absolute
            # so successive exits cannot postpone checking zombie leaders.
            probe_all = not events or time.monotonic() >= next_probe
            to_probe = remaining if probe_all else fallback
            done = {pid for pid in to_probe if is_done(pid)}
            if probe_all:
                next_probe = time.monotonic() + _PROBE_INTERVAL
            remaining.difference_update(done)
            fallback.difference_update(done)
            for fd, pid in list(pidfds.items()):
                if pid in done:
                    release(fd)
        return remaining
    finally:
        for fd in pidfds:
            try:
                os.close(fd)
            except OSError:
                pass
