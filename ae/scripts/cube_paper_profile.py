"""Explicit Cube disk reconstruction; never changes the default AE profile."""
from copy import deepcopy
from contextlib import contextmanager
import os
from pathlib import Path

PROFILE = 'paper-disk'
WORKSPACE = Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/cube-paper-disk')
SERVICE_CPUS = '48-71'
RUNNER_CPUS = '48-51'

def validate(args):
    if not getattr(args, 'cube_profile', None):
        return
    if os.environ.get('AE_CPUS') or os.environ.get('AE_NUMA_NODE'):
        raise ValueError('Cube profile placement cannot be overridden by AE_CPUS or AE_NUMA_NODE')
    forbidden = ('all', 'quick_check', 'available', 'list', 'analyze_existing',
                 'execute_plan', 'probe_plan', 'publish_output', 'no_pin',
                 'experiment_config', 'group', 'resume')
    if (args.cube_profile != PROFILE or set(args.experiment or []) != {'table-02-cube'}
            or any(getattr(args, key, None) for key in forbidden)
            or any(getattr(args, key, None) is not None for key in
                   ('limit', 'max_events', 'gpu_cases', 'numa_node', 'cpus'))):
        raise ValueError('--cube-profile requires complete explicit table-02-cube only; profile controls placement')

def effective(config, profile):
    value = deepcopy(config)
    if profile is None:
        return value
    if profile != PROFILE:
        raise ValueError('Unknown Cube profile')
    value['baseline_storage'] = 'disk'
    value['measurement'] = dict(pin=True, numa_node=2, cpus=RUNNER_CPUS,
                                policy_cpus=SERVICE_CPUS)
    cube = value.setdefault('cube', {})
    cube.update(profile=PROFILE, manage_memory_service=False,
                disk_workspace=str(WORKSPACE), service_cpus=SERVICE_CPUS,
                template='cube-official-blog-sandbox-code-2c2g-20260609')
    cube.pop('memory_manifest', None)
    cube['profile_provenance'] = {
        'kind': 'documented-condition-reconstruction',
        'storage': 'private disk-backed XFS; June 4 report specifies disk1; June 10 loop backing identity unavailable',
        'placement': 'June 10 controller 48-51; June 9 Cubelet NUMA2 whole-node affinity and maximum P-state',
        'topology_difference': 'June 9 had CPUs48-71,144-167; current NUMA2 has only online48-71. No machine-wide SMT change is made.',
        'retained_service_repair': 'Master transaction-ownership recovery remains installed; independent backend hashes required',
        'not_exact_environment_claim': True}
    return value

@contextmanager
def preparation_lease(work, profile):
    # Unlike the default RAM campaign, reconstruction uses NUMA2. The fixed
    # quick entry predates this profile, so exclude it before moving Cube.
    from ae.repro.result_storage import run_lock
    if profile == PROFILE:
        with run_lock(Path(work) / '.quick-run.lock'):
            yield
    else:
        yield
