#!/usr/bin/env python3
"""Real CRIU negative control: inherited named FIFO must not replay old bytes."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--criu', type=Path, required=True)
    parser.add_argument('--worker', type=Path, required=True)
    args = parser.parse_args()
    root = args.out.resolve(); root.mkdir(exist_ok=False)
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
        raise OSError('cannot become child subreaper')
    result = dict(passed=False, kernel=os.uname().release,
                  criu_sha256=hashlib.sha256(args.criu.read_bytes()).hexdigest(),
                  worker_sha256=hashlib.sha256(args.worker.read_bytes()).hexdigest(),
                  steps=[])
    owned = {}
    fifo = root / 'transport.fifo'; os.mkfifo(fifo)
    transport = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    process = None

    def identity(pid):
        return int(Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[19])

    def own(pid):
        owned[pid] = identity(pid)
        return pid

    def kill(pid):
        if pid not in owned:
            return
        try:
            assert identity(pid) == owned[pid]
            os.kill(pid, signal.SIGKILL)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        owned.pop(pid)

    def wait_until(check):
        until = time.monotonic() + 15
        while not check():
            if time.monotonic() >= until:
                raise TimeoutError('worker state did not become ready')
            time.sleep(.005)

    def invoke(command, pass_fds=()):
        run = subprocess.run(list(map(str, command)), capture_output=True, text=True,
                             timeout=45, pass_fds=pass_fds)
        result['steps'].append(dict(command=list(map(str, command)), rc=run.returncode,
                                    stdout=run.stdout, stderr=run.stderr))
        if run.returncode:
            raise RuntimeError('CRIU failed; see saved command and image logs')

    def drain():
        content = b''
        while True:
            try:
                chunk = os.read(transport, 4096)
            except BlockingIOError:
                return content
            if not chunk:
                return content
            content += chunk

    try:
        ready = root / 'ready.pid'
        process = subprocess.Popen([str(args.worker), str(fifo), str(ready)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        own(process.pid)
        wait_until(lambda: ready.exists() and ready.read_text().strip())
        original = own(int(ready.read_text()))
        wait_until(lambda: Path(f'/proc/{original}/wchan').read_text().strip() == '__do_sys_pause')
        os.kill(original, signal.SIGSTOP)
        wait_until(lambda: Path(f'/proc/{original}/stat').read_text().rpartition(')')[2].split()[0] == 'T')
        os.write(transport, b'A')
        images = root / 'images'; images.mkdir()
        flags = ['--shell-job', '--tcp-close', '--ext-unix-sk', '--file-locks']
        invoke([args.criu, 'dump', '-t', original, '-D', images, '--leave-stopped',
                *flags, '-o', 'dump.log', '-v4'])
        kill(original); process.wait(timeout=10); owned.pop(process.pid, None)
        assert drain() == b'A'
        for inherited, expected in ((False, b'BA'), (True, b'B')):
            os.write(transport, b'B')
            name = 'inherited' if inherited else 'ordinary'
            pidfile = root / f'{name}.pid'
            command = [args.criu, 'restore', '-d', '-D', images, '--leave-stopped',
                       '--pidfile', pidfile, *flags, '-o', f'{name}.log', '-v4']
            if inherited:
                command += ['--inherit-fd', f'fd[{transport}]:{str(fifo).lstrip("/")}']
            invoke(command, (transport,) if inherited else ())
            restored = own(int(pidfile.read_text()))
            observed = drain()
            result['steps'].append(dict(case=name, observed=observed.decode(), expected=expected.decode()))
            assert observed == expected, (name, observed, expected)
            kill(restored)
        result['passed'] = True
    except Exception as error:
        result['error'] = repr(error)
        raise
    finally:
        for pid in list(owned):
            kill(pid)
        os.close(transport)
        (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(dict(passed=result['passed'], out=str(root), error=result.get('error'))))


if __name__ == '__main__':
    main()
