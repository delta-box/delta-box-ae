"""Fixed, explicitly selected reconstruction of the paper's nested E2B cohort.

The deployment manifest and original recordings are local dependencies, not
repository data. This profile never changes default AE inputs or E2B execution.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat

PROFILE = 'paper-nested'
EXPERIMENT = 'table-02-e2b'
ROOT_CONFIG = Path('/etc/deltabox-ae/review.json')
MANIFEST = Path('/etc/deltabox-ae/e2b-paper-inputs.json')
INPUT_ROOT = Path('/home/atc-ae/delta-box-ae/ae/work/e2b-paper-reproduction/historical-inputs')
CONTRACT_SHA256 = 'b0a78ab37d3da21cf6aee51c23dab12e92e518d5d446b7f999f3e7b3ba88ae37'
# Only immutable identifiers and digests are public. No recordings or ledger.
COHORT = (
 ('django__django-10914','a0fdceca98bfafc35c13212b4374254ab7fc5af6235cd0ef67cd8c24e0da57cb','adaa88802b5fe2b61c12827fbda4393507dd872bf56527ba803b3ffbd677b897','e7fd69d051eaa67cb17f172a39b57253e9cb831a',29,26),
 ('django__django-12915','6f9ca2e51a2c792e36f434680bd9306ba902e0378d4d68c8b5918f6f42c83ff9','9de16c3e328d8f04d09ff6cd8bc3497fa698a5d4230b6ccac0900ce715dab9b9','4652f1f0aa459a7b980441d629648707c32e36bf',24,14),
 ('sympy__sympy-12454','c6ad4803ae94b21a9640fd586d5971006b594f985b63bbc8065a007336ec51b6','2d55eb79c095a562feafbf90147c3d8e3ac75eb4242eba1cceef0ce536558c9c','d3fcdb72bfcbb560eb45264ac1c03f359436edef',29,22),
 ('sympy__sympy-13177','f3a62cdd9078429df22362e1a133d818eb1768a6f85414e101ec3f83f589db55','21b7de296525189dc2b3d8d3b65a966ed02b3ece8f15055c778a63df1e5f85ba','662cfb818e865f580e18b59efbb3540c34232beb',29,22),
 ('astropy__astropy-14309','24d1bf809cdf94146596d1119c247b1cfdf02d4017559a22c1f0809babfe33ff','262e9b9c23bf480bf0999ced9d720036676d26f1ec0cf5564f43130e9e2fd3b3','cdb66059a2feb44ee49021874605ba90801f9986',29,22),
 ('matplotlib__matplotlib-18869','5cad00d79b98556283ee694bddc1d0799c94509f24cb74f8b020c12113d3fcae','58b27e81b264fc62cc2eba28f877360b70e91f8c497c9cac84464e19855c5be6','b7d05919865fc0c37a0164cf467d5d5513bd0ede',29,28),
 ('pytest-dev__pytest-5221','6f4ad08bccf2318937c222fbdb2b0f4f7b3aef5ccc5cbdd1179f0ac2554f9e83','7e53d8ab4228001563322214f0551eef224025b0f4177085a4bf929f88ea96a5','4a2fdce62b73944030cff9b3e52862868ca9584d',29,27),
 ('sphinx-doc__sphinx-8506','c1f542cca8d7a2ff23f19268fd94727c8476a6172f3258efcf2ce5f0f15b281c','2105801eb49640ead9c451a8b2a5fb5c3b22a2d63da63c344025f644ed7ebfc7','e4bd3bd3ddd42c6642ff779a4f7381f219655c2c',29,24),
)


def validate(args):
    if not getattr(args, 'e2b_profile', None):
        return
    forbidden = ('all', 'quick_check', 'available', 'list', 'analyze_existing',
                 'execute_plan', 'probe_plan', 'publish_output', 'no_pin',
                 'experiment_config', 'group', 'resume',
                 'cube_profile')
    if (args.e2b_profile != PROFILE or args.experiment != [EXPERIMENT]
            or any(getattr(args, key, None) for key in forbidden)
            or any(getattr(args, key, None) is not None for key in
                   ('limit', 'max_events', 'gpu_cases', 'numa_node', 'cpus'))
            or (getattr(args, 'reuse_completed_from', None) and not getattr(args, 'output', None))
            or Path(args.config).absolute() != ROOT_CONFIG):
        raise ValueError('--e2b-profile requires complete explicit table-02-e2b only and fixed hosted configuration; no overrides or resume; references require a new output')
    if any(os.environ.get(key) for key in ('AE_CPUS', 'AE_NUMA_NODE', 'AE_CONFIG')):
        raise ValueError('E2B paper profile configuration/placement cannot be overridden by environment')


def effective(config, profile):
    value = deepcopy(config)
    if profile is None:
        return value
    if profile != PROFILE:
        raise ValueError('Unknown E2B profile')
    value['baseline_storage'] = 'disk'
    value['recorded_search_order'] = False
    value['replay_message_policy'] = 'strict'
    value['measurement'] = dict(pin=True, numa_node=1, cpus='28-31')
    # Dynamic SSH address/key/storage/binary may only be supplied by the suite
    # context after L1 startup. Do not inherit direct-host or prebuilt-parent keys.
    value['e2b'] = dict(profile=PROFILE, execution='paper-nested-pending',
                        l1_workspace=config.get('e2b', {}).get('l1_workspace', 'l1-work'),
                        paper_manifest=str(MANIFEST),
                        paper_contract=str(INPUT_ROOT / 'e2b-paper-185-input-action-contract.json'),
                        fresh_base_per_input=True, warm_action_worker=False,
                        vcpus=1, mem_mib=2048, disk_mb=4096,
                        fc_version='v1.14.1_458ca91',
                        profile_provenance={
                            'kind': 'documented-condition-reconstruction',
                            'inputs': 'recovered original eight recordings; 227 expansions and 185 measured actions',
                            'topology': 'fresh nested L1 suite and fresh 1-vCPU/2048-MiB E2B base per input',
                            'worker': 'fresh Python process per action, sequential dual mock cursors',
                            'not_exact_original_binary_claim': True,
                            'source_limit': 'original guest bytes were not cryptographically bound; reconstructed source and runtime provenance are recorded separately'})
    return value


def active(config):
    return config.get('e2b', {}).get('profile') == PROFILE


def validate_effective(config, *, guest_ready=False):
    e2b = config.get('e2b', {})
    if not active(config):
        raise ValueError('Not the E2B paper profile')
    expected = effective({}, PROFILE)
    if (config.get('baseline_storage') != 'disk' or config.get('measurement') != expected['measurement']
            or config.get('recorded_search_order') is not False
            or config.get('replay_message_policy') != 'strict'):
        raise ValueError('E2B paper storage or placement differs')
    for key in ('paper_manifest', 'paper_contract', 'fresh_base_per_input',
                'warm_action_worker', 'vcpus', 'mem_mib', 'disk_mb', 'fc_version'):
        if e2b.get(key) != expected['e2b'][key] or type(e2b.get(key)) is not type(expected['e2b'][key]):
            raise ValueError('E2B paper fixed setting differs: ' + key)
    if not guest_ready and e2b.get('execution') != 'paper-nested-pending':
        raise ValueError('L1 dynamic execution settings must be produced inside the pinned suite')
    if not guest_ready:
        dynamic = {'ssh_host', 'ssh_key', 'from_build', 'root_build', 'storage',
                   'remote_path', 'resume_binary', 'parent_manifest', 'sandbox_dir',
                   'gocache', 'gomodcache', 'sidecar_ip', 'infra', 'transport_manifest',
                   'worker_mock_port', 'index_port'}
        if dynamic.intersection(e2b):
            raise ValueError('Unexpected pre-supplied E2B guest configuration')


def _root_read(path):
    path = Path(path)
    if not path.is_absolute() or any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Paper input path must be absolute without symlinks: ' + str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or before.st_mode & 0o022 or before.st_nlink != 1):
            raise ValueError('Paper input must be a root-owned, non-writable regular file: ' + str(path))
        with os.fdopen(fd, 'rb', closefd=False) as source:
            raw = source.read()
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError('Paper input changed while being read: ' + str(path))
        return raw
    finally:
        os.close(fd)


def verify_inputs(config, *, guest_ready=False):
    """Static admission only. Never connects to or starts L1/guest services."""
    validate_effective(config, guest_ready=guest_ready)
    raw = _root_read(MANIFEST)
    manifest = json.loads(raw)
    if (manifest.get('schema_version') != 1 or manifest.get('profile') != PROFILE
            or not isinstance(manifest.get('inputs'), list)
            or len(manifest['inputs']) != len(COHORT)):
        raise ValueError('Invalid fixed E2B paper manifest')
    contract_path = INPUT_ROOT / 'e2b-paper-185-input-action-contract.json'
    contract_ref = manifest.get('contract', {})
    if contract_ref.get('path') != str(contract_path) or contract_ref.get('sha256') != CONTRACT_SHA256:
        raise ValueError('E2B paper action contract binding differs')
    contract_raw = _root_read(contract_path)
    if hashlib.sha256(contract_raw).hexdigest() != CONTRACT_SHA256:
        raise ValueError('E2B paper action contract hash mismatch')
    contract = json.loads(contract_raw)
    if (contract.get('n_inputs') != 8 or contract.get('n_observed_expansions') != 227
            or contract.get('n_measured_checkpoint_restore_pairs') != 185
            or [item.get('instance') for item in contract.get('inputs', [])] != [r[0] for r in COHORT]):
        raise ValueError('E2B paper action contract coverage differs')
    verified = []
    for item, (instance, trace_sha, rtt_sha, commit, expansions, actions) in zip(manifest['inputs'], COHORT):
        if (item.get('instance') != instance or item.get('repository_commit') != commit
                or item.get('expansions') != expansions or item.get('actions') != actions):
            raise ValueError('E2B paper input order/base commit/coverage differs')
        files = {}
        for key, name, expected_sha in (('trajectory', 'trajectory.json', trace_sha), ('rtt', 'ms_trace.jsonl', rtt_sha)):
            path = INPUT_ROOT / 'ms' / instance / name
            ref = item.get(key, {})
            if ref.get('path') != str(path) or ref.get('sha256') != expected_sha:
                raise ValueError('E2B paper input binding differs: ' + instance + '/' + name)
            content = _root_read(path)
            if hashlib.sha256(content).hexdigest() != expected_sha or ref.get('bytes') != len(content):
                raise ValueError('E2B paper input bytes/hash mismatch: ' + str(path))
            files[key] = dict(path=str(path), sha256=expected_sha, bytes=len(content))
        verified.append(dict(instance=instance, repository_commit=commit, expansions=expansions,
                             actions=actions, **files))
    return dict(profile=PROFILE, manifest=dict(path=str(MANIFEST),
                sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)),
                contract=dict(path=str(contract_path), sha256=CONTRACT_SHA256),
                inputs=verified, observed_expansions=227, measured_actions=185)


def input_rows(config):
    proof = verify_inputs(config)
    return [dict(instance=row['instance'], local=row['trajectory']['path'],
                 sha256=row['trajectory']['sha256'], repository_commit=row['repository_commit'],
                 expected_expansions=row['expansions'], expected_actions=row['actions'],
                 rtt=row['rtt'], paper_manifest=proof['manifest'], paper_contract=proof['contract'])
            for row in proof['inputs']]
