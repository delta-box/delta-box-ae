"""Stage the current Table2 CRIU cohort into an owned L1 directory."""
import hashlib,importlib.util,json,os,shutil,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=Path('/home/atc-ae/delta-box-ae')
def now():return time.monotonic_ns()
def write(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(v,indent=2)+'\n');tmp.replace(p)
def filehash(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def treehash(root):
    h=hashlib.sha256();n=0
    for p in sorted(root.rglob('*')):
        rel=str(p.relative_to(root))
        if p.is_symlink():h.update(b'L'+rel.encode()+b'\0'+os.readlink(p).encode())
        elif p.is_file():h.update(b'F'+rel.encode()+b'\0');h.update(bytes.fromhex(filehash(p)));n+=1
    return dict(sha256=h.hexdigest(),files=n)
def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m
def prepare(args):
    sys.path[:0]=[str(ROOT/'ae'),str(ROOT)]
    stage=load(ROOT/'ae/runners/baseline.py','baseline_overlap_stage');stage.VENDOR=HERE/'vendor'
    config=json.loads((HERE/'config.json').read_text())
    item=next(x for x in json.loads((HERE/'inputs.json').read_text()) if x['instance']==args.instance)
    args.out.mkdir(parents=True,exist_ok=False)
    trace=Path(item['trajectory']['path']);assert filehash(trace)==item['trajectory']['sha256']
    assert filehash(trace.parent/'ms_trace.jsonl')==item['ms_trace']['sha256']
    commit=json.loads(trace.read_text()).get('repository',{}).get('commit')
    if not commit:
        commit=subprocess.check_output(['git','-c','safe.directory=*','-C',config['payload']+'/repos/swe-bench_'+args.instance,'rev-parse','HEAD'],text=True).strip()
    payload,traces,records=stage.stage_payload(config,trace,args.instance,args.out,restore_history_order=True,repository_commit=commit)
    driver=args.out/'driver';driver.mkdir()
    for p in (HERE/'vendor/criu').glob('*.py'):shutil.copy2(p,driver/p.name)
    env=dict(os.environ,AE_BASE=str(driver),SPR_PAYLOAD=str(payload),MOCK_TRACES_ROOT=str(traces),MOATLESS_VENV=config['moatless_venv'],
             PYTHONPATH=os.pathsep.join([str(driver),str(payload),str(payload/'moatless-det-src')]),
             MOCK_MESSAGE_POLICY='audit',MOCK_MISMATCH_DIR=str(args.out/'diagnostics'),MOCK_LATENCY_POLICY='recorded',
             FINALBENCH_CRIU=config['criu_bin'],FINALBENCH_CRIU_LAZY_RESTORE='1',FINALBENCH_CRIU_ALIGNED='1',FINALBENCH_MAILBOX_POLL_S='0.001')
    deps=stage.stage_local_dependencies(config,args.out,env);runtime=stage.configure_test_runtime(config,'criu',env)
    os.environ.update(env);sys.path[:0]=env['PYTHONPATH'].split(os.pathsep)
    d=load(driver/'criu_copytree_pilot.py','criu_overlap_driver')
    meta=dict(instance=args.instance,input=item,repository_commit=commit,cpus=sorted(os.sched_getaffinity(0)),
              numa=subprocess.check_output(['numactl','--show'],text=True),kernel=os.uname().release,
              criu=dict(path=config['criu_bin'],sha256=filehash(config['criu_bin'])),
              sources=[dict(path=str(p),sha256=filehash(p)) for p in driver.glob('*.py')],
              inputs=records,dependencies=deps,runtime=runtime,frequency_policy='host-owned 2.1GHz; sampled and restored',
              producer_sources=[dict(path=str(p),sha256=filehash(p)) for p in HERE.glob('*.py')])
    for p in HERE.glob('*.py'):shutil.copy2(p,args.out/p.name)
    write(args.out/'manifest.json',meta)
    return d
