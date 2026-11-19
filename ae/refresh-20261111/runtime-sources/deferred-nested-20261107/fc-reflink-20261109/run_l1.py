#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from audit_result import audit_trace
from resource_barrier import observe_owned_pids, reclaim

P = Path(__file__).resolve().parent
PILOT = os.environ.get('FC_RUN_PHASE') == 'pilot'
B = P / ('pilot' if PILOT else 'formal')
B.mkdir(exist_ok=True)
R = B / 'results'
SNAPS = P / 'snapshots'
LANES = [
    (0, '0-7', ['astropy__astropy-7746', 'django__django-14997', 'sympy__sympy-17630']),
    (1, '12-19', ['pylint-dev__pylint-7228', 'django__django-14672', 'sympy__sympy-22840']),
    (2, '24-31', ['matplotlib__matplotlib-23964', 'pylint-dev__pylint-7080', 'matplotlib__matplotlib-26020']),
    (3, '36-43', ['astropy__astropy-14182', 'astropy__astropy-13453', 'psf__requests-863']),
]
if PILOT:
    LANES = [(0, '0-7', ['sympy__sympy-17630'])]
EXPECTED = sum(len(row[2]) for row in LANES)
LOCK = threading.RLock()
LANE_LIMIT = threading.BoundedSemaphore(2)
STOP = threading.Event()
ACTIVE = {}
OWNED_PIDS = set()
STATE = {'started_epoch': time.time(), 'rc': {}, 'errors': [], 'phase': 'preflight'}


def save():
    with LOCK:
        dest = B / 'progress.json'
        tmp = dest.with_suffix('.tmp')
        tmp.write_text(json.dumps(STATE, indent=2))
        tmp.replace(dest)


def stop(reason):
    with LOCK:
        if reason not in STATE['errors']:
            STATE['errors'].append(reason)
        STOP.set()
    save()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def preflight():
    resume_file = B/'resume.json'
    resumed = json.loads(resume_file.read_text()) if resume_file.exists() else None
    if resumed:
        assert not PILOT and resumed['reason'] == 'NVMe guard interruption; same measured code and inputs'
    assert not R.exists() or resumed, 'refuse to overwrite a previous batch'
    assert shutil.disk_usage('/').free >= 45 * 2**30
    assert SNAPS.is_mount()
    assert 'xfs' in subprocess.check_output(['findmnt','-n','-o','FSTYPE','-T',str(SNAPS)], text=True)
    lock = json.loads((P/'source-lock.json').read_text())
    for name, sha in lock.items():
        assert digest(P/name) == sha, name
    framework = json.loads((P/'framework-lock.json').read_text())
    for name, sha in framework.items():
        assert digest(name) == sha, name
    subprocess.run(['/usr/bin/python3', str(P/'test_continuous_timing.py')], check=True)
    subprocess.run(['/usr/bin/python3', str(P/'test_socket_spawn.py')], check=True)
    inputs = json.loads((P/'inputs.json').read_text())
    prepared = {}
    for item in inputs:
        trace = Path(item['trajectory']['path'])
        assert digest(trace) == item['trajectory']['sha256'], trace
        data = json.loads(trace.read_text())
        nodes = []
        def walk(n):
            nodes.append(n)
            for child in n.get('children', []):
                walk(child)
        walk(data['root'])
        assert sum('build_action' in (n.get('completions') or {}) for n in nodes) == item['old_max_steps'] == 29
        commit = data.get('repository', {}).get('commit')
        fallback = None
        if not commit:
            repo = '/mnt/disk2/dyp/spr_payload/repos/swe-bench_' + item['instance']
            fallback = subprocess.check_output(['git', '-c', 'safe.directory=*', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
        prepared[item['instance']] = dict(item, fallback_commit=fallback, repository_commit=commit or fallback)
    cfg = json.loads((P/'config-fc-diff.json').read_text())
    files = [Path(cfg['kernel']), Path(cfg['base_xfs']), Path(cfg['images_dir'])/'data-django.xfs', Path('/usr/local/bin/firecracker')]
    identities = {str(f): {'sha256': digest(f), 'size': f.stat().st_size} for f in files}
    mount = subprocess.check_output(['findmnt', '-T', str(P), '-n', '-o', 'SOURCE,FSTYPE,OPTIONS'], text=True).strip()
    assert mount.startswith('/dev/vda1 ') and '9p' not in mount and 'tmpfs' not in mount, mount
    numa = {str(i): Path(f'/sys/devices/system/node/node{i}/cpulist').read_text().strip() for i in range(4)}
    assert not any(l.startswith('fcdm') for l in subprocess.check_output(['dmsetup', 'ls'], text=True).splitlines()), 'unexpected prior FC dm devices'
    result = {'epoch': time.time(), 'mount': mount, 'numa_cpus': numa, 'input': list(prepared.values()), 'identities': identities}
    (B/'preflight.json').write_text(json.dumps(result, indent=2))
    if resumed:
        old_lock = json.loads((P/resumed['prior_source_lock']).read_text())
        # Only runner/resumption bookkeeping changed. Measured driver, inputs,
        # environment configuration and audit must retain their frozen identity.
        allowed_changes = {'run_l1.py', 'accept_pilot_prefix.py', 'PLAN.md'}
        for name, sha in old_lock.items():
            if name not in allowed_changes:
                assert lock[name] == sha, ('changed measured input', name)
        completed = resumed['completed']
        assert {q.name for q in R.iterdir() if q.is_dir()} == set(completed)
        for name, expected_sha in completed.items():
            checked = audit_trace(R/name, prepared[name])
            assert checked['sha256'] == expected_sha, name
            STATE['rc'][name] = 0
        STATE['resumed_completed'] = list(completed)
        STATE['previous_attempt'] = resumed['previous_attempt']
    else:
        R.mkdir()
    return prepared


def _lane(node, cpus, instances, prepared):
    for instance in instances:
        if STATE['rc'].get(instance) == 0:
            continue
        if STOP.is_set():
            return
        item = prepared[instance]
        command = ['numactl', f'--membind={node}', f'--physcpubind={cpus}',
                   '/usr/bin/python3', str(P/'ae_baseline_shim.py'), '--backend', 'fc-diff',
                   '--instance', instance, '--trace', item['trajectory']['path'],
                   '--config', str(P/'config-fc-diff.json'), '--out', str(R/instance)]
        if item['fallback_commit']:
            command += ['--repository-commit', item['fallback_commit']]
        started = time.time()
        code = 1
        try:
            with (R/(instance+'.log')).open('w') as log:
                env = dict(os.environ, TMPDIR=str(P/'tmp'), FCDM_SNAPSHOT_ROOT=str(SNAPS),
                           FC_MERGE_VALIDATE='1' if PILOT else '0',
                           FC_VALIDATION_LOG=str(B/'merge-validation.jsonl'))
                proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                        env=env, cwd='/home/atc-ae/delta-box-ae', start_new_session=True)
                with LOCK:
                    ACTIVE[instance] = proc
                code = proc.wait()
            if code != 0:
                raise RuntimeError(f'{instance} rc={code}')
            audit = audit_trace(R/instance, item)
            (R/instance/'continuous-audit.json').write_text(json.dumps({k:v for k,v in audit.items() if k not in ['checkpoints','restores']}, indent=2))
        except BaseException as exc:
            code = code or 2
            stop(f'{instance}: {type(exc).__name__}: {exc}')
        finally:
            with LOCK:
                ACTIVE.pop(instance, None)
                STATE['rc'][instance] = code
                with (B/'lanes.jsonl').open('a') as f:
                    f.write(json.dumps({'node': node, 'cpus': cpus, 'instance': instance,
                                        'start': started, 'end': time.time(), 'rc': code})+'\n')
            save()


def lane(node, cpus, instances, prepared):
    # Keep all four NUMA queues, but bound simultaneous snapshot disk growth.
    with LANE_LIMIT:
        _lane(node, cpus, instances, prepared)


def main():
    (P/'tmp').mkdir(exist_ok=True)
    try:
        prepared = preflight()
        STATE['phase'] = 'running-two-lanes-with-storage-barriers'
        STATE['max_active_lanes'] = 2
        save()
        queues = [[node, cpus, [i for i in instances if STATE['rc'].get(i) != 0]]
                  for node, cpus, instances in LANES]
        threads = []
        signalled = {}
        with (B/'resources.jsonl').open('a') as resources:
            while any(q[2] for q in queues) or any(t.is_alive() for t in threads):
                if not any(t.is_alive() for t in threads):
                    for thread in threads:
                        thread.join()
                    reclaim(P, B, OWNED_PIDS)
                    if STOP.is_set():
                        break
                    selected = [q for q in queues if q[2]][:2]
                    wave = [(q[0], q[1], [q[2].pop(0)]) for q in selected]
                    threads = [threading.Thread(target=lane, args=(*spec, prepared)) for spec in wave]
                    for thread in threads:
                        thread.start()
                OWNED_PIDS.update(observe_owned_pids(P))
                STATE['owned_controller_pids'] = sorted(OWNED_PIDS)
                mem = next(int(l.split()[1])*1024 for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:'))
                free = shutil.disk_usage('/').free
                snapshot_free = shutil.disk_usage(SNAPS).free
                resources.write(json.dumps({'epoch':time.time(), 'free_bytes':free, 'snapshot_free_bytes':snapshot_free, 'mem_available':mem})+'\n'); resources.flush()
                if free < 12*2**30 or mem < 8*2**30 or snapshot_free < 4*2**30:
                    stop('resource guard: disk below 12 GiB, snapshot XFS below 4 GiB, or available memory below 8 GiB')
                if (P/'STOP').exists():
                    stop('STOP: '+(P/'STOP').read_text())
                if STOP.is_set():
                    with LOCK:
                        for name, proc in ACTIVE.items():
                            if proc.poll() is None and name not in signalled:
                                proc.send_signal(signal.SIGTERM); signalled[name] = time.time()
                time.sleep(3)
        for thread in threads:
            thread.join()
        reclaim(P, B, OWNED_PIDS)
    except BaseException as exc:
        stop(f'{type(exc).__name__}: {exc}')
    try:
        dm_left = [l for l in subprocess.check_output(['dmsetup', 'ls'], text=True).splitlines() if l.startswith('fcdm')]
        loops_left = {}
        for backing in Path('/sys/block').glob('loop*/loop/backing_file'):
            text = backing.read_text().strip()
            if str(P) in text and text != str(P/'snapshot-store.xfs'):
                loops_left[str(backing)] = text
        (B/'cleanup-audit.json').write_text(json.dumps({'dm_left':dm_left,'loops_left':loops_left},indent=2))
        if dm_left or loops_left:
            stop('owned FC device cleanup incomplete')
    except Exception as exc:
        stop('cleanup audit failed: '+str(exc))
    STATE.update(phase='finished', ok=len(STATE['rc'])==EXPECTED and all(v==0 for v in STATE['rc'].values()) and not STATE['errors'], finished_epoch=time.time())
    save()
    (B/'finished.json').write_text(json.dumps(STATE, indent=2))
    return 0 if STATE['ok'] else 1


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: stop('runner received SIGTERM'))
    signal.signal(signal.SIGINT, lambda *_: stop('runner received SIGINT'))
    raise SystemExit(main())
