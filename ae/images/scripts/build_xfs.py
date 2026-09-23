#!/usr/bin/env python3
"""Build paper image layouts on Linux. Never overwrite an existing output.

split: recover base/data images from the historical monolithic XFS image.
oci: export a newly built master OCI image into that same layout.
filesystems: create empty ext4/XFS variants for Figure 9.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

GROUPS = {
    'django': (9216, ['django']), 'sympy': (9216, ['sympy']),
    'sphinx': (10240, ['sphinx']),
    'sci': (18432, ['astropy', 'matplotlib', 'scikit-learn', 'xarray']),
    'tools': (10240, ['pytest', 'pylint', 'requests', 'flask', 'seaborn']),
}
# Bound DeltaBox cohorts use these four groups. Sphinx is a legacy optional disk.
DEFAULT_GROUPS = ['django', 'sympy', 'sci', 'tools']
EXCLUDE = ['opt', 'testbed', 'testbed_original_data', 'testbed_src', 'repos',
           'overlay_workspace', 'app', 'dev', 'proc', 'sys', 'tmp', 'run',
           'var/cache/apt/archives', 'var/lib/apt/lists', 'var/log', 'var/tmp',
           'root', 'home']


def run(*args, **kw):
    return subprocess.run([str(x) for x in args], check=True, **kw)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(4 << 20), b''):
            h.update(b)
    return h.hexdigest()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@contextmanager
def mounted(image, parent, readonly=False):
    mount = Path(tempfile.mkdtemp(prefix='.mount-', dir=parent))
    active = False
    try:
        opts = 'loop,ro,nouuid,norecovery' if readonly else 'loop,nouuid'
        run('mount', '-o', opts, image, mount)
        active = True
        yield mount
    finally:
        if active:
            run('umount', mount)
        mount.rmdir()


def new_image(path, size_mib, fs='xfs', reflink=True):
    if size_mib < 512:
        raise ValueError('use at least 512 MiB')
    with path.open('xb') as f:
        f.truncate(size_mib * 1024 * 1024)
    if fs == 'ext4':
        run('mkfs.ext4', '-q', '-F', path)
    else:
        # Match 4-KiB experiments; disable newer XFS features for the 6.8 guest.
        run('mkfs.xfs', '-q', '-f', '-b', 'size=4096', '-m',
            f'reflink={int(reflink)},crc=1,bigtime=0', path)


def copy_tree(src, dst, excludes=()):
    dst.mkdir(parents=True, exist_ok=True)
    run('rsync', '-aHAX', '--numeric-ids',
        *[f'--exclude=/{p}/***' for p in excludes], str(src) + '/', str(dst) + '/')


def configure_base(root, pubkey):
    for name in ['mnt/data', 'testbed', 'testbed_original_data', 'overlay_workspace',
                 'app', 'dev/shm', 'dev/pts', 'proc', 'sys', 'run', 'root/agentfs', 'tmp']:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / 'tmp').chmod(0o1777)
    for name, major, minor in [('null', 1, 3), ('zero', 1, 5), ('random', 1, 8), ('urandom', 1, 9)]:
        node = root / 'dev' / name
        if not node.exists():
            os.mknod(node, stat.S_IFCHR | 0o666, os.makedev(major, minor))
    opt = root / 'opt'
    if opt.is_symlink():
        opt.unlink()
    elif opt.exists():
        shutil.rmtree(opt)
    opt.symlink_to('/mnt/data/opt')
    fstab = root / 'etc/fstab'
    lines = fstab.read_text().splitlines() if fstab.exists() else []
    write(fstab, xfs_fstab(lines))
    # Runtime code is injected by the experiment runner, as in historical runs.
    write(root / 'etc/systemd/system/data-postmount.service', '''[Unit]
Description=Report data disk contents
RequiresMountsFor=/mnt/data
After=mnt-data.mount
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'test -d /mnt/data/testbeds && test -d /opt/miniconda3/envs/testbed'
[Install]
WantedBy=multi-user.target
''')
    write(root / 'etc/systemd/network/20-ae.network', '''[Match]
Name=eth0
[Network]
Address=172.16.0.2/24
Gateway=172.16.0.1
DNS=1.1.1.1
''')
    # Images have no historical users' keys or reusable password login.
    write(root / 'root/.ssh/authorized_keys', pubkey.read_text().strip() + '\n')
    (root / 'root/.ssh').chmod(0o700)
    (root / 'root/.ssh/authorized_keys').chmod(0o600)
    write(root / 'etc/ssh/sshd_config.d/00-ae.conf',
          'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n')
    for p in (root / 'etc/ssh').glob('ssh_host_*'):
        p.unlink()
    run('chroot', root, 'ssh-keygen', '-A')
    run('chroot', root, 'passwd', '-l', 'root')
    run('chroot', root, 'systemctl', 'enable', 'ssh', 'systemd-networkd', 'data-postmount')
    write(root / 'etc/machine-id', '')
    (root / 'var/lib/dbus/machine-id').unlink(missing_ok=True)
    run('chroot', root, 'criu', '--version')


def xfs_fstab(lines):
    # The actual historical XFS mother disk still declares an ext4 root.
    # Do not carry that stale entry into the newly formatted XFS guest.
    kept = []
    for line in lines:
        fields = line.split()
        if fields and not fields[0].startswith('#'):
            if len(fields) >= 2 and (fields[1] in ('/', '/mnt/data', '/mnt/swe_env')
                                     or fields[0] == '/dev/vdb'):
                continue
        kept.append(line)
    return '\n'.join(kept + ['/dev/vda / xfs defaults 0 0',
                             '/dev/vdb /mnt/data xfs ro,nouuid,noatime 0 0']) + '\n'


def split_tree(source, out, groups, pubkey, base_mib):
    conda = source / 'opt/miniconda3'
    testbeds = source / 'testbed'
    if not (conda / 'envs/testbed/bin/python').is_file() or not testbeds.is_dir():
        raise ValueError('source must be a multi-environment master with '
                         '/opt/miniconda3/envs/testbed and /testbed/<repo>__<version>; '
                         'an official SWE-bench per-instance OCI image is not this layout')
    plans = {}
    for group in groups:
        prefixes = GROUPS[group][1]
        envs = sorted(p.name for p in (conda / 'envs').iterdir()
                      if p.is_dir() and any(p.name.startswith(x + '__') for x in prefixes))
        if not envs:
            raise ValueError(f'no environments for {group}')
        for env in envs:
            if not (testbeds / env).is_dir():
                raise ValueError(f'missing testbed for {env}')
        plans[group] = envs
    image = out / 'base.xfs'
    new_image(image, base_mib)
    with mounted(image, out) as dst:
        copy_tree(source, dst, EXCLUDE)
        configure_base(dst, pubkey)
    for group in groups:
        image = out / f'data-{group}.xfs'
        new_image(image, GROUPS[group][0])
        with mounted(image, out) as dst:
            copy_tree(conda, dst / 'opt/miniconda3', ['envs'])
            for env in ['testbed'] + plans[group]:
                copy_tree(conda / 'envs' / env, dst / 'opt/miniconda3/envs' / env)
            for env in plans[group]:
                copy_tree(testbeds / env, dst / 'testbeds' / env)
            write(dst / 'MANIFEST.json', json.dumps({'group': group, 'specs': plans[group]}, indent=2))
    return plans


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ['split', 'oci']:
        p = sub.add_parser(name)
        p.add_argument('--source', required=True, help='master XFS file or master Docker image')
        p.add_argument('--out', type=Path, required=True, help='NEW directory, never an existing path')
        p.add_argument('--groups', nargs='+', choices=GROUPS, default=DEFAULT_GROUPS,
                       help='default: django sympy sci tools; add sphinx only for a bound workload')
        p.add_argument('--ssh-pubkey', type=Path, required=True)
        p.add_argument('--base-mib', type=int, default=3072)
    p = sub.add_parser('filesystems')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--size-mib', type=int, default=4096,
                   help='default matches recovered swesearch_replay.sh LOOP_MB=4096')
    args = ap.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        ap.error('requires Linux root (mount, mkfs and chroot); use a separate build host')
    if not os.environ.get('AE_PRIVATE_MOUNT_NS'):
        env = dict(os.environ, AE_PRIVATE_MOUNT_NS='1')
        os.execvpe('unshare', ['unshare', '--mount', '--propagation', 'private',
                             sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env)
    out = args.out.absolute()
    if out.exists() or out.is_symlink():
        ap.error(f'output already exists: {out}')
    pubkey = None
    if args.command != 'filesystems':
        pubkey = args.ssh_pubkey.resolve(strict=True)
        if not pubkey.read_text().startswith(('ssh-rsa ', 'ssh-ed25519 ', 'ecdsa-sha2-')):
            ap.error('--ssh-pubkey must be a public key, not a private key')
    out.mkdir(parents=True)
    metadata = {'command': args.command, 'status': 'incomplete'}
    write(out / 'build.json', json.dumps(metadata, indent=2))
    if args.command == 'filesystems':
        for name, fs, reflink in [('ext4', 'ext4', False), ('xfs', 'xfs', False),
                                 ('xfs_reflink', 'xfs', True)]:
            new_image(out / f'{name}.img', args.size_mib, fs, reflink)
    elif args.command == 'split':
        source = Path(args.source).resolve(strict=True)
        if not source.is_file():
            ap.error('source must be a regular image file')
        metadata['source'] = str(source)
        metadata['source_sha256'] = sha256(source)
        with mounted(source, out, readonly=True) as src:
            metadata['groups'] = split_tree(src, out, args.groups, pubkey, args.base_mib)
    else:
        inspect = json.loads(run('docker', 'image', 'inspect', args.source,
                                 capture_output=True, text=True).stdout)[0]
        metadata['source_image_id'] = inspect['Id']
        metadata['source_repo_digests'] = inspect.get('RepoDigests', [])
        cid = run('docker', 'create', inspect['Id'], '/bin/true', capture_output=True,
                  text=True).stdout.strip()
        try:
            with tempfile.TemporaryDirectory(prefix='.export-', dir=out) as stage:
                # docker cp exports without executing the container; -a preserves UID/GID.
                run('docker', 'cp', '-a', cid + ':/.' , stage)
                metadata['groups'] = split_tree(Path(stage), out, args.groups, pubkey, args.base_mib)
        finally:
            run('docker', 'rm', cid)
    metadata['artifacts'] = [{'file': p.name, 'bytes': p.stat().st_size, 'sha256': sha256(p)}
                             for p in sorted(out.iterdir()) if p.suffix in ['.xfs', '.img']]
    metadata['status'] = 'built-not-boot-tested'
    write(out / 'build.json', json.dumps(metadata, indent=2) + '\n')
    print(out / 'build.json')


if __name__ == '__main__':
    main()
