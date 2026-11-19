#!/usr/bin/env python3
import fcntl,hashlib,json,os,shutil,signal,subprocess,time,traceback
from pathlib import Path
from audit import audit
P=Path(__file__).resolve().parent
LANES=[['astropy__astropy-7746','django__django-14997','sympy__sympy-17630'],
       ['pylint-dev__pylint-7228','django__django-14672','sympy__sympy-22840'],
       ['matplotlib__matplotlib-23964','pylint-dev__pylint-7080','matplotlib__matplotlib-26020'],
       ['astropy__astropy-14182','astropy__astropy-13453','psf__requests-863']]
PILOTS=[(0,'sympy__sympy-17630'),(1,'django__django-14672'),(2,'matplotlib__matplotlib-23964'),(3,'psf__requests-863')]
def write(name,value):
    t=P/(name+'.tmp');t.write_text(json.dumps(value,indent=2));t.replace(P/name)
def main():
    lock=(P/'runner.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    active=[];errors=[];state=dict(started_epoch=time.time(),phase='preflight',completed=[])
    write('progress.json',state)
    try:
        assert not (P/'finished.json').exists()
        assert shutil.disk_usage('/mnt/disk2').free>70*2**30
        for name,digest in json.loads((P/'source-lock.json').read_text()).items():
            assert hashlib.sha256((P/name).read_bytes()).hexdigest()==digest,name
        specs={x['instance']:x for x in json.loads((P/'inputs.json').read_text())}
        for s in specs.values():
            for key in ['trajectory','ms_trace']:
                assert hashlib.sha256(Path(s[key]['path']).read_bytes()).hexdigest()==s[key]['sha256']
            assert hashlib.sha256(Path(s['schedule_path']).read_bytes()).hexdigest()==s['schedule_sha256']
        mount=json.loads(subprocess.check_output(['findmnt','-J','-T',str(P)],text=True))
        assert mount['filesystems'][0]['source']=='/dev/vda1'
        write('preflight.json',dict(mount=mount,epoch=time.time(),free_bytes=shutil.disk_usage('/mnt/disk2').free))
        batches=[('pilot',4,PILOTS[:2]),('pilot',4,PILOTS[2:])]
        batches += [('main',29,[(lane,LANES[lane][index]) for lane in pair]) for index in range(3) for pair in [[0,1],[2,3]]]
        for phase,n,pair in batches:
            assert not (P/'STOP').exists(),'STOP requested'
            assert shutil.disk_usage('/mnt/disk2').free>50*2**30,'insufficient space for next pair'
            active=[]
            for lane,instance in pair:
                out=P/'results'/phase/instance;out.parent.mkdir(parents=True,exist_ok=True)
                tmp=P/'tmp'/f'{phase}-{lane}';tmp.mkdir(parents=True,exist_ok=True)
                env=dict(os.environ,TMPDIR=str(tmp))
                cmd=['numactl','--all',f'--physcpubind={lane*12}-{lane*12+7}',f'--membind={lane}',
                     '/mnt/disk2/dyp/moatless_det_venv/bin/python',str(P/'run_trace.py'),'--instance',instance,'--out',str(out),'--max-steps',str(n)]
                with (out.parent/(instance+'.log')).open('w') as log:
                    proc=subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                active.append(dict(proc=proc,lane=lane,instance=instance,path=out,started=time.time()))
            state.update(phase=phase,active=[dict(instance=x['instance'],lane=x['lane'],pid=x['proc'].pid) for x in active]);write('progress.json',state)
            stop=False
            while any(x['proc'].poll() is None for x in active):
                free=shutil.disk_usage('/mnt/disk2').free
                bad=any(x['proc'].poll() not in (None,0) for x in active)
                with (P/'resources.jsonl').open('a') as f:f.write(json.dumps(dict(epoch=time.time(),free_bytes=free))+'\n')
                if (bad or free<15*2**30 or (P/'STOP').exists() or any(time.time()-x['started']>7200 for x in active)) and not stop:
                    stop=True
                    for x in active:
                        if x['proc'].poll() is None:x['proc'].terminate()
                time.sleep(3)
            assert not stop,'interrupted pair; keep all partial evidence'
            for x in active:
                assert x['proc'].returncode==0,(x['instance'],x['proc'].returncode)
                rec=audit(x['path'],specs[x['instance']]);rec['phase']=phase;state['completed'].append(rec)
            active=[];state['active']=[];write('progress.json',state)
            subprocess.run(['fstrim','/'],check=True,capture_output=True)
        assert len([x for x in state['completed'] if x['phase']=='main'])==12
    except BaseException as exc:
        errors.append(repr(exc));traceback.print_exc()
    finally:
        for x in active:
            if x['proc'].poll() is None:
                x['proc'].terminate()
                try:x['proc'].wait(timeout=330)
                except Exception as exc:errors.append('cleanup timeout '+str(exc))
        state.update(phase='finished',ok=not errors,errors=errors,finished_epoch=time.time(),active=[])
        write('finished.json',state);write('progress.json',state)
    return 0 if not errors else 1
if __name__=='__main__':raise SystemExit(main())
