"""Remove unused Cube workspace copies from a newly private producer namespace.

An unshare copies existing mounts. A long Replay producer must not hold Cube's
loop-backed XFS alive after Cube unmounts it in the parent namespace. Only local
mount copies are removed: no loop detach, host unmount, service or file changes.
"""
import os
from pathlib import Path
import re
import subprocess


def mount_records():
    rows = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, fs = left.split(), right.split()
        target = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
        rows.append(dict(mount_id=int(fields[0]), target=target,
                         propagation=fields[6:], fstype=fs[0], options=fs[2].split(',')))
    return rows


def release_inherited_cube_mounts(repo):
    prefix = str(Path(repo) / 'ae/results/selected') + '/'
    pattern = re.compile(r'[^/]+/lanes/numa[0-9]+/environment/attempt-[0-9]+/'
                         r'[^/]+/(?:control-plane/)?cube-memory/ram$')
    rows = mount_records()
    roots = [r for r in rows if r['target'].startswith(prefix)
             and pattern.fullmatch(r['target'][len(prefix):])]
    if not roots:
        return {'removed': []}
    namespace = os.readlink('/proc/self/ns/mnt')
    if namespace in (os.readlink('/proc/1/ns/mnt'), os.readlink(f'/proc/{os.getppid()}/ns/mnt')):
        raise RuntimeError('Inherited mount cleanup requires a new private mount namespace')
    selected = []
    for root in roots:
        if root['fstype'] != 'tmpfs' or 'noswap' not in root['options']:
            raise RuntimeError('Unexpected inherited Cube workspace mount')
        tree = [r for r in rows if r['target'] == root['target']
                or r['target'].startswith(root['target'] + '/')]
        for r in tree:
            if r['propagation'] or (r != root and
                    (r['target'] != root['target'] + '/storage' or r['fstype'] != 'xfs')):
                raise RuntimeError('Unexpected inherited Cube mount tree or propagation')
        selected.extend(tree)
    targets = [r['target'] for r in selected]
    if len(set(targets)) != len(targets):
        raise RuntimeError('Ambiguous inherited Cube mount layers')
    removed = []
    for record in sorted(selected, key=lambda r: r['target'].count('/'), reverse=True):
        if [r for r in mount_records() if r['target'] == record['target']] != [record]:
            raise RuntimeError('Inherited Cube mount identity changed')
        subprocess.run(['umount', record['target']], check=True)
        if any(r['target'] == record['target'] for r in mount_records()):
            raise RuntimeError('Inherited Cube mount remains')
        removed.append(record)
    return {'namespace': namespace, 'removed': removed,
            'scope': 'inherited copies only; host resources and loop associations unchanged'}
