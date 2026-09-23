#!/usr/bin/env python3
"""Non-performance restore probe of an owned failed checkpoint copy.

Requires Linux root. It never edits the CRIU binary, sysctls, or source images.
The restored worker is left stopped and only that newly restored PID is killed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cpu', type=int, default=0)
    parser.add_argument('--criu', type=Path, default=Path('/usr/sbin/criu'))
    args = parser.parse_args()
    os.sched_setaffinity(0, {args.cpu})
    args.out.mkdir(parents=True, exist_ok=False)
    copied = args.out / 'images'
    subprocess.run(['cp', '-a', '--reflink=auto', '--sparse=always', str(args.source), str(copied)], check=True)
    version = subprocess.check_output([str(args.criu), '--version'], text=True).strip()
    metadata = {
        'purpose': 'non-performance restore diagnosis; leave stopped; owned PID cleanup',
        'source': str(args.source), 'copied_images': str(copied),
        'kernel': os.uname().release, 'affinity': sorted(os.sched_getaffinity(0)),
        'criu': str(args.criu), 'criu_version': version,
        'criu_sha256': hashlib.sha256(args.criu.read_bytes()).hexdigest(),
        'source_image_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(args.source.glob('*.img'))},
    }
    pidfile = args.out / 'restored.pid'
    command = [str(args.criu), 'restore', '-d', '--leave-stopped',
               '-D', str(copied), '-W', str(args.out), '--shell-job', '--tcp-close',
               '--file-locks', '--ext-unix-sk', '--pidfile', str(pidfile),
               '--log-file', 'restore-v4.log', '--log-pid', '-v4']
    metadata['command'] = command
    started_ticks = int(float(Path('/proc/uptime').read_text().split()[0]) * os.sysconf('SC_CLK_TCK'))
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
        metadata.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except subprocess.TimeoutExpired as error:
        metadata.update(error='restore timeout', stdout=str(error.stdout), stderr=str(error.stderr))
        raise
    finally:
        if pidfile.exists():
            pid = int(pidfile.read_text())
            proc = Path('/proc') / str(pid)
            if proc.exists():
                fields = (proc / 'stat').read_text().rpartition(')')[2].split()
                # PID was created by this invocation, whose PID file is private.
                if int(fields[19]) < started_ticks:
                    raise RuntimeError('refusing cleanup of a pre-existing PID')
                metadata['restored_process'] = {
                    'pid': pid, 'state': fields[0], 'starttime_ticks': int(fields[19]),
                    'exe': os.readlink(proc / 'exe'),
                }
                os.kill(pid, signal.SIGKILL)
                metadata['owned_pid_cleanup'] = 'SIGKILL'
        metadata['finished_unix'] = time.time()
        (args.out / 'probe.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps({'out': str(args.out), 'returncode': metadata.get('returncode'),
                      'restored_process': metadata.get('restored_process')}, indent=2))


if __name__ == '__main__':
    main()
