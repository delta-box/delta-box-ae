#!/usr/bin/env python3
"""Fail closed if a V3 E2B template references missing or truncated objects.

Run before publishing a locally rebuilt template. Presence alone cannot prove
content correctness; the manifest records hashes for subsequent verification.
"""
import argparse
import hashlib
import json
import re
import struct
import subprocess
import uuid
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inspect(storage, build):
    root = storage / 'templates'
    files = {}
    headers = {}
    for kind in ('rootfs.ext4', 'memfile'):
        p = root / build / (kind + '.header')
        data = p.read_bytes()
        if len(data) < 64 or (len(data) - 64) % 40:
            raise ValueError(f'invalid header size: {p}')
        version, block, size, generation, own, base = struct.unpack('<QQQQ16s16s', data[:64])
        if version != 3 or str(uuid.UUID(bytes=own)) != build:
            raise ValueError(f'unsupported or mismatched header: {p}')
        end = 0
        references = {}
        for offset in range(64, len(data), 40):
            start, length, parent, source = struct.unpack('<QQ16sQ', data[offset:offset + 40])
            if start != end or not length or start + length > size:
                raise ValueError(f'invalid mapping at {offset}: {p}')
            end = start + length
            if parent == bytes(16):
                continue
            parent = str(uuid.UUID(bytes=parent))
            obj = root / parent / kind
            if not obj.is_file() or obj.stat().st_size < source + length:
                raise ValueError(f'missing/truncated referenced object: {obj}, needs {source + length}')
            references[parent] = references.get(parent, 0) + length
            files[str(obj)] = obj
        if end != size:
            raise ValueError(f'incomplete address space: {p}')
        headers[kind] = dict(sha256=digest(p), size=size, block_size=block,
                             generation=generation, references=references)
        files[str(p)] = p
    for name in ('metadata.json', 'snapfile'):
        p = root / build / name
        if not p.is_file():
            raise ValueError(f'missing template artifact: {p}')
        files[str(p)] = p
    return dict(build_id=build, headers=headers, files={
        name: dict(bytes=p.stat().st_size, sha256=digest(p)) for name, p in sorted(files.items())
    })


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--storage', type=Path)
    ap.add_argument('--build')
    ap.add_argument('--output', type=Path)
    ap.add_argument('--verify-manifest', type=Path)
    ap.add_argument('--registered-template')
    args = ap.parse_args()
    if args.verify_manifest:
        expected = json.loads(args.verify_manifest.read_text())
        if args.registered_template:
            parts = args.registered_template.split(':', 1)
            env_id, tag = parts[0], parts[1] if len(parts) == 2 else 'default'
            if not all(re.fullmatch(r'[A-Za-z0-9_-]+', s) for s in (env_id, tag)):
                raise SystemExit('invalid template identifier')
            sql = ("select build_id from env_build_assignments "
                   f"where env_id='{env_id}' and tag='{tag}'")
            build = subprocess.check_output(['docker', 'exec', 'ae-e2b-postgres',
                    'psql', '-U', 'postgres', '-d', 'postgres', '-X', '-A', '-t',
                    '-v', 'ON_ERROR_STOP=1', '-c', sql], text=True).strip()
            if build != expected['build_id']:
                raise SystemExit('registered template differs from verified build; refusing to run')
            key = 'template:info:{' + env_id + '}:' + tag
            cached = subprocess.check_output(['docker', 'exec', 'ae-e2b-redis',
                     'redis-cli', '--raw', 'GET', key], text=True).strip()
            if cached and json.loads(cached)['build']['ID'] != expected['build_id']:
                raise SystemExit('cached template differs from verified build; refusing to run')
        result = inspect(Path(expected['storage']), expected['build_id'])
        if result['files'] != expected['files'] or result['headers'] != expected['headers']:
            raise SystemExit('template integrity mismatch; refusing to run')
        status = 'verified'
    else:
        if not (args.storage and args.build and args.output):
            ap.error('--storage, --build and --output are required when creating a manifest')
        result = inspect(args.storage, args.build)
        result['storage'] = str(args.storage)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        status = 'complete'
    print(json.dumps(dict(build_id=result['build_id'], objects=len(result['files']), status=status)))
