import fcntl,json,os,pathlib,shlex,shutil,signal,subprocess,sys,time,traceback
P=pathlib.Path(__file__).resolve().parent;W=P.parent.parent
SSH=['ssh','-q','-i',str(W/'l1/l1_ed25519'),'-p','2222','-o','ControlMaster=auto','-o','ControlPersist=600','-o','ControlPath=/tmp/e2b-repair-l1-%C','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null','-o','BatchMode=yes','ubuntu@127.0.0.1']
def remote(args,**kw):return subprocess.run(SSH+[shlex.join(['sudo','-n',*args])],**kw)
def save(name,obj):(P/name).write_text(json.dumps(obj,indent=2)+'\n')
lock=open(P.parent/'host.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert not (W/'host-freq-before.json').exists()
assert not (P/'run-state.json').exists()
assert shutil.disk_usage('/mnt/disk2').free>30*2**30
state={'started_epoch':time.time(),'phase':'prepare','completed_rounds':[]}
save('run-state.json',state)
sampler=None;entered=False;env=False;logs=False;errors=[];proc=None
def interrupted(*args):raise KeyboardInterrupt('stop requested')
signal.signal(signal.SIGTERM,interrupted)
try:
 qpid=int((W/'l1/qemu.pid').read_text());q=pathlib.Path('/proc')/str(qpid)
 save('host-preflight.json',{'epoch':time.time(),'qemu_pid':qpid,'qemu_argv':(q/'cmdline').read_bytes().decode().split('\0'),'thread_affinities':{t.name:sorted(os.sched_getaffinity(int(t.name))) for t in (q/'task').iterdir()},'nvme_mount':json.loads(subprocess.check_output(['findmnt','-J','-T',str(P)],text=True))})
 remote(['/mnt/disk2/dyp/cube-l1/python-venv/bin/python',str(P/'preflight.py')],check=True)
 logs=True;remote(['python3',str(P/'log_capture.py'),'start'],check=True)
 entered=True;subprocess.run(['python3',str(W/'host_l1_ctl.py'),'enter'],check=True)
 sampler=subprocess.Popen(['python3',str(W/'host_l1_ctl.py'),'sample',str(P/'host-samples.jsonl'),str(P/'STOP_SAMPLER')])
 env=True;remote(['python3',str(P/'environment_cube.py'),'enter'],check=True)
 for cohort in ['n64']:
  state.update(phase='running',current_cohort=cohort);save('run-state.json',state)
  with open(P/(cohort+'-ssh.log'),'w') as log:
   proc=subprocess.Popen(SSH+[shlex.join(['sudo','-n','python3',str(P/'run_trial.py'),cohort])],stdout=log,stderr=subprocess.STDOUT)
   stop_sent=False
   while proc.poll() is None:
    free=shutil.disk_usage('/mnt/disk2').free
    with open(P/'host-disk.jsonl','a') as out:out.write(json.dumps({'epoch':time.time(),'free_bytes':free})+'\n')
    if (free<15*2**30 or (P/'STOP').exists()) and not stop_sent:
     remote(['python3','-c','from pathlib import Path;Path('+repr(str(P/'STOP'))+').write_text("host stop or NVMe below 15 GiB")'],check=True);stop_sent=True
    time.sleep(5)
   rc=proc.wait();proc=None
   state['completed_rounds'].append({'cohort':cohort,'rc':rc});save('run-state.json',state)
   if rc:raise RuntimeError(f'{cohort} failed: {rc}')
except BaseException as e:
 errors.append(str(e));traceback.print_exc()
finally:
 state['phase']='restoring';save('run-state.json',state)
 if proc and proc.poll() is None:
  remote(['python3','-c','from pathlib import Path;Path('+repr(str(P/'STOP'))+').write_text("host runner interrupted")'])
  try:proc.wait(timeout=180)
  except Exception as e:errors.append('round stopping: '+str(e))
 for enabled,name,args in [(env,'environment restore',['python3',str(P/'environment_cube.py'),'leave']),(logs,'log capture',['python3',str(P/'log_capture.py'),'finish'])]:
  if enabled:
   try:
    if remote(args).returncode:errors.append(name+' failed')
   except Exception as e:errors.append(name+': '+str(e))
 if sampler:
  (P/'STOP_SAMPLER').touch()
  try:sampler.wait(timeout=20)
  except Exception as e:errors.append('sampler: '+str(e))
 if entered and (W/'host-freq-before.json').exists():
  before=json.loads((W/'host-freq-before.json').read_text());save('host-frequency-before.json',before)
  if subprocess.run(['python3',str(W/'host_l1_ctl.py'),'leave']).returncode:errors.append('frequency restore failed')
  after={c:{k:pathlib.Path(f'/sys/devices/system/cpu/cpu{c}/cpufreq/{k}').read_text().strip() for k in v} for c,v in before.items()}
  save('host-frequency-restored.json',{'matched':after==before,'values':after})
  if before!=after:errors.append('frequency restoration mismatch')
 if env:
  try:
   if remote(['/mnt/disk2/dyp/cube-l1/python-venv/bin/python',str(P/'postflight.py')]).returncode:errors.append('postflight failed')
  except Exception as e:errors.append('postflight: '+str(e))
 (P/'l1-results').mkdir(exist_ok=True)
 fetch=subprocess.run(['rsync','-a','--rsync-path=sudo rsync','--exclude=__pycache__/','-e',shlex.join(SSH[:-1]),'ubuntu@127.0.0.1:'+str(P)+'/',str(P/'l1-results')+'/'])
 if fetch.returncode:errors.append('fetch failed '+str(fetch.returncode))
 state.update(phase='finished',ok=not errors,errors=errors,finished_epoch=time.time());save('finished.json',state);save('run-state.json',state)
 print(json.dumps(state),flush=True)
raise SystemExit(0 if state['ok'] else 1)
