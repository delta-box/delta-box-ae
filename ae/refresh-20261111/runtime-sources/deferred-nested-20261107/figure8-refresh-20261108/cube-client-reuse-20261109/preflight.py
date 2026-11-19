from pathlib import Path
import hashlib,json,sys,subprocess,urllib.request,time,importlib.util
P=Path(__file__).resolve().parent;sys.path.insert(0,'/mnt/disk2/dyp/cube-l1/sdk-runtime')
from cubesandbox import Sandbox,Config
from db_read import query
assert not Sandbox.list(config=Config(api_url='http://127.0.0.1:3000'))
assert not subprocess.run(['pgrep','-x','firecracker'],capture_output=True).stdout
nodes=json.load(urllib.request.urlopen('http://127.0.0.1:8089/internal/node',timeout=10));assert {n['InstanceID'] for n in nodes['data']}=={'10.0.2.15'}
row=query("SELECT * FROM t_cube_node_status WHERE node_id='198.18.0.1'");fields,values=[x.split('\t') for x in row.strip().splitlines()];assert values[fields.index('deleted_at')]!='NULL'
assert row==(P.parent/'cube-orphan-fixed-20261109/orphan-status-after.tsv').read_text()
assert importlib.util.find_spec('e2b') is None,'Unexpected command protocol branch'
expected=json.loads((P.parent/'scaling-audit-20261109/offline-results.json').read_text())['configuration']['source'];sdk=Path('/mnt/disk2/dyp/cube-l1/sdk-runtime')
for name,fact in expected.items():assert hashlib.sha256((sdk/name).read_bytes()).hexdigest()==fact['sha256']
assert hashlib.sha256((P/'bench_fanout.py').read_bytes()).hexdigest()=='5611d06b73d3c33edc63b062dd94f0a579718668f02f492356b2d6bbbd3d2b74'
assert hashlib.sha256((P/'verify_state').read_bytes()).hexdigest()=='cb04e1145a8766eaf072f29eedad63bcf611f69b04483bf682f152b13fbc0806'
(P/'input-hashes.json').write_text(json.dumps({q.name:hashlib.sha256(q.read_bytes()).hexdigest() for q in P.iterdir() if q.is_file() and (q.suffix in ['.py','.md','.c'] or q.name=='verify_state')},indent=2)+'\n')
(P/'preflight-idle.json').write_text(json.dumps({'epoch':time.time(),'nodes':nodes,'orphan_status_sha256':hashlib.sha256(row.encode()).hexdigest(),'sdk_files':expected,'no_VMMs':True,'no_sandboxes':True},indent=2)+'\n')
print('Idle Cube and preserved repair verified; existing SDK and binary hashes match.',flush=True)
