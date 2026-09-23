#!/usr/bin/env python3
"""Check a private CRIU in a disposable VM rootfs, with read-only data disk.

Invoke under unshare --mount --net --propagation private; no benchmark runs.
"""
import argparse, hashlib, json, subprocess, sys
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--runtime-repo',type=Path,required=True)
p.add_argument('--config',type=Path,required=True)
p.add_argument('--binary',type=Path,required=True)
p.add_argument('--out',type=Path,required=True)
a=p.parse_args();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
sys.path.insert(0,str(a.runtime_repo/'replay'))
import run_instance
from host_execution import InstanceRun
config=json.loads(a.config.read_text())
config.update(data_xfs=str(Path(config['images_dir'])/'data-tools.xfs'),work_dir=str(a.out),vcpus=4,mem_mib=4096,ssh_timeout=120)
spec=InstanceRun(a.out/'run.json',config)
record={'purpose':'guest binary compatibility, disposable rootfs; not a benchmark',
        'binary':str(a.binary),'binary_sha256':hashlib.sha256(a.binary.read_bytes()).hexdigest(),'checks':[]}
try:
    with run_instance.managed_vm(spec) as machine:
        subprocess.run(machine.scp+[str(a.binary),f'root@{machine.args.guest_ip}:/tmp/criu-exact-parent'],check=True,timeout=60)
        commands=[('guest','uname -a; getconf GNU_LIBC_VERSION; python3 --version; command -v gcc; command -v make; command -v git; df -h /tmp; criu --version'),
                  ('capability','DELTABOX_CRIU_CAPABILITIES=1 /tmp/criu-exact-parent --version'),
                  ('dependencies','ldd /tmp/criu-exact-parent')]
        for name,command in commands:
            result=subprocess.run(machine.ssh+[command],capture_output=True,text=True,timeout=30)
            record['checks'].append({'name':name,'command':command,'rc':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
            (a.out/(name+'.log')).write_text(result.stdout+result.stderr)
        cap=record['checks'][1]
        record['compatible']=cap['rc']==0 and 'DeltaBox capabilities: exact-parent-v1' in cap['stdout']
finally:
    (a.out/'compatibility.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record,indent=2))
