#!/usr/bin/env python3
"""Run one baseline producer with its work directory on host NVMe.

The producer keeps its canonical output path. A private mount namespace bind-mounts
nvme_work_root over that suite, so checkpoint and rsync traffic stays on /mnt/disk2.
After the producer exits, reconstructable copies are removed and the remaining
evidence is copied back to the real results directory.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae')]
from repro.common import write_json
from repro.staging_cleanup import cleanup_reconstructable_staging

NVME = Path('/mnt/disk2')


def mount_info(path):
    return json.loads(subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,SOURCE,FSTYPE'],
        text=True))['filesystems'][0]


def bind_private(source, target, stack):
    subprocess.run(['mount', '--bind', str(source), str(target)], check=True)
    stack.callback(subprocess.run, ['umount', str(target)], check=True)


def run(args):
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    suite = args.suite.resolve(strict=True)
    work = args.work_root
    if (os.geteuid() != 0 or not command or Path(args.key).name != args.key
            or args.key in ('.', '..')):
        raise ValueError('Require root, a command, and a safe job key')
    if not work.is_absolute() or any(parent.is_symlink() for parent in (work, *work.parents)):
        raise ValueError('NVMe work root must be an absolute path without symlinks')
    work.mkdir(parents=True, exist_ok=True)
    if work.stat().st_dev != NVME.stat().st_dev:
        raise ValueError('NVMe work root is not on /mnt/disk2')
    if '--out' not in command or Path(command[command.index('--out') + 1]).resolve() != suite / args.key:
        raise ValueError('Producer output must be the selected suite/job path')
    if (suite / args.key).exists() or (work / args.key).exists():
        raise FileExistsError(args.key)
    env = dict(os.environ)
    meta = dict(storage_mode='nvme', work_root=str(work),
                device=os.stat(work).st_dev, experiment_key=args.key)
    code = 1

    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('NVMe producer terminated')

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix='nvme-archive-', dir=ROOT / 'ae/work') as temporary:
        archive = Path(temporary)
        with ExitStack() as mounts:
            bind_private(suite, archive, mounts)
            bind_private(work, suite, mounts)
            info = mount_info(suite)
            if NVME.stat().st_dev != suite.stat().st_dev:
                raise RuntimeError('Suite did not switch to the NVMe device: ' + json.dumps(info))
            meta['mount'] = info
            identity = json.loads(env.get('AE_MEASUREMENT_IDENTITY', '{}'))
            identity.update(storage_mode='nvme', nvme_work_root=str(work))
            env['AE_MEASUREMENT_IDENTITY'] = json.dumps(identity)
            try:
                child = subprocess.Popen(command, env=env, start_new_session=True)
                try:
                    code = child.wait()
                except BaseException:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()
                    code = child.returncode
                    raise
            finally:
                output = suite / args.key
                if output.is_dir():
                    try:
                        if code == 0:
                            report = cleanup_reconstructable_staging(output)
                            write_json(output / 'staging-cleanup.json', report)
                    except BaseException as error:
                        meta['cleanup_error'] = f'{type(error).__name__}: {error}'
                        code = 1
                        raise
                    finally:
                        kept = 0
                        for dirpath, _dirs, files in os.walk(output):
                            for name in files:
                                try:
                                    kept += os.path.getsize(os.path.join(dirpath, name))
                                except OSError:
                                    pass
                        if code == 0 or kept <= 2 * 1024 ** 3:
                            subprocess.run(['cp', '-a', '--sparse=always', '--reflink=never',
                                            str(output), str(archive / args.key)], check=True)
                        else:
                            pointer = archive / args.key
                            pointer.mkdir()
                            write_json(pointer / 'nvme-retained.json', dict(
                                meta, status='retained-on-nvme', returncode=code,
                                path=str(work / args.key),
                                reason='failed job is larger than 2GiB; left on NVMe'))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
