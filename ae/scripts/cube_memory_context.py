#!/usr/bin/env python3
"""Run an idle Cube service against a private RAM copy, restoring it in finally."""
import argparse, fcntl, json, os, signal, subprocess, time, urllib.request
from pathlib import Path

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

def sandboxes():
    with urllib.request.urlopen('http://127.0.0.1:3000/sandboxes',timeout=10) as r:
        value=json.load(r)
    if not isinstance(value,list): raise ValueError('Unknown Cube inventory format')
    return value

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--node',type=int,default=0)
    p.add_argument('--cpus',default='0-3')
    p.add_argument('command',nargs=argparse.REMAINDER)
    a=p.parse_args()
    if os.geteuid()!=0: raise ValueError('Root required for private RAM mounts and service restoration')
    if not a.command or a.command[0]!='--': raise ValueError('Require -- followed by the measured command')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    lock=open('/run/lock/deltabox-cube-memory.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if DROP.exists() or sandboxes(): raise ValueError('Cube must be idle and no prior experiment override may exist')
    if output('systemctl','is-active',SERVICE)!='active': raise ValueError('Expected running Cube service')
    device=output('findmnt','-n','-o','SOURCE','-T',STORAGE)
    if not device.startswith('/dev/loop'): raise ValueError('Cube storage must be a loop-backed XFS image')
    source=Path(json.loads(output('losetup','--list','--json',device))['loopdevices'][0]['back-file'])
    before={'main_pid':output('systemctl','show',SERVICE,'-p','MainPID','--value'),
            'unit':output('systemctl','cat',SERVICE),
            'source':str(source),'source_size':source.stat().st_size,
            'source_allocated':source.stat().st_blocks*512,'cpu_list':a.cpus,'numa_node':a.node,
            'allowed_cpus':output('systemctl','show',SERVICE,'-p','AllowedCPUs','--value'),
            'allowed_nodes':output('systemctl','show',SERVICE,'-p','AllowedMemoryNodes','--value')}
    (out/'before.json').write_text(json.dumps(before,indent=2)+'\n')
    ram=out/'ram';ram.mkdir();mounted=stopped=override=placement=False;loop=None;volume=None
    def interrupted(signum, frame): raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        run('mount','-t','tmpfs','-o',f'size=12G,noswap,mpol=bind:{a.node},mode=0700','tmpfs',ram);mounted=True
        run('systemctl','stop',SERVICE);stopped=True
        if sandboxes(): raise ValueError('Cube inventory changed during exclusive setup')
        image=ram/'storage.xfs'
        frozen=False
        try:
            run('fsfreeze','--freeze',STORAGE);frozen=True
            run('cp','--sparse=always','--reflink=never',source,image)
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
        DROP.write_text('[Service]\nPrivateMounts=yes\nBindPaths='+ ' '.join(bindings)+'\n'
            'ExecStart=\n'
            f'ExecStart=/usr/bin/numactl --physcpubind={a.cpus} --membind={a.node} /usr/local/services/cubetoolbox/scripts/systemd/cubelet-start.sh\n'
            'ExecStartPost=\n'+f'ExecStartPost=/usr/bin/python3 {pin}\n')
        override=True
        run('systemctl','daemon-reload')
        placement=True
        run('systemctl','set-property','--runtime',SERVICE,f'AllowedCPUs={a.cpus}',f'AllowedMemoryNodes={a.node}')
        run('systemctl','start',SERVICE)
        pid=int(output('systemctl','show',SERVICE,'-p','MainPID','--value'))
        proof={'schema_version':1,'service_pid':pid,'service_start_ticks':Path(f'/proc/{pid}/stat').read_text().split()[21],
               'node':a.node,'cpus':a.cpus,'ram_mount':json.loads(output('findmnt','-J','-T',ram)),
               'loop':json.loads(output('losetup','--list','--json',loop)), 'paths':{},
               'status':Path(f'/proc/{pid}/status').read_text(), 'source_image':str(source)}
        for path in [str(STORAGE),*PATHS]:
            proof['paths'][path]=json.loads(output('nsenter','-t',pid,'-m','findmnt','-J','-T',path))
        if sandboxes():raise ValueError('Unexpected Cube activity before measurement')
        (out/'storage.json').write_text(json.dumps(proof,indent=2)+'\n')
        return run(*a.command[1:]).returncode
    finally:
        errors=[]
        def cleanup(label, fn):
            try: return fn()
            except Exception as exc: errors.append(f'{label}: {type(exc).__name__}: {exc}')
        if override:
            cleanup('stop private service',lambda:run('systemctl','stop',SERVICE))
            cleanup('remove override',lambda:DROP.unlink(missing_ok=True))
            cleanup('reload original unit',lambda:run('systemctl','daemon-reload'))
        if placement:
            cleanup('restore placement',lambda:run('systemctl','set-property','--runtime',SERVICE,
                    'AllowedCPUs='+before['allowed_cpus'],'AllowedMemoryNodes='+before['allowed_nodes']))
        if stopped:
            cleanup('start original service',lambda:run('systemctl','start',SERVICE))
            restored={'override_removed':not DROP.exists(),'cleanup_errors':errors}
            for key,prop in [('service_status','ActiveState'),('pid','MainPID'),('allowed_cpus','AllowedCPUs'),('allowed_nodes','AllowedMemoryNodes')]:
                restored[key]=cleanup('read '+prop,lambda prop=prop:output('systemctl','show',SERVICE,'-p',prop,'--value'))
            (out/'restored.json').write_text(json.dumps(restored,indent=2)+'\n')
        if volume and os.path.ismount(volume):cleanup('unmount RAM XFS',lambda:run('umount',volume))
        if loop:cleanup('detach RAM loop',lambda:run('losetup','-d',loop))
        if mounted:cleanup('unmount RAM',lambda:run('umount',ram))
        if errors:
            (out/'cleanup-errors.json').write_text(json.dumps(errors,indent=2)+'\n')
            raise RuntimeError('Cube service restoration or RAM cleanup failed: '+'; '.join(errors))

if __name__=='__main__':raise SystemExit(main())
