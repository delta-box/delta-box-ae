#!/usr/bin/env python3
"""Start the self-contained DeltaBox experiment L1 using a writable overlay.
Runs only its own QEMU process. No host services, CPU governor, or mount changes.
"""
import argparse, hashlib, json, os, shutil, socket, subprocess, time
from pathlib import Path


def run(args, **kw):
    return subprocess.run([str(x) for x in args], check=True, **kw)


def qmp(work, command, arguments=None):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(15); s.connect(str(work/'qmp.sock')); f=s.makefile('rwb')
        json.loads(f.readline())
        for msg in ({'execute':'qmp_capabilities'}, {'execute':command, **({'arguments':arguments} if arguments else {})}):
            f.write(json.dumps(msg).encode()+b'\n'); f.flush()
            while True:
                reply=json.loads(f.readline())
                if 'error' in reply: raise RuntimeError(reply['error'])
                if 'return' in reply: break
        return reply['return']


def expand(spec):
    cpus=[]
    for part in spec.split(','):
        ends=part.split('-'); cpus.extend(range(int(ends[0]),int(ends[-1])+1))
    return sorted(set(cpus))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,default=Path('ae/work/appliance'))
    sub=p.add_subparsers(dest='action',required=True)
    s=sub.add_parser('start'); s.add_argument('--image',type=Path,required=True)
    s.add_argument('--cpus',required=True,help='Host CPU list explicitly allocated to this run')
    s.add_argument('--memory-gib',type=int,default=120); s.add_argument('--ssh-port',type=int,default=2223)
    s.add_argument('--functional-only',action='store_true',help='Allow fewer host CPUs or RAM; not the paper performance configuration')
    sub.add_parser('status'); sub.add_parser('stop')
    s=sub.add_parser('ssh'); s.add_argument('command',nargs=argparse.REMAINDER)
    a=p.parse_args(); w=a.work.expanduser().resolve()
    if a.action=='start':
        if (w/'pid').exists():
            pid=int((w/'pid').read_text())
            if Path('/proc') .joinpath(str(pid)).exists(): raise SystemExit('Existing appliance process: use status/stop first')
        for binary in ('qemu-system-x86_64','qemu-img','ssh','ssh-keygen','genisoimage'):
            if not shutil.which(binary): raise SystemExit('Missing host dependency: '+binary)
        if not os.access('/dev/kvm',os.R_OK|os.W_OK): raise SystemExit('Current account needs read/write access to /dev/kvm')
        nested=[x.read_text().strip().lower() for x in Path('/sys/module').glob('kvm_*/parameters/nested')]
        if not any(x in ('1','y','yes') for x in nested): raise SystemExit('Enable KVM nested virtualization on the host first')
        cpus=expand(a.cpus)
        if not set(cpus)<=os.sched_getaffinity(0): raise SystemExit('Requested CPUs are outside the available affinity mask')
        if not a.functional_only and (len(cpus)<48 or a.memory_gib<120): raise SystemExit('Performance configuration needs 48 allocated CPUs and 120 GiB RAM; use --functional-only for a smoke test')
        if a.memory_gib%4: raise SystemExit('Memory must divide evenly among four virtual NUMA nodes')
        by_node={}
        for c in cpus:
            nodes=list(Path(f'/sys/devices/system/cpu/cpu{c}').glob('node[0-9]*'))
            node=int(nodes[0].name[4:]) if nodes else 0
            by_node.setdefault(node,[]).append(c)
        eligible=[(n,cs[:12]) for n,cs in sorted(by_node.items()) if len(cs)>=12]
        if not a.functional_only and len(eligible)<4: raise SystemExit('Allocate 12 host CPUs from each of four physical NUMA nodes for the paper placement')
        mapping=eligible[:4] if len(eligible)>=4 else [(next(iter(by_node)),[cpus[i%len(cpus)] for i in range(12*n,12*n+12)]) for n in range(4)]
        # Check the physical node policy, not just machine-wide free RAM.
        requirements={}
        for node,_ in mapping: requirements[node]=requirements.get(node,0)+a.memory_gib//4
        for node,gib in requirements.items():
            values={line.split()[2].rstrip(':'):int(line.split()[3])*1024
                    for line in Path(f'/sys/devices/system/node/node{node}/meminfo').read_text().splitlines()}
            available=values['MemFree']+max(0,values['FilePages']-values.get('Shmem',0))+values.get('SReclaimable',0)
            if available<(gib+8)*2**30:
                raise SystemExit(f'NUMA node {node} has only {available/2**30:.1f} GiB available; needs {gib} GiB plus 8 GiB headroom. Choose another allocation or free your own jobs.')
        image=a.image.expanduser().resolve(); info=json.loads(run(['qemu-img','info','--output=json',image],capture_output=True,text=True).stdout)
        if info.get('backing-filename'): raise SystemExit('Release image must be self-contained')
        w.mkdir(parents=True,exist_ok=True); os.chmod(w,0o700)
        key=w/'id_ed25519'
        if not key.exists(): run(['ssh-keygen','-q','-t','ed25519','-N','','-f',key])
        pub=key.with_suffix('.pub').read_text().strip()
        (w/'user-data').write_text('#cloud-config\nssh_pwauth: false\nssh_deletekeys: true\nusers:\n  - name: atc-ae\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    groups: [docker, kvm]\n    ssh_authorized_keys:\n      - '+pub+'\n')
        (w/'network-config').write_text('version: 2\nethernets:\n  enp0s3:\n    match:\n      macaddress: 52:54:00:12:34:56\n    set-name: enp0s3\n    dhcp4: false\n    dhcp6: false\n    accept-ra: false\n    addresses: [10.0.2.15/24]\n    nameservers:\n      addresses: [10.0.2.3]\n    routes:\n      - to: default\n        via: 10.0.2.2\n')
        (w/'meta-data').write_text('instance-id: deltabox-ae-'+hashlib.sha256(key.with_suffix('.pub').read_bytes()).hexdigest()[:16]+'\nlocal-hostname: deltabox-ae\n')
        run(['genisoimage','-quiet','-output',w/'seed.iso','-volid','cidata','-joliet','-rock',w/'user-data',w/'meta-data',w/'network-config'])
        disk=w/'run.qcow2'
        if not disk.exists(): run(['qemu-img','create','-q','-f','qcow2','-F','qcow2','-b',image,disk])
        cmd=[('/usr/bin/qemu-system-x86_64' if Path('/usr/bin/qemu-system-x86_64').exists() else 'qemu-system-x86_64'),'-name','deltabox-ae-v3','-machine','q35,accel=kvm','-cpu','host','-smp','48,sockets=4,cores=12,threads=1','-m',str(a.memory_gib*1024)]
        for n in range(4):
            cmd+=['-object',f'memory-backend-ram,id=mem{n},size={a.memory_gib//4}G,host-nodes={mapping[n][0]},policy=bind','-numa',f'node,nodeid={n},cpus={12*n}-{12*n+11},memdev=mem{n}']
        cmd+=['-drive',f'file={disk},format=qcow2,if=virtio,cache=none,aio=native,discard=unmap',
              '-drive',f'file={w/"seed.iso"},format=raw,media=cdrom,readonly=on',
              '-netdev',f'user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:{a.ssh_port}-:22',
              '-device','virtio-net-pci,netdev=net0,mac=52:54:00:12:34:56','-display','none','-serial',f'file:{w/"serial.log"}',
              '-qmp',f'unix:{w/"qmp.sock"},server=on,wait=off','-pidfile',str(w/'pid'),'-daemonize']
        run(['taskset','-c',a.cpus,*cmd])
        # Prefer terminating this disposable VM over unrelated host work under pressure.
        Path('/proc', (w/'pid').read_text().strip(), 'oom_score_adj').write_text('500')
        for cpu in qmp(w,'query-cpus-fast'):
            v=cpu['cpu-index']; os.sched_setaffinity(cpu['thread-id'],{mapping[v//12][1][v%12]})
        (w/'run.json').write_text(json.dumps({'image':str(image),'cpus':cpus,'numa_mapping':mapping,'memory_gib':a.memory_gib,'ssh_port':a.ssh_port,'functional_only':a.functional_only,'command':cmd,'started_epoch':time.time()},indent=2))
        print('Started. SSH public key is injected by cloud-init; allow about a minute, then run the ssh subcommand.')
    elif a.action=='status':
        print(json.dumps({'status':qmp(w,'query-status'),'configuration':json.loads((w/'run.json').read_text())},indent=2))
    elif a.action=='stop':
        qmp(w,'system_powerdown'); print('Sent guest shutdown; wait for process exit before modifying the disk.')
    else:
        cfg=json.loads((w/'run.json').read_text())
        cmd=['ssh','-i',str(w/'id_ed25519'),'-p',str(cfg['ssh_port']),'-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-o','StrictHostKeyChecking=accept-new','-o',f'UserKnownHostsFile={w/"known_hosts"}','atc-ae@127.0.0.1',*a.command]
        raise SystemExit(subprocess.call(cmd))

if __name__=='__main__': main()
