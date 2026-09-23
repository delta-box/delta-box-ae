#!/usr/bin/env python3
"""Extract the streamed supplement and verify every member against remote hashes."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('archive', type=Path)
    ap.add_argument('destination', type=Path)
    ap.add_argument('--report', type=Path, required=True)
    args = ap.parse_args()
    dest = args.destination.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    observed = {}
    with tarfile.open(args.archive, mode='r|gz') as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith('/') or '..' in parts:
                raise ValueError('Unsafe member name')
            target = dest.joinpath(*parts)
            if not target.resolve().is_relative_to(dest) or not member.isfile():
                raise ValueError('Unsupported member: ' + member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.import-partial')
            h = hashlib.sha256()
            n = 0
            with archive.extractfile(member) as src, temporary.open('wb') as out:
                for chunk in iter(lambda: src.read(1 << 20), b''):
                    out.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
            if n != member.size:
                raise ValueError('Truncated member')
            if target.exists():
                old = hashlib.sha256()
                with target.open('rb') as f:
                    for chunk in iter(lambda: f.read(1 << 20), b''):
                        old.update(chunk)
                if old.hexdigest() != h.hexdigest():
                    temporary.unlink()
                    raise ValueError('Refusing to replace different local data: ' + str(target))
                temporary.unlink()
            else:
                temporary.replace(target)
            observed[member.name] = {'bytes': n, 'sha256': h.hexdigest()}
    sources = json.loads((dest / 'SOURCE_FILES.json').read_text())
    mismatches = [row['path'] for row in sources
                  if observed.get(row['path']) != {'bytes': row['bytes'], 'sha256': row['sha256']}]
    report = {'source_files': len(sources), 'bytes': sum(x['bytes'] for x in sources),
              'verified_files': len(sources) - len(mismatches), 'mismatches': mismatches,
              'missing_remote_sources': json.loads((dest / 'MISSING_SOURCES.json').read_text())}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    if mismatches:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
