#!/usr/bin/env python3
"""Reconstruct the FC-Diff+dm rootfs injection from finalbench/fc_diff_dm.

Keep historical absolute GUEST paths (the replay venv/scripts embed them).
HOST inputs are explicit. Supply a Python 3.11 prefix from a compatible Ubuntu
userspace; chroot validation checks that the copied venv can actually start.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
from build_xfs import mounted, run, sha256, write, copy_tree

GUEST_ROOT = Path('mnt/disk2/dyp')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ['base', 'payload', 'venv', 'driver', 'python-root', 'out']:
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--instance', required=True)
    p.add_argument('--size-gib', type=int, default=8)
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+-\d+', a.instance):
        p.error('invalid instance id')
    if sys.platform != 'linux' or os.geteuid() != 0:
        p.error('requires Linux root')
    if not os.environ.get('AE_PRIVATE_MOUNT_NS'):
        os.execvpe('unshare', ['unshare', '--mount', '--propagation', 'private', sys.executable,
                             str(Path(__file__).resolve()), *sys.argv[1:]],
                   dict(os.environ, AE_PRIVATE_MOUNT_NS='1'))
    if a.out.exists() or a.out.is_symlink():
        p.error('output directory already exists')
    pairs = [(a.venv, GUEST_ROOT / 'moatless_det_venv'),
             (a.payload / 'moatless-det-src', GUEST_ROOT / 'spr_payload/moatless-det-src'),
             (a.payload / 'repos' / ('swe-bench_' + a.instance), GUEST_ROOT / 'spr_payload/repos' / ('swe-bench_' + a.instance)),
             (a.payload / 'index_store' / a.instance, GUEST_ROOT / 'spr_payload/index_store' / a.instance),
             (a.payload / 'det_traces/ms' / a.instance, GUEST_ROOT / 'spr_payload/det_traces/ms' / a.instance),
             (a.driver, Path('root/guest_controller_driver.py'))]
    pairs += [(a.payload / n, GUEST_ROOT / 'spr_payload' / n)
              for n in ['mock_llm_server.py', 'protocol.py', 'replay_driver.py', 'trajectory_index.py']]
    pairs += [(a.python_root / n, Path(n)) for n in
              ['usr/bin/python3.11', 'usr/lib/python3.11', 'usr/lib/x86_64-linux-gnu/libpython3.11.so.1.0']]
    for src, _ in pairs:
        if not src.exists():
            p.error(f'missing input: {src}')
    a.out.mkdir(parents=True)
    image = a.out / 'fc-dm.xfs'
    if a.base.stat().st_size > a.size_gib * (1 << 30):
        p.error('requested image size is smaller than base')
    run('cp', '--reflink=auto', '--sparse=always', a.base, image)
    with image.open('r+b') as f:
        f.truncate(a.size_gib * (1 << 30))
    with mounted(image, a.out) as root:
        run('xfs_growfs', root)
        for src, rel in pairs:
            dst = root / rel
            if src.is_dir():
                copy_tree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        lib = root / 'usr/lib/x86_64-linux-gnu'
        for name, target in [('libpython3.11.so', 'libpython3.11.so.1'), ('libpython3.11.so.1', 'libpython3.11.so.1.0')]:
            (lib / name).unlink(missing_ok=True)
            (lib / name).symlink_to(target)
        link = root / 'var/lib/finalbench/det_mock_traces/qwen3-coder-30b-ms' / a.instance
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to('/mnt/disk2/dyp/spr_payload/det_traces/ms/' + a.instance)
        write(root / 'etc/systemd/system/finalbench-fc-dm.service', f'''[Unit]
Description=FC-Diff+dm replay controller
After=network.target
[Service]
Type=simple
Environment=INSTANCE_ID={a.instance}
Environment=CONTROL_PORT=18080
Environment=MOCK_PORT=19999
Environment=MOCK_TRACES_ROOT=/var/lib/finalbench/det_mock_traces
Environment=PYTHONHASHSEED=0
Environment=OPENAI_API_KEY=dummy
Environment=PYTHONPATH=/mnt/disk2/dyp/spr_payload:/mnt/disk2/dyp/spr_payload/moatless-det-src
ExecStart=/mnt/disk2/dyp/moatless_det_venv/bin/python /root/guest_controller_driver.py
StandardOutput=journal+console
StandardError=journal+console
Restart=no
[Install]
WantedBy=multi-user.target
''')
        run('chroot', root, 'systemctl', 'enable', 'finalbench-fc-dm')
        run('chroot', root, '/mnt/disk2/dyp/moatless_det_venv/bin/python', '-c',
            'import ssl, json, requests; print("replay interpreter OK")')
    write(a.out / 'build.json', json.dumps({'instance': a.instance, 'base_sha256': sha256(a.base),
          'image_sha256': sha256(image), 'driver_sha256': sha256(a.driver),
          'status': 'built-chroot-checked-not-boot-tested'}, indent=2) + '\n')


if __name__ == '__main__':
    main()
