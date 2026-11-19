import hashlib,json,os,pathlib,subprocess,sys,time
P=pathlib.Path(__file__).resolve().parent
STATE=P/'environment-before.json';CG=pathlib.Path('/sys/fs/cgroup')
VM='0-3,12-15,24-27,36-39';CTL='32-35,44-47';CTLS=set(range(32,36))|set(range(44,48))
DOCKERS=['cube-sandbox-mysql','cube-sandbox-redis','cube-proxy','cube-proxy-coredns']
GROUPS=['cube_sandbox','cube_sandbox/sandbox','cube_sandbox/cubelet','cube_sandbox_v2']
def run(args):return subprocess.check_output(args,text=True,timeout=60)
def capture():
 s={'epoch':time.time(),'cgroups':{},'processes':{},'docker':{},'subtree':(CG/'cube_sandbox/cgroup.subtree_control').read_text().strip()}
 for group in GROUPS:
  for key in ['cpuset.cpus','cpuset.mems']:
   f=CG/group/key;s['cgroups'][str(f)]=f.read_text().strip() if f.exists() else None
 for proc in pathlib.Path('/proc').iterdir():
  if not proc.name.isdigit():continue
  try:
   exe=os.readlink(proc/'exe')
   if not (exe.startswith('/usr/local/services/cubetoolbox/') and pathlib.Path(exe).name in ['cubelet','cubemaster','cube-api','network-agent']):continue
   s['processes'][proc.name]={'exe':exe,'sha256':hashlib.sha256((proc/'exe').read_bytes()).hexdigest(),'affinity':sorted(os.sched_getaffinity(int(proc.name))),'threads':{f.name:sorted(os.sched_getaffinity(int(f.name))) for f in (proc/'task').iterdir()}}
  except (FileNotFoundError,ProcessLookupError):pass
 for n in DOCKERS:
  d=json.loads(run(['docker','inspect',n]))[0]
  s['docker'][n]={'cpus':d['HostConfig']['CpusetCpus'],'mounts':sorted([{k:m[k] for k in ['Source','Destination']} for m in d['Mounts']],key=lambda m:(m['Destination'],m['Source']))}
 return s
def enter():
 assert not STATE.exists()
 s=capture();STATE.write_text(json.dumps(s,indent=2))
 (CG/'cube_sandbox/cgroup.subtree_control').write_text('+cpuset')
 for g,cpus in [('cube_sandbox/sandbox',VM),('cube_sandbox/cubelet',CTL),('cube_sandbox_v2',VM)]:
  (CG/g/'cpuset.mems').write_text('0-3');(CG/g/'cpuset.cpus').write_text(cpus)
 for pid,r in s['processes'].items():
  for t in (pathlib.Path('/proc')/pid/'task').iterdir():
   try:os.sched_setaffinity(int(t.name),CTLS)
   except ProcessLookupError:pass
 for n in DOCKERS:run(['docker','update','--cpuset-cpus',CTL,n])
 during=capture();(P/'environment-during.json').write_text(json.dumps(during,indent=2))
 old=json.loads((P.parent/'fullpage-parallel-final/preflight.json').read_text())
 assert all(old['software'][r['exe']]==r['sha256'] for r in during['processes'].values())
 flush=run(['docker','exec','cube-sandbox-mysql','sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -e "SHOW VARIABLES WHERE Variable_name IN (\'innodb_flush_log_at_trx_commit\',\'sync_binlog\',\'innodb_flush_method\')"'])
 assert flush==old['mysql_flush']
 mounts={}
 targets=['/','/data/cubelet/storage','/data/CubeMaster',str(P)]
 targets += [m['Source'] for d in during['docker'].values() for m in d['mounts']]
 for t in targets:mounts[t]=json.loads(run(['findmnt','-J','-T',t]))
 (P/'preflight.json').write_text(json.dumps({'epoch':time.time(),'mounts':mounts,'mysql_flush':flush,'service_hashes_match_formal':True,'free':run(['free','-b'])},indent=2))
 print('Cube environment prepared',flush=True)
def leave():
 if not STATE.exists():return
 s=json.loads(STATE.read_text());errors=[]
 for f,v in s['cgroups'].items():
  if v is not None:
   try:pathlib.Path(f).write_text(v+'\n')
   except Exception as e:errors.append(str(e))
 if 'cpuset' not in s['subtree'].split():
  try:(CG/'cube_sandbox/cgroup.subtree_control').write_text('-cpuset')
  except Exception as e:errors.append(str(e))
 for pid,r in s['processes'].items():
  proc=pathlib.Path('/proc')/pid
  try:
   assert os.readlink(proc/'exe')==r['exe']
   for t in (proc/'task').iterdir():
    try:os.sched_setaffinity(int(t.name),r['threads'].get(t.name,r['affinity']))
    except ProcessLookupError:pass
  except Exception as e:errors.append(str(e))
 for n,r in s['docker'].items():
  try:run(['docker','update','--cpuset-cpus',r['cpus'],n])
  except Exception as e:errors.append(str(e))
 after=capture()
 for key in ['cgroups','docker','subtree']:
  if after[key]!=s[key]:errors.append(key+' restoration mismatch')
 for pid,r in s['processes'].items():
  if after['processes'].get(pid,{}).get('affinity')!=r['affinity']:errors.append(pid+' affinity mismatch')
 (P/'environment-restored.json').write_text(json.dumps({'epoch':time.time(),'errors':errors,'state':after},indent=2))
 assert not errors,errors
 STATE.rename(P/'environment-before.restored.json')
 print('Cube environment restored',flush=True)
if __name__=='__main__':{'enter':enter,'leave':leave}[sys.argv[1]]()
