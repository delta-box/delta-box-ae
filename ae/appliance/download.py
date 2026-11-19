#!/usr/bin/env python3
"""Download and verify the baseline image and its DeltaBox boot layer."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

RECORD = 24358847
BASE_SHA256 = '5b7455ebaa2d5bec7380642c1aa25147a55847252bbbcecfdb89882aa2801191'
BASE = 'DeltaBox-ATC2026-v3-l1.qcow2'
LAYER = 'deltabox-direct.qcow2'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch(name):
    url = f'https://zenodo.org/records/{RECORD}/files/{name}?download=1'
    return urllib.request.urlopen(url, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    dest = args.output_dir.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    base = dest / BASE
    if base.exists():
        if digest(base) != BASE_SHA256:
            raise RuntimeError('Existing base image checksum differs')
        print('Base image verified')
    else:
        subprocess.run([sys.executable, str(Path(__file__).with_name('download_appliance.py')),
                        '--output-dir', str(dest), '--jobs', str(args.jobs), '--extract'], check=True)
    with fetch('DIRECT-APPLIANCE.json') as response:
        metadata = json.load(response)
    if metadata['record_id'] != RECORD or metadata['base']['sha256'] != BASE_SHA256:
        raise RuntimeError('Unexpected appliance manifest')
    expected = metadata['layer']
    if expected['name'] != LAYER:
        raise RuntimeError('Unexpected image layer name')
    layer = dest / LAYER
    if layer.exists():
        if layer.stat().st_size != expected['size'] or digest(layer) != expected['sha256']:
            raise RuntimeError('Existing DeltaBox layer checksum differs')
        print('DeltaBox layer already verified')
    else:
        partial = dest / (LAYER + '.partial')
        with fetch(LAYER) as source, partial.open('wb') as target:
            shutil.copyfileobj(source, target, 2 << 20)
        if partial.stat().st_size != expected['size'] or digest(partial) != expected['sha256']:
            raise RuntimeError('Downloaded DeltaBox layer failed checksum')
        partial.replace(layer)
    (dest / 'DIRECT-APPLIANCE.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print('Verified environment:', dest)


if __name__ == '__main__':
    main()
