"""Own one XFS loop, validate, then run an independent formal cohort."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback

P = Path(__file__).resolve().parent
M = P/'snapshots'
IMAGE = P/'snapshot-store.xfs'
state = dict(phase='preflight', started_epoch=time.time(), errors=[])


def save():
    t = P/'batch-state.tmp'
    t.write_text(json.dumps(state, indent=2)); t.replace(P/'batch-state.json')


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def checked_phase(phase):
    assert not (P/'STOP').exists(), 'STOP requested'
    state['phase'] = phase; save()
    with (P/(phase+'.log')).open('w') as log:
        proc = subprocess.Popen(['python3', '-u', str(P/'run_l1.py')],
                                env=dict(os.environ, FC_RUN_PHASE=phase),
                                stdout=log, stderr=subprocess.STDOUT)
        state['child_pid'] = proc.pid; save()
        code = proc.wait()
    result = json.loads((P/phase/'finished.json').read_text())
    assert code == 0 and result['ok'], (phase, code, result)
    if phase == 'pilot':
        proofs = [json.loads(line) for line in (P/'pilot/merge-validation.jsonl').read_text().splitlines()]
        assert proofs and all(row['ok'] for row in proofs)
        assert max(row['depth'] for row in proofs) >= 2
        directory = P/'pilot/results/sympy__sympy-17630'
        receipt = json.loads((directory/'run.json').read_text())
        raw = json.loads((directory/receipt['result']['path']).read_text())
        assert len(proofs) == sum(e['merge']['n_diffs'] > 0 for e in raw['restore_events'])
        state['pilot_validated_merges'] = len(proofs); save()


def main():
    lock = (P/'l1-batch.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert not (P/'batch-finished.json').exists()
    loop = None
    mounted = False
    try:
        assert shutil.disk_usage('/').free >= 70*2**30
        assert not IMAGE.exists() and not M.exists()
        for name, expected in json.loads((P/'source-lock.json').read_text()).items():
            assert hashlib.sha256((P/name).read_bytes()).hexdigest() == expected, name
        M.mkdir()
        with IMAGE.open('xb') as f:
            f.truncate(40*2**30)
        loop = subprocess.check_output(['losetup', '--find', '--show', '--direct-io=on', str(IMAGE)], text=True).strip()
        state['owned_loop'] = loop; save()
        run(['mkfs.xfs', '-f', '-K', '-m', 'reflink=1', loop], stdout=subprocess.PIPE)
        run(['mount', '-o', 'noatime', loop, str(M)])
        mounted = True
        dio = Path('/sys/block', Path(loop).name, 'loop/dio').read_text().strip()
        assert dio == '1', 'loop direct I/O is required'
        backing = subprocess.check_output(['findmnt','-n','-o','SOURCE,FSTYPE,OPTIONS','-T',str(IMAGE)], text=True).strip()
        assert backing.startswith('/dev/vda1 ') and 'ext4' in backing
        info = subprocess.check_output(['xfs_info', str(M)], text=True)
        assert 'reflink=1' in info
        state['storage'] = dict(backing=backing, loop=loop, direct_io=dio, xfs_info=info,
                                mount=subprocess.check_output(['findmnt','-n','-o','SOURCE,FSTYPE,OPTIONS','-T',str(M)], text=True).strip())
        state['phase'] = 'synthetic-validation'; save()
        env = dict(os.environ, FC_MERGE_VALIDATE='1', FC_VALIDATION_LOG=str(P/'synthetic-merge-validation.jsonl'))
        run(['numactl','--membind=0','--physcpubind=0-7','python3', str(P/'test_reflink.py')], env=env)
        from accept_pilot_prefix import accept
        state['validation_acceptance'] = accept(P); save()
        checked_phase('formal')
        run(['python3', str(P/'analyze.py')])
    except BaseException as exc:
        state['errors'].append(f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
    finally:
        # Only detach our own loop; never use global losetup/dm cleanup.
        try:
            if mounted:
                run(['umount', str(M)])
                mounted = False
            if loop:
                run(['losetup', '-d', loop])
                loop = None
            if IMAGE.exists():
                IMAGE.unlink()
            if M.exists():
                M.rmdir()
        except BaseException as exc:
            state['errors'].append('owned XFS cleanup: '+repr(exc))
        state.update(phase='finished', ok=not state['errors'], finished_epoch=time.time(),
                     storage_removed=not IMAGE.exists() and not M.is_mount())
        save()
        (P/'batch-finished.json').write_text(json.dumps(state, indent=2))
    return 0 if state['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
