#!/usr/bin/env python3
"""Prepare an independent legacy-workload Python; never runs during measurement."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/20251031/'
       'cpython-3.9.25%2B20251031-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz')
SHA256 = 'fe04e8b27bd69ca2144fc542428f8b9b5287b6a2e45516a4acfe2c2bc3102773'
LOCK = Path(__file__).resolve().parents[1] / 'configs/requests-test-python39-requirements.txt'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True, help='Fresh, independent Linux x86_64 installation directory')
    parser.add_argument('--archive', type=Path, help='Optional offline copy of the pinned official archive')
    parser.add_argument('--wheelhouse', type=Path, help='Use only these local wheels; no package index access')
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    if sys.platform != 'linux':
        raise RuntimeError('The pinned interpreter requires Linux x86_64; download/copy archives separately on other hosts')
    import platform
    if platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('The pinned interpreter requires x86_64')
    if prefix.exists():
        raise FileExistsError(f'Refusing to overwrite an existing installation: {prefix}')
    prefix.parent.mkdir(parents=True, exist_ok=True)
    archive = args.archive.resolve() if args.archive else prefix.with_suffix('.tar.gz')
    if not archive.exists():
        if args.archive:
            raise FileNotFoundError(archive)
        partial = archive.with_suffix(archive.suffix + '.partial')
        with urllib.request.urlopen(URL, timeout=60) as source, partial.open('xb') as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        partial.replace(archive)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != SHA256:
        raise RuntimeError(f'Python archive checksum mismatch: {actual}')
    prefix.mkdir()
    subprocess.run(['tar', '-xzf', str(archive), '--strip-components=1', '-C', str(prefix)], check=True)
    python = prefix / 'bin/python3.9'
    command = [str(python), '-m', 'pip', 'install', '--require-hashes', '-r', str(LOCK)]
    if args.wheelhouse:
        command += ['--no-index', '--find-links', str(args.wheelhouse.resolve())]
    subprocess.run(command, check=True)
    identity = subprocess.check_output([str(python), '-c',
        'import json,sys,importlib.metadata as m; print(json.dumps({"python":sys.version,'
        '"packages":sorted((d.metadata["Name"],d.version) for d in m.distributions())}))'], text=True)
    report = {'archive_url': URL, 'archive_sha256': actual,
              'requirements_sha256': hashlib.sha256(LOCK.read_bytes()).hexdigest(),
              'identity': json.loads(identity), 'purpose': 'legacy Requests functional test environment; separate from Moatless'}
    (prefix / 'ae-environment.json').write_text(json.dumps(report, indent=2) + '\n')
    print(python)


if __name__ == '__main__':
    main()
