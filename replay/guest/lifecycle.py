"""Own replay processes and namespaces, including partial startup/restore."""
from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import ExitStack
import ctypes
import errno
from functools import lru_cache
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time


try:
    from process_wait import _pidfd_opener
except ImportError:
    from backends.deltabox.gsd.process_wait import _pidfd_opener


def open_pidfd(pid):
    # Use the core's Linux libc/syscall adapter on older guest Python builds.
    # No numeric-PID kill fallback: inability to pin ownership is fatal.
    opener = _pidfd_opener()
    if opener is None:
        raise OSError(errno.ENOSYS, "pidfd_open is required for replay ownership")
    return opener(pid)


@lru_cache(maxsize=1)
def _libc_pidfd_signaller():
    if sys.platform != "linux":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        wrapper = getattr(libc, 'pidfd_send_signal', None)
        if wrapper is not None:
            wrapper.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
            wrapper.restype = ctypes.c_int
            call = lambda fd, sig: wrapper(fd, sig, None, 0)
        elif os.uname().machine in ('x86_64', 'aarch64') and ctypes.sizeof(ctypes.c_void_p) == 8:
            syscall = libc.syscall
            syscall.restype = ctypes.c_long
            # __NR_pidfd_send_signal is 424 on x86_64 and asm-generic ABIs.
            call = lambda fd, sig: syscall(ctypes.c_long(424), ctypes.c_int(fd),
                                          ctypes.c_int(sig), ctypes.c_void_p(), ctypes.c_uint(0))
        else:
            return None
    except (OSError, AttributeError):
        return None

    def send(fd, sig):
        result = call(fd, sig)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    return send


def send_pidfd_signal(fd, sig):
    native = getattr(signal, 'pidfd_send_signal', None)
    sender = native if callable(native) else _libc_pidfd_signaller()
    if sender is None:
        raise OSError(errno.ENOSYS, "pidfd_send_signal is required for replay ownership")
    return sender(fd, sig)


def _stop_process(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=2.0)


class OwnedDumpPool:
    """Track the core's check_call jobs without changing successful dump order.

    Cancelling a Future does not kill its subprocess. The wrapper instead closes
    admission, aborts owned running commands, and lets queued jobs raise a normal
    terminal exception before Popen. Core completion callbacks still record each
    failed dump (their current API does not handle Future.cancel()).
    """
    def __init__(self, executor):
        self.executor = executor
        self.lock = threading.Lock()
        self.closing = False
        self.processes = set()

    def submit(self, function, *args, **kwargs):
        if function is not subprocess.check_call:
            raise TypeError("replay dump ownership only supports subprocess.check_call")
        return self.executor.submit(self._check_call, *args, **kwargs)

    def submit_owned_task(self, function, *args):
        """A detached pipeline receives the same abortable command runner.

        The task must complete its cleanup even when admission is closed; it
        owns a frozen process and resolves the checkpoint's public Future.
        """
        return self.executor.submit(function, self._check_call, *args)

    def _check_call(self, *args, **kwargs):
        timeout = kwargs.pop('timeout', None)
        with self.lock:
            if self.closing:
                raise CancelledError("replay stopped before dump started")
            proc = subprocess.Popen(*args, **kwargs)
            self.processes.add(proc)
        try:
            returncode = proc.wait(timeout=timeout)
            if returncode:
                raise subprocess.CalledProcessError(returncode, proc.args)
            return 0
        except BaseException:
            if proc.poll() is None:
                _stop_process(proc)
            raise
        finally:
            with self.lock:
                self.processes.discard(proc)

    def shutdown(self, wait=True, **kwargs):
        return self.executor.shutdown(wait=wait, **kwargs)

    def abort(self):
        with self.lock:
            self.closing = True
            processes = tuple(self.processes)
        for proc in processes:
            _stop_process(proc)
        self.executor.shutdown(wait=False)
        # Python's executor atexit joins these workers anyway. Bound our own
        # wait and report survivors; do not claim wait=False alone kills jobs.
        deadline = time.monotonic() + 3.0
        for thread in tuple(getattr(self.executor, '_threads', ())):
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise TimeoutError("dump worker did not stop after its command was reaped")


class ReplayResources(ExitStack):
    def __init__(self):
        super().__init__()
        self._namespaces = {}
        self._controllers = []
        self.cleanup_errors = []

    def _cleanup(self, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
        except Exception as error:
            name = getattr(function, "__name__", type(function).__name__)
            self.cleanup_errors.append(f"{name}: {type(error).__name__}: {error}")
            print(f"[replay] cleanup: {self.cleanup_errors[-1]}", file=sys.stderr, flush=True)

    def callback(self, callback, /, *args, **kwargs):
        super().callback(self._cleanup, callback, *args, **kwargs)
        return callback

    def popen(self, *args, **kwargs):
        proc = subprocess.Popen(*args, **kwargs)
        self.callback(_stop_process, proc)
        return proc

    def popen_namespace(self, *args, **kwargs):
        proc = self.popen(*args, **kwargs)
        # If initialization fails before the pidfile is published/read, the
        # sentinel still owns its direct ns-init child. Recover that ownership
        # before stopping the sentinel; plain terminate() would orphan it.
        self.callback(self._stop_launcher_children, proc)
        return proc

    def _stop_launcher_children(self, proc):
        if proc.poll() is not None:
            return
        try:
            with open(f"/proc/{proc.pid}/task/{proc.pid}/children") as stream:
                children = [int(pid) for pid in stream.read().split()]
        except FileNotFoundError:
            return
        for pid in children:
            try:
                fd = open_pidfd(pid)
            except ProcessLookupError:
                continue
            try:
                # Validate ancestry after pinning the child. Never signal a
                # number which has since become another process's child.
                with open(f"/proc/{pid}/status") as stream:
                    fields = dict(line.split(':', 1) for line in stream if ':' in line)
                if int(fields['PPid']) != proc.pid:
                    continue
                send_pidfd_signal(fd, signal.SIGKILL)
                if not select.select([fd], [], [], 2.0)[0]:
                    raise TimeoutError(f"launcher child {pid} did not exit")
            except (FileNotFoundError, ProcessLookupError):
                pass
            finally:
                os.close(fd)

    def own_namespace(self, pid):
        if pid is None:
            return
        if pid <= 1 or pid == os.getpid():
            raise ValueError("refusing invalid replay namespace PID")
        previous = self._namespaces.get(pid)
        if previous is not None and not select.select([previous], [], [], 0)[0]:
            return
        # An exited pidfd is never reused for a new task with the same number.
        fd = open_pidfd(pid)
        if previous is not None:
            os.close(previous)
        self._namespaces[pid] = fd

    def create_controller(self, controller_class, **kwargs):
        # Register before __init__: root overlays or pools may already exist
        # when a later constructor step raises.
        controller = controller_class.__new__(controller_class)
        self._controllers.append(controller)
        try:
            controller_class.__init__(controller, **kwargs)
        finally:
            pool = getattr(controller, '_dump_pool', None)
            if pool is not None:
                controller._dump_pool = OwnedDumpPool(pool)
        return controller

    def own_controller(self, controller):
        self._controllers.append(controller)
        controller._dump_pool = OwnedDumpPool(controller._dump_pool)

    def _kill_namespace(self, pid, fd):
        try:
            try:
                send_pidfd_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not select.select([fd], [], [], 2.0)[0]:
                raise TimeoutError(f"namespace {pid} did not exit")
        finally:
            os.close(fd)
            self._namespaces.pop(pid, None)

    def _kill_namespaces(self):
        for pid, fd in tuple(self._namespaces.items()):
            self._cleanup(self._kill_namespace, pid, fd)

    def abort_dumps(self, controller):
        """Stop failed replay work before settlement; a caught error is not EOF."""
        self._kill_namespaces()
        controller._dump_pool.abort()

    def _close_controller(self, controller):
        pool = getattr(controller, '_dump_pool', None)
        if pool is not None:
            self._cleanup(pool.abort)
        restamp = getattr(controller, '_restamp_pool', None)
        restamp_stopped = True
        if restamp is not None:
            self._cleanup(restamp.shutdown, wait=False, cancel_futures=True)
            deadline = time.monotonic() + 2.0
            for thread in tuple(getattr(restamp, '_threads', ())):
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    restamp_stopped = False
                    self.cleanup_errors.append("restamp worker did not stop")
        for proc in getattr(controller, '_lazy_page_daemons', ()):
            self._cleanup(_stop_process, proc)
        cache = getattr(controller, '_restamp_cache_root', None)
        if cache and restamp_stopped:
            self._cleanup(shutil.rmtree, cache, ignore_errors=True)
        mount_fd = getattr(controller, 'mount_fd', None)
        if mount_fd is not None:
            # Clear ownership before close, so repeated cleanup cannot close a
            # different descriptor that later reused the same numeric fd.
            controller.mount_fd = None
            self._cleanup(os.close, mount_fd)
        root_overlays = getattr(controller, 'root_overlays', None)
        if root_overlays is not None:
            self._cleanup(root_overlays.teardown)

    def __exit__(self, exc_type, exc, traceback):
        # Kill the owned task trees first, then unblock any CRIU subprocess
        # that was operating on them. Successful runs already settled dumps.
        self._cleanup(self._kill_namespaces)
        for controller in self._controllers:
            self._cleanup(self._close_controller, controller)
        super().__exit__(exc_type, exc, traceback)
        if self.cleanup_errors and exc is None:
            raise RuntimeError("replay cleanup failed: " + "; ".join(self.cleanup_errors))
        return False
