from pathlib import Path
import sys,os,json,time,argparse,signal,hashlib
P=Path(__file__).resolve().parent;sys.path.insert(0,'/mnt/disk2/dyp/cube-l1/sdk-runtime')
import bench_fanout as b
import client_mode as cm
signal.signal(signal.SIGTERM,b._ae_interrupted)
ap=argparse.ArgumentParser();ap.add_argument('cohort',choices=['control','round1','round2','round3','round4']);a=ap.parse_args()
os.environ.update(CUBE_API_URL='http://127.0.0.1:3000',CUBE_PROXY_NODE_IP='127.0.0.1',TMPDIR=str(P/'tmp'));(P/'tmp').mkdir(exist_ok=True)
args=argparse.Namespace(cube_api_url='http://127.0.0.1:3000',cube_template='cube-agent-full-4c8g-l1e',cube_proxy_node_ip='127.0.0.1',timeout=900,request_timeout=180.,exec_timeout=120.,mem_mib=64,max_workers=64,e2b_batch_size=0)
cm.install();out=P/'results'/a.cohort;out.mkdir(parents=True,exist_ok=True);assert not(out/'finished.json').exists()
if a.cohort=='control':
 plan=[('warmup','original',1),('warmup','reuse',1)]+[('control',mode,n) for mode,n in [('original',1),('reuse',1),('reuse',16),('original',16),('reuse',1),('original',1),('original',16),('reuse',16)]]
else:
 order=[[1,4,16],[4,16,1],[16,1,4],[1,16,4]][int(a.cohort[-1])-1];plan=[('warmup','reuse',1)]+[('measured','reuse',n) for n in order]
(out/'plan.json').write_text(json.dumps({'cohort':a.cohort,'plan':plan,'args':vars(args)},indent=2));state={'cohort':a.cohort,'started_epoch':time.time(),'rows':[]}
for i,(role,mode,n) in enumerate(plan):
 if (P/'STOP').exists():raise RuntimeError('STOP requested')
 name=f'{i:02}-{role}-{mode}-n{n}';cm.set_mode(mode)
 progress={'cohort':a.cohort,'index':i,'role':role,'mode':mode,'n':n,'phase':'quiescence','seconds':45,'epoch':time.time()}
 (P/'progress.json').write_text(json.dumps(progress));(out/(name+'-quiescence.json')).write_text(json.dumps(progress));time.sleep(45)
 progress.update(phase='running',epoch=time.time());(P/'progress.json').write_text(json.dumps(progress));start=len(cm.events)
 row=b.bench_cube(args,[n])[0];row.update(role=role,mode=mode,cohort=a.cohort,driver_sha256=hashlib.sha256((P/'bench_fanout.py').read_bytes()).hexdigest(),client_instrumentation=cm.events[start:],client_configuration=cm.fact())
 f=out/(name+'.json');f.write_text(json.dumps(row,indent=2)+'\n');state['rows'].append({'file':str(f),'role':role,'mode':mode,'n':n,'success':row['success'],'ready_ms':row.get('ready_e2e_ms')});(out/'progress-results.json').write_text(json.dumps(state,indent=2))
 if not row['success'] or not row.get('all_children_retained_until_ready'):
  state.update(ok=False,failed=str(f),finished_epoch=time.time());(out/'finished.json').write_text(json.dumps(state,indent=2));raise SystemExit(1)
 assert cm.fact()['shared_contexts_created']<=1
 if mode=='reuse':assert cm.fact()['shared_contexts_created']==1
state.update(ok=True,finished_epoch=time.time());(out/'finished.json').write_text(json.dumps(state,indent=2))
