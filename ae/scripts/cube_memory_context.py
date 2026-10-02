#!/usr/bin/env python3
"""Run an idle Cube service against a private RAM copy, restoring it in finally."""
import argparse, fcntl, json, os, signal, subprocess, time, urllib.request
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vendor.finalbench.fc_diff_dm.fc_capacity import node_available, GIB

SERVICE='cube-sandbox-cubelet.service'
STORAGE=Path('/data/cubelet/storage')
DROP=Path('/run/systemd/system')/f'{SERVICE}.d/zzzz-deltabox-fig167-ram.conf'
PATHS=['/data/cubelet/root', '/data/cubelet/state', '/data/snapshot_pack/disks',
       '/data/cube-shim/disks', '/usr/local/services/cubetoolbox/cube-snapshot',
       '/usr/local/services/cubetoolbox/cube-image']

def run(*args, **kw):
    return subprocess.run(list(map(str,args)), check=True, **kw)

def output(*args):
    return subprocess.check_output(list(map(str,args)), text=True).strip()

def process_thp_enabled(pid):
    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
        if line.startswith('THP_enabled:'):
            return int(line.split(':', 1)[1].strip())
    raise RuntimeError('Cannot verify Cube process THP policy')


def sandboxes():
    with urllib.request.urlopen('http://127.0.0.1:3000/sandboxes',timeout=10) as r:
        value=json.load(r)
    if not isinstance(value,list): raise ValueError('Unknown Cube inventory format')
    return value

def wait_loop_release(image, *, timeout=10):
    """losetup -d requests lazy destruction; wait before releasing its backing FS."""
    deadline = time.monotonic() + timeout
    while output('losetup', '-j', image, '-O', 'NAME', '--noheadings'):
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Cube RAM loop still attached to {image}; storage retained')
        time.sleep(0.05)

@contextmanager
def memory_service(output_dir, *, node, cpus, size_gib=16, lease_fd=None, recovery_guard=None):
    """Hold a verified private service for the caller, restoring it on every exit."""
    a=SimpleNamespace(output=Path(output_dir),node=node,cpus=cpus)
    if os.geteuid()!=0:
        raise ValueError('Root required for private RAM mounts and service restoration')
    if type(size_gib) is not int or size_gib < 12:
        raise ValueError('Cube RAM workspace must be at least 12 GiB')
    from runners.cube_memory import cpuset
    if not cpuset(cpus) or node < 0:
        raise ValueError('Invalid Cube placement')
    available=node_available(node)
    if available < (size_gib+2)*GIB:
        raise ValueError(f'Cube NUMA {node} has {available/GIB:.2f} GiB available; needs {size_gib+2} GiB')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    lock = None
    if lease_fd is None:
        lock = open('/run/lock/deltabox-cube-memory.lock', 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        if type(lease_fd) is not int or lease_fd < 0:
            raise ValueError('Invalid borrowed Cube lease')
        held = os.fstat(lease_fd)
        named = os.stat('/run/lock/deltabox-cube-memory.lock', follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError('Borrowed Cube lease belongs to a different file')
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if DROP.exists() or sandboxes(): raise ValueError('Cube must be idle and no prior experiment override may exist')
    if output('systemctl','is-active',SERVICE)!='active': raise ValueError('Expected running Cube service')
    device=output('findmnt','-n','-o','SOURCE','-T',STORAGE)
    if not device.startswith('/dev/loop'): raise ValueError('Cube storage must be a loop-backed XFS image')
    source=Path(json.loads(output('losetup','--list','--json',device))['loopdevices'][0]['back-file'])
    source_present = source.is_file()
    if not source_present and not str(source).endswith(' (deleted)'):
        raise ValueError('Cube loop backing file is unavailable without a deleted-file marker')
    source_size = source.stat().st_size if source_present else int(output('blockdev','--getsize64',device))
    source_allocated = source.stat().st_blocks * 512 if source_present else None
    copy_method = 'sparse-file-copy' if source_present else 'xfs-copy-frozen-live-device'
    before={'main_pid':output('systemctl','show',SERVICE,'-p','MainPID','--value'),
            'unit':output('systemctl','cat',SERVICE),
            'source':str(source),'source_size':source_size,
            'source_allocated':source_allocated,'source_device':device,'copy_method':copy_method,'cpu_list':a.cpus,'numa_node':a.node,
            'allowed_cpus':output('systemctl','show',SERVICE,'-p','AllowedCPUs','--value'),
            'allowed_nodes':output('systemctl','show',SERVICE,'-p','AllowedMemoryNodes','--value'),
            'sandbox_cgroup':{name:(Path('/sys/fs/cgroup/cube_sandbox')/name).read_text().strip() for name in ('cpuset.cpus','cpuset.mems')}}
    before['thp_enabled'] = process_thp_enabled(before['main_pid'])
    (out/'before.json').write_text(json.dumps(before,indent=2)+'\n')
    ram=out/'ram';ram.mkdir();mounted=stopped=override=placement=cgroup_placement=False;loop=None;volume=None;image=None
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        raise KeyboardInterrupt(f'signal {signum}')
    previous_term=signal.signal(signal.SIGTERM,interrupted)
    try:
        mounted=True
        run('mount','-t','tmpfs','-o',f'size={size_gib}G,noswap,mpol=bind:{a.node},mode=0700','tmpfs',ram)
        stopped=True
        run('systemctl','stop',SERVICE)
        if sandboxes(): raise ValueError('Cube inventory changed during exclusive setup')
        image=ram/'storage.xfs'
        frozen=False
        try:
            frozen=True
            run('fsfreeze','--freeze',STORAGE)
            if source_present:
                run('cp','--sparse=always','--reflink=never',source,image)
            else:
                # xfs_copy explicitly supports frozen sources and skips free
                # filesystem blocks. Never detach the original live loop.
                run('xfs_copy','-d','-b','-L',out/'xfs-copy.log',device,image)
        finally:
            if frozen:run('fsfreeze','--unfreeze',STORAGE)
        loop=output('losetup','--find','--show',image)
        volume=ram/'storage';volume.mkdir()
        run('mount','-t','xfs','-o','nouuid',loop,volume)
        bindings=[f'{volume}:{STORAGE}']
        for i,path in enumerate(PATHS):
            target=ram/f'path-{i}';target.mkdir()
            run('cp','-a',str(Path(path))+'/.',target)
            bindings.append(f'{target}:{path}')
        DROP.parent.mkdir(parents=True,exist_ok=True)
        pin=out/'pin.py'
        pin.write_text('from pathlib import Path\nimport time\np=Path("/sys/fs/cgroup/cube_sandbox")\n'
            'for _ in range(200):\n'
            ' if (p/"cpuset.cpus").exists(): break\n'
            ' time.sleep(.1)\n'
            f'(p/"cpuset.mems").write_text({str(a.node)!r})\n'
            f'(p/"cpuset.cpus").write_text({a.cpus!r})\n')
        override=True
        DROP.write_text('[Service]\nPrivateMounts=yes\nBindPaths='+ ' '.join(bindings)+'\n'
            'ExecStart=\n'
            f'ExecStart=/usr/bin/python3 {Path(__file__).with_name("cube_no_thp_exec.py")} '
            f'/usr/bin/numactl --physcpubind={a.cpus} --membind={a.node} /usr/local/services/cubetoolbox/scripts/systemd/cubelet-start.sh\n'
            'ExecStartPost=\n'+f'ExecStartPost=/usr/bin/python3 {pin}\n')
        run('systemctl','daemon-reload')
        placement=True
        run('systemctl','set-property','--runtime',SERVICE,f'AllowedCPUs={a.cpus}',f'AllowedMemoryNodes={a.node}')
        cgroup_placement=True
        group=Path('/sys/fs/cgroup/cube_sandbox')
        (group/'cpuset.mems').write_text(str(a.node)+'\n')
        (group/'cpuset.cpus').write_text(a.cpus+'\n')
        run('systemctl','start',SERVICE)
        pid=int(output('systemctl','show',SERVICE,'-p','MainPID','--value'))
        if process_thp_enabled(pid) != 0:
            raise RuntimeError('Private Cubelet must disable THP before snapshots')
        proof={'schema_version':1,'service_pid':pid,'service_start_ticks':Path(f'/proc/{pid}/stat').read_text().split()[21],
               'node':a.node,'cpus':a.cpus,'ram_mount':json.loads(output('findmnt','-J','-T',ram)),
               'loop':json.loads(output('losetup','--list','--json',loop)), 'paths':{},
               'status':Path(f'/proc/{pid}/status').read_text(), 'source_image':str(source),
               'thp_policy':'disabled in private service tree to avoid pagemap/PFN relocation race'}
        for path in [str(STORAGE),*PATHS]:
            proof['paths'][path]=json.loads(output('nsenter','-t',pid,'-m','findmnt','-J','-T',path))
        if sandboxes():raise ValueError('Unexpected Cube activity before measurement')
        (out/'storage.json').write_text(json.dumps(proof,indent=2)+'\n')
        from runners.cube_memory import verify
        config={'cube':{'memory_manifest':str(out/'storage.json')},
                'measurement':{'numa_node':node,'cpus':cpus}}
        (out/'verified.json').write_text(json.dumps(verify(config),indent=2)+'\n')
        yield out/'storage.json'
    finally:
        if recovery_guard is not None and Path(recovery_guard).exists():
            (out/'retained.json').write_text(json.dumps({'reason':'Unresolved owned Cube resources', 'guard':str(recovery_guard), 'ram':str(ram), 'loop':loop},indent=2)+'\n')
            signal.signal(signal.SIGTERM,previous_term)
            if lock is not None:
                lock.close()
            raise RuntimeError('Cube RAM and service override retained for resource recovery')
        cleanup_guard = Path(recovery_guard) if recovery_guard is not None else out/'RECOVERY_REQUIRED.json'
        errors=[]
        def step(label, fn):
            try:
                return fn()
            except BaseException as exc:
                errors.append(f'{label}: {type(exc).__name__}: {exc}')
                raise
        def stopped_service():
            active = output('systemctl','show',SERVICE,'-p','ActiveState','--value')
            pid = output('systemctl','show',SERVICE,'-p','MainPID','--value')
            if active not in ('inactive','failed') or pid != '0':
                raise RuntimeError('Private Cubelet did not stop')
        try:
            cleanup_guard.write_text(json.dumps({'reason':'Cube storage restoration in progress','output':str(out)})+'\n')
            # Each prerequisite must finish before any subsequent restoration or
            # release. A live or ambiguously configured service keeps its storage.
            if override:
                step('stop private service',lambda:run('systemctl','stop',SERVICE))
                step('verify private service stopped',stopped_service)
                step('remove override',lambda:DROP.unlink(missing_ok=True))
                step('reload original unit',lambda:run('systemctl','daemon-reload'))
            if placement:
                step('restore placement',lambda:run('systemctl','set-property','--runtime',SERVICE,
                     'AllowedCPUs='+before['allowed_cpus'],'AllowedMemoryNodes='+before['allowed_nodes']))
            if cgroup_placement:
                for name in ('cpuset.mems','cpuset.cpus'):
                    step('restore sandbox '+name,lambda name=name:(Path('/sys/fs/cgroup/cube_sandbox')/name).write_text(before['sandbox_cgroup'][name]+'\n'))
            if stopped:
                step('start original service',lambda:run('systemctl','start',SERVICE))
                restored={'override_removed':not DROP.exists(),'cleanup_errors':errors}
                for key,prop in [('service_status','ActiveState'),('pid','MainPID'),('allowed_cpus','AllowedCPUs'),('allowed_nodes','AllowedMemoryNodes')]:
                    restored[key]=step('read '+prop,lambda prop=prop:output('systemctl','show',SERVICE,'-p',prop,'--value'))
                if (not restored['override_removed'] or restored['service_status']!='active' or
                        int(restored['pid'])<=0 or restored['allowed_cpus']!=before['allowed_cpus'] or
                        restored['allowed_nodes']!=before['allowed_nodes']):
                    raise RuntimeError('Original Cubelet identity or placement did not restore')
                restored['thp_enabled'] = step('verify original THP policy',lambda:process_thp_enabled(restored['pid']))
                if restored['thp_enabled'] != before['thp_enabled']:
                    raise RuntimeError('Original Cubelet THP policy did not restore')
                restored['storage_device']=step('verify original mounted storage',lambda:output(
                    'nsenter','-t',restored['pid'],'-m','findmnt','-n','-o','SOURCE','-T',STORAGE))
                if restored['storage_device'] != device:
                    raise RuntimeError('Original Cubelet did not return to its original storage device')
                (out/'restored.json').write_text(json.dumps(restored,indent=2)+'\n')
            if volume and os.path.ismount(volume):step('unmount RAM XFS',lambda:run('umount',volume))
            if loop:
                step('detach RAM loop',lambda:run('losetup','-d',loop))
            elif image is not None and image.exists():
                owned=step('recover interrupted loop attachment',lambda:output('losetup','-j',image,'-O','NAME','--noheadings'))
                for owned_device in (owned or '').splitlines():
                    if owned_device.strip():step('detach recovered RAM loop',lambda owned_device=owned_device:run('losetup','-d',owned_device.strip()))
            if image is not None and image.exists():
                step('wait for RAM loop release',lambda:wait_loop_release(image))
            if mounted and os.path.ismount(ram):step('unmount RAM',lambda:run('umount',ram))
            cleanup_guard.unlink()
        except BaseException as exc:
            if not errors:errors.append(f'{type(exc).__name__}: {exc}')
            (out/'cleanup-errors.json').write_text(json.dumps(errors,indent=2)+'\n')
            raise RuntimeError('Cube restoration stopped; private resources retained: '+'; '.join(errors)) from exc
        finally:
            signal.signal(signal.SIGTERM,previous_term)
            if lock is not None:
                lock.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--node',type=int,default=0)
    p.add_argument('--cpus',default='0-3')
    p.add_argument('--size-gib',type=int,default=16)
    p.add_argument('command',nargs=argparse.REMAINDER)
    a=p.parse_args()
    if not a.command or a.command[0]!='--':
        raise ValueError('Require -- followed by the measured command')
    with memory_service(a.output,node=a.node,cpus=a.cpus,size_gib=a.size_gib):
        return run(*a.command[1:]).returncode

if __name__=='__main__':raise SystemExit(main())
