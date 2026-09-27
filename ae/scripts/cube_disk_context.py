#!/usr/bin/env python3
"""Temporarily isolate Cube on an explicitly selected physical disk.

The original XFS image and all service directories are preserved. Private files
remain after exit as evidence; only mounts, loop attachment and the temporary
service override owned by this context are released.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runners.cube_disk import (CGROUP, GIB, PATHS, SERVICE, allocated_bytes,
    disk_identity, disk_proof, path_identity, process_start_ticks, require_space, verify, visible_mount)
from runners.cube_memory import cpuset

STORAGE = Path('/data/cubelet/storage')
DROP = Path('/run/systemd/system') / f'{SERVICE}.d/zzzz-deltabox-cube-paper-disk.conf'
MEMORY_DROP = DROP.with_name('zzzz-deltabox-fig167-ram.conf')
LOCK = Path('/run/lock/deltabox-cube-memory.lock')


def run(*args, **kwargs):
    kwargs.setdefault('timeout', 600)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True, timeout=60).strip()


def sandboxes():
    with urllib.request.urlopen('http://127.0.0.1:3000/sandboxes', timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, list):
        raise ValueError('Unknown Cube inventory format')
    return value


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            value.update(chunk)
    return value.hexdigest()


def tree_manifest(root):
    """Compare copied bytes, links and metadata without retaining secret contents."""
    root = Path(root)
    records = []
    links = {}
    paths = [root, *sorted(root.rglob('*'))]
    for path in paths:
        info = path.lstat()
        relative = '.' if path == root else path.relative_to(root).as_posix()
        item = dict(path=relative, mode=stat.S_IMODE(info.st_mode),
                    kind=stat.S_IFMT(info.st_mode), uid=info.st_uid,
                    gid=info.st_gid, mtime_ns=info.st_mtime_ns)
        if stat.S_ISREG(info.st_mode):
            item.update(bytes=info.st_size, sha256=digest(path))
            key = (info.st_dev, info.st_ino)
            item['hardlink_group'] = links.setdefault(key, relative)
        elif stat.S_ISLNK(info.st_mode):
            item['target'] = os.readlink(path)
        elif not stat.S_ISDIR(info.st_mode):
            item['rdev'] = info.st_rdev
        item['xattrs'] = {
            name: hashlib.sha256(os.getxattr(path, name, follow_symlinks=False)).hexdigest()
            for name in sorted(os.listxattr(path, follow_symlinks=False))
        }
        records.append(item)
    encoded = json.dumps(records, sort_keys=True, separators=(',', ':')).encode()
    return {'entries': len(records), 'sha256': hashlib.sha256(encoded).hexdigest(),
            'records': records}


def sync_private_tree(root):
    """Finish only this preparation's writes; never drop host page cache."""
    started = time.monotonic()
    files = directories = 0
    pending = [Path(root)]
    ordered_directories = []
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            ordered_directories.append(path)
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
                files += 1
            finally:
                os.close(fd)
    for path in reversed(ordered_directories):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
            directories += 1
        finally:
            os.close(fd)
    return {'files_fsynced': files, 'directories_fsynced': directories,
            'elapsed_s': time.monotonic() - started,
            'scope': 'private preparation files and directories only; no global sync or cache dropping'}


def acquire_lock():
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, 'a')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ValueError('Unsafe Cube service lease file')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BaseException:
        handle.close()
        raise


def service_property(name):
    return output('systemctl', 'show', SERVICE, '-p', name, '--value')


def copy_image(source, image, freeze_state=None):
    freeze_state = {} if freeze_state is None else freeze_state
    freeze_attempted = False
    try:
        freeze_attempted = True
        freeze_state.update(attempted=True, unfrozen=False)
        run('fsfreeze', '--freeze', STORAGE, timeout=30)
        before = (source.stat().st_size, source.stat().st_mtime_ns, path_identity(source))
        source_hash = digest(source)
        run('cp', '--sparse=always', '--reflink=never', '--preserve=all', source, image)
        copied_hash = digest(image)
        after = (source.stat().st_size, source.stat().st_mtime_ns, path_identity(source))
        if before != after or source_hash != copied_hash or digest(source) != source_hash:
            raise ValueError('Cube source image changed or its private copy is incomplete')
        if image.stat().st_size != source.stat().st_size:
            raise ValueError('Cube private image logical size differs from source')
        return {'source': str(source), 'copy': str(image), 'sha256': source_hash,
                'logical_bytes': source.stat().st_size,
                'source_allocated_bytes': source.stat().st_blocks * 512,
                'copy_allocated_bytes': image.stat().st_blocks * 512,
                'copy_policy': 'full logical-byte SHA equality; sparse, reflink=never; source unchanged'}
    finally:
        if freeze_attempted:
            run('fsfreeze', '--unfreeze', STORAGE, timeout=30)
            freeze_state['unfrozen'] = True


def validate_placement(node, cpus):
    if type(node) is not int or node < 0 or not cpuset(cpus):
        raise ValueError('Invalid Cube disk service placement')
    available = cpuset((Path(f'/sys/devices/system/node/node{node}') / 'cpulist').read_text())
    if not cpuset(cpus) <= available:
        raise ValueError('Cube service CPUs must be entirely inside the requested NUMA node')


def _validate_path_text(path):
    # systemd BindPaths is whitespace separated. Reject interpolation characters
    # instead of guessing escaping or expanding a user-selected path.
    if any(character.isspace() or character in ':\\%' for character in str(path)):
        raise ValueError('Cube disk paths cannot contain whitespace, colon, backslash or percent')


@contextmanager
def disk_service(output_dir, *, workspace, node, cpus, reserve_gib=10):
    if os.geteuid() != 0:
        raise ValueError('Root required for Cube private disk mounts and service restoration')
    validate_placement(node, cpus)
    if type(reserve_gib) is not int or reserve_gib < 10:
        raise ValueError('Cube disk reserve must be at least 10 GiB')
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('Cube disk workspace must already exist')
    _validate_path_text(workspace)
    chosen_disk = disk_proof(workspace)
    out = Path(output_dir).resolve()
    _validate_path_text(out)
    out.mkdir(parents=True, exist_ok=False)
    lock = acquire_lock()
    stopped = override = placement = False
    private = image = volume = loop = None
    before = None
    prior_signal = None
    body_error = None
    copied = {}
    freeze_state = {}
    try:
        if DROP.exists() or MEMORY_DROP.exists() or sandboxes():
            raise ValueError('Cube must be idle and have no prior experimental override')
        if service_property('ActiveState') != 'active':
            raise ValueError('Expected an active original Cube service')
        original_pid = int(service_property('MainPID'))
        if original_pid <= 0:
            raise ValueError('Original Cube service has no live MainPID')
        device = visible_mount(STORAGE)['source']
        if not device.startswith('/dev/loop'):
            raise ValueError('Original Cube storage must be a loop-backed XFS image')
        source_loop = json.loads(output('losetup', '--list', '--json', device))['loopdevices'][0]
        source = Path(source_loop['back-file']).resolve(strict=True)
        if source_loop.get('ro') or source_loop.get('offset', 0) or source_loop.get('sizelimit', 0):
            raise ValueError('Expected a complete writable source loop image')
        if visible_mount(STORAGE)['fstype'] != 'xfs':
            raise ValueError('Original Cube storage must be XFS')
        for path in PATHS:
            if not Path(path).is_dir():
                raise ValueError('Missing original Cube directory: ' + path)
        directories_bytes = sum(allocated_bytes([path]) for path in PATHS)
        # Reserve the entire source image's logical size, although cp remains
        # sparse. This covers every possible newly allocated block in that XFS.
        admitted = require_space(workspace, reserve_bytes=reserve_gib * GIB,
                                 allocation_bytes=source.stat().st_size + directories_bytes)
        before = {
            'main_pid': original_pid, 'unit_sha256': hashlib.sha256(output('systemctl', 'cat', SERVICE).encode()).hexdigest(),
            'source': str(source), 'source_loop': source_loop,
            'source_size': source.stat().st_size, 'source_allocated_bytes': source.stat().st_blocks * 512,
            'copied_directories_allocated_bytes': directories_bytes, 'admission': admitted,
            'allowed_cpus': service_property('AllowedCPUs'),
            'allowed_nodes': service_property('AllowedMemoryNodes'),
            'cgroup_cpus': (CGROUP / 'cpuset.cpus').read_text(),
            'cgroup_nodes': (CGROUP / 'cpuset.mems').read_text(),
            'original_bindings': {str(path): path_identity(f'/proc/{original_pid}/root{path}')
                                  for path in [str(STORAGE), *PATHS]},
            'workspace_disk': chosen_disk,
        }
        write_json(out / 'before.json', before)
        private = Path(tempfile.mkdtemp(prefix='cube-paper-disk-', dir=workspace))
        os.chmod(private, 0o700)
        image = private / 'storage.xfs'
        volume = private / 'storage'
        volume.mkdir()
        write_json(out / 'workspace.json', {'private_root': str(private), 'workspace': str(workspace),
                   'selected_disk': chosen_disk, 'retention': 'preserve all private files after exit'})

        def interrupted(signum, frame):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise KeyboardInterrupt(f'signal {signum}')
        prior_signal = signal.signal(signal.SIGTERM, interrupted)
        stopped = True  # A failed stop can still have changed service state.
        run('systemctl', 'stop', SERVICE)
        if sandboxes():
            raise ValueError('Cube inventory changed during exclusive disk setup')
        copied['image'] = copy_image(source, image, freeze_state)
        write_json(out / 'copy-proof.json', copied)
        # The image is charged at its full logical size during every setup step;
        # this does not allocate or preallocate those still-sparse bytes.
        for index, path in enumerate(PATHS):
            remaining = sum(allocated_bytes([item]) for item in PATHS[index:])
            require_space(workspace, reserve_bytes=reserve_gib * GIB,
                          allocation_bytes=max(0, image.stat().st_size - image.stat().st_blocks * 512) + remaining)
            target = private / f'path-{index}'
            target.mkdir()
            original = tree_manifest(path)
            run('cp', '-a', str(Path(path)) + '/.', target)
            copied_tree = tree_manifest(target)
            if original != copied_tree or original != tree_manifest(path):
                raise ValueError('Cube private directory copy or unchanged-source verification failed: ' + path)
            copied[path] = {'source': path, 'copy': str(target),
                            'entries': original['entries'], 'sha256': original['sha256'],
                            'metadata_hardlinks_xattrs_verified': True}
            write_json(out / 'copy-proof.json', copied)
        if disk_identity(disk_proof(workspace)) != disk_identity(chosen_disk):
            raise ValueError('Cube workspace device changed during setup')
        require_space(workspace, reserve_bytes=reserve_gib * GIB,
                      allocation_bytes=max(0, image.stat().st_size - image.stat().st_blocks * 512))
        copied['pre_measurement_flush'] = sync_private_tree(private)
        write_json(out / 'copy-proof.json', copied)
        loop = output('losetup', '--find', '--show', image)
        run('mount', '-t', 'xfs', '-o', 'nouuid', loop, volume)
        bindings = {str(STORAGE): volume}
        bindings.update({path: private / f'path-{i}' for i, path in enumerate(PATHS)})
        pin = out / 'pin.py'
        pin.write_text('from pathlib import Path\nimport time\np=Path("/sys/fs/cgroup/cube_sandbox")\n'
            'for _ in range(200):\n'
            ' if (p/"cpuset.cpus").exists(): break\n'
            ' time.sleep(.1)\n'
            f'(p/"cpuset.mems").write_text({str(node)!r})\n'
            f'(p/"cpuset.cpus").write_text({cpus!r})\n')
        DROP.parent.mkdir(parents=True, exist_ok=True)
        override = True
        DROP.write_text('[Service]\nPrivateMounts=yes\nBindPaths=' +
            ' '.join(f'{value}:{key}' for key, value in bindings.items()) + '\n'
            'ExecStart=\n' +
            f'ExecStart=/usr/bin/numactl --physcpubind={cpus} --membind={node} /usr/local/services/cubetoolbox/scripts/systemd/cubelet-start.sh\n'
            'ExecStartPost=\n' + f'ExecStartPost=/usr/bin/python3 {pin}\n')
        run('systemctl', 'daemon-reload')
        placement = True
        run('systemctl', 'set-property', '--runtime', SERVICE,
            f'AllowedCPUs={cpus}', f'AllowedMemoryNodes={node}')
        run('systemctl', 'start', SERVICE)
        pid = int(service_property('MainPID'))
        if pid <= 0 or service_property('ActiveState') != 'active':
            raise ValueError('Private Cube disk service failed to start')
        if sandboxes():
            raise ValueError('Unexpected Cube activity before disk measurement')
        measure_paths = [image, *[private / f'path-{i}' for i in range(len(PATHS))]]
        proof = {
            'schema_version': 1, 'profile': 'paper-disk',
            'service_pid': pid, 'service_start_ticks': process_start_ticks(pid),
            'node': node, 'cpus': cpus, 'workspace': str(workspace),
            'private_root': str(private), 'private_root_identity': path_identity(private),
            'workspace_disk': chosen_disk, 'image_identity': path_identity(image),
            'loop': json.loads(output('losetup', '--list', '--json', loop))['loopdevices'][0],
            'bindings': {key: {'source': str(value), 'identity': path_identity(value)}
                         for key, value in bindings.items()},
            'initial_allocated_bytes': allocated_bytes(measure_paths),
            'reserve_bytes': reserve_gib * GIB,
            'initial_image_logical_bytes': image.stat().st_size,
            'copy_proof': str(out / 'copy-proof.json'),
            'source_image': str(source),
            'admission_policy': 'source logical image size + six directories allocation + reserve; sparse copy; never preallocate',
        }
        proof['identity'] = {
            'profile': 'paper-disk', 'disk': disk_identity(chosen_disk),
            'workspace_mount_options': chosen_disk['mount']['options'],
            'node': node, 'service_cpus': cpus,
            'source_image_sha256': copied['image']['sha256'],
            'source_image_logical_bytes': copied['image']['logical_bytes'],
            'source_directories': {path: copied[path]['sha256'] for path in PATHS},
            'original_service_unit_sha256': before['unit_sha256'],
            'private_xfs_mount_options': 'nouuid',
            'copy_policy': 'sparse; reflink=never; no preallocation; private files and dirs fsynced before service start',
            'page_cache_policy': 'preparation copy and verification read the private data; host cache is not dropped',
        }
        write_json(out / 'storage.json', proof)
        config = {'cube': {'disk_manifest': str(out / 'storage.json'), 'service_cpus': cpus},
                  'measurement': {'numa_node': node, 'cpus': cpus}}
        write_json(out / 'verified.json', verify(config))
        yield out / 'storage.json'
    except BaseException as error:
        body_error = error
        write_json(out / 'context-error.json', {'type': type(error).__name__, 'error': str(error)})
        raise
    finally:
        errors = []
        def cleanup(label, function):
            try:
                return function()
            except BaseException as error:
                errors.append(f'{label}: {type(error).__name__}: {error}')
        if override:
            cleanup('stop private service', lambda: run('systemctl', 'stop', SERVICE))
            cleanup('remove owned disk override', lambda: DROP.unlink(missing_ok=True))
            cleanup('reload original service', lambda: run('systemctl', 'daemon-reload'))
        if placement and before is not None:
            cleanup('restore service placement', lambda: run('systemctl', 'set-property', '--runtime', SERVICE,
                    'AllowedCPUs=' + before['allowed_cpus'], 'AllowedMemoryNodes=' + before['allowed_nodes']))
        if freeze_state.get('attempted') and not freeze_state.get('unfrozen'):
            def ensure_unfrozen():
                run('fsfreeze', '--unfreeze', STORAGE, timeout=30)
                freeze_state['unfrozen'] = True
            cleanup('retry owned source unfreeze', ensure_unfrozen)
        if stopped and before is not None:
            if not freeze_state.get('attempted') or freeze_state.get('unfrozen'):
                cleanup('restart original service', lambda: run('systemctl', 'start', SERVICE))
            else:
                errors.append('original service remains stopped because source unfreeze could not be confirmed')
            cleanup('restore original sandbox nodes',
                    lambda: (CGROUP / 'cpuset.mems').write_text(before['cgroup_nodes']))
            cleanup('restore original sandbox CPUs',
                    lambda: (CGROUP / 'cpuset.cpus').write_text(before['cgroup_cpus']))
            restored = {}
            for name in ('ActiveState', 'MainPID', 'AllowedCPUs', 'AllowedMemoryNodes'):
                restored[name] = cleanup('read restored ' + name, lambda name=name: service_property(name))
            def verify_restored():
                if (restored['ActiveState'] != 'active' or int(restored['MainPID']) <= 0
                        or restored['AllowedCPUs'] != before['allowed_cpus']
                        or restored['AllowedMemoryNodes'] != before['allowed_nodes']):
                    raise RuntimeError('Original Cube service state/placement was not restored')
                restored_pid = int(restored['MainPID'])
                for path, expected in before['original_bindings'].items():
                    if path_identity(f'/proc/{restored_pid}/root{path}') != expected:
                        raise RuntimeError('Original Cube service storage was not restored: ' + path)
            cleanup('verify original service restoration', verify_restored)
            restored.update(override_removed=not DROP.exists(), cleanup_errors=list(errors))
            cleanup('write restoration evidence', lambda: write_json(out / 'restored.json', restored))
        if loop is None and image is not None and image.exists():
            def recover_owned_loop():
                rows = json.loads(output('losetup', '--list', '--json', '--associated', image))['loopdevices']
                rows = [row for row in rows if row.get('back-file') == str(image)]
                if len(rows) > 1:
                    raise RuntimeError('Multiple loop attachments for this unique private image')
                return rows[0]['name'] if rows else None
            loop = cleanup('discover private loop after setup error', recover_owned_loop)
        if volume is not None and os.path.ismount(volume):
            cleanup('unmount private disk XFS', lambda: run('umount', volume))
        if loop is not None:
            cleanup('detach private disk loop', lambda: run('losetup', '-d', loop))
        if prior_signal is not None:
            cleanup('restore SIGTERM handler', lambda: signal.signal(signal.SIGTERM, prior_signal))
        lock.close()
        if errors:
            write_json(out / 'cleanup-errors.json', errors)
            if body_error is None:
                raise RuntimeError('Cube disk service cleanup/restoration failed: ' + '; '.join(errors))
            if hasattr(body_error, 'add_note'):
                body_error.add_note('Cube disk cleanup also failed: ' + '; '.join(errors))
