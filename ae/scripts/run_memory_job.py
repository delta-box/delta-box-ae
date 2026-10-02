#!/usr/bin/env python3
"""Run one AE producer on private noswap tmpfs, then archive its evidence.

The producer keeps its canonical output paths. Only its private mount namespace
sees RAM at the suite directory; failures retain proven resources for recovery.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae')]
from ae.scripts.job_mount_namespace import release_inherited_cube_mounts
from repro.common import configured_path, load_config, write_json
from repro.staging_cleanup import cleanup_reconstructable_staging
from vendor.finalbench.fc_diff_dm.fc_capacity import GIB, check_capacity, job_size_gib
from ae.scripts.owned_job_lifecycle import OwnedChildren


def mount_info(path):
    return json.loads(subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,FSTYPE,OPTIONS'],
        text=True))['filesystems'][0]


def all_mount_records():
    records = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        point = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        records.append(dict(mount_id=int(fields[0]), target=point))
    return records


def mount_record(path):
    """Resolve the visible layer, including when /tmp already is a mount."""
    records = [row for row in all_mount_records() if row['target'] == str(path)]
    if not records:
        return None
    visible = int(subprocess.check_output(
        ['findmnt', '--noheadings', '--output', 'ID', '--target', str(path)], text=True).strip())
    rows = [row for row in records if row['mount_id'] == visible]
    if len(rows) != 1:
        raise RuntimeError('Cannot identify visible mount layer: ' + str(path))
    return rows[0]


def path_identity(path):
    observed = path.stat()
    return dict(device=observed.st_dev, inode=observed.st_ino)


def mount_private(command, target, mounts, *, source=None):
    previous = dict(mount=mount_record(target), **path_identity(target))
    expected = path_identity(source) if source is not None else None
    try:
        subprocess.run(command, check=True)
    finally:
        current = mount_record(target)
        if current is not None and current != previous['mount']:
            observed = path_identity(target)
            record = dict(current, **observed, previous=previous, owned=False)
            mounts.append(record)  # Record even a partially successful command.
            if expected is not None:
                record['owned'] = observed == expected
            else:
                info = mount_info(target)
                record['owned'] = (info['fstype'] == 'tmpfs'
                    and observed['device'] != previous['device'])
            if not record['owned']:
                raise RuntimeError('Mounted source identity changed; retained: ' + str(target))
    if current is None or current == previous['mount']:
        raise RuntimeError('Private mount was not observed: ' + str(target))


def bind_private(source, target, mounts):
    mount_private(['mount', '--bind', str(source), str(target)], target, mounts, source=source)


def verify_owned_mount(record):
    target = Path(record['target'])
    if (not record['owned'] or mount_record(target) != {
            key: record[key] for key in ('mount_id', 'target')}
            or path_identity(target) != {key: record[key] for key in ('device', 'inode')}):
        raise RuntimeError('Owned mount identity changed; refusing cleanup: ' + str(target))


def unmount_owned(record):
    verify_owned_mount(record)
    subprocess.run(['umount', record['target']], check=True)
    target = Path(record['target'])
    previous = record['previous']
    if (mount_record(target) != previous['mount']
            or path_identity(target) != {key: previous[key] for key in ('device', 'inode')}):
        raise RuntimeError('Original mount layer was not restored: ' + str(target))
    record['unmounted'] = True


def verify_owned_directory(path, recorded):
    current = path.lstat()
    if (not stat.S_ISDIR(current.st_mode) or current.st_dev != recorded.st_dev
            or current.st_ino != recorded.st_ino or current.st_uid != recorded.st_uid):
        raise RuntimeError('Owned directory identity changed: ' + str(path))


def sparse_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '--sparse=always', '--reflink=never', '--preserve=mode',
                    str(source), str(target)], check=True)


def stage_index(config, command, ram, mounts):
    if '--instance' not in command or not config.get('payload'):
        return None
    instance = command[command.index('--instance') + 1]
    if Path(instance).name != instance or instance in ('.', '..'):
        raise ValueError('Unsafe instance name')
    source = configured_path(config, 'payload') / 'index_store'
    staged = ram / '.inputs' / 'index_store'
    staged.mkdir(parents=True)
    shutil.copytree(source / instance, staged / instance, symlinks=False)
    bind_private(staged, source, mounts)
    return dict(source=str(source / instance), mount=mount_info(source),
                staged_before_measurement=True)


def stage_e2b(config, ram, mounts):
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
    bind_private(staged, storage, mounts)
    sandbox = configured_path(config, 'e2b.sandbox_dir').resolve(strict=True)
    private_sandbox = ram / '.inputs' / 'e2b-sandbox'
    private_sandbox.mkdir()
    bind_private(private_sandbox, sandbox, mounts)
    return dict(storage=mount_info(storage), sandbox=mount_info(sandbox),
                parent_files=len(files), staging_outside_measurement=True)


def retain_recovery(meta, errors):
    fd, name = tempfile.mkstemp(prefix='memory-job-cleanup-error-', suffix='.json', dir=ROOT / 'ae/work')
    with os.fdopen(fd, 'w') as stream:
        json.dump(dict(meta, status='cleanup-failed', cleanup_errors=errors), stream, indent=2)
        stream.write('\n')
    if 'AE_HOSTED_CALLER_UID' in os.environ:
        guard = ROOT / 'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json'
        try:
            fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return name
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(reason='Memory producer cleanup failed', receipt=name,
                           suite=meta['suite'], archive_path=meta.get('archive_path'),
                           mounts=meta['mounts']), stream, indent=2)
            stream.write('\n')
    return name


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
    identity = json.loads(env.get('AE_MEASUREMENT_IDENTITY', '{}'))
    meta = dict(storage_mode='tmpfs-noswap', node=args.node,
                cpus=sorted(os.sched_getaffinity(0)),
                frequency_policy=identity.get('frequency_policy') or 'maximum-pstate',
                numa_policy=subprocess.check_output(['numactl', '--show'], text=True),
                experiment=args.experiment, archive_after_measurement=True, suite=str(suite))
    suite_stat = suite.stat()
    meta['suite_identity'] = dict(device=suite_stat.st_dev, inode=suite_stat.st_ino)
    code = 1
    child = children = archive = archive_status = None
    archive_stat = archived_stat = None
    mounts = []
    inputs = []
    errors = []
    archived = False
    ram_ready = False

    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('Memory producer terminated')

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        meta['inherited_cube_mounts'] = release_inherited_cube_mounts(ROOT)
        children = OwnedChildren()
        # Explicit directory ownership avoids TemporaryDirectory's recursive
        # finalizer following an archive bind whose unmount failed.
        archive = Path(tempfile.mkdtemp(prefix='memory-archive-', dir=ROOT / 'ae/work'))
        archive_stat = archive.stat()
        meta.update(archive_path=str(archive), archive_identity=path_identity(archive))
        bind_private(suite, archive, mounts)
        mount_private(['mount', '-t', 'tmpfs', '-o',
            f'size={args.size_gib}G,noswap,mpol=bind:{args.node},mode=0755',
            'deltabox-ae-memory', str(suite)], suite, mounts)
        ram_ready = True
        archive_status = archive / '.memory-jobs' / (args.key + '.json')
        verify_owned_mount(mounts[0])
        write_json(archive_status, dict(meta, status='preparing'))
        meta['mount'] = mount_info(suite)
        if args.experiment == 'table-02-fc-diff':
            job_size_gib(args.experiment, dict(config, memory_job_size_gib=args.size_gib))
            needed = (8 + 3 * int(config.get('mem_mib', 8192)) / 1024 + 2) * GIB
            # Preserve the original node budget and tmpfs filesystem allowance.
            filesystem_needed = (8 + 2 * int(config.get('mem_mib', 8192)) / 1024 + 2) * GIB
            meta['admission'] = check_capacity(suite, 'before-staging', int(needed),
                archive / '.memory-jobs' / (args.key + '-capacity.jsonl'), node=args.node,
                filesystem_required=int(filesystem_needed) + 2 * GIB)
        if meta['mount']['fstype'] != 'tmpfs' or 'noswap' not in meta['mount']['options'].split(','):
            raise RuntimeError('Memory measurement requires verified noswap tmpfs')
        meta['index'] = stage_index(config, command, suite, inputs)
        if args.experiment == 'table-02-e2b':
            meta['e2b'] = stage_e2b(config, suite, inputs)
        identity.update({k: meta[k] for k in ('storage_mode', 'node', 'cpus', 'frequency_policy')})
        env['AE_MEASUREMENT_IDENTITY'] = json.dumps(identity)
        # Keep invocation-specific recovery paths out of measured backing metadata.
        env['AE_MEMORY_JOB'] = json.dumps({key: value for key, value in meta.items()
            if key not in ('suite', 'suite_identity', 'archive_path', 'archive_identity', 'inherited_cube_mounts')})
        temporary_root = suite / '.tmp'
        temporary_root.mkdir()
        # Keep E2B socket paths short without changing their RAM backing.
        bind_private(temporary_root, Path('/tmp'), inputs)
        env['TMPDIR'] = '/tmp'
        meta['temporary_mount'] = mount_info(Path('/tmp'))
        verify_owned_mount(mounts[0])
        write_json(archive_status, dict(meta, status='running'))
        child = subprocess.Popen(command, env=env, start_new_session=True)
        children.register(child.pid)
        children.start_live_reaping(child.pid)
        code = child.wait()
    except BaseException as error:
        meta['original_error'] = f'{type(error).__name__}: {error}'
        code = 1
        raise
    finally:
        try:
            children_ok = True
            try:
                if children is not None:
                    try:
                        children.stop_live_reaping()
                    except BaseException as error:
                        children_ok = False
                        errors.append(f'live reap cleanup: {type(error).__name__}: {error}')
                        code = 1
                    children.cleanup(child)
            except BaseException as error:
                children_ok = False
                errors.append(f'child cleanup: {type(error).__name__}: {error}')
                code = 1
            meta['owned_children'] = children.evidence if children is not None else []
            meta['producer_returncode'] = child.returncode if child is not None else None
            output = suite / args.key
            archive_ok = True
            if ram_ready and children_ok:
                try:
                    verify_owned_mount(mounts[1])
                    verify_owned_mount(mounts[0])
                    if output.is_symlink():
                        raise RuntimeError('Producer output is a symlink; retained')
                    if output.is_dir():
                        try:
                            if code == 0:
                                cleanup_reconstructable_staging(output)
                        except BaseException as error:
                            errors.append(f'staging cleanup: {type(error).__name__}: {error}')
                            code = 1
                        usage = shutil.disk_usage(suite)
                        meta['final_tmpfs_bytes'] = dict(total=usage.total, used=usage.used, free=usage.free)
                        write_json(output / 'memory-job.json', dict(meta, returncode=code))
                        write_json(archive_status, dict(meta, status='archiving', returncode=code))
                        if (archive / args.key).exists():
                            raise FileExistsError('Canonical archive destination already exists')
                        subprocess.run(['cp', '-a', '--sparse=always', '--reflink=never',
                                        str(output), str(archive / args.key)], check=True)
                        archived_stat = (archive / args.key).stat()
                        archived = True
                        write_json(archive_status, dict(meta, status='archived', returncode=code))
                except BaseException as error:
                    archive_ok = False
                    meta['archive_error'] = f'{type(error).__name__}: {error}'
                    errors.append(f'archive: {type(error).__name__}: {error}')
                    code = 1
            # A signal can interrupt subprocess.run during cp. Retain the
            # subreaper until synchronous archive helpers and their adopted
            # descendants have also been reaped, before exposing any old path.
            try:
                if children is not None:
                    children.cleanup(None)
            except BaseException as error:
                children_ok = False
                errors.append(f'archive child cleanup: {type(error).__name__}: {error}')
                code = 1
            # An input mount exposes RAM to another path. Do not dismantle its
            # parent volume or archive alias until every dependency is restored.
            if children_ok and archive_ok:
                for record in [*reversed(inputs), *reversed(mounts)]:
                    try:
                        unmount_owned(record)
                    except BaseException as error:
                        errors.append(f'unmount: {type(error).__name__}: {error}')
                        code = 1
                        break
            try:
                if children is not None:
                    children.cleanup(None)
                    children.restore()
            except BaseException as error:
                errors.append(f'final child cleanup: {type(error).__name__}: {error}')
                code = 1
            meta.update(returncode=code, mounts=[*mounts, *inputs])
            if not errors:
                try:
                    verify_owned_directory(suite, suite_stat)
                    if archive is not None:
                        verify_owned_directory(archive, archive_stat)
                        if any(row['target'] == str(archive) or row['target'].startswith(str(archive) + '/')
                               for row in all_mount_records()):
                            raise RuntimeError('Archive directory still contains a mount')
                        archive.rmdir()  # Never recurse into an archive bind.
                except BaseException as error:
                    errors.append(f'directory cleanup: {type(error).__name__}: {error}')
                    code = 1
            if archived and all(row.get('unmounted') for row in [*mounts, *inputs]):
                try:
                    verify_owned_directory(suite, suite_stat)
                    verify_owned_directory(output, archived_stat)
                    final = dict(meta, returncode=code, cleanup_errors=errors,
                                 cleanup_status='failed' if errors else 'ok')
                    write_json(output / 'memory-job.json', final)
                    write_json(suite / '.memory-jobs' / (args.key + '.json'), dict(final, status='archived'))
                except BaseException as error:
                    errors.append(f'final receipt: {type(error).__name__}: {error}')
                    code = 1
            if errors:
                meta['returncode'] = code
                receipt = retain_recovery(meta, errors)
                raise RuntimeError('Memory cleanup failed; owned resources retained: ' + receipt)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
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
