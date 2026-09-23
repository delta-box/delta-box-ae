#!/usr/bin/env python3
"""Run one AE producer on private noswap tmpfs, then archive its evidence.

The producer keeps its canonical output paths: only its mount namespace sees
RAM at the suite directory. The parent suite and other experiments stay on disk.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae')]
from repro.common import configured_path, load_config, write_json
from repro.staging_cleanup import cleanup_reconstructable_staging


def mount_info(path):
    return json.loads(subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,FSTYPE,OPTIONS'],
        text=True))['filesystems'][0]


def sparse_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '--sparse=always', '--reflink=never', '--preserve=mode',
                    str(source), str(target)], check=True)


def bind_private(source, target, stack):
    subprocess.run(['mount', '--bind', str(source), str(target)], check=True)
    stack.callback(subprocess.run, ['umount', str(target)], check=True)


def stage_index(config, command, ram, stack):
    if '--instance' not in command or not config.get('payload'):
        return None
    instance = command[command.index('--instance') + 1]
    if Path(instance).name != instance or instance in ('.', '..'):
        raise ValueError('Unsafe instance name')
    source = configured_path(config, 'payload') / 'index_store'
    staged = ram / '.inputs' / 'index_store'
    staged.mkdir(parents=True)
    shutil.copytree(source / instance, staged / instance, symlinks=False)
    bind_private(staged, source, stack)
    return dict(source=str(source / instance), mount=mount_info(source),
                staged_before_measurement=True)


def stage_e2b(config, ram, stack):
    """Keep baked absolute paths, but isolate every mutable E2B storage path."""
    settings = config.get('e2b', {})
    if settings.get('execution') != 'local':
        raise ValueError('Memory E2B requires local execution inheriting CPU/memory binding')
    from runners.e2b_environment import _parent_dependencies
    storage = configured_path(config, 'e2b.storage').resolve()
    parent = configured_path(config, 'e2b.parent_manifest').resolve()
    manifest = json.loads(parent.read_text())
    files = _parent_dependencies(manifest, storage, settings['from_build'])
    staged = ram / '.inputs' / 'e2b-storage'
    staged.mkdir(parents=True)
    for item in files:
        source = Path(item['path'])
        sparse_copy(source, staged / source.relative_to(storage))
    sparse_copy(parent, staged / parent.relative_to(storage))
    bind_private(staged, storage, stack)
    sandbox = configured_path(config, 'e2b.sandbox_dir').resolve(strict=True)
    private_sandbox = ram / '.inputs' / 'e2b-sandbox'
    private_sandbox.mkdir()
    bind_private(private_sandbox, sandbox, stack)
    return dict(storage=mount_info(storage), sandbox=mount_info(sandbox),
                parent_files=len(files), staging_outside_measurement=True)


def run(args):
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    suite = args.suite.resolve(strict=True)
    if (os.geteuid() != 0 or not command or Path(args.key).name != args.key
            or args.key in ('.', '..') or args.size_gib <= 0):
        raise ValueError('Require root, a command, a safe job key and positive RAM limit')
    if '--out' not in command or Path(command[command.index('--out')+1]).resolve() != suite / args.key:
        raise ValueError('Producer output must be the selected suite/job path')
    if (suite / args.key).exists():
        raise FileExistsError(suite / args.key)
    config = load_config(args.config)
    if args.experiment == 'table-02-cube':
        raise ValueError('Cube service memory/NUMA placement needs its own verified configuration')
    env = dict(os.environ)
    meta = dict(storage_mode='tmpfs-noswap', node=args.node,
                cpus=sorted(os.sched_getaffinity(0)), frequency_policy='maximum-pstate',
                numa_policy=subprocess.check_output(['numactl', '--show'], text=True),
                experiment=args.experiment, archive_after_measurement=True)
    code = 1
    # execute() sends SIGTERM to the owned group on timeout; retain RAM evidence.
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("Memory producer terminated")
    signal.signal(signal.SIGTERM, interrupted)
    # Bind the original output directory before covering it with private RAM.
    # Archival can then preserve absolute paths without rewriting measured data.
    with tempfile.TemporaryDirectory(prefix='memory-archive-', dir=ROOT/'ae/work') as temporary:
        archive = Path(temporary)
        with ExitStack() as mounts:
            bind_private(suite, archive, mounts)
            subprocess.run(['mount', '-t', 'tmpfs', '-o',
                f'size={args.size_gib}G,noswap,mpol=bind:{args.node},mode=0755',
                'deltabox-ae-memory', str(suite)], check=True)
            mounts.callback(subprocess.run, ['umount', str(suite)], check=True)
            archive_status = archive / '.memory-jobs' / (args.key + '.json')
            write_json(archive_status, dict(meta, status='preparing'))
            meta['mount'] = mount_info(suite)
            if meta['mount']['fstype'] != 'tmpfs' or 'noswap' not in meta['mount']['options'].split(','):
                raise RuntimeError('Memory measurement requires verified noswap tmpfs')
            with ExitStack() as inputs:
                meta['index'] = stage_index(config, command, suite, inputs)
                if args.experiment == 'table-02-e2b':
                    meta['e2b'] = stage_e2b(config, suite, inputs)
                identity = json.loads(env.get('AE_MEASUREMENT_IDENTITY', '{}'))
                identity.update({k: meta[k] for k in ('storage_mode', 'node', 'cpus', 'frequency_policy')})
                env['AE_MEASUREMENT_IDENTITY'] = json.dumps(identity)
                env['AE_MEMORY_JOB'] = json.dumps(meta)
                # Keep temporary VM files inside the same bounded RAM volume.
                temporary_root = suite / '.tmp'
                temporary_root.mkdir()
                # AF_UNIX paths are limited to 108 bytes. A worktree plus
                # evidence path can exceed that before E2B appends its socket.
                # Keep all bytes on this owned RAM volume while exposing a
                # short /tmp alias only inside the private mount namespace.
                bind_private(temporary_root, Path('/tmp'), inputs)
                env['TMPDIR'] = '/tmp'
                meta['temporary_mount'] = mount_info(Path('/tmp'))
                write_json(archive_status, dict(meta, status='running'))
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
                                cleanup_reconstructable_staging(output)
                        except BaseException as error:
                            meta['cleanup_error'] = f'{type(error).__name__}: {error}'
                            code = 1
                            raise
                        finally:
                            write_json(output / 'memory-job.json', dict(meta, returncode=code))
                            write_json(archive_status, dict(meta, status='archiving', returncode=code))
                            # Archive even when post-measurement validation or cleanup fails.
                            subprocess.run(['cp', '-a', '--sparse=always', '--reflink=never',
                                            str(output), str(archive / args.key)], check=True)
                            write_json(archive_status, dict(meta, status='archived', returncode=code))
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--suite', type=Path, required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--experiment', required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--node', type=int, required=True)
    p.add_argument('--size-gib', type=int, default=16)
    p.add_argument('command', nargs=argparse.REMAINDER)
    return run(p.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
