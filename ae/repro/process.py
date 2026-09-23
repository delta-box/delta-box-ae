"""Execute owned experiment processes and preserve failure evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .common import write_json


def execute(argv: list[str], output: Path, *, cwd: Path, timeout: float,
            env: dict[str, str] | None = None, stop_event: threading.Event | None = None,
            termination_grace: float = 30) -> dict:
    if not argv or not all(isinstance(arg, str) for arg in argv):
        raise ValueError('Command must be a nonempty argument list')
    if timeout <= 0 or termination_grace <= 0:
        raise ValueError('Timeout must be positive')
    output.mkdir(parents=True, exist_ok=False)
    record = {'command': argv, 'cwd': str(cwd), 'status': 'running',
              'started_at': datetime.now(timezone.utc).isoformat()}
    write_json(output / 'process.json', record)
    started = time.monotonic()
    process = None
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise KeyboardInterrupt('Experiment terminated')
        previous_sigterm = signal.signal(signal.SIGTERM, interrupted)
    try:
        with (output / 'stdout.log').open('wb') as log:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            if stop_event is None:
                code = process.wait(timeout=timeout)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    if stop_event.is_set():
                        raise RuntimeError("Measurement suite cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    try:
                        code = process.wait(timeout=min(0.5, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
        record.update(status='ok' if code == 0 else 'failed', returncode=code)
    except BaseException as exc:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=termination_grace)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        record.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                      returncode=process.returncode if process is not None else None)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        # Drivers own their child process group; reap surviving mock/worker processes
        # even when the leading process exited normally or raised during startup.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        record.update(elapsed_s=time.monotonic() - started,
                      finished_at=datetime.now(timezone.utc).isoformat())
        write_json(output / 'process.json', record)
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return record
