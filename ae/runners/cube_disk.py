"""Verify the explicit Cube paper-disk service, including physical backing.

This profile records disk storage as disk storage. It does not relax the
separate RAM/noswap verifier, and does not choose or fall back to a device.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

from repro.common import configured_path, file_record
from runners.cube_memory import cpuset

GIB = 1 << 30
SERVICE = 'cube-sandbox-cubelet.service'
STORAGE = '/data/cubelet/storage'
PATHS = (
    '/data/cubelet/root', '/data/cubelet/state', '/data/snapshot_pack/disks',
    '/data/cube-shim/disks', '/usr/local/services/cubetoolbox/cube-snapshot',
    '/usr/local/services/cubetoolbox/cube-image',
)
CGROUP = Path('/sys/fs/cgroup/cube_sandbox')
DISK_FILESYSTEMS = {'ext4', 'xfs', 'btrfs'}


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def process_start_ticks(pid):
    return Path(f'/proc/{pid}/stat').read_text().split()[21]


def process_status(pid):
    return dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)


def path_identity(path):
    info = Path(path).stat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def visible_mount(path, *, pid=None):
    command = ['findmnt', '-J', '-T', str(path),
               '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN']
    effective = Path(path)
    if pid is not None:
        command = ['nsenter', '-t', str(pid), '-m', *command]
        effective = Path(f'/proc/{pid}/root{path}')
    device = effective.stat().st_dev
    wanted = f'{os.major(device)}:{os.minor(device)}'
    rows = json.loads(output(*command))['filesystems']
    found = [row for row in rows if row.get('maj:min') == wanted]
    if len(found) != 1:
        raise ValueError('Cannot identify the visible disk mount by device: ' + str(path))
    return found[0]


def _flatten_devices(rows):
    for row in rows:
        yield row
        yield from _flatten_devices(row.get('children', []))


def disk_proof(path):
    """Reject RAM/overlay/network storage and retain its physical block ancestry."""
    path = Path(path).resolve(strict=True)
    mount = visible_mount(path)
    source = mount['source'].split('[', 1)[0]
    if mount['fstype'] not in DISK_FILESYSTEMS or not source.startswith('/dev/'):
        raise ValueError('Cube paper-disk workspace must be on explicit physical disk storage')
    ancestry = json.loads(output('lsblk', '--json', '--bytes', '--inverse',
        '--output', 'NAME,PATH,TYPE,MAJ:MIN,SIZE,MODEL,SERIAL,WWN,UUID,FSTYPE,PKNAME', source))
    devices = list(_flatten_devices(ancestry['blockdevices']))
    if any(row.get('type') == 'loop' for row in devices):
        raise ValueError('Cube workspace itself cannot be on an unverified loop backing')
    physical = sorted(({
        key: row.get(key) for key in ('path', 'maj:min', 'size', 'model', 'serial', 'wwn')
    } for row in devices if row.get('type') == 'disk'), key=lambda row: row['path'])
    if not physical:
        raise ValueError('Cannot prove physical disk ancestry for Cube workspace')
    return {'path': str(path), 'device': path.stat().st_dev,
            'mount': mount, 'physical_disks': physical, 'lsblk': ancestry}


def disk_identity(proof):
    mount = proof['mount']
    return {'device': proof['device'], 'source': mount['source'].split('[', 1)[0],
            'fstype': mount['fstype'], 'major_minor': mount['maj:min'],
            'physical_disks': proof['physical_disks']}


def allocated_bytes(paths):
    """Count actual allocation without following links or counting hardlinks twice."""
    seen = set()
    total = 0
    pending = [Path(path) for path in paths]
    while pending:
        path = pending.pop()
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return total


def require_space(path, *, reserve_bytes, allocation_bytes=0):
    if reserve_bytes < 10 * GIB or allocation_bytes < 0:
        raise ValueError('Cube disk requires at least 10 GiB reserve and a nonnegative allocation')
    state = os.statvfs(path)
    available = state.f_bavail * state.f_frsize
    required = reserve_bytes + allocation_bytes
    if available < required:
        raise ValueError(f'Cube paper-disk capacity: {available/GIB:.3f} GiB available; '
                         f'needs {allocation_bytes/GIB:.3f} GiB allocation plus '
                         f'{reserve_bytes/GIB:.3f} GiB reserve')
    return {'available_bytes': available, 'reserve_bytes': reserve_bytes,
            'next_allocation_bytes': allocation_bytes, 'required_bytes': required}


def verify(config):
    manifest = configured_path(config, 'cube.disk_manifest')
    expected = json.loads(manifest.read_text())
    if expected.get('profile') != 'paper-disk' or expected.get('schema_version') != 1:
        raise ValueError('Unrecognized Cube disk manifest')
    cube = config.get('cube', {})
    measurement = config.get('measurement', {})
    node = int(measurement.get('numa_node', 2))
    service_cpus = cube.get('service_cpus', measurement.get('cpus', '48-51'))
    pid = int(output('systemctl', 'show', SERVICE, '-p', 'MainPID', '--value'))
    if (pid != expected['service_pid'] or pid <= 0
            or process_start_ticks(pid) != expected['service_start_ticks']
            or node != expected['node'] or cpuset(service_cpus) != cpuset(expected['cpus'])):
        raise ValueError('Cube disk service identity or placement changed')
    status = process_status(pid)
    if (cpuset(status['Cpus_allowed_list'].strip()) != cpuset(service_cpus)
            or cpuset(status['Mems_allowed_list'].strip()) != {node}):
        raise ValueError('Cube disk daemon affinity differs from the requested service placement')
    if (cpuset((CGROUP / 'cpuset.cpus.effective').read_text().strip()) != cpuset(service_cpus)
            or cpuset((CGROUP / 'cpuset.mems.effective').read_text().strip()) != {node}):
        raise ValueError('Cube sandbox cgroup differs from disk-service placement')
    workspace = Path(expected['workspace'])
    private = Path(expected['private_root'])
    if (not private.resolve(strict=True).is_relative_to(workspace.resolve(strict=True))
            or path_identity(private) != expected['private_root_identity']):
        raise ValueError('Cube private disk directory identity changed')
    actual_disk = disk_proof(workspace)
    if disk_identity(actual_disk) != disk_identity(expected['workspace_disk']):
        raise ValueError('Cube workspace physical disk identity changed')
    targets = {STORAGE, *PATHS}
    if set(expected['bindings']) != targets:
        raise ValueError('Cube disk manifest must isolate all seven service paths')
    paths = {}
    for target, binding in expected['bindings'].items():
        source = Path(binding['source'])
        if not source.resolve(strict=True).is_relative_to(private.resolve(strict=True)):
            raise ValueError('Cube disk binding escapes the private workspace')
        current_source = path_identity(source)
        current_visible = path_identity(f'/proc/{pid}/root{target}')
        mount = visible_mount(target, pid=pid)
        if (current_source != binding['identity'] or current_visible != current_source
                or mount['target'] != target):
            raise ValueError('Cube disk binding identity differs: ' + target)
        if target != STORAGE and disk_identity(disk_proof(source)) != disk_identity(actual_disk):
            raise ValueError('Cube bound directory is not on the selected physical disk')
        paths[target] = mount
    storage = paths[STORAGE]
    if storage['fstype'] != 'xfs' or not storage['source'].startswith('/dev/loop'):
        raise ValueError('Cube storage must be a private disk-backed loop XFS')
    loop = json.loads(output('losetup', '--list', '--json', storage['source']))['loopdevices'][0]
    old_loop = expected['loop']
    backing = Path(loop['back-file'])
    if (loop['name'] != old_loop['name'] or str(backing) != old_loop['back-file']
            or loop.get('ro') or loop.get('offset', 0) or loop.get('sizelimit', 0)
            or path_identity(backing) != expected['image_identity']):
        raise ValueError('Cube disk loop or backing-file identity changed')
    if disk_identity(disk_proof(backing)) != disk_identity(actual_disk):
        raise ValueError('Cube XFS image is not on the selected physical disk')
    measure_paths = [private / 'storage.xfs', *[private / f'path-{i}' for i in range(len(PATHS))]]
    allocation = allocated_bytes(measure_paths)
    increase = max(0, allocation - expected['initial_allocated_bytes'])
    capacity = require_space(workspace, reserve_bytes=expected['reserve_bytes'])
    capacity.update(current_allocated_bytes=allocation, growth_bytes=increase,
                    image_logical_bytes=backing.stat().st_size)
    identity = expected.get('identity')
    if not isinstance(identity, dict) or identity.get('profile') != 'paper-disk':
        raise ValueError('Missing stable Cube paper-disk identity')
    if (identity['disk'] != disk_identity(actual_disk)
            or identity['workspace_mount_options'] != actual_disk['mount']['options']
            or identity['node'] != node or cpuset(identity['service_cpus']) != cpuset(service_cpus)):
        raise ValueError('Stable Cube paper-disk settings changed')
    return {'manifest': file_record(manifest), 'profile': 'paper-disk', 'identity': identity,
            'service_pid': pid, 'node': node, 'service_cpus': service_cpus,
            'runner_cpus': measurement.get('cpus', '48-51'), 'paths': paths,
            'loop': loop, 'workspace_disk': actual_disk, 'capacity': capacity,
            'scope': 'Private disk-backed Cubelet rootfs, snapshots, state, staging and images; external control plane unchanged'}
