from pathlib import Path
import subprocess,os,sys,json,time,signal,fcntl,hashlib
P=Path(__file__).resolve().parent;STATE=P/'nbd-before.json';PARAM=Path('/sys/module/nbd/parameters/nbds_max')
EXPECTED={'183791':('isqk8aan2cs97c4hp2xbs','nbd22'),'183914':('inpe5gs4f620hfsdu4ots','nbd24'),'184059':('i50u7039986qvce5uzwlp','nbd12')}
def run(a):return subprocess.run(a,capture_output=True,text=True,check=True,timeout=120)
def active():return {x.name:(x/'pid').read_text().strip() for x in Path('/sys/block').glob('nbd*') if (x/'pid').exists()}
def stop():run(['systemctl','stop','ae-e2b-api','ae-e2b-orchestrator'])
def start():run(['systemctl','start','ae-e2b-orchestrator','ae-e2b-api'])
def enter():
 assert not STATE.exists();assert PARAM.read_text().strip()=='64'
 sys.path.insert(0,str(P));import deployment
 assert deployment.info()['running_sandboxes']==0
 assert not any('/dev/nbd' in l for l in run(['findmnt','-rn','-o','SOURCE']).stdout.splitlines())
 occupied=active();assert not occupied,occupied
 rec={'epoch':time.time(),'nbds_max':64,'target':128,'active_devices':occupied,'orphans':{}}
 STATE.write_text(json.dumps(rec,indent=2));stop()
 assert not active(),active()
 run(['modprobe','-r','nbd']);run(['modprobe','nbd','nbds_max=128']);assert PARAM.read_text().strip()=='128'
 start();rec.update(after=128,removed_orphan_pids=[pid for pid in rec['orphans'] if not (Path('/proc')/pid).exists()],finished_epoch=time.time())
 (P/'nbd-during.json').write_text(json.dumps(rec,indent=2));print('No pre-existing NBD consumers; device capacity 64 -> 128.',flush=True)
def leave():
 if not STATE.exists():return
 rec=json.loads(STATE.read_text());stop();assert not active(),active()
 if PARAM.exists() and PARAM.read_text().strip()!=str(rec['nbds_max']):
  run(['modprobe','-r','nbd']);run(['modprobe','nbd','nbds_max='+str(rec['nbds_max'])])
 elif not PARAM.exists():run(['modprobe','nbd','nbds_max='+str(rec['nbds_max'])])
 start();r={'epoch':time.time(),'restored_nbds_max':int(PARAM.read_text()),'active_devices':active()};assert r['restored_nbds_max']==rec['nbds_max']
 (P/'nbd-restored.json').write_text(json.dumps(r,indent=2));STATE.rename(P/'nbd-before.restored.json');print('NBD device capacity restored to 64.',flush=True)
if __name__=='__main__':{'enter':enter,'leave':leave}[sys.argv[1]]()
