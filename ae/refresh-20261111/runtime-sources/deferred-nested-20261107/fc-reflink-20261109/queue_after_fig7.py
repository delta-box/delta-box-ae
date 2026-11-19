"""Success-gated, exclusive host coordinator for the CoW FC cohort."""
import fcntl
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import traceback

P = Path(__file__).resolve().parent
W = P.parent
SSH = ['ssh','-q','-i',str(W/'l1/l1_ed25519'),'-p','2222',
       '-o','ControlMaster=auto','-o','ControlPersist=600',
       '-o','ControlPath=/tmp/fc-reflink-l1-%C','-o','BatchMode=yes',
       '-o','StrictHostKeyChecking=no','-o','UserKnownHostsFile=/dev/null',
       '-o','ConnectTimeout=15','ubuntu@127.0.0.1']
state = dict(phase='queued-after-figure7', queued_epoch=time.time(), errors=[])


def save():
    tmp = P/'queue-state.tmp'
    tmp.write_text(json.dumps(state,indent=2)); tmp.replace(P/'queue-state.json')


def remote(code):
    r = subprocess.run(SSH+['sudo -n python3 -'], input=code, text=True, capture_output=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr[-3000:])
    return r.stdout


def remote_state():
    return json.loads(remote('from pathlib import Path\nimport json\np=Path('+repr(str(P))+')\nf=p/"batch-finished.json"\ng=p/"batch-state.json"\nprint(json.dumps({"done":f.exists(),"state":json.loads(f.read_text()) if f.exists() else json.loads(g.read_text()) if g.exists() else {}}))'))


def note(text):
    ledger = Path('/mnt/disk2/dyp/deltabox-runtime/ae/report/README.md')
    with (ledger.parent/'.ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old = ledger.read_text()
        history = ledger.parent/'.history'; history.mkdir(exist_ok=True)
        shutil.copy2(ledger,history/f'fc-reflink-{time.time_ns()}.md')
        tmp = ledger.with_suffix('.tmp-fc-reflink')
        tmp.write_text(old+'\n\n'+text+'\n'); tmp.replace(ledger)


def main():
    own = (P/'queue.lock').open('w')
    fcntl.flock(own,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert not (P/'finished.json').exists()
    entered = launched = False
    sampler = None
    shared = None
    save()
    try:
        dep = json.loads((P/'dependency.json').read_text())
        marker = Path(dep['path'])
        while not marker.exists():
            assert not (P/'STOP').exists(), 'STOP requested'
            time.sleep(5)
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == dep['sha256'], 'Figure7 receipt changed'
        finished = json.loads(marker.read_text())
        assert finished['ok'] and finished['l1_progress']['ok']
        assert len(finished['l1_progress']['completed']) == 24
        state['dependency'] = dep; save()
        shared = (W/'figure8-refresh-20261108/host.lock').open('w')
        while True:
            assert not (P/'STOP').exists(), 'STOP requested'
            try:
                fcntl.flock(shared,fcntl.LOCK_EX|fcntl.LOCK_NB); break
            except BlockingIOError:
                time.sleep(5)
        assert not (W/'host-freq-before.json').exists(), 'previous experiment still owns frequency'
        assert shutil.disk_usage('/mnt/disk2').free >= 60*2**30
        for name, expected in json.loads((P/'source-lock.json').read_text()).items():
            assert hashlib.sha256((P/name).read_bytes()).hexdigest() == expected, name
        entered = True
        subprocess.run(['python3',str(W/'host_l1_ctl.py'),'enter'],check=True)
        state['frequency_before'] = json.loads((W/'host-freq-before.json').read_text()); save()
        sampler = subprocess.Popen(['python3',str(W/'host_l1_ctl.py'),'sample',str(P/'host-samples.jsonl'),str(P/'STOP_SAMPLER')])
        launch = json.loads(remote('''from pathlib import Path
import subprocess,json,time
p=Path(PPATH)
assert not (p/'launch.json').exists()
with (p/'batch.log').open('w') as log:
 proc=subprocess.Popen(['python3','-u',str(p/'run_batch_l1.py')],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
d={'pid':proc.pid,'epoch':time.time()};(p/'launch.json').write_text(json.dumps(d));print(json.dumps(d))
'''.replace('PPATH',repr(str(P)))))
        launched = True
        state.update(phase='running-validation-then-formal', launch=launch); save()
        note('### FC-REFLINK-STARTED-20261109\nFigure 7 success receipt verified and shared host lock acquired. CoW candidate validation started; the formal 12 traces start only after validation passes. Plan: `'+str(P/'PLAN.md')+'`. Paper and DeltaBox unchanged.')
        while True:
            if (P/'STOP').exists() or shutil.disk_usage('/mnt/disk2').free < 12*2**30:
                remote('from pathlib import Path\nPath('+repr(str(P/'STOP'))+').write_text("host STOP or NVMe guard")')
            reply = remote_state(); state['l1_state'] = reply['state']; save()
            if reply['done']:
                assert reply['state']['ok'], str(reply['state']['errors'])
                break
            time.sleep(10)
    except BaseException as exc:
        state['errors'].append(repr(exc)); traceback.print_exc()
        if launched:
            # Do not release the shared lock or restore frequency while our child
            # is still running. Preserve state and retry acknowledgement.
            while True:
                try:
                    remote('from pathlib import Path\nPath('+repr(str(P/'STOP'))+').write_text("coordinator failed; stop owned batch")')
                    if remote_state()['done']:
                        break
                except Exception as e:
                    state['shutdown_wait_error'] = repr(e); save()
                time.sleep(10)
    finally:
        if sampler:
            (P/'STOP_SAMPLER').touch()
            try:
                sampler.wait(timeout=30)
            except Exception as exc:
                state['errors'].append('sampler: '+repr(exc))
        if entered and (W/'host-freq-before.json').exists():
            try:
                subprocess.run(['python3',str(W/'host_l1_ctl.py'),'leave'],check=True)
                mismatches = []
                for cpu, expected in state['frequency_before'].items():
                    for key, value in expected.items():
                        actual = Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/{key}').read_text().strip()
                        if actual != value:
                            mismatches.append([cpu, key, value, actual])
                state['frequency_restore_mismatches'] = mismatches
                assert not mismatches, mismatches
            except Exception as exc:
                state['errors'].append('frequency restore: '+repr(exc))
        if launched:
            try:
                (P/'l1-results').mkdir(exist_ok=True)
                subprocess.run(['rsync','-a','--rsync-path=sudo rsync',
                                '--exclude=__pycache__/','--exclude=tmp/','--exclude=snapshots/',
                                '--exclude=snapshot-store.xfs','--exclude=work/',
                                '-e',shlex.join(SSH[:-1]),'ubuntu@127.0.0.1:'+str(P)+'/',str(P/'l1-results')+'/'],check=True)
                if not state['errors']:
                    subprocess.run(['python3',str(P/'analyze.py')],check=True)
            except Exception as exc:
                state['errors'].append('evidence/analysis: '+repr(exc))
        state.update(phase='finished',ok=launched and not state['errors'],finished_epoch=time.time(),
                     frequency_restored=not (W/'host-freq-before.json').exists())
        save(); (P/'finished.json').write_text(json.dumps(state,indent=2))
        note('### FC-REFLINK-FINISHED-20261109\n'+json.dumps({k:state[k] for k in ['ok','errors','frequency_restored']})+'\nEvidence: `'+str(P)+'`. Validation batch excluded; paper unchanged.')
        if state['ok']:
            summary = json.loads((P/'summary.json').read_text())
            lines = ['### FC-REFLINK-RESULTS-20261109',
                     '| Group | CK count | RS count | CK ms | RS ms |',
                     '|---|---:|---:|---:|---:|']
            for group, row in summary['groups'].items():
                lines.append(f"| {group} | {row['ck_count']} | {row['rs_count']} | {row['ck_ms']:.6f} | {row['rs_ms']:.6f} |")
            note('\n'.join(lines)+'\nSame 12 inputs, cold protocol and continuous endpoints; NVMe memory files moved to XFS reflink, base clone plus official v1.6.0 merging and fsync. Pilot excluded. Old 7727.237 ms and paper preserved. See source locks and stage intervals in the evidence directory.')
    return 0 if state['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
