#!/usr/bin/env python3
"""Mount NEW Figure 9 quick-check images and verify reflink behavior and CoW isolation.

Writes only into the freshly built ext4.img/xfs.img/xfs_reflink.img. Run in a
private mount namespace: sudo unshare --mount --propagation private python3 ...
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
from build_xfs import sha256


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    a = p.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        p.error('requires Linux root')
    results = []
    for name in ['ext4', 'xfs', 'xfs_reflink']:
        image = a.directory / (name + '.img')
        if not image.is_file():
            p.error(f'missing {image}')
        with tempfile.TemporaryDirectory(prefix='.quick-check-', dir=a.directory) as tmp:
            mp = Path(tmp)
            opts = 'loop' if name == 'ext4' else 'loop,nouuid'
            subprocess.run(['mount', '-o', opts, str(image), tmp], check=True)
            try:
                source, clone = mp/'ae-original', mp/'ae-clone'
                with source.open('xb') as f:
                    f.write(b'AE-reflink-check\n' * 65536)
                before = sha256(source)
                result = subprocess.run(['cp', '--reflink=always', str(source), str(clone)], capture_output=True)
                expected = name == 'xfs_reflink'
                if (result.returncode == 0) != expected:
                    raise RuntimeError(f'{name}: unexpected reflink result {result.returncode}')
                if expected:
                    if sha256(clone) != before:
                        raise RuntimeError('clone content mismatch')
                    with clone.open('r+b') as f:
                        f.write(b'changed')
                    if sha256(source) != before:
                        raise RuntimeError('CoW isolation failed')
                results.append({'filesystem': name, 'reflink_supported': expected,
                                'cow_isolation_checked': expected, 'passed': True})
                source.unlink()
                clone.unlink(missing_ok=True)
            finally:
                subprocess.run(['umount', tmp], check=True)
    # Quick check writes change image hashes; retain a separate post-test manifest.
    report = {'tests': results, 'post_test_images': [
        {'file': p.name, 'sha256': sha256(p)} for p in sorted(a.directory.glob('*.img'))]}
    output = a.directory / 'quick-check-results.json'
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(output)


if __name__ == '__main__':
    main()
