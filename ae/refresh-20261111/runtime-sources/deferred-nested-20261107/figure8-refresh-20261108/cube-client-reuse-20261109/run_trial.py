#!/usr/bin/env python3
import pathlib,subprocess,sys,time,json,os,signal,shutil
P=pathlib.Path(__file__).resolve().parent
kind=sys.argv[1];backend='cube'
out=P/'results'/kind;out.mkdir(parents=True,exist_ok=True)
py='/mnt/disk2/dyp/cube-l1/python-venv/bin/python' if backend=='cube' else '/mnt/disk2/dyp/ae-hosted-20260922/e2b/sdk-venv/bin/python'
with open(out/'run.log','w') as log,open(out/'resources.jsonl','w') as resources:
 p=subprocess.Popen(['taskset','-c','4-7',py,str(P/'run_backend.py'),kind],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 (out/'process.json').write_text(json.dumps({'pid':p.pid,'started_epoch':time.time()}))
 reason=None;terminated=None;started=time.time()
 while p.poll() is None:
  mem=dict((l.split(':')[0],int(l.split()[1])*1024) for l in pathlib.Path('/proc/meminfo').read_text().splitlines() if l.startswith(('MemAvailable:','MemFree:','Cached:')))
  v={'epoch':time.time(),'free_bytes':shutil.disk_usage('/').free,'storage_free_bytes':shutil.disk_usage('/data/cubelet/storage').free,**mem}
  for n in ['e2b','cube_sandbox','cube_sandbox_v2']:
   base=pathlib.Path('/sys/fs/cgroup')/n
   v[n]={k:(base/k).read_text().strip() for k in ['memory.current','memory.peak','cpu.stat'] if (base/k).exists()}
  resources.write(json.dumps(v)+'\n');resources.flush()
  if time.time()-started>1800:reason='round exceeded 30 minute bound'
  if v['free_bytes']<8*2**30:reason='L1 disk below 8 GiB'
  if v['storage_free_bytes']<15*2**30:reason='NVMe storage below15 GiB'
  if v['MemAvailable']<8*2**30:reason='L1 memory below 8 GiB'
  if (P/'STOP').exists():reason='host STOP: '+(P/'STOP').read_text()
  if reason and terminated is None:
   os.killpg(p.pid,signal.SIGTERM);terminated=time.time();(out/'stopped.json').write_text(json.dumps({'reason':reason,'epoch':terminated}))
  if terminated and time.time()-terminated>150:os.killpg(p.pid,signal.SIGKILL)
  time.sleep(2)
 rc=p.wait();(out/'exit.json').write_text(json.dumps({'rc':rc,'reason':reason,'finished_epoch':time.time()}))
raise SystemExit(rc if rc>=0 else 128-rc)
