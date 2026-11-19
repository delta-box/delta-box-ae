#!/usr/bin/env python3
import fcntl,json,shlex,shutil,subprocess,time,traceback
from pathlib import Path
P=Path(__file__).resolve().parent;W=P.parent
SSH=['ssh','-q','-i',str(W/'l1/l1_ed25519'),'-p','2222','-o','ControlMaster=auto','-o','ControlPersist=600',
     '-o','ControlPath=/tmp/criu-overlap-l1-%C','-o','BatchMode=yes','-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null','-o','ConnectTimeout=15','ubuntu@127.0.0.1']
state=dict(phase='preflight',started_epoch=time.time())
def save():
    p=P/'host-progress.tmp';p.write_text(json.dumps(state,indent=2));p.replace(P/'host-progress.json')
def remote(code):
    r=subprocess.run(SSH+['sudo -n python3 -'],input=code,text=True,capture_output=True,timeout=90)
    if r.returncode:raise RuntimeError(r.stderr[-3000:]+r.stdout[-1000:])
    return r.stdout
def main():
    lock=(W/'figure8-refresh-20261108/host.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    entered=False;launched=False;sampler=None;errors=[];save()
    try:
        assert not (W/'host-freq-before.json').exists(),'frequency ownership belongs to another experiment'
        assert shutil.disk_usage('/mnt/disk2').free>60*2**30
        assert not (P/'host-finished.json').exists()
        for name in ['fc-continuous-20261109','figure7-e2b-cube-20261109']:
            assert json.loads((W/name/'finished.json').read_text())['ok'],name
        entered=True;subprocess.run(['python3',str(W/'host_l1_ctl.py'),'enter'],check=True)
        sampler=subprocess.Popen(['python3',str(W/'host_l1_ctl.py'),'sample',str(P/'host-samples.jsonl'),str(P/'STOP_SAMPLER')])
        launch=json.loads(remote('from pathlib import Path\nimport subprocess,json,time\np=Path('+repr(str(P))+')\nassert not (p/"launch.json").exists()\nassert not (p/"STOP").exists()\nwith (p/"run.log").open("w") as log:\n proc=subprocess.Popen(["python3",str(p/"run_l1.py")],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)\nx=dict(pid=proc.pid,epoch=time.time());(p/"launch.json").write_text(json.dumps(x));print(json.dumps(x))'))
        launched=True;state.update(phase='running',launch=launch);save()
        while True:
            if (P/'STOP').exists() or shutil.disk_usage('/mnt/disk2').free<12*2**30:
                remote('from pathlib import Path\nPath('+repr(str(P/'STOP'))+').write_text("host STOP or space guard")')
            reply=json.loads(remote('from pathlib import Path\nimport json\np=Path('+repr(str(P))+')\nf=p/"finished.json";g=p/"progress.json"\nprint(json.dumps(dict(done=f.exists(),state=json.loads(f.read_text()) if f.exists() else json.loads(g.read_text()) if g.exists() else {})))'))
            state['l1']=reply['state'];state['host_free_bytes']=shutil.disk_usage('/mnt/disk2').free;save()
            if reply['done']:
                assert reply['state']['ok'],reply['state'].get('errors');break
            time.sleep(10)
    except BaseException as exc:
        errors.append(repr(exc));traceback.print_exc()
        if launched:
            try:remote('from pathlib import Path\nPath('+repr(str(P/'STOP'))+').write_text("host coordinator failed")')
            except Exception as exc:errors.append('STOP delivery '+repr(exc))
    finally:
        if launched and errors:
            try:
                for _ in range(70):
                    if remote('from pathlib import Path\nprint(Path('+repr(str(P/'finished.json'))+').exists())').strip()=='True':break
                    time.sleep(5)
                else:errors.append('L1 cleanup receipt not received')
            except Exception as exc:errors.append('cleanup confirmation '+repr(exc))
        if sampler:
            (P/'STOP_SAMPLER').touch()
            try:sampler.wait(timeout=15)
            except Exception as exc:errors.append('sampler '+repr(exc))
        if entered and (W/'host-freq-before.json').exists():
            try:subprocess.run(['python3',str(W/'host_l1_ctl.py'),'leave'],check=True)
            except Exception as exc:errors.append('frequency restore '+repr(exc))
        if launched:
            (P/'l1-results').mkdir(exist_ok=True)
            try:
                subprocess.run(['rsync','-a','--rsync-path=sudo rsync','--exclude=__pycache__/','--exclude=tmp/',
                                '-e',shlex.join(SSH[:-1]),'ubuntu@127.0.0.1:'+str(P)+'/',str(P/'l1-results')+'/'],check=True)
            except Exception as exc:errors.append('evidence fetch '+repr(exc))
        state.update(phase='finished',ok=launched and not errors,errors=errors,finished_epoch=time.time());save()
        (P/'host-finished.json').write_text(json.dumps(state,indent=2))
    return 0 if state['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
