#!/usr/bin/env python3
"""Build a DeltaBox guest kernel and base/data disks using the supplied recipes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = Path(__file__).resolve().parent
INPUT_CHECKSUMS = SCRIPTS.parent / 'inputs-20260922.sha256'
sys.path.insert(0, str(REPO / 'ae'))
sys.path.insert(0, str(SCRIPTS))
from repro.common import install_termination_handler, write_json
from repro.process import execute
from build_xfs import DEFAULT_GROUPS, GROUPS


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--master-xfs', type=Path, help='Existing, idle mother XFS disk')
    source.add_argument('--source-image', help='Existing local multi-environment master OCI image (not a SWE-bench per-instance image)')
    source.add_argument('--miniconda', type=Path, help='Build master OCI from this pinned Linux x86-64 installer')
    p.add_argument('--miniconda-sha256', help='Publisher-verified installer checksum')
    p.add_argument('--image-tag', help='New local master image tag; required with --miniconda')
    p.add_argument('--env-specs', type=Path, help='Optional replacement for historical environment specs')
    kernel = p.add_mutually_exclusive_group(required=True)
    kernel.add_argument('--kernel-source', type=Path, help='Clean patched Linux 6.8 source directory; compile a new kernel')
    kernel.add_argument('--kernel', type=Path, help='Existing patched vmlinux; copy and hash without rebuilding')
    p.add_argument('--kernel-config', type=Path)
    p.add_argument('--groups', nargs='+', choices=GROUPS, default=DEFAULT_GROUPS,
                   help='Data groups to build; a subset supports only matching workloads')
    p.add_argument('--ssh-pubkey', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='New bundle directory; never overwritten')
    p.add_argument('--payload', type=Path, help='Prepared host baseline payload, not built here')
    p.add_argument('--moatless-venv', type=Path, help='Prepared host replay virtualenv, not built here')
    p.add_argument('--plan', action='store_true', help='Print commands only; do not build or create output')
    return p


def validate(args):
    # Preserve the final path component so dangling output symlinks are rejected too.
    args.output = args.output.absolute()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError(f'Refusing existing output: {args.output}')
    for name in ('kernel', 'kernel_source', 'kernel_config', 'ssh_pubkey', 'master_xfs', 'miniconda',
                 'env_specs', 'payload', 'moatless_venv'):
        value = getattr(args, name)
        if value is not None:
            value = value.resolve(strict=True)
            setattr(args, name, value)
            directory = name in ('kernel_source', 'payload', 'moatless_venv')
            if (directory and not value.is_dir()) or (not directory and not value.is_file()):
                raise ValueError(f'Invalid {name}: {value}')
    if args.kernel_source and not (args.kernel_source / 'fs/overlayfs').is_dir():
        raise ValueError('kernel-source must be the Linux source root, containing fs/overlayfs')
    if args.kernel:
        if args.kernel_config:
            raise ValueError('--kernel-config applies only to --kernel-source')
        with args.kernel.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                raise ValueError('--kernel must be an ELF vmlinux, not bzImage or a disk image')
    if args.source_image and 'swebench/sweb.eval.' in args.source_image.lower():
        raise ValueError('--source-image requires a multi-environment master, not an official '
                         'SWE-bench per-instance image; see ae/images/README.md')
    args.groups = list(dict.fromkeys(args.groups))
    if not args.ssh_pubkey.read_text().startswith(('ssh-rsa ', 'ssh-ed25519 ', 'ecdsa-')):
        raise ValueError('ssh-pubkey must be a public key (never supply a private key)')
    if args.miniconda:
        if not args.image_tag or not re.fullmatch(r'[a-fA-F0-9]{64}', args.miniconda_sha256 or ''):
            raise ValueError('--miniconda requires --image-tag and a verified --miniconda-sha256')
        digest = hashlib.sha256()
        with args.miniconda.open('rb') as stream:
            for block in iter(lambda: stream.read(4 << 20), b''):
                digest.update(block)
        if digest.hexdigest() != args.miniconda_sha256.lower():
            raise ValueError('Miniconda installer SHA-256 mismatch')
    elif any((args.image_tag, args.miniconda_sha256, args.env_specs)):
        raise ValueError('--image-tag, --miniconda-sha256 and --env-specs require --miniconda')


def verify_image_inputs(args):
    """Keep the documented existing-image checks inside the build entry point.

    The manifest selects the supplied mother disk and optional prebuilt kernel.
    Reconstructed OCI inputs and freshly compiled kernels have their own build
    records. The read-only --plan path does not scan whole disk images.
    """
    if args.master_xfs is None:
        return
    expected = {}
    for line in INPUT_CHECKSUMS.read_text().splitlines():
        digest, name = line.split()
        if not re.fullmatch(r'[a-fA-F0-9]{64}', digest) or name in expected:
            raise ValueError(f'Invalid image input manifest: {INPUT_CHECKSUMS}')
        expected[name] = digest.lower()
    inputs = [('ubuntu-24.04.xfs', args.master_xfs)]
    if args.kernel is not None:
        inputs.append(('vmlinux', args.kernel))
    for name, path in inputs:
        if name not in expected:
            raise ValueError(f'Image input manifest lacks {name}: {INPUT_CHECKSUMS}')
        print(f'Checking image input: {path}', flush=True)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(4 << 20), b''):
                digest.update(block)
        if digest.hexdigest() != expected[name]:
            raise ValueError(f'Image input differs from the selected AE version: {path}')


def commands(args):
    steps = []
    if args.miniconda:
        master = ['bash', str(SCRIPTS / 'build_master.sh'), str(args.miniconda),
                  args.miniconda_sha256, str(args.output / 'master'), args.image_tag]
        if args.env_specs:
            master.append(str(args.env_specs))
        steps.append(('master', master))
    kernel = ['bash', str(SCRIPTS / 'build_kernel.sh')]
    if args.kernel:
        kernel += ['--existing', str(args.kernel), str(args.output / 'kernel')]
    else:
        kernel += [str(args.kernel_source), str(args.output / 'kernel')]
    if args.kernel_config:
        kernel.append(str(args.kernel_config))
    steps.append(('kernel', kernel))
    privilege = [] if os.geteuid() == 0 else ['sudo', '-n', '-E']
    steps.append(('disks', [*privilege, sys.executable, str(SCRIPTS / 'build_xfs.py'),
                           'split' if args.master_xfs else 'oci', '--source',
                           str(args.master_xfs or args.source_image or args.image_tag),
                           '--out', str(args.output / 'disks'), '--ssh-pubkey', str(args.ssh_pubkey),
                           '--groups', *args.groups]))
    return steps


def authorize_disks():
    # Kernel / master builds can outlast sudo's credential cache. Refresh at the
    # terminal before redirecting the privileged stage's output to its log.
    if os.geteuid() != 0:
        # Hosted AE accounts can have NOPASSWD execution while sudo -v still
        # requires a password. Do not break their non-interactive SSH builds.
        probe = subprocess.run(['sudo', '-n', 'true'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return
        subprocess.run(['sudo', '-v'], check=True)


def run(args, steps):
    args.output.mkdir(parents=True, exist_ok=False)
    record = {'status': 'running', 'steps': [], 'scope': 'guest kernel and selected base/data disks',
              'groups': args.groups, 'kernel_mode': 'existing' if args.kernel else 'build'}
    manifest = args.output / 'bundle.json'
    write_json(manifest, record)
    try:
        for name, command in steps:
            log = args.output / 'logs' / name
            item = {'name': name, 'command': command, 'status': 'running',
                    'log': str(log / 'stdout.log')}
            record['steps'].append(item)
            write_json(manifest, record)
            print(f'[{name}] {log / "stdout.log"}', flush=True)
            try:
                if name == 'disks':
                    authorize_disks()
                result = execute(command, log, cwd=REPO, timeout=7 * 24 * 3600)
                item.update(status=result['status'], returncode=result.get('returncode'))
                if result.get('error'):
                    item['error'] = result['error']
            except BaseException as error:
                item.update(status='failed', error=f'{type(error).__name__}: {error}')
                raise
            finally:
                write_json(manifest, record)
            if item['status'] != 'ok':
                raise RuntimeError(f'{name} failed; see {log / "stdout.log"}')
        required = ['kernel/vmlinux', 'kernel/artifacts.sha256', 'disks/build.json', 'disks/base.xfs']
        required += [f'disks/data-{group}.xfs' for group in args.groups]
        for path in required:
            if not (args.output / path).is_file():
                raise RuntimeError(f'Build did not produce {path}')
        config = json.loads((REPO / 'ae/configs/example.json').read_text())
        config.update(kernel=str(args.output / 'kernel/vmlinux'), base_xfs=str(args.output / 'disks/base.xfs'),
                      images_dir=str(args.output / 'disks'),
                      deltafs=str(args.kernel_source.parent) if args.kernel_source else '${AE_DELTAFS}',
                      payload=str(args.payload) if args.payload else '${AE_PAYLOAD}',
                      moatless_venv=str(args.moatless_venv) if args.moatless_venv else '${AE_MOATLESS_VENV}',
                      measurement={'pin': False})
        config['image_groups'] = args.groups
        write_json(args.output / 'config.json', config)
        record.update(status='ok', config=str(args.output / 'config.json'))
    except BaseException as error:
        record.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        write_json(manifest, record)
    print(f'IMAGES BUILT: {args.output / "config.json"}\nRun Kick the Tires to validate boot and C/R; baseline services are separate.')


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        validate(args)
        steps = commands(args)
        if args.plan:
            print(json.dumps(dict(steps=steps), indent=2))
            return 0
        if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'AMD64'):
            p.error('Image construction requires a Linux x86-64 host with root privileges')
        verify_image_inputs(args)
        run(args, steps)
    except KeyboardInterrupt:
        print('Build interrupted; partial output and logs are retained', file=sys.stderr)
        return 130
    except Exception as error:
        print(f'Image build failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    install_termination_handler()
    raise SystemExit(main())
