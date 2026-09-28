"""Own one nested L1 and eight fresh E2B bases for the fixed paper cohort.

This wrapper is entered only under the existing full-run and pinned resource
leases. Each input owns new guest storage. Historical snapshots stay untouched;
all transfers, base builds and verification lie outside inner event timers.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time
import uuid

from ae.scripts.e2b_l1_context import L1Config, owned_l1, trusted, digest, file_asset
from ae.scripts.e2b_paper_profile import verify_inputs, validate_effective, COHORT
from ae.scripts.e2b_paper_guest_probe import content_identity

REPO = Path('/home/atc-ae/delta-box-ae')
WORK = REPO / 'ae/work/e2b-paper-reproduction'
DEPLOYMENT = WORK / 'runtime-deployment-156g.json'
GUEST = '/opt/e2b-paper'
ENV = {'HOST_ENVD_PATH': GUEST+'/runtime/envd/envd',
       'HOST_BUSYBOX_DIR': GUEST+'/runtime/busybox', 'BUSYBOX_VERSION': '1.36.1',
       'FIRECRACKER_VERSIONS_DIR': GUEST+'/runtime/fc-versions',
       'HOST_KERNELS_DIR': GUEST+'/runtime/kernels',
       'ALLOW_SANDBOX_INTERNAL_CIDRS': '10.0.2.2/32',
       'DEFAULT_FIRECRACKER_VERSION': 'v1.14.1_458ca91'}


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(obj, stream, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def checked_deployment():
    raw = trusted(DEPLOYMENT).read_bytes()
    data = json.loads(raw)
    if data.get('schema_version') != 1 or data.get('kind') != 'e2b-paper-runtime-deployment-v1':
        raise ValueError('Unknown paper runtime deployment')
    l1 = file_asset(data['l1_manifest'])
    if l1 != WORK/'l1-measurement-assets-156g.json' or data.get('share_tag') != 'ae_runtime_v1':
        raise ValueError('Unexpected paper assets or share')
    if data.get('oci_manifest') != 'sha256:9da1d3aecd725a91d879bbc59e9872ed4cab3b98d21b802426a24f877d69ee12':
        raise ValueError('Offline OCI manifest differs from recovered layers')
    rows = data.get('files')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Empty runtime identity')
    seen = set()
    for row in rows:
        rel = Path(row['path'])
        if (rel.is_absolute() or '..' in rel.parts or str(rel) in seen
                or rel.parts[0] not in ('runtime', 'oci')
                or len(row.get('sha256','')) != 64):
            raise ValueError('Invalid runtime file identity')
        seen.add(str(rel))
    needed = {'runtime/bin/create-build', 'runtime/bin/resume-build', 'runtime/envd/envd',
              'runtime/fc-versions/v1.14.1_458ca91/amd64/firecracker',
              'runtime/kernels/vmlinux-6.1.158/amd64/vmlinux.bin',
              'runtime/busybox/1.36.1/amd64/busybox'}
    if not needed <= seen:
        raise ValueError('Missing fixed runtime dependency')
    return data, hashlib.sha256(raw).hexdigest(), l1


def ssh(vm, command, *, timeout=120, stdin=None, log=None):
    vm.verify()
    if log:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.with_suffix('.command').open('x') as out:
            out.write(command+'\n')
    try:
        cp = subprocess.run(vm._ssh_argv(command), input=stdin, text=True,
                            capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if log:
            for suffix, value in (('.stdout',error.stdout),('.stderr',error.stderr)):
                value=value or ''
                log.with_suffix(suffix).write_text(value.decode(errors='replace') if isinstance(value,bytes) else value)
            write(log.with_suffix('.timeout.json'),dict(timeout=timeout,partial_output=True))
        raise
    if log:
        log.with_suffix('.stdout').write_text(cp.stdout)
        log.with_suffix('.stderr').write_text(cp.stderr)
    if cp.returncode:
        raise RuntimeError('Owned L1 command failed with code %d; evidence=%s' % (cp.returncode, log))
    return cp.stdout


def guest_json(vm, source, *, log=None, timeout=1800):
    return json.loads(ssh(vm, 'sudo -n python3 -B -', stdin=source, timeout=timeout, log=log))


def setup_owned_forwarding(vm, evidence):
    """Prepare/verify IPv4 forwarding only inside this owned L1, before Go pools.

    The namespace probe checks the kernel's real inheritance policy without
    changing it. Only the UUID namespace successfully created here is removed.
    All operations and failures remain in the preparation log, outside timers.
    """
    namespace = 'e2b-paper-forward-' + uuid.uuid4().hex
    source = r'''import json,pathlib,subprocess,uuid
namespace=REPLACE_NAMESPACE
keys=('net.ipv4.ip_forward','net.ipv4.conf.all.forwarding',
      'net.ipv4.conf.default.forwarding','net.core.devconf_inherit_init_net')
proof={'scope':'owned L1 only; no host configuration','namespace':namespace,
       'status':'preparing','commands':[],'namespace_created':False,'namespace_removed':False}
def run(argv):
 try:
  cp=subprocess.run(argv,text=True,capture_output=True,timeout=20)
 except subprocess.TimeoutExpired as error:
  def text(value):
   return value.decode(errors='replace') if isinstance(value,bytes) else (value or '')
  proof['commands'].append({'argv':argv,'timeout':20,'stdout':text(error.stdout),'stderr':text(error.stderr)})
  raise
 proof['commands'].append({'argv':argv,'returncode':cp.returncode,'stdout':cp.stdout,'stderr':cp.stderr})
 if cp.returncode:
  raise RuntimeError('Forwarding command failed: '+repr(argv)+'; stderr='+cp.stderr)
 return cp.stdout
def read(prefix=()):
 selected=keys[:3] if prefix else keys
 values=run([*prefix,'sysctl','-n',*selected]).strip().splitlines()
 if len(values)!=len(selected) or any(not v.strip().isdigit() for v in values):
  raise ValueError('Malformed forwarding sysctl output')
 return dict(zip(selected,(int(v.strip()) for v in values)))
def verify(values,scope):
 if any(values[k]!=1 for k in keys[:3]):
  raise ValueError(scope+' IPv4 forwarding is not enabled: '+repr(values))
primary=None
cleanup_error=None
try:
 if pathlib.Path('/run/netns',namespace).exists():
  raise ValueError('Unique forwarding namespace already exists')
 proof['root_before']=read()
 run(['sysctl','-w','net.ipv4.ip_forward=1','net.ipv4.conf.default.forwarding=1'])
 proof['root_after']=read()
 verify(proof['root_after'],'Root')
 proof['namespace_add_attempted']=True
 run(['ip','netns','add',namespace])
 proof['namespace_created']=True
 proof['namespace_values']=read(('ip','netns','exec',namespace))
 verify(proof['namespace_values'],'New namespace')
except BaseException as error:
 primary=error
 proof['error']=type(error).__name__+': '+str(error)
finally:
 if proof['namespace_created']:
  try:
   run(['ip','netns','delete',namespace])
   if pathlib.Path('/run/netns',namespace).exists():
    raise RuntimeError('Owned forwarding namespace remains after delete')
   proof['namespace_removed']=True
  except BaseException as error:
   cleanup_error=error
   proof['cleanup_error']=type(error).__name__+': '+str(error)
 if proof.get('namespace_add_attempted') and not proof['namespace_created']:
  proof['uncertain_namespace_cleanup']='No unconfirmed name is deleted; owned L1 is torn down on this failed preparation'
 proof['status']='verified' if primary is None and cleanup_error is None else 'failed'
 print(json.dumps(proof,sort_keys=True),flush=True)
if primary is not None:
 raise primary
if cleanup_error is not None:
 raise cleanup_error
'''.replace('REPLACE_NAMESPACE', repr(namespace))
    proof = guest_json(vm, source, log=Path(evidence)/'forwarding-probe', timeout=180)
    required = ('net.ipv4.ip_forward', 'net.ipv4.conf.all.forwarding',
                'net.ipv4.conf.default.forwarding')
    if (not isinstance(proof, dict) or proof.get('status') != 'verified'
            or proof.get('namespace') != namespace
            or proof.get('namespace_created') is not True
            or proof.get('namespace_removed') is not True
            or any(type(proof.get(scope, {}).get(key)) is not int
                   or proof[scope][key] != 1
                   for scope in ('root_after', 'namespace_values') for key in required)):
        raise ValueError('Owned L1 forwarding proof is incomplete or failed')
    write(Path(evidence)/'forwarding.json', proof)
    return proof


def install_runtime(vm, data, evidence):
    """Copy frozen, read-only share to L1 disk and verify the complete tree."""
    script = '''set -euo pipefail
sudo -n mkdir -p /mnt/ae-runtime
sudo -n mount -t 9p -o trans=virtio,version=9p2000.L,ro ae_runtime_v1 /mnt/ae-runtime
sudo -n mkdir -p /opt/e2b-paper
sudo -n cp -a /mnt/ae-runtime/runtime /mnt/ae-runtime/oci /opt/e2b-paper/
sudo -n mkdir -p /opt/e2b-paper/runtime/packages/orchestrator
sudo -n modprobe nbd nbds_max=64
sudo -n sysctl -w vm.unprivileged_userfaultfd=1
sudo -n sync
'''
    ssh(vm, 'bash -s', stdin=script, log=evidence/'install-runtime', timeout=600)
    setup_owned_forwarding(vm, evidence)
    return verify_runtime(vm, data, evidence/'runtime-before.json')


def verify_runtime(vm, data, result):
    source = '''import hashlib,json,os,pathlib,stat
root=pathlib.Path('/opt/e2b-paper')
rows=REPLACE
assert root.is_dir() and not any(p.is_symlink() for p in (root,*root.parents)), 'runtime root symlink'
assert all((root/p).is_dir() and not (root/p).is_symlink() for p in ('runtime','oci')), 'runtime root member symlink'
paths=[p for part in ('runtime','oci') for p in (root/part).rglob('*')]
assert not any(p.is_symlink() for p in paths), 'runtime symlink'
seen={str(p.relative_to(root)) for p in paths if not p.is_dir()}
assert seen=={r['path'] for r in rows}, 'runtime file set differs'
for row in rows:
 p=root/row['path'];s=p.stat()
 assert not p.is_symlink() and stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 assert h.hexdigest()==row['sha256'], row['path']
print(json.dumps({'status':'verified','files':rows,'uname':list(os.uname())}))
'''.replace('REPLACE', repr(data['files']))
    proof = guest_json(vm, source, log=Path(result).with_suffix('.probe'))
    write(result, proof)
    return proof


def capture(vm, guest_root, builds, output):
    source = (REPO/'ae/scripts/e2b_paper_guest_probe.py').read_text()
    source = source[:source.index("if __name__ == '__main__':")]
    source += '\nprint(json.dumps(capture('+repr(guest_root+'/storage')+','+repr(builds)+'),sort_keys=True))\n'
    value = guest_json(vm, source, log=Path(output).with_suffix('.probe'), timeout=1800)
    write(output, value)
    return value


def create_command(storage, build, oci):
    flags = [GUEST+'/runtime/bin/create-build', '-template', 'e2b-paper-original-input',
             '-to-build', build, '-storage', storage, '-sandbox-dir', str(Path(storage).parent/'sandboxes'),
             '-firecracker', 'v1.14.1_458ca91', '-kernel', 'vmlinux-6.1.158',
             '-memory', '2048', '-vcpu', '1', '-disk', '4096', '-hugepages=false',
             '-timeout', '12', '-v', '-oci-layout', GUEST+'/oci', '-oci-manifest', oci]
    env = ['sudo', '-n', 'env', '-u', 'LAUNCH_DARKLY_API_KEY',
           *[k+'='+v for k,v in ENV.items()]]
    return 'cd /opt/e2b-paper/runtime/packages/orchestrator && '+shlex.join(env+flags)


def free_ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(('127.0.0.1',0)); second.bind(('127.0.0.1',0))
        return first.getsockname()[1], second.getsockname()[1]


def ready_config(original, transport_path, build, root, worker, index):
    value = json.loads(json.dumps(original))
    value['e2b'].update(execution='paper-nested-ready', transport_manifest=str(transport_path),
                        from_build=build, storage=root+'/storage',
                        worker_mock_port=worker, index_port=index)
    validate_effective(value, guest_ready=True)
    return value


SNAPSHOT_FILES = ('metadata.json', 'snapfile', 'memfile', 'memfile.header',
                  'rootfs.ext4', 'rootfs.ext4.header')


def validate_snapshot_closure(raw, storage, roots):
    """Validate the complete guest-probed V3 mapping closure, never a count alone.

    Files remain in the owned guest. The trusted preparation manifest binds
    their hashes and parsed header semantics; the suite repeats the real guest
    SHA/header probe after the input. A fresh create can contain several builds.
    """
    def ident(value):
        if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).int == 0:
            raise ValueError('Invalid snapshot build identity')
        return value
    if not isinstance(raw, dict) or raw.get('schema_version') != 1 or raw.get('status') != 'verified':
        raise ValueError('Snapshot closure is not verified')
    if (not isinstance(storage, str) or raw.get('storage') != storage
            or str(Path(storage)) != storage or '..' in Path(storage).parts):
        raise ValueError('Snapshot closure storage differs')
    if not isinstance(roots, list) or not roots or len(set(roots)) != len(roots):
        raise ValueError('Snapshot closure roots are empty or duplicated')
    roots = [ident(b) for b in roots]
    requested = raw.get('requested_builds')
    if (not isinstance(requested, list) or len(requested) != len(set(requested))
            or set(requested) != set(roots)):
        raise ValueError('Snapshot closure requested identities differ')
    builds, rows = raw.get('builds'), raw.get('files')
    if (not isinstance(builds, dict) or not isinstance(rows, list)
            or type(raw.get('build_count')) is not int or raw['build_count'] != len(builds)):
        raise ValueError('Snapshot closure build count or files differ')
    for build in builds:
        ident(build)
    expected = {str(Path(storage)/'templates'/b/name) for b in builds for name in SNAPSHOT_FILES}
    files = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get('path') not in expected
                or row['path'] in files or type(row.get('bytes')) is not int or row['bytes'] < 0
                or not isinstance(row.get('sha256'), str) or len(row['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in row['sha256'])):
            raise ValueError('Snapshot closure file identity is missing, duplicated or invalid')
        files[row['path']] = row
    if set(files) != expected:
        raise ValueError('Snapshot closure requires exactly six files per build')
    visited, pending = set(), list(roots)
    while pending:
        build = pending.pop()
        if build in visited:
            continue
        if build not in builds:
            raise ValueError('Snapshot closure is missing a referenced build')
        visited.add(build)
        detail = builds[build]
        template = detail.get('template', {})
        if (template.get('build_id') != build or template.get('firecracker_version') != 'v1.14.1_458ca91'
                or template.get('kernel_version') != 'vmlinux-6.1.158'):
            raise ValueError('Snapshot closure runtime or build identity differs')
        headers = detail.get('headers')
        if (not isinstance(headers, list) or len(headers) != 2
                or {h.get('file') for h in headers} != {'memfile.header', 'rootfs.ext4.header'}):
            raise ValueError('Snapshot closure must contain both headers per build')
        for header in headers:
            size = header.get('logical_bytes')
            if (header.get('build') != build or header.get('version') != 3
                    or header.get('block_size') != 4096 or type(size) is not int
                    or size <= 0 or size % 4096
                    or (header['file'] == 'memfile.header' and size != 2048*1024**2)):
                raise ValueError('Snapshot closure header resource/version/identity differs')
            refs = header.get('referenced_builds')
            if not isinstance(refs, dict):
                raise ValueError('Snapshot closure header has no mapping references')
            mapped = 0
            for parent, bound in refs.items():
                ident(parent)
                if (not isinstance(bound, dict) or any(type(bound.get(k)) is not int or bound[k] <= 0
                        or bound[k] % 4096 for k in ('mapped_bytes', 'required_file_bytes'))):
                    raise ValueError('Snapshot closure mapped extent is invalid')
                target = str(Path(storage)/'templates'/parent/header['file'].removesuffix('.header'))
                if target not in files or files[target]['bytes'] < bound['required_file_bytes']:
                    raise ValueError('Snapshot closure parent is missing or shorter than mapped extent')
                mapped += bound['mapped_bytes']
                pending.append(parent)
            if mapped > size:
                raise ValueError('Snapshot closure mapped bytes exceed logical size')
    if visited != set(builds):
        raise ValueError('Snapshot closure contains an unreferenced build')
    return {path: content_identity(row) for path, row in files.items()}


def completed_build_ids(output, fresh_ids, expected_actions, base):
    """Bind every new build to a successful raw root/action transport receipt."""
    output = Path(output)
    if output.resolve() != output:
        raise ValueError('Completed input path is not canonical')
    record = json.loads((output/'run.json').read_text())
    if record.get('status') != 'ok':
        raise ValueError('Completed input has no successful run record')
    result = record.get('result', {})
    rel = Path(result.get('path', ''))
    path = output/rel
    if (rel.is_absolute() or '..' in rel.parts or path.resolve() != path
            or not path.is_relative_to(output) or not path.is_file()):
        raise ValueError('Completed pilot escapes this input')
    content = path.read_bytes()
    if result.get('bytes') != len(content) or result.get('sha256') != hashlib.sha256(content).hexdigest():
        raise ValueError('Completed pilot identity changed')
    data = json.loads(content)
    actions = [step for row in data.get('iterations', []) for step in row.get('e2b_steps', [])]
    if len(actions) != expected_actions or data.get('n_e2b_steps') != expected_actions:
        raise ValueError('Completed input action count differs')
    steps = [data.get('root_setup', {})] + actions
    seen, created = set(fresh_ids), []
    for step in steps:
        build = step.get('to_build')
        if (step.get('ok') is not True or not isinstance(build, str)
                or str(uuid.UUID(build)) != build or uuid.UUID(build).int == 0 or build in seen):
            raise ValueError('Completed input reuses or omits a physical build identity')
        receipt_path = Path(step.get('transport_receipt', ''))
        if (not receipt_path.is_absolute() or receipt_path.resolve() != receipt_path
                or not receipt_path.is_relative_to(output)):
            raise ValueError('Completed transport receipt escapes this input')
        bound = [r for r in record.get('artifacts', []) if r.get('path') == str(receipt_path.relative_to(output))]
        receipt_bytes = receipt_path.read_bytes()
        if (len(bound) != 1 or bound[0].get('bytes') != len(receipt_bytes)
                or bound[0].get('sha256') != hashlib.sha256(receipt_bytes).hexdigest()):
            raise ValueError('Completed transport receipt identity changed')
        receipt = json.loads(receipt_bytes)
        if (receipt.get('kind') != 'ssh-files-outside-inner-timers' or receipt.get('returncode') != 0
                or receipt.get('transfer_errors') or receipt.get('to_build') != build
                or receipt.get('from_build') not in seen or (not created and receipt.get('from_build') != base)):
            raise ValueError('Completed transport build lineage differs')
        seen.add(build);created.append(build)
    return created


def validate_post_closure(before, after_base, all_builds, storage, base, created=None):
    """Keep every initial ancestor immutable and reject unaccounted new builds."""
    initial = validate_snapshot_closure(before, storage, [base])
    repeated = validate_snapshot_closure(after_base, storage, [base])
    if repeated != initial or after_base['builds'] != before['builds']:
        raise ValueError('Fresh base changed during input execution')
    roots = all_builds.get('requested_builds')
    all_files = validate_snapshot_closure(all_builds, storage, roots)
    if (any(all_files.get(p) != row for p, row in initial.items())
            or any(all_builds['builds'].get(b) != row for b, row in before['builds'].items())):
        raise ValueError('Fresh base changed in complete post-input closure')
    if created is not None:
        expected = set(before['builds']) | set(created)
        if (len(created) != len(set(created)) or set(created) & set(before['builds'])
                or set(all_builds['builds']) != expected or set(roots) != expected):
            raise ValueError('Snapshot count differs or identities differ from fresh closure+root+complete action contract')
    return {'initial_build_ids': sorted(before['builds']), 'created_build_ids': created,
            'all_build_ids': sorted(all_builds['builds'])}


def base_proof(instance, build, root, raw, runtime_sha):
    validate_snapshot_closure(raw, root+'/storage', [build])
    details = raw['builds'][build]
    memory = next(h for h in details['headers'] if h['file']=='memfile.header')
    disk = next(h for h in details['headers'] if h['file']=='rootfs.ext4.header')
    if memory['logical_bytes'] != 2048*1024**2 or raw['status'] != 'verified':
        raise ValueError('Fresh base memory or closure differs')
    return dict(status='verified', instance=instance, fresh_base_build_id=build,
                guest_storage=root+'/storage', runtime_manifest_sha=runtime_sha,
                created_unix=time.time(), fresh_for_input=True, reused=False,
                guest_proof=dict(snapshot_files=[dict(file=Path(r['path']).name,path=r['path'],bytes=r['bytes'],sha256=r['sha256']) for r in raw['files']],
                    header_closure_verified=True, vcpus=1, vcpus_evidence='fixed create command; independently observed at L2 root setup',
                    mem_mib=2048, requested_disk_mib=4096, actual_rootfs_bytes=disk['logical_bytes']),
                snapshot_closure=raw)


def run(path):
    from ae.scripts import run_review as review
    path = Path(path)
    plan = json.loads(path.read_text())
    config = json.loads(Path(plan['review_config']).read_text())
    inputs = verify_inputs(config)
    if (plan.get('workers',1) != 1 or len(plan['jobs']) != 8
            or [j.get('key') for j in plan['jobs']] != ['table-02-e2b__'+r[0] for r in COHORT]
            or any(j.get('reused_verified') and j.get('execution') != 'referenced-completed-measurement'
                   for j in plan['jobs'])):
        raise ValueError('Paper profile requires the ordered original eight measured or verified-reference inputs')
    referenced = [j for j in plan['jobs'] if j.get('reused_verified')]
    if referenced:
        from ae.repro.e2b_reuse import verify_referenced_job
        for job in referenced:
            verify_referenced_job(job, plan)
    if len(referenced) == len(plan['jobs']):
        return review.execute_plan(path, paper_context_ready=True)
    data, runtime_sha, l1_manifest = checked_deployment()
    output = Path(plan['review_output'])
    # Suite output itself is created exactly once by execute_plan.
    evidence = output.parent/'e2b-paper-l1'
    evidence.mkdir(parents=True, mode=0o700)
    write(evidence/'inputs.json',inputs)
    write(evidence/'runtime-deployment.json',data)
    cfg = L1Config(l1_manifest, WORK/'l1-work', Path('/home/dyp/.ssh/id_ed25519.pub'),
                   Path('/home/dyp/.ssh/id_ed25519'), readiness_timeout=600,stop_grace=60)
    state = dict(status='starting',purpose='paper-condition-reconstruction',started_unix=time.time(),
                 original_l1_configuration_unknown=True, deployment_sha256=runtime_sha,
                 measurement_plan=str(path),input_count=8,expected_actions=185,
                 measured_input_count=8-len(referenced),referenced_input_count=len(referenced),
                 expected_new_actions=sum(COHORT[i][5] for i,j in enumerate(plan['jobs']) if not j.get('reused_verified')))
    items = {}
    def save_state():
        tmp=evidence/'state.json.tmp';tmp.write_text(json.dumps(state,indent=2)+'\n');tmp.replace(evidence/'state.json')
    try:
        with owned_l1(cfg) as vm:
            state.update(status='running',lifecycle=str(vm.manifest_path),identity=vm.identity)
            save_state()
            install_runtime(vm,data,evidence)
            def before(index,job,active_plan,suite):
                vm.verify()
                instance=COHORT[index-1][0]
                row=evidence/instance;row.mkdir(mode=0o700)
                guest_root='/var/tmp/e2b-paper-'+uuid.uuid4().hex
                build=str(uuid.uuid4())
                host_output=suite/job['key']
                if host_output.exists():
                    raise ValueError('Fresh input output already exists')
                ssh(vm,'mkdir -m 700 -p '+shlex.quote(guest_root+'/storage'),log=row/'create-directory')
                command=create_command(guest_root+'/storage',build,data['oci_manifest'])
                started=time.time()
                ssh(vm,command,timeout=1800,log=row/'create-base')
                raw=capture(vm,guest_root,[build],row/'base-before.json')
                proof=base_proof(instance,build,guest_root,raw,runtime_sha)
                proof['create_command']=command;proof['create_started_unix']=started
                proofpath=row/'fresh-base.json';write(proofpath,proof)
                worker,idx=free_ports()
                transport=row/'transport.json'
                write(transport,dict(kind='e2b-paper-owned-transport-v1',host_output_root=str(host_output),
                    lifecycle=str(vm.manifest_path),qemu_identity=vm.identity,ssh_port=vm.ssh_port,
                    ssh_identity=str(cfg.ssh_identity),known_hosts=str(vm.folder/'known_hosts'),guest_root=guest_root,
                    instance=instance,fresh_base_build_id=build,fresh_base_manifest=str(proofpath),
                    worker_mock_port=worker,index_port=idx))
                effective=ready_config(config,transport,build,guest_root,worker,idx)
                config_path=row/'config.json';write(config_path,effective)
                cmd=job['command'];cmd[cmd.index('--config')+1]=str(config_path)
                job['paper_preparation']=dict(fresh_base_manifest=str(proofpath),sha256=digest(proofpath),
                    transport_manifest=str(transport),effective_config=str(config_path),guest_root=guest_root)
                items[index]=dict(instance=instance,row=row,root=guest_root,build=build,base=raw,output=host_output)
            def after(index,job,active_plan,suite):
                vm.verify()
                item=items[index]
                # Flush completed snapshots outside C/R timers before a probe can fail.
                ssh(vm,'sudo -n sync',log=item['row']/'sync')
                # Prove the old base did not change, including all six files.
                raw=capture(vm,item['root'],[item['build']],item['row']/'base-after.json')
                repeated = validate_snapshot_closure(raw, item['root']+'/storage', [item['build']])
                initial = validate_snapshot_closure(item['base'], item['root']+'/storage', [item['build']])
                if repeated != initial or raw['builds'] != item['base']['builds']:
                    raise ValueError('Fresh base changed during input execution')
                listing=guest_json(vm,"import json,pathlib\np=pathlib.Path("+repr(item['root']+'/storage/templates')+")\nprint(json.dumps(sorted(x.name for x in p.iterdir() if x.is_dir())))\n",log=item['row']/'list-builds')
                all_builds=capture(vm,item['root'],listing,item['row']/'all-builds-after.json')
                created = (completed_build_ids(item['output'], set(item['base']['builds']), COHORT[index-1][5], item['build'])
                           if job.get('status')=='ok' else None)
                identities = validate_post_closure(item['base'], raw, all_builds,
                    item['root']+'/storage', item['build'], created)
                verify_runtime(vm,data,item['row']/'runtime-after.json')
                job['paper_post_validation']=dict(status='verified',base_unchanged=True,
                    all_builds_manifest=str(item['row']/'all-builds-after.json'),
                    sha256=digest(item['row']/'all-builds-after.json'),build_count=all_builds['build_count'],
                    snapshot_identities=identities)
            result=review.execute_plan(path,paper_context_ready=True,paper_before_job=before,paper_after_job=after)
            state.update(status='completed' if result==0 else 'failed',returncode=result)
            save_state()
            return result
    except BaseException as error:
        state.update(status='failed',error=type(error).__name__+': '+str(error))
        if not (output/'suite.json').exists():
            output.mkdir(parents=True,exist_ok=True)
            failed=json.loads(json.dumps(plan))
            failed.update(status='failed',preparation_error=state['error'])
            for job in failed['jobs']:
                if not job.get('reused_verified'):
                    job.update(status='not-run',reason='Owned L1 preparation failed before measurement')
            write(output/'suite.json',failed)
        raise
    finally:
        state['finished_unix']=time.time();save_state()
