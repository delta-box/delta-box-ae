from pathlib import Path
import subprocess,json,time,shutil,fcntl,traceback,shlex,signal,os
P=Path(__file__).resolve().parent;W=Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/deferred-nested-20261107')
SSH=['ssh','-q','-i',str(W/'l1/l1_ed25519'),'-p','2222','-o','ControlMaster=auto','-o','ControlPersist=600','-o','ControlPath=/tmp/e2b-repair-l1-%C','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null','-o','BatchMode=yes','ubuntu@127.0.0.1']
def remote(args,**kw):return subprocess.run(SSH+[shlex.join(['sudo','-n',*args])],**kw)
def save(n,o):(P/n).write_text(json.dumps(o,indent=2)+'\n')
lock=open(P.parent/'host.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert not (P/'run-state.json').exists();assert not (W/'host-freq-before.json').exists();assert shutil.disk_usage('/mnt/disk2').free>40*2**30
state={'phase':'preflight','started_epoch':time.time(),'completed_cohorts':[]};save('run-state.json',state)
nbd=quota=template=deployed=frequency=False;sampler=proc=None;errors=[]
def interrupted(*args):raise KeyboardInterrupt('stop requested')
signal.signal(signal.SIGTERM,interrupted)
try:
 remote(['python3',str(P/'preflight.py')],check=True)
 qpid=int((W/'l1/qemu.pid').read_text());q=Path('/proc')/str(qpid);save('host-preflight.json',{'qemu_pid':qpid,'argv':(q/'cmdline').read_bytes().decode().split('\0'),'affinities':{p.name:sorted(os.sched_getaffinity(int(p.name))) for p in (q/'task').iterdir()},'mount':json.loads(subprocess.check_output(['findmnt','-J','-T',str(P)],text=True))})
 template=True;remote(['python3',str(P/'template_setup.py'),'enter'],check=True)
 nbd=True;remote(['python3',str(P/'nbd_capacity.py'),'enter'],check=True)
 quota=True;remote(['python3',str(P/'quota.py'),'enter'],check=True)
 frequency=True;subprocess.run(['python3',str(W/'host_l1_ctl.py'),'enter'],check=True)
 sampler=subprocess.Popen(['python3',str(W/'host_l1_ctl.py'),'sample',str(P/'host-samples.jsonl'),str(P/'STOP_SAMPLER')])
 deployed=True;remote(['python3',str(P/'deployment.py'),'enter'],check=True)
 for cohort in ['acceptance','round1','round2','round3','round4']:
  state.update(phase='running',current=cohort);save('run-state.json',state)
  with (P/(cohort+'-ssh.log')).open('w') as log:
   proc=subprocess.Popen(SSH+[shlex.join(['sudo','-n','python3',str(P/'run_trial.py'),cohort])],stdout=log,stderr=subprocess.STDOUT)
   stop=False
   while proc.poll() is None:
    free=shutil.disk_usage('/mnt/disk2').free
    with (P/'host-disk.jsonl').open('a') as f:f.write(json.dumps({'epoch':time.time(),'free_bytes':free,'cohort':cohort})+'\n')
    if (free<15*2**30 or (P/'STOP').exists()) and not stop:remote(['touch',str(P/'STOP')],check=True);stop=True
    time.sleep(5)
   rc=proc.wait();proc=None;state['completed_cohorts'].append({'cohort':cohort,'rc':rc});save('run-state.json',state)
   if rc:raise RuntimeError(cohort+' failed with rc '+str(rc))
except BaseException as e:errors.append(str(e));traceback.print_exc()
finally:
 state['phase']='restoring';save('run-state.json',state)
 if proc and proc.poll() is None:
  remote(['touch',str(P/'STOP')])
  try:proc.wait(timeout=200)
  except Exception as e:errors.append('probe stop: '+str(e))
 for enabled,name in [(deployed,'deployment'),(nbd,'nbd_capacity'),(quota,'quota'),(template,'template_setup')]:
  if enabled:
   try:
    if remote(['python3',str(P/(name+'.py')),'leave']).returncode:errors.append(name+' restore failed')
   except Exception as e:errors.append(name+' restore '+str(e))
 if sampler:
  (P/'STOP_SAMPLER').touch()
  try:sampler.wait(timeout=20)
  except Exception as e:errors.append('sampler '+str(e))
 if frequency and (W/'host-freq-before.json').exists():
  before=json.loads((W/'host-freq-before.json').read_text());save('host-frequency-before.json',before)
  if subprocess.run(['python3',str(W/'host_l1_ctl.py'),'leave']).returncode:errors.append('frequency restore failed')
  after={c:{k:Path(f'/sys/devices/system/cpu/cpu{c}/cpufreq/{k}').read_text().strip() for k in v} for c,v in before.items()};save('host-frequency-restored.json',{'matched':before==after,'values':after})
  if before!=after:errors.append('frequency restore mismatch')
 (P/'l1-results').mkdir(exist_ok=True)
 if subprocess.run(['rsync','-a','--rsync-path=sudo rsync','--exclude=bin/','--exclude=__pycache__/','--exclude=tmp/','-e',shlex.join(SSH[:-1]),'ubuntu@127.0.0.1:'+str(P)+'/',str(P/'l1-results')+'/']).returncode:errors.append('fetch failed')
 state.update(phase='finished',ok=not errors,errors=errors,finished_epoch=time.time());save('finished.json',state);save('run-state.json',state);print(json.dumps(state),flush=True)
raise SystemExit(0 if state['ok'] else 1)
