"""Process-local descendant ownership for private AE storage wrappers.

Copied from the already validated NVMe wrapper. No global process scanning,
service changes, or measurement configuration occurs in this helper.
"""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time

def direct_children():
    return {int(pid) for path in Path('/proc/self/task').glob('*/children')
            for pid in path.read_text().split()}


def process_identity(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    if int(fields[1]) != os.getpid():
        raise RuntimeError('Process is not an owned direct child: ' + str(pid))
    return dict(pid=pid, start_ticks=int(fields[19]))


def subreaper_flag(value=None):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    flag = ctypes.c_int()
    code = libc.prctl(37 if value is None else 36,
                      ctypes.cast(ctypes.byref(flag), ctypes.c_void_p) if value is None else ctypes.c_void_p(value),
                      0, 0, 0)
    if code != 0:
        raise OSError(ctypes.get_errno(), 'prctl child subreaper failed')
    return flag.value if value is None else value


class OwnedChildren:
    """A process-local subreaper; signal/reap only proven direct descendants."""
    def __init__(self):
        if (not hasattr(os, 'pidfd_open') or not hasattr(os, 'P_PIDFD')
                or not hasattr(signal, 'pidfd_send_signal')):
            raise RuntimeError('Private job lifecycle requires Linux pidfd support')
        if direct_children():
            raise RuntimeError('Private job helper already owns unrelated asynchronous children')
        self.previous = subreaper_flag()
        self.entries = {}
        self.evidence = []
        self.producer_pid = None
        self.live_reap_error = None
        self.reap_busy = False
        self.reap_pending = False
        subreaper_flag(1)

    def register(self, pid):
        identity = process_identity(pid)
        fd = os.pidfd_open(pid, 0)
        try:
            if process_identity(pid) != identity:
                raise RuntimeError('Owned child identity changed during pidfd acquisition')
        except BaseException:
            os.close(fd)
            raise
        proof = dict(identity)
        record = dict(identity, pidfd=fd, term_sent=False, kill_sent=False, proof=proof)
        self.entries[pid] = record
        self.evidence.append(proof)
        return record

    def start_live_reaping(self, producer_pid):
        """Reap terminal adopted children while the sole producer is active.

        CRIU restores an original PID. A restored orphan can become our child;
        leaving its zombie until final cleanup would block the next restore.
        This window contains only producer.wait, never synchronous helper calls.
        """
        if self.producer_pid is not None or producer_pid not in self.entries:
            raise RuntimeError('Live reaping requires one registered producer')
        self.producer_pid = producer_pid
        self.previous_sigchld = signal.signal(signal.SIGCHLD, self.reap_terminated)
        self.reap_terminated(None, None)  # Adopted exit can precede handler installation.

    def reap_terminated(self, signum, frame):
        if self.producer_pid is None:
            return
        if self.reap_busy:
            self.reap_pending = True
            return
        self.reap_busy = True
        try:
            while True:
                self.reap_pending = False
                for pid in direct_children() - {self.producer_pid}:
                    try:
                        record = self.entries.get(pid) or self.register(pid)
                        result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                        if result is not None:
                            record['proof'].update(reaped=True, reaped_during_producer=True,
                                                   exit_status=result.si_status)
                            os.close(self.entries.pop(pid)['pidfd'])
                    except (FileNotFoundError, ProcessLookupError):
                        # Process identity is rechecked before each acquisition;
                        # a disappearing candidate is not signaled or guessed.
                        continue
                if not self.reap_pending:
                    break
        except Exception as error:
            self.live_reap_error = f'{type(error).__name__}: {error}'
        finally:
            self.reap_busy = False
        # SIGCHLD can reenter after the loop decides to break but before busy
        # is cleared. Drain that deferred notification before returning.
        if self.reap_pending and self.producer_pid is not None:
            self.reap_terminated(None, None)

    def stop_live_reaping(self):
        if self.producer_pid is not None:
            self.producer_pid = None
            signal.signal(signal.SIGCHLD, self.previous_sigchld)
        if self.live_reap_error:
            raise RuntimeError('Adopted-child live reaping failed: ' + self.live_reap_error)

    def send(self, record, signum):
        if process_identity(record['pid']) != {key: record[key] for key in ('pid', 'start_ticks')}:
            raise RuntimeError('Owned child identity changed; refusing signal')
        signal.pidfd_send_signal(record['pidfd'], signum)

    def cleanup(self, child):
        # Popen must reap its own leader to preserve the actual producer code.
        if child is not None and child.poll() is None:
            record = self.entries.get(child.pid) or self.register(child.pid)
            try:
                try:
                    self.send(record, signal.SIGTERM)
                except (FileNotFoundError, ProcessLookupError):
                    pass  # Natural exit races are completed by Popen.wait.
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    self.send(record, signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                child.wait(timeout=5)
        if child is not None and child.poll() is None:
            raise RuntimeError('Owned producer was not reaped')
        if child is not None and child.pid in self.entries:
            record = self.entries.pop(child.pid)
            record['proof'].update(reaped=True, returncode=child.returncode)
            os.close(record['pidfd'])
        deadline = time.monotonic() + 20
        force_deadline = None
        while True:
            pids = direct_children()
            if not pids:
                break
            for pid in pids:
                try:
                    record = self.entries.get(pid) or self.register(pid)
                    result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                    if result is not None:
                        record['proof']['reaped'] = True
                        os.close(self.entries.pop(pid)['pidfd'])
                        continue
                    if not record['term_sent']:
                        self.send(record, signal.SIGTERM)
                        record['term_sent'] = True
                        record['proof']['term_sent'] = True
                    if time.monotonic() >= deadline and not record['kill_sent']:
                        self.send(record, signal.SIGKILL)
                        record['kill_sent'] = True
                        record['proof']['kill_sent'] = True
                    result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                    if result is not None:
                        record['proof']['reaped'] = True
                        os.close(self.entries.pop(pid)['pidfd'])
                except (FileNotFoundError, ProcessLookupError):
                    if pid in self.entries:
                        record = self.entries[pid]
                        result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                        if result is not None:
                            record['proof']['reaped'] = True
                            os.close(self.entries.pop(pid)['pidfd'])
                    # An exiting child may still be listed before waitid can
                    # observe its terminal state. Keep its pinned fd and retry.
            if time.monotonic() >= deadline:
                if force_deadline is None:
                    force_deadline = time.monotonic() + 5
                if time.monotonic() >= force_deadline and direct_children():
                    raise RuntimeError('Owned descendants remain after pidfd cleanup')
            time.sleep(0.05)

    def restore(self):
        self.stop_live_reaping()
        if direct_children():
            raise RuntimeError('Refuse subreaper restore while owned descendants remain')
        subreaper_flag(self.previous)
        if subreaper_flag() != self.previous or direct_children():
            raise RuntimeError('Owned descendants appeared during subreaper restore')
        for record in self.entries.values():
            os.close(record['pidfd'])
        self.entries.clear()


