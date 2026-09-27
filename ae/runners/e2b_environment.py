"""Explicit E2B execution placement and external implementation evidence."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from repro.common import configured_path, configured_value, file_record

SNAPSHOT_FILES = ('metadata.json', 'snapfile', 'memfile', 'memfile.header',
                  'rootfs.ext4', 'rootfs.ext4.header')


def _isolated_file(storage, path):
    """Recheck isolation after execution too, including replaced parent directories."""
    path = Path(path)
    if (not path.is_absolute() or not path.is_relative_to(storage)
            or path.resolve() != path or path.stat().st_nlink != 1):
        raise ValueError(f'E2B snapshot input is not an independent file inside isolated storage: {path}')


def _parent_dependencies(manifest, storage, base):
    if (manifest.get('schema_version') != 1 or manifest.get('status') != 'verified'
            or manifest.get('independent_copies') is not True
            or manifest.get('source_unchanged') is not True
            or Path(manifest.get('destination', '')).resolve() != storage
            or manifest.get('base_build') != base):
        raise ValueError('E2B parent manifest does not identify this isolated base/storage')
    headers = manifest.get('headers')
    if not isinstance(headers, dict) or base not in headers:
        raise ValueError('E2B parent manifest has no base header closure')
    visited, pending = set(), [base]
    while pending:
        build = pending.pop()
        if build in visited:
            continue
        if build not in headers:
            raise ValueError('E2B parent manifest omits a referenced ancestor')
        visited.add(build)
        rows = headers[build]
        if (not isinstance(rows, list) or len(rows) != 2
                or {row.get('file') for row in rows} != {'memfile.header', 'rootfs.ext4.header'}
                or any(row.get('build') != build for row in rows)):
            raise ValueError('E2B parent manifest has incomplete build headers')
        for header in rows:
            refs = header.get('referenced_builds')
            if not isinstance(refs, dict):
                raise ValueError('E2B parent manifest has invalid header references')
            pending.extend(parent for parent in refs if parent != '00000000-0000-0000-0000-000000000000')
    if visited != set(headers) or manifest.get('build_count') != len(visited):
        raise ValueError('E2B parent manifest build closure is inconsistent')
    dependencies, seen = [], set()
    rows = manifest.get('files')
    if not isinstance(rows, list):
        raise ValueError('E2B parent manifest contains no files')
    for row in rows:
        relative = Path(row['relative'])
        target = storage / relative
        if relative.is_absolute() or '..' in relative.parts or not target.resolve().is_relative_to(storage):
            raise ValueError('E2B parent manifest escapes isolated storage')
        if relative in seen:
            raise ValueError('E2B parent manifest repeats an input file')
        seen.add(relative)
        if (type(row.get('bytes')) is not int or row['bytes'] < 0
                or not re.fullmatch('[0-9a-f]{64}', str(row.get('sha256')))):
            raise ValueError('E2B parent manifest has invalid input identity')
        _isolated_file(storage, target)
        dependencies.append({'path': str(target), 'bytes': row['bytes'], 'sha256': row['sha256']})
    required = {Path('templates') / build / name for build in visited for name in SNAPSHOT_FILES}
    if {path for path in seen if path.parts[0] == 'templates'} != required:
        raise ValueError('E2B parent manifest omits required snapshot files')
    if (manifest.get('total_bytes') != sum(row['bytes'] for row in rows)
            or manifest.get('snapshot_bytes') != sum(row['bytes'] for row in rows
                if Path(row['relative']).parts[0] == 'templates')):
        raise ValueError('E2B parent manifest input byte totals are inconsistent')
    return dependencies


def verify_snapshot_inputs(evidence):
    """Check the recorded parent copies, never mutable child builds, outside timing."""
    if evidence.get('profile') == 'paper-nested':
        from ae.scripts.e2b_paper_profile import _root_read
        from ae.scripts.e2b_paper_transport import Transport
        for name in ('transport_manifest', 'fresh_base_manifest'):
            expected = evidence[name]
            raw = _root_read(expected['path'])
            if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                raise ValueError('Owned paper transport/fresh-base evidence changed')
        Transport(Path(evidence['transport_manifest']['path'])).guard()
        return
    for expected in evidence.get('snapshot_dependencies', evidence.get('base_build', [])):
        if 'snapshot_dependencies' in evidence:
            _isolated_file(Path(evidence['storage']), Path(expected['path']))
        actual = file_record(Path(expected['path']))
        if any(actual[key] != expected[key] for key in ('bytes', 'sha256')):
            raise ValueError(f'E2B snapshot input changed: {expected["path"]}')


def configure_paper(config, env, *, instance):
    """Bind only a fresh base and the live L1 owned by this serial suite."""
    from ae.scripts.e2b_paper_profile import validate_effective, _root_read
    from ae.scripts.e2b_paper_transport import Transport
    validate_effective(config, guest_ready=True)
    chosen = config['e2b']
    if chosen.get('execution') != 'paper-nested-ready':
        raise ValueError('Paper E2B requires the suite-owned ready guest context')
    manifest = Path(chosen['transport_manifest'])
    # Root-owned complete manifest and live ancestry are both required.
    _root_read(manifest)
    transport = Transport(manifest)
    d = transport.manifest
    if (not instance or d.get('instance') != instance
            or chosen.get('from_build') != d.get('fresh_base_build_id')
            or chosen.get('storage') != transport.storage):
        raise ValueError('Paper fresh base, input or isolated guest storage mismatch')
    fresh_path = Path(d['fresh_base_manifest'])
    fresh = json.loads(_root_read(fresh_path))
    proof = fresh.get('guest_proof')
    if (fresh.get('status') != 'verified' or fresh.get('instance') != instance
            or fresh.get('fresh_base_build_id') != chosen['from_build']
            or fresh.get('guest_storage') != transport.storage
            or not re.fullmatch('[0-9a-f]{64}', str(fresh.get('runtime_manifest_sha')))
            or not isinstance(proof, dict) or proof.get('header_closure_verified') is not True
            or type(proof.get('vcpus')) is not int or proof['vcpus'] != 1
            or type(proof.get('mem_mib')) is not int or proof['mem_mib'] != 2048
            or proof.get('requested_disk_mib') != 4096
            or type(proof.get('actual_rootfs_bytes')) is not int or proof['actual_rootfs_bytes'] <= 0):
        raise ValueError('Paper fresh-base source/resources/header closure proof is incomplete')
    from ae.scripts.e2b_paper_suite import validate_snapshot_closure
    closure = fresh.get('snapshot_closure')
    files = validate_snapshot_closure(closure, transport.storage, [chosen['from_build']])
    rows = proof.get('snapshot_files', [])
    expected = {path: dict(file=Path(path).name, path=path, bytes=row['bytes'], sha256=row['sha256'])
                for path, row in files.items()}
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or len(rows) != len(expected) or len({r.get('path') for r in rows}) != len(rows)
            or any(r != expected.get(r.get('path')) for r in rows)):
        raise ValueError('Paper fresh base must bind all six real snapshot files per closure build')
    root_headers = closure['builds'][chosen['from_build']]['headers']
    disk = next(h for h in root_headers if h['file'] == 'rootfs.ext4.header')
    if proof['actual_rootfs_bytes'] != disk['logical_bytes']:
        raise ValueError('Paper fresh-base disk resource proof differs from its header')
    # Common module import still expects its historical SSH environment, but
    # the paper driver immediately installs the owned transport before use.
    for name in list(env):
        if name.startswith('E2B_') or name == 'AE_E2B_PAPER_TRANSPORT':
            env.pop(name)
    env.update(AE_E2B_PAPER_TRANSPORT=str(manifest),
               E2B_EXECUTION='ssh', E2B_L1_KEY=str(transport.key),
               E2B_L1_HOST='ubuntu@127.0.0.1', E2B_L1_SSH_PORT=str(transport.port),
               E2B_INFRA=transport.runtime,
               E2B_REMOTE_PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               E2B_SIDECAR_IP_FOR_SANDBOX='10.0.2.2')
    return {'profile': 'paper-nested', 'execution': 'owned-nested-ssh',
            'storage': transport.storage, 'from_build': chosen['from_build'],
            'transport_manifest': file_record(manifest),
            'fresh_base_manifest': file_record(fresh_path), 'fresh_base_proof': fresh,
            'snapshot_verification_scope': 'root-owned preproof plus suite-owned post-input guest SHA/header recheck',
            'sidecar_ip': '10.0.2.2'}


def configure(config, env, *, instance=None):
    if config.get('e2b', {}).get('profile') == 'paper-nested':
        return configure_paper(config, env, instance=instance)
    chosen = config['e2b']
    mode = chosen.get('execution', 'ssh')
    if mode not in ('ssh', 'local'):
        raise ValueError('e2b.execution must be ssh or local')
    infra = configured_path(config, 'e2b.infra')
    env.update(E2B_EXECUTION=mode, E2B_INFRA=str(infra),
               E2B_REMOTE_PATH=str(configured_value(config, 'e2b.remote_path')))
    if mode == 'ssh':
        env.update(E2B_L1_KEY=str(configured_path(config, 'e2b.ssh_key')),
                   E2B_L1_HOST=str(configured_value(config, 'e2b.ssh_host')),
                   E2B_L1_SSH_PORT=str(chosen.get('ssh_port', 56555)))
    else:
        # Never inherit a stale L1 key/endpoint or guess a routable guest address.
        for name in ('E2B_L1_KEY', 'E2B_L1_HOST', 'E2B_L1_SSH_PORT'):
            env.pop(name, None)
        configured_value(config, 'e2b.sidecar_ip')
        env['HOST_BUSYBOX_DIR'] = str(infra / 'packages/orchestrator/.busybox')
        env['NBD_POOL_SIZE'] = str(chosen.get('nbd_pool_size', 8))
    for key, variable in (('sidecar_ip', 'E2B_SIDECAR_IP_FOR_SANDBOX'),
                          ('sandbox_dir', 'E2B_SANDBOX_DIR'),
                          ('gocache', 'E2B_GOCACHE'), ('gomodcache', 'E2B_GOMODCACHE'),
                          ('resume_binary', 'E2B_RESUME_BINARY')):
        env.pop(variable, None)
        if chosen.get(key):
            env[variable] = str(configured_path(config, 'e2b.' + key).resolve()
                                if mode == 'local' and key != 'sidecar_ip'
                                else configured_value(config, 'e2b.' + key))
    storage = str(configured_path(config, 'e2b.storage').resolve()
                  if mode == 'local' else configured_value(config, 'e2b.storage'))
    evidence = {'execution': mode, 'infra': str(infra),
                'storage': storage,
                'sidecar_ip': env.get('E2B_SIDECAR_IP_FOR_SANDBOX', '10.0.2.2')}
    for name, args in (('commit', ['rev-parse', 'HEAD']), ('status', ['status', '--porcelain'])):
        evidence[name] = subprocess.check_output(['git', '-C', str(infra), *args], text=True).strip()
    diff = subprocess.check_output(['git', '-C', str(infra), 'diff', 'HEAD', '--binary'])
    evidence['dirty_diff_sha256'] = hashlib.sha256(diff).hexdigest()
    source = infra / 'packages/orchestrator/cmd/resume-build'
    evidence['resume_sources'] = [file_record(p) for p in sorted(source.glob('*.go'))]
    if chosen.get('resume_binary'):
        binary = configured_path(config, 'e2b.resume_binary')
        if mode == 'local' and not os.access(binary, os.X_OK):
            raise ValueError(f'E2B resume binary is not executable: {binary}')
        evidence['resume_binary'] = file_record(binary) if mode == 'local' else {'remote_path': str(binary)}
    if chosen.get('phase_probe_manifest'):
        path = configured_path(config, 'e2b.phase_probe_manifest')
        probe = json.loads(path.read_text())
        if probe.get('binary_sha256') != evidence.get('resume_binary', {}).get('sha256'):
            raise ValueError('E2B phase probe manifest does not match the executable')
        env['DELTABOX_E2B_PHASES'] = '1'
        evidence['phase_probe_manifest'] = file_record(path)
        evidence['phase_protocol'] = 'completed OTel spans; post-timer serialization'
    if mode == 'local':
        build = Path(storage) / 'templates' / configured_value(config, 'e2b.from_build')
        evidence['base_build'] = [file_record(build / name) for name in SNAPSHOT_FILES]
        evidence['nbd_pool_size'] = int(env['NBD_POOL_SIZE'])
        if chosen.get('parent_manifest'):
            path = configured_path(config, 'e2b.parent_manifest')
            manifest = json.loads(path.read_text())
            evidence['parent_manifest'] = file_record(path)
            evidence['snapshot_dependencies'] = _parent_dependencies(
                manifest, Path(storage), configured_value(config, 'e2b.from_build'))
            verify_snapshot_inputs(evidence)
    return evidence
