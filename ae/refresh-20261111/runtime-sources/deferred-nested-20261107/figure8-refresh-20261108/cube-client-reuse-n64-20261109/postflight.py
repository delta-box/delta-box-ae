from pathlib import Path
import sys,json,subprocess,urllib.request,hashlib,time
P=Path(__file__).resolve().parent;sys.path.insert(0,'/mnt/disk2/dyp/cube-l1/sdk-runtime')
from cubesandbox import Sandbox,Config
from db_read import query
pre=json.loads((P/'preflight-idle.json').read_text());remaining=Sandbox.list(config=Config(api_url='http://127.0.0.1:3000'));vmm=subprocess.run(['pgrep','-x','firecracker'],capture_output=True,text=True).stdout.split();nodes=json.load(urllib.request.urlopen('http://127.0.0.1:8089/internal/node',timeout=10));row=query("SELECT * FROM t_cube_node_status WHERE node_id='198.18.0.1'")
assert not remaining and not vmm and {x['InstanceID'] for x in nodes['data']}=={'10.0.2.15'}
live=json.loads((P/'vmm-lock.json').read_text())
assert hashlib.sha256(Path(live['exe']).read_bytes()).hexdigest()==live['sha256']
cube_vm=[]
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit():continue
 try:
  if 'containerd-shim-cube-rs' in str((proc/'exe').resolve(strict=True)):cube_vm.append(proc.name)
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
assert not cube_vm
assert hashlib.sha256(row.encode()).hexdigest()==pre['orphan_status_sha256']
sdk=Path('/mnt/disk2/dyp/cube-l1/sdk-runtime');changed=[n for n,fact in pre['sdk_files'].items() if hashlib.sha256((sdk/n).read_bytes()).hexdigest()!=fact['sha256']];assert not changed
restore=json.loads((P/'environment-restored.json').read_text());assert not restore['errors']
from environment_cube import capture
before=json.loads((P/'environment-before.restored.json').read_text());actual=capture()
for key in ['cgroups','docker','subtree']:assert actual[key]==before[key]
for pid,r in before['processes'].items():
 a=actual['processes'][pid]
 for key in ['exe','sha256','affinity']:assert a[key]==r[key]
 for tid,aff in a['threads'].items():assert aff==r['threads'].get(tid,r['affinity'])
original=json.loads((P.parent/'fullpage-parallel-final/preflight.json').read_text())['software'];bins={}
for name,digest in original.items():
 if '/cubetoolbox/' in name or '/cube-l1/' in name:
  f=Path(name)
  if f.is_file():
   observed=hashlib.sha256(f.read_bytes()).hexdigest();assert observed==digest,(name,observed,digest);bins[name]=observed
result={'software_hashes':bins,'Cube_VMM':live,'remaining_Cube_VMMs':cube_vm,'all_current_service_threads_restored':True,'epoch':time.time(),'remaining_sandboxes':remaining,'remaining_VMMs':vmm,'SDK_files_changed':changed,'orphan_row_unchanged':True,'nodes':nodes,'environment_restore_errors':restore['errors']}
(P/'postflight.json').write_text(json.dumps(result,indent=2)+'\n');print('No remaining sandboxes/VMs; SDK files and orphan record unchanged; environment restored.')
