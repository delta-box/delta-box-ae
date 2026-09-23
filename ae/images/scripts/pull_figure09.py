#!/usr/bin/env python3
"""Fetch the exact OCI references recorded by the Figure 9 inputs; record digests.

These images supply /testbed for the filesystem replay. No replacement of a
missing image with a different instance is performed.
"""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--instance', action='append', default=[])
    p.add_argument('--pull', action='store_true', help='without this, only print the plan')
    p.add_argument('--out', type=Path)
    a = p.parse_args()
    rows = json.loads((Path(__file__).resolve().parents[1] / 'configs/figure09-oci-inputs.json').read_text())
    ids = {r['instance'] for r in rows}
    if set(a.instance) - ids:
        p.error(f'unknown instances: {sorted(set(a.instance)-ids)}')
    names = sorted({r['image'] for r in rows if not a.instance or r['instance'] in a.instance})
    if not a.pull:
        print('\n'.join(names)); return
    if not a.out or a.out.exists() or a.out.is_symlink():
        p.error('--pull needs a NEW --out JSON file')
    a.out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        cp = subprocess.run(['docker', 'pull', '--platform=linux/amd64', name], check=False)
        item = {'requested': name, 'pull_returncode': cp.returncode}
        if cp.returncode == 0:
            image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', name]))[0]
            item.update(image_id=image['Id'], repo_digests=image.get('RepoDigests', []))
        results.append(item)
        a.out.write_text(json.dumps(results, indent=2) + '\n')
    if any(x['pull_returncode'] for x in results):
        raise SystemExit('Some original image references could not be fetched; see output JSON')


if __name__ == '__main__':
    main()
