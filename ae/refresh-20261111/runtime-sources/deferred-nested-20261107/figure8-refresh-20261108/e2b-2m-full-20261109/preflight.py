from pathlib import Path
import json,os,subprocess,hashlib
import deployment
P=Path(__file__).resolve().parent
s=deployment.capture()
assert s['info']['running_sandboxes']==0 and not s['vmm_pids']
pool=Path('/sys/fs/cgroup/e2b/cpuset.cpus').read_text().strip()
assert pool=='0-3,12-15,24-27,36-39',pool
for name in ['api','orchestrator']:assert s['services'][name]['affinity']==[32,33,34,35,44,45,46,47]
assert hashlib.sha256((P/'bench_fanout.py').read_bytes()).hexdigest()=='5611d06b73d3c33edc63b062dd94f0a579718668f02f492356b2d6bbbd3d2b74'
assert hashlib.sha256((P/'verify_state').read_bytes()).hexdigest()=='cb04e1145a8766eaf072f29eedad63bcf611f69b04483bf682f152b13fbc0806'
s.update(vm_cpuset=pool,mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(P)],text=True)))
(P/'preflight.json').write_text(json.dumps(s,indent=2))
