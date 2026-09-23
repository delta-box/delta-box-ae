#!/usr/bin/env python3
"""Export, import and verify the separate, content-addressed paper data bundle."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def manifests(root):
    rows = []
    for path in sorted((root / 'paper').glob('*/files.jsonl')):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    if not rows:
        raise RuntimeError('No paper file manifests found')
    objects, targets = {}, set()
    for row in rows:
        target = PurePosixPath(row['target'])
        if target.is_absolute() or '..' in target.parts or len(target.parts) < 4 or target.parts[0] != 'paper' or target.parts[2] != 'data':
            raise RuntimeError('Unsafe target: ' + row['target'])
        sha = row['sha256']
        if len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise RuntimeError('Invalid SHA-256 in manifest')
        if row['target'] in targets:
            raise RuntimeError('Duplicate target: ' + row['target'])
        targets.add(row['target'])
        if sha in objects and objects[sha] != row['bytes']:
            raise RuntimeError('Inconsistent object size')
        objects[sha] = row['bytes']
    canonical = sorted((r['target'], r['sha256'], r['bytes']) for r in rows)
    manifest_hash = hashlib.sha256(json.dumps(canonical, separators=(',', ':')).encode()).hexdigest()
    return rows, objects, manifest_hash


def materialize(root, rows):
    objects = root / 'traces/objects'
    for row in rows:
        target = root / row['target']
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(root.resolve()):
            raise RuntimeError('Target directory escapes repository')
        link = os.path.relpath(objects / row['sha256'], target.parent)
        if target.is_symlink():
            if os.readlink(target) != link:
                raise RuntimeError('Existing link differs: ' + row['target'])
        elif target.exists():
            raise RuntimeError('Existing file blocks materialization: ' + row['target'])
        else:
            target.symlink_to(link)


def verify(root, rows, objects):
    for sha, size in objects.items():
        path = root / 'traces/objects' / sha
        if not path.is_file() or path.stat().st_size != size or digest_file(path) != sha:
            raise RuntimeError('Missing or corrupt object: ' + sha)
    for row in rows:
        path = root / row['target']
        expected = root / 'traces/objects' / row['sha256']
        if not path.is_symlink() or path.resolve() != expected.resolve():
            raise RuntimeError('Missing or incorrect data link: ' + row['target'])


def export_bundle(root, dest, rows, objects, manifest_hash):
    verify(root, rows, objects)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as output:
        proc = subprocess.Popen(['zstd', '-q', '-T2', '-8', '-c'], stdin=subprocess.PIPE, stdout=output)
        try:
            with tarfile.open(fileobj=proc.stdin, mode='w|') as tar:
                info = json.dumps(dict(format=1, manifest_sha256=manifest_hash,
                                       objects=len(objects), bytes=sum(objects.values()))).encode()
                member = tarfile.TarInfo('bundle.json')
                member.size, member.mode = len(info), 0o444
                tar.addfile(member, io.BytesIO(info))
                for sha, size in sorted(objects.items()):
                    member = tarfile.TarInfo('objects/' + sha)
                    member.size, member.mode = size, 0o444
                    with (root / 'traces/objects' / sha).open('rb') as source:
                        tar.addfile(member, source)
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError('zstd compression failed')
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    sha = digest_file(dest)
    dest.with_name(dest.name + '.sha256').write_text(sha + '  ' + dest.name + '\n')
    index = dict(filename=dest.name, sha256=sha, compressed_bytes=dest.stat().st_size,
                 uncompressed_object_bytes=sum(objects.values()), unique_objects=len(objects),
                 materialized_file_references=len(rows), manifest_sha256=manifest_hash,
                 storage='Curated archive; original historical data excluded',
                 repository_path=dest.resolve().relative_to(root.resolve()).as_posix())
    (root / 'paper/data-bundle.json').write_text(json.dumps(index, indent=2) + '\n')
    return index


def import_bundle(root, source, rows, objects, manifest_hash):
    release = root / 'paper/data-bundle.json'
    if release.exists():
        expected = json.loads(release.read_text())
        if digest_file(source) != expected['sha256']:
            raise RuntimeError('Bundle SHA-256 does not match the committed release index')
    objdir = root / 'traces/objects'
    objdir.mkdir(parents=True, exist_ok=True)
    if not objdir.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Object directory escapes repository')
    proc = subprocess.Popen(['zstd', '-dc', str(source)], stdout=subprocess.PIPE)
    seen, header = set(), False
    try:
        with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
            for member in tar:
                if not member.isfile():
                    raise RuntimeError('Bundle contains a non-regular entry')
                stream = tar.extractfile(member)
                if not header:
                    if member.name != 'bundle.json' or member.size > 10000:
                        raise RuntimeError('Missing bundle header')
                    info = json.loads(stream.read())
                    if info.get('format') != 1 or info.get('manifest_sha256') != manifest_hash:
                        raise RuntimeError('Bundle was built for different file manifests')
                    header = True
                    continue
                name = PurePosixPath(member.name)
                if len(name.parts) != 2 or name.parts[0] != 'objects' or name.parts[1] not in objects:
                    raise RuntimeError('Unexpected bundle entry: ' + member.name)
                sha = name.parts[1]
                if sha in seen or member.size != objects[sha]:
                    raise RuntimeError('Duplicate entry or size mismatch')
                dest = objdir / sha
                partial = objdir / ('.' + sha + '.partial')
                digest = hashlib.sha256()
                try:
                    with partial.open('xb') as output:
                        for block in iter(lambda: stream.read(1 << 20), b''):
                            output.write(block)
                            digest.update(block)
                    if digest.hexdigest() != sha:
                        raise RuntimeError('Object hash mismatch: ' + sha)
                    if dest.exists():
                        if digest_file(dest) != sha:
                            raise RuntimeError('Existing object differs: ' + sha)
                        partial.unlink()
                    else:
                        partial.chmod(0o444)
                        partial.rename(dest)
                finally:
                    if partial.exists():
                        partial.unlink()
                seen.add(sha)
        if proc.wait() != 0 or seen != set(objects):
            raise RuntimeError('Incomplete or invalid compressed bundle')
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    materialize(root, rows)
    verify(root, rows, objects)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('command', choices=['export', 'import', 'verify'])
    parser.add_argument('bundle', nargs='?', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    rows, objects, manifest_hash = manifests(root)
    if args.command == 'import' and args.bundle is None:
        release = json.loads((root / 'paper/data-bundle.json').read_text())
        args.bundle = root / release['repository_path']
    if args.command == 'export' and args.bundle is None:
        parser.error('export requires a bundle path inside the repository')
    if args.command == 'export':
        print(json.dumps(export_bundle(root, args.bundle, rows, objects, manifest_hash), indent=2))
    else:
        if args.command == 'import':
            import_bundle(root, args.bundle, rows, objects, manifest_hash)
        else:
            verify(root, rows, objects)
        print(json.dumps(dict(verified_objects=len(objects), file_references=len(rows),
                              object_bytes=sum(objects.values()))))


if __name__ == '__main__':
    main()
