#!/usr/bin/env python3
from pathlib import Path
import subprocess, json, time, os, hashlib, gzip, sys
P=Path(__file__).resolve().parent
H=Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b')
T=Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/real-tests-20261106')
STATE=P/'deployment-before.json'
NAMES=['api','orchestrator']
def run(a, **kw): return subprocess.check_output(a,text=True,timeout=120,**kw).strip()
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def info():
 r=subprocess.run(['curl','-sS','--http2-prior-knowledge','--max-time','10','-H','content-type: application/grpc','-H','te: trailers','--data-binary','@-','http://127.0.0.1:5008/InfoService/ServiceInfo'],input=b'\0'*5,capture_output=True,check=True)
 data=r.stdout[5:];pos=0;fields={}
 def varint():
  nonlocal pos
  v=s=0
  while True:
   b=data[pos];pos+=1;v|=(b&127)<<s
   if b<128:return v
   s+=7
 while pos<len(data):
  tag=varint();field,wire=tag>>3,tag&7
  if wire==0:fields[field]=varint()
  elif wire==2:n=varint();pos+=n
  elif wire in [1,5]:pos+=8 if wire==1 else 4
  else:raise RuntimeError('unknown protobuf wire')
 return {k:fields.get(n,0) for k,n in [('running_sandboxes',104),('cpu_count',108),('cpu_allocated',110),('memory_allocated',111)]}
def capture():
 cfg=json.loads((H/'config/services.json').read_text())
 r={'epoch':time.time(),'config_sha256':sha(H/'config/services.json'),'nbd_pool_size':cfg['orchestrator']['env'].get('NBD_POOL_SIZE'),'nbds_max':Path('/sys/module/nbd/parameters/nbds_max').read_text().strip(),'services':{},'info':info(),'vmm_pids':subprocess.run(['pgrep','-x','firecracker'],capture_output=True,text=True).stdout.split()}
 for n in NAMES:
  u='ae-e2b-'+n+'.service';pid=run(['systemctl','show',u,'-p','MainPID','--value']);exe=os.readlink('/proc/'+pid+'/exe')
  effective_env=dict(x.split(b'=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
  r['services'][n]={'envd_timeout_env':effective_env.get(b'ENVD_TIMEOUT',b'').decode(),'pid':int(pid),'exe':exe,'sha256':sha(exe),'affinity':sorted(os.sched_getaffinity(int(pid))),'unit':run(['systemctl','cat',u]),'log_offset':(H/'logs'/(n+'.log')).stat().st_size,'stock_sha256':sha(T/'stock-bin'/n)}
 return r

def enter():
 assert not STATE.exists()
 s=capture();assert s['info']['running_sandboxes']==0,s['info']
 for n in NAMES:
  assert s['services'][n]['exe']==str(T/'stock-bin'/n)
  assert not Path('/etc/systemd/system/ae-e2b-'+n+'.service.d/90-fig8-twoflags.conf').exists()
 assert s['nbd_pool_size']=='32' or s['nbd_pool_size']==32
 STATE.write_text(json.dumps(s,indent=2))
 for n in NAMES:
  d=Path('/etc/systemd/system/ae-e2b-'+n+'.service.d');d.mkdir(exist_ok=True)
  (d/'90-fig8-twoflags.conf').write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 -B '+str(P/'launch_configured.py')+' '+n+'\n')
 subprocess.run(['systemctl','stop','ae-e2b-api','ae-e2b-orchestrator'],check=True,timeout=120)
 subprocess.run(['systemctl','daemon-reload'],check=True)
 subprocess.run(['systemctl','start','ae-e2b-orchestrator','ae-e2b-api'],check=True,timeout=120)
 for _ in range(30):
  try:
   a=capture()
   if all(a['services'][n]['exe']==str(P/'bin'/n) for n in NAMES):break
  except Exception:pass
  time.sleep(2)
 else:raise RuntimeError('configured services did not become ready')
 assert a['config_sha256']==s['config_sha256'] and a['nbd_pool_size']==s['nbd_pool_size'] and a['nbds_max']==s['nbds_max']
 assert a['services']['orchestrator']['envd_timeout_env']=='60s'
 (P/'deployment-during.json').write_text(json.dumps(a,indent=2));print('Configured API and orchestrator active; NBD unchanged.',flush=True)

def leave():
 if not STATE.exists():return
 s=json.loads(STATE.read_text());errors=[]
 try:(P/'before-restoration.json').write_text(json.dumps(capture(),indent=2))
 except Exception as e:errors.append('pre-restore capture: '+str(e))
 subprocess.run(['systemctl','stop','ae-e2b-api','ae-e2b-orchestrator'],check=True,timeout=120)
 for n in NAMES:
  d=Path('/etc/systemd/system/ae-e2b-'+n+'.service.d');(d/'90-fig8-twoflags.conf').unlink(missing_ok=True)
  try:d.rmdir()
  except OSError:pass
 subprocess.run(['systemctl','daemon-reload'],check=True)
 subprocess.run(['systemctl','start','ae-e2b-orchestrator','ae-e2b-api'],check=True,timeout=120)
 for _ in range(30):
  try:
   a=capture()
   if all(a['services'][n]['exe']==s['services'][n]['exe'] for n in NAMES):break
  except Exception:pass
  time.sleep(2)
 else:raise RuntimeError('stock services did not return')
 for n in NAMES:
  for key in ['exe','sha256','unit','stock_sha256','affinity','envd_timeout_env']:
   if a['services'][n][key]!=s['services'][n][key]:errors.append(n+': '+key+' mismatch')
  with open(H/'logs'/(n+'.log'),'rb') as f,gzip.open(P/(n+'-experiment.log.gz'),'wb') as g:
   f.seek(s['services'][n]['log_offset'])
   while True:
    chunk=f.read(1024*1024)
    if not chunk:break
    g.write(chunk)
 for k in ['config_sha256','nbd_pool_size','nbds_max']:
  if a[k]!=s[k]:errors.append(k+' mismatch')
 (P/'deployment-restored.json').write_text(json.dumps({'state':a,'errors':errors},indent=2))
 assert not errors,errors
 STATE.rename(P/'deployment-before.restored.json');print('Original official services and configuration restored.',flush=True)
if __name__=='__main__':{'enter':enter,'leave':leave,'info':lambda:print(json.dumps(info()))}[sys.argv[1]]()
