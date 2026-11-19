from pathlib import Path
import subprocess,sys,time,json,os,signal,shutil
P=Path(__file__).resolve().parent;cohort=sys.argv[1];out=P/'results'/cohort;out.mkdir(parents=True,exist_ok=True)
py='/mnt/disk2/dyp/ae-hosted-20260922/e2b/sdk-venv/bin/python'
with (out/'run.log').open('w') as log,(out/'resources.jsonl').open('w') as resources:
 proc=subprocess.Popen(['taskset','-c','4-7',py,'-u',str(P/'run_backend.py'),cohort],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 (out/'process.json').write_text(json.dumps({'pid':proc.pid,'epoch':time.time()}));started=time.time();reason=None;terminated=None
 while proc.poll() is None:
  m={l.split(':')[0]:int(l.split()[1]) for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith(('MemAvailable:','MemFree:','HugePages_Total:','HugePages_Free:','HugePages_Rsvd:','HugePages_Surp:'))}
  row={'epoch':time.time(),'free_bytes':shutil.disk_usage(P).free,**m}
  row['hugepages_per_node']={str(q.parent.parent.name):{k:int((q/k).read_text()) for k in ['nr_hugepages','free_hugepages','surplus_hugepages']} for q in Path('/sys/devices/system/node').glob('node*/hugepages/hugepages-2048kB')}

  base=Path('/sys/fs/cgroup/e2b');row['e2b']={k:(base/k).read_text().strip() for k in ['memory.current','memory.peak','cpu.stat']}
  resources.write(json.dumps(row)+'\n');resources.flush()
  if row['free_bytes']<20*2**30:reason='L1 disk below20GiB'
  if m['MemAvailable']<8*2**20:reason='L1 available memory below8GiB'
  if m['HugePages_Free']-m['HugePages_Rsvd']<512:reason='hugetlb free unreserved margin below1GiB'
  if time.time()-started>1800:reason='cohort exceeded30min'
  if (P/'STOP').exists():reason='STOP '+(P/'STOP').read_text()
  if reason and terminated is None:
   os.killpg(proc.pid,signal.SIGTERM);terminated=time.time();(out/'stopped.json').write_text(json.dumps({'reason':reason,'epoch':terminated}))
  if terminated and time.time()-terminated>180:os.killpg(proc.pid,signal.SIGKILL)
  time.sleep(2)
 rc=proc.wait();(out/'exit.json').write_text(json.dumps({'rc':rc,'reason':reason,'epoch':time.time()}))
raise SystemExit(rc if rc>=0 else 128-rc)
