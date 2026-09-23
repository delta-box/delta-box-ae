"""Verify Cube's daemon namespace, rather than trusting the client's cwd."""
import json
import os
from pathlib import Path
import subprocess
from repro.common import configured_path, file_record

STORAGE='/data/cubelet/storage'
SERVICE='cube-sandbox-cubelet.service'

def output(*args):
    return subprocess.check_output(list(map(str,args)),text=True).strip()

def cpuset(text):
    values=set()
    for item in text.split(','):
        bounds=item.split('-');a=int(bounds[0]);b=int(bounds[-1])
        if b<a:raise ValueError('Invalid CPU range')
        values.update(range(a,b+1))
    return values

def require_tmpfs(mount,node):
    opts=mount['options'].split(',')
    if mount['fstype']!='tmpfs' or 'noswap' not in opts:
        raise ValueError('Cube working data must be on noswap tmpfs')
    if f'mpol=bind:{node}' not in opts:
        raise ValueError('Cube RAM mount is not bound to the requested NUMA node')

def verify(config):
    path=configured_path(config,'cube.memory_manifest')
    expected=json.loads(path.read_text())
    measurement=config.get('measurement',{})
    node=int(measurement.get('numa_node',2));cpus=measurement.get('cpus','52-55')
    pid=int(output('systemctl','show',SERVICE,'-p','MainPID','--value'))
    if (pid!=expected['service_pid'] or
        Path(f'/proc/{pid}/stat').read_text().split()[21]!=expected['service_start_ticks'] or
        expected['node']!=node or cpuset(expected['cpus'])!=cpuset(cpus)):
        raise ValueError('Cube RAM service identity or requested placement changed')
    status=dict(line.split(':',1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
    if cpuset(status['Cpus_allowed_list'].strip())!=cpuset(cpus) or cpuset(status['Mems_allowed_list'].strip())!={node}:
        raise ValueError('Cube daemon affinity differs from the measurement')
    cgroup=Path('/sys/fs/cgroup/cube_sandbox')
    if (cpuset((cgroup/'cpuset.cpus.effective').read_text().strip())!=cpuset(cpus) or
        cpuset((cgroup/'cpuset.mems.effective').read_text().strip())!={node}):
        raise ValueError('Cube sandbox cgroup is not pinned with its daemon')
    paths={}
    for target in expected['paths']:
        mounts=json.loads(output('nsenter','-t',pid,'-m','findmnt','-J','-T',target,
                                 '-o','TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN'))['filesystems']
        device=Path(f'/proc/{pid}/root{target}').stat().st_dev
        candidates=[m for m in mounts if m['maj:min']==f'{os.major(device)}:{os.minor(device)}']
        if len(candidates)!=1:
            raise ValueError('Cannot identify the visible Cube mount by device number')
        mount=candidates[0]
        paths[target]=mount
        if target!=STORAGE:require_tmpfs(mount,node)
    mount=paths[STORAGE]
    if mount['fstype']!='xfs' or not mount['source'].startswith('/dev/loop'):
        raise ValueError('Cube storage is not the RAM-backed reflink XFS device')
    loop=json.loads(output('losetup','--list','--json',mount['source']))['loopdevices'][0]
    backing=Path(loop['back-file'])
    expected_loop=expected['loop']['loopdevices'][0]
    if loop['name']!=expected_loop['name'] or str(backing)!=expected_loop['back-file']:
        raise ValueError('Cube loop device identity changed')
    ram=json.loads(output('findmnt','-J','-T',backing))['filesystems'][0]
    require_tmpfs(ram,node)
    return dict(manifest=file_record(path),service_pid=pid,node=node,cpus=cpus,
                paths=paths,loop=loop,ram_mount=ram,
                scope='Cubelet rootfs, memory snapshots, state, disk staging and images in RAM; external control-plane services unchanged')
