from pathlib import Path
import sys,os,json,time,argparse,signal,hashlib
P=Path(__file__).resolve().parent;sys.path.insert(0,'/mnt/disk2/dyp/cube-l1/sdk-runtime')
import bench_fanout as b
import client_mode as cm
signal.signal(signal.SIGTERM,b._ae_interrupted)
ap=argparse.ArgumentParser();ap.add_argument('cohort',choices=['n64']);a=ap.parse_args()
os.environ.update(CUBE_API_URL='http://127.0.0.1:3000',CUBE_PROXY_NODE_IP='127.0.0.1',TMPDIR=str(P/'tmp'));(P/'tmp').mkdir(exist_ok=True)
args=argparse.Namespace(cube_api_url='http://127.0.0.1:3000',cube_template='cube-agent-full-4c8g-l1e',cube_proxy_node_ip='127.0.0.1',timeout=900,request_timeout=180.,exec_timeout=120.,mem_mib=64,max_workers=64,e2b_batch_size=0)
original_audit=b.audit_identities
vmm_audits=[]
def audit(backend,children,timeout):
 identities=original_audit(backend,children,timeout)
 expected=json.loads((P/'vmm-lock.json').read_text())
 observed=[];digests={}
 for proc in Path('/proc').iterdir():
  if not proc.name.isdigit():continue
  try:
   exe=os.readlink(proc/'exe')
   if 'containerd-shim-cube-rs' not in exe:continue
   stat=(proc/'exe').stat();key=(stat.st_dev,stat.st_ino)
   if key not in digests:digests[key]=hashlib.sha256((proc/'exe').read_bytes()).hexdigest()
   digest=digests[key];assert digest==expected['sha256']
   observed.append({'pid':int(proc.name),'exe':exe,'sha256':digest,'affinity':sorted(os.sched_getaffinity(int(proc.name)))})
  except (FileNotFoundError,ProcessLookupError):pass
 assert len(observed)==len(children)+1,(len(observed),len(children))
 vmm_audits.append({'epoch':time.time(),'vms':observed,'scope':'after main timer; all children and source retained'})
 return identities
b.audit_identities=audit

cm.install();out=P/'results'/a.cohort;out.mkdir(parents=True,exist_ok=True);assert not(out/'finished.json').exists()
plan=[('warmup','reuse',1),('measured','reuse',64)]
(out/'plan.json').write_text(json.dumps({'cohort':a.cohort,'plan':plan,'args':vars(args)},indent=2));state={'cohort':a.cohort,'started_epoch':time.time(),'rows':[]}
for i,(role,mode,n) in enumerate(plan):
 if (P/'STOP').exists():raise RuntimeError('STOP requested')
 name=f'{i:02}-{role}-{mode}-n{n}';cm.set_mode(mode)
 progress={'cohort':a.cohort,'index':i,'role':role,'mode':mode,'n':n,'phase':'quiescence','seconds':45,'epoch':time.time()}
 (P/'progress.json').write_text(json.dumps(progress));(out/(name+'-quiescence.json')).write_text(json.dumps(progress));time.sleep(45)
 progress.update(phase='running',epoch=time.time());(P/'progress.json').write_text(json.dumps(progress));start=len(cm.events);vmm_audits.clear()
 row=b.bench_cube(args,[n])[0];row.update(role=role,mode=mode,cohort=a.cohort,driver_sha256=hashlib.sha256((P/'bench_fanout.py').read_bytes()).hexdigest(),client_instrumentation=cm.events[start:],client_configuration=cm.fact(),vmm_audit_after_timer=list(vmm_audits))
 f=out/(name+'.json');f.write_text(json.dumps(row,indent=2)+'\n');state['rows'].append({'file':str(f),'role':role,'mode':mode,'n':n,'success':row['success'],'ready_ms':row.get('ready_e2e_ms')});(out/'progress-results.json').write_text(json.dumps(state,indent=2))
 if not row['success'] or not row.get('all_children_retained_until_ready'):
  state.update(ok=False,failed=str(f),finished_epoch=time.time());(out/'finished.json').write_text(json.dumps(state,indent=2));raise SystemExit(1)
 assert cm.fact()['shared_contexts_created']<=1
 if mode=='reuse':assert cm.fact()['shared_contexts_created']==1
state.update(ok=True,finished_epoch=time.time());(out/'finished.json').write_text(json.dumps(state,indent=2))
