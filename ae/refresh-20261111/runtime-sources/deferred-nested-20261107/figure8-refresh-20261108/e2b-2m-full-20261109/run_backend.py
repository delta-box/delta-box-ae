from pathlib import Path
import argparse,json,os,signal,time,subprocess,hashlib,re,shutil
import bench_fanout as b
P=Path(__file__).resolve().parent;H=Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b')
signal.signal(signal.SIGTERM,b._ae_interrupted)
ap=argparse.ArgumentParser();ap.add_argument('cohort',choices=['acceptance','round1','round2','round3','round4']);a=ap.parse_args()
os.environ.update({k:str(v) for k,v in json.loads((H/'config/client.json').read_text()).items()})
os.environ['TMPDIR']=str(P/'tmp');(P/'tmp').mkdir(exist_ok=True)
template=json.loads((P/'template.json').read_text())
args=argparse.Namespace(e2b_api_url='http://127.0.0.1:3100',e2b_sandbox_url='http://127.0.0.1:3102',e2b_api_key='',e2b_template=template['template']+':default',timeout=900,request_timeout=180.,exec_timeout=120.,mem_mib=64,max_workers=64,e2b_batch_size=0)
out=P/'results'/a.cohort;out.mkdir(parents=True,exist_ok=True);assert not (out/'finished.json').exists()
current={};audit_rows=[];original_audit=b.audit_identities

def audit(backend,children,timeout):
 identities=original_audit(backend,children,timeout)
 # Called by unchanged driver after ready_finished_epoch; all VMs still alive.
 vms=[]
 for pid in subprocess.run(['pgrep','-x','firecracker'],capture_output=True,text=True).stdout.split():
  q=Path('/proc')/pid;cmd=(q/'cmdline').read_bytes().decode().split('\0');sock=cmd[cmd.index('--api-sock')+1]
  c=json.loads(subprocess.check_output(['curl','-fsS','--max-time','5','--unix-socket',sock,'http://localhost/machine-config'],text=True))
  smaps=(q/'smaps').read_text();sizes=[];blocks=re.split(r'(?=^[0-9a-f]+-[0-9a-f]+ )',smaps,flags=re.M)
  for block in blocks:
   m=re.search(r'^Size:\s+(\d+) kB',block,re.M)
   if m and int(m[1])>=100*1024:
    sizes.append({k:int(v) for k,v in re.findall(r'^(Size|KernelPageSize|MMUPageSize|Private_Hugetlb|Shared_Hugetlb):\s+(\d+) kB',block,re.M)})
  vms.append({'pid':int(pid),'api_socket':sock,'machine_config':c,'large_mappings':sizes,'affinity':sorted(os.sched_getaffinity(int(pid)))})
 rec={'epoch':time.time(),'vms':vms,'hugepages':[l for l in Path('/proc/meminfo').read_text().splitlines() if 'Huge' in l],'snapshot_headers':[]}
 dst=out/(current['name']+'-headers');dst.mkdir(exist_ok=True)
 for d in (H/'storage/templates').iterdir():
  meta=d/'metadata.json'
  if meta.is_file() and meta.stat().st_mtime>=current['start']-2:
   sub=dst/d.name;sub.mkdir(exist_ok=True)
   for f in [meta,*d.glob('*.header')]:shutil.copy2(f,sub/f.name)
   rec['snapshot_headers'].append(d.name)
 audit_rows.append(rec)
 assert len(vms)==len(children)+1,(len(vms),len(children))
 for vm in vms:
  c=vm['machine_config'];assert c['vcpu_count']==4 and c['mem_size_mib']==8192 and c['huge_pages']=='2M',c
 return identities
b.audit_identities=audit
orders=[[1,4,16,64],[4,16,64,1],[16,64,1,4],[64,1,4,16]]
plan=[('acceptance',n) for n in [1,16,64]] if a.cohort=='acceptance' else [('warmup',1)]+[('measured',n) for n in orders[int(a.cohort[-1])-1]]
(out/'plan.json').write_text(json.dumps({'cohort':a.cohort,'points':plan,'args':vars(args)|{'e2b_api_key':'existing private config'},'driver_sha256':hashlib.sha256((P/'bench_fanout.py').read_bytes()).hexdigest(),'verifier_sha256':hashlib.sha256((P/'verify_state').read_bytes()).hexdigest()},indent=2))
state={'cohort':a.cohort,'started_epoch':time.time(),'rows':[]}
for role,n in plan:
 if (P/'STOP').exists():raise RuntimeError('STOP requested')
 name=f'{role}-n{n}';current={'name':name,'start':time.time()};audit_rows=[]
 progress={'cohort':a.cohort,'role':role,'n':n,'phase':'quiescence','seconds':45,'epoch':time.time()}
 (P/'progress.json').write_text(json.dumps(progress));(out/(name+'-quiescence.json')).write_text(json.dumps(progress))
 time.sleep(45);progress.update(phase='running',epoch=time.time());(P/'progress.json').write_text(json.dumps(progress));current['start']=time.time()
 row=b.bench_e2b(args,[n])[0];row.update(role=role,cohort=a.cohort,diagnostics_after_timer=audit_rows)
 dest=out/(name+'.json');dest.write_text(json.dumps(row,indent=2));state['rows'].append({'file':str(dest),'n':n,'role':role,'success':row['success'],'ready_ms':row.get('ready_e2e_ms')})
 (out/'progress-results.json').write_text(json.dumps(state,indent=2))
 if not row['success'] or not row['all_children_retained_until_ready']:
  state.update(ok=False,failed=str(dest),finished_epoch=time.time());(out/'finished.json').write_text(json.dumps(state,indent=2));raise SystemExit(1)
state.update(ok=True,finished_epoch=time.time());(out/'finished.json').write_text(json.dumps(state,indent=2))
