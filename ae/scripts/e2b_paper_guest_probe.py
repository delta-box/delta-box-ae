"""Read-only snapshot closure proof, run inside the owned E2B L1.

Only V3 uncompressed headers used by the recovered experiment are accepted.
No artifact or state is repaired by this probe.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import uuid

FILES = ('metadata.json', 'snapfile', 'memfile', 'memfile.header', 'rootfs.ext4', 'rootfs.ext4.header')
ZERO = str(uuid.UUID(int=0))


def canonical_uuid(value):
    if str(uuid.UUID(value)) != value:
        raise ValueError('Noncanonical snapshot UUID')
    return value


def file_record(path):
    st = path.stat()
    if path.is_symlink() or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError('Expected an independent snapshot file')
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    after = path.stat()
    if (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Snapshot changed while hashing')
    return {'path': str(path), 'bytes': st.st_size, 'allocated_bytes': st.st_blocks * 512,
            'sha256': h.hexdigest(), 'mode': st.st_mode & 0o777, 'inode': st.st_ino,
            'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns}


def parse_header(raw, build):
    if len(raw) < 64 or (len(raw) - 64) % 40:
        raise ValueError('Invalid V3 header length')
    version, block, size, generation, ident, base = struct.unpack('<QQQQ16s16s', raw[:64])
    if (version != 3 or block != 4096 or size <= 0 or size % block
            or str(uuid.UUID(bytes=ident)) != build):
        raise ValueError('Header version/block/size/build differs')
    rows = [struct.unpack('<QQ16sQ', raw[i:i+40]) for i in range(64, len(raw), 40)]
    if not rows:
        rows = [(0, (size + block - 1) // block * block, ident, 0)]
    refs, end = {}, 0
    for offset, length, source, stored in rows:
        source = str(uuid.UUID(bytes=source))
        if (length == 0 or offset != end or offset % block or length % block or stored % block
                or offset + length > (size + block - 1) // block * block):
            raise ValueError('Header mappings have a gap, overlap or invalid range')
        end = offset + length
        if source != ZERO:
            row = refs.setdefault(source, {'mapped_bytes': 0, 'required_file_bytes': 0})
            row['mapped_bytes'] += length
            row['required_file_bytes'] = max(row['required_file_bytes'], stored + length)
    if end != (size + block - 1) // block * block:
        raise ValueError('Header does not cover the logical image')
    return {'version': version, 'block_size': block, 'logical_bytes': size,
            'generation': generation, 'build': build, 'metadata_base_build': str(uuid.UUID(bytes=base)),
            'mapping_count': len(rows), 'referenced_builds': refs}


def capture(storage, builds):
    storage = Path(storage)
    if (storage.parent.parent != Path('/var/tmp') or storage.name != 'storage'
            or not storage.parent.name.startswith('e2b-paper-')
            or len(storage.parent.name) != len('e2b-paper-') + 32
            or storage.resolve() != storage):
        raise ValueError('Storage must be the owned reconstruction subtree')
    canonical_uuid(str(uuid.UUID(hex=storage.parent.name[len('e2b-paper-'):])) )
    templates = storage / 'templates'
    if templates.is_symlink():
        raise ValueError('Snapshot directory is a symlink')
    pending = [canonical_uuid(b) for b in builds]
    if not pending:
        raise ValueError('Empty snapshot closure is not proof')
    roots = list(pending)
    found, records = {}, []
    while pending:
        build = pending.pop()
        if build in found:
            continue
        path = templates / build
        if path.is_symlink() or not path.is_dir():
            raise ValueError('Missing or symlinked parent snapshot')
        info = [file_record(path / name) for name in FILES]
        metadata = json.loads((path/'metadata.json').read_text())
        template = metadata.get('template', {})
        if (template.get('build_id') != build or template.get('kernel_version') != 'vmlinux-6.1.158'
                or template.get('firecracker_version') != 'v1.14.1_458ca91'):
            raise ValueError('Snapshot runtime metadata mismatch')
        headers = []
        for name in ('memfile', 'rootfs.ext4'):
            header = parse_header((path/(name+'.header')).read_bytes(), build)
            header['file'] = name + '.header'
            if name == 'memfile' and header['logical_bytes'] != 2048 * 1024**2:
                raise ValueError('L2 memory must be 2048MiB')
            for ancestor, bound in header['referenced_builds'].items():
                parent_file = templates / ancestor / name
                if parent_file.is_symlink() or parent_file.stat().st_size < bound['required_file_bytes']:
                    raise ValueError('Snapshot parent is missing or shorter than its mapped extent')
                pending.append(ancestor)
            headers.append(header)
        found[build] = {'template': template, 'headers': headers,
                        'from_image': metadata.get('from_image')}
        records.extend(info)
    # Bind the parsed header/metadata semantics to the originally hashed files,
    # including changes and reversions during traversal of the parent closure.
    for prior in records:
        if file_record(Path(prior['path'])) != prior:
            raise ValueError('Snapshot closure changed during verification')
    fs = subprocess.run(['findmnt', '-J', '-T', str(storage), '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'],
                        check=True, capture_output=True, text=True)
    fs = json.loads(fs.stdout)['filesystems']
    if (len(fs) != 1 or fs[0]['fstype'] != 'ext4' or fs[0]['source'] != '/dev/vda1'
            or fs[0]['target'] != '/' or 'rw' not in fs[0]['options'].split(',')):
        raise ValueError('E2B snapshots are not on the owned L1 virtual disk')
    v = os.statvfs(storage)
    return {'schema_version': 1, 'status': 'verified', 'storage': str(storage), 'requested_builds': roots,
            'build_count': len(found), 'builds': found, 'files': records, 'filesystem': fs[0],
            'free_bytes': v.f_bavail*v.f_frsize,
            'closure_rule': 'Complete6-file V3 transitive nonzero mapping UUID closure; no state mutation'}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--storage', required=True)
    p.add_argument('--build', action='append', required=True)
    a = p.parse_args()
    print(json.dumps(capture(a.storage, a.build), sort_keys=True))
