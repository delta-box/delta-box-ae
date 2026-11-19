#!/usr/bin/env python3
"""Full-agent CubeSandbox replay.

deferred-nested-20261107: real-tests-20261106/cube_cold.py with two changes only: the step budget is the
trajectory's build_action count (same rollback events as copy+replay), and every rollback is cold.

The real Moatless agent (SearchTree execution, FileRepository, CodeIndex/FAISS) runs
inside the Cube VM.  The retained search tree and the recorded-LLM mock stay on the
host so that a VM rollback does not erase search progress (same shape as FC-Diff).
Timers (perf_counter, no settle anywhere):
  checkpoint: create_snapshot start -> API return -> first guest RPC success
  restore:    rollback start        -> API return -> first guest RPC success
The first-RPC interval is the post-rollback reconnect the agent actually waits for.
"""
import argparse, csv, hashlib, http.client, json, os, shlex, statistics, subprocess, sys, tarfile, time, traceback, uuid
from pathlib import Path

TASK = Path(os.environ['AGENT_TASK'])
sys.path.insert(0, os.environ['CUBE_SDK_PATH'])
from cubesandbox import Sandbox  # noqa: E402

VENDOR = Path('/home/atc-ae/delta-box-ae/ae/vendor/spr_payload')
TRACES_ROOT = Path('/mnt/disk2/dyp/d-overlayfs/traces/swe-search')
VENV_PY = '/mnt/disk2/dyp/moatless_det_venv/bin/python'
GUEST_PORT = 18080
MOCK_PORT = 19999
RETRY = ('invalid connection', 'connection reset', 'connection refused', 'connection aborted', 'broken pipe',
         'server disconnected', 'already in progress', 'timed out', 'timeout', 'active snapshot operation', 'duplicate entry')
now = time.perf_counter


def guest(sb, method, path, obj=None, timeout=30.0):
    body = json.dumps(obj).encode() if obj is not None else None
    conn = http.client.HTTPConnection(sb._config.proxy_node_ip, sb._config.proxy_port, timeout=timeout)
    try:
        headers = {'Host': sb.get_host(GUEST_PORT), 'Connection': 'close'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        if response.status >= 300:
            raise RuntimeError(f'guest {path} HTTP {response.status}: {data[:2600]!r}')
        return json.loads(data)
    finally:
        conn.close()


def first_rpc(sb, deadline_s=60.0):
    start = now()
    attempts = 0
    last = None
    while now() - start < deadline_s:
        attempts += 1
        try:
            reply = guest(sb, 'GET', '/ping', timeout=5.0)
            if reply.get('ok'):
                return reply, attempts
        except Exception as error:  # noqa: BLE001
            last = f'{type(error).__name__}: {error}'
        time.sleep(0.001)
    raise TimeoutError(f'guest did not answer after {attempts} attempts: {last}')


def retryable(error):
    text = f'{type(error).__name__}: {error}'.lower()
    return any(marker in text for marker in RETRY)


def timed_api(call):
    """Run one Cube API call; time spent in retries stays inside the timer."""
    start = now()
    retries = 0
    while True:
        try:
            value = call()
            return value, (now() - start) * 1000.0, retries
        except Exception as error:  # noqa: BLE001
            if retries >= 3 or not retryable(error):
                raise
            retries += 1
            time.sleep(0.05)


def touch(sb):
    """After the first RPC: time until the agent has read all of its own present pages."""
    t = now()
    r = guest(sb, 'POST', '/touch', {}, timeout=180.0)
    return dict(touch_ms=(now() - t) * 1000.0, touch_guest_ms=r.get('ms'), touch_bytes=r.get('bytes'), touch_runs=r.get('runs'), touch_errors=r.get('errors'), touch_mapped=r.get('mapped'))


import fcntl
LOCK = open('/tmp/dyp-cold.lock', 'w')  # keeps one lane's eviction out of the other lane's rollback window
def resident(paths):
    if not paths: return 0
    out = subprocess.run(['fincore', '-b', '-n', '-o', 'RES', *map(str, paths)], capture_output=True, text=True, timeout=60).stdout
    return sum(int(x) for x in out.split() if x.isdigit())


def drop_files(paths):
    for f in paths:
        fd = os.open(f, os.O_RDONLY)
        try:
            os.fsync(fd); os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


VOLUMES = Path('/data/cubelet/storage/cubecow-reflink/volumes')
BACKING = '/var/lib/cube-storage.xfs'


SHIM = {'pid': None}


def evict(sid):
    t = now(); files = sorted(VOLUMES.glob(f'*/tpl-{sid}-*'))
    before = resident(files); drop_files(files)
    fd = os.open(BACKING, os.O_RDONLY); os.fsync(fd); os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED); os.close(fd)
    left = resident(files); rec = None; tries = 0
    while left > (32 << 20) and tries < 3 and SHIM['pid']:  # pages the running VM still has mapped, same treatment as E2B
        tries += 1
        cg = open(f"/proc/{SHIM['pid']}/cgroup").read().strip().split('::')[-1]
        try:
            Path(f'/sys/fs/cgroup{cg}/memory.reclaim').write_text(str(int(left * 1.5)))
        except OSError as e:
            rec = f'{cg} {str(e)[:50]}'
        left = resident(files)
    return dict(ms=(now() - t) * 1000.0, files=len(files), before=before, after=left, backing=resident([BACKING]), reclaim=rec, tries=tries)


def n_expansions(trajectory):
    """build_action completions in the trajectory: the event stream copy+replay walks (walker.py)."""
    count = 0
    stack = [json.loads(Path(trajectory).read_text())['root']]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        comps = (node.get('completions') or {}).get('build_action')
        for c in (comps if isinstance(comps, list) else [comps] if comps else []):
            if isinstance(c, dict) and isinstance(c.get('response'), dict):
                count += 1
        stack.extend(node.get('children') or [])
    return count


def checkpoint(sb, name):
    start_ns = time.time_ns()
    start = now()
    snap, api_ms, retries = timed_api(lambda: sb.create_snapshot(name=name))
    api_end = now()
    reply, attempts = first_rpc(sb)
    end = now()
    loaded = {}
    return snap.snapshot_id, dict(**loaded, start_ns=start_ns, api_ms=api_ms, reconnect_ms=(end - api_end) * 1000.0, blocking_ms=(end - start) * 1000.0,
                                  api_retries=retries, rpc_attempts=attempts, ping=reply)


def rollback(sb, snapshot_id):
    start_ns = time.time_ns()
    start = now()
    _, api_ms, retries = timed_api(lambda: sb.rollback(snapshot_id))
    api_end = now()
    reply, attempts = first_rpc(sb)
    end = now()
    loaded = {}
    return dict(**loaded, start_ns=start_ns, api_ms=api_ms, reconnect_ms=(end - api_end) * 1000.0, blocking_ms=(end - start) * 1000.0,
                api_retries=retries, rpc_attempts=attempts, ping=reply)


def vm_pids(sandbox_id):
    found = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            cmd = (proc / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        except OSError:
            continue
        if sandbox_id in cmd and 'cube_agent_replay' not in cmd:
            found.append((int(proc.name), cmd[:160]))
    return found


def pin(sandbox_id, cpus):
    pids = vm_pids(sandbox_id)
    done = []
    for pid, cmd in pids:
        result = subprocess.run(['taskset', '-a', '-cp', cpus, str(pid)], capture_output=True, text=True)
        done.append(dict(pid=pid, cmd=cmd, rc=result.returncode, err=result.stderr[-200:]))
    return done


def pin_state(sandbox_id):
    out = {}
    for pid, cmd in vm_pids(sandbox_id):
        for t in Path(f'/proc/{pid}/task').glob('*'):
            try:
                line = next(l for l in (t / 'status').read_text().splitlines() if l.startswith('Cpus_allowed_list'))
                name = (t / 'comm').read_text().strip()
            except (OSError, StopIteration):
                continue
            out.setdefault(line.split()[1], []).append(name)
    return {k: dict(threads=len(v), names=sorted(set(v))[:8]) for k, v in out.items()}


def upload(sb, local, remote):
    sb.files.write(remote, Path(local).read_bytes())


def prepare_guest(sb, instance, args):
    result = sb.commands.run('mkdir -p /tmp/agent && id && nproc && free -m | sed -n 2p && python3.11 -V', timeout=30)
    identity = getattr(result, 'stdout', '')
    trace_dir = f'/tmp/agent/traces/qwen3-coder-30b-p-eagle-ms/mcts-iter30/{instance}'
    sb.commands.run(f'mkdir -p {trace_dir}', timeout=30)
    source = TRACES_ROOT / 'qwen3-coder-30b-p-eagle-ms/mcts-iter30' / instance
    upload(sb, source / 'trajectory.json', trace_dir + '/trajectory.json')
    upload(sb, source / 'ms_trace.jsonl', trace_dir + '/ms_trace.jsonl')
    upload(sb, '/mnt/disk2/dyp/deltabox-runtime/ae/work/real-tests-20261106/guest_agent_driver_touch.py', '/tmp/agent/guest_agent_driver.py')
    upload(sb, '/mnt/disk2/dyp/deltabox-runtime/ae/work/real-tests-20261106/nltk_offline.tar', '/tmp/agent/nltk_offline.tar')
    sb.commands.run('mkdir -p /mnt/disk2/dyp/spr_payload/nltk_offline && tar -xf /tmp/agent/nltk_offline.tar -C /mnt/disk2/dyp/spr_payload/nltk_offline && rm -f /tmp/agent/nltk_offline.tar', timeout=120)
    upload(sb, TASK / 'vendor_overlay.tar', '/tmp/agent/vendor_overlay.tar')
    env = {'AGENT_TRAJECTORY': trace_dir + '/trajectory.json', 'PYTHONHASHSEED': '0', 'OPENAI_API_KEY': 'dummy', 'CUSTOM_LLM_API_KEY': 'dummy',
           'LITELLM_LOG': 'ERROR', 'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'FAISS_OPT_LEVEL': 'generic', 'FAISS_DISABLE_CPU_FEATURES': 'AVX512_SPR,AVX512,AVX2',
           'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'OMP_THREAD_LIMIT': '1', 'MALLOC_ARENA_MAX': '1',
           'PYTHONPATH': '/tmp/agent/vendor:/mnt/disk2/dyp/spr_payload/moatless-det-src', 'MARKER_DIR': '/tmp/agent/markers', 'NLTK_DATA': '/mnt/disk2/dyp/spr_payload/nltk_offline'}
    exports = ' '.join(f'{k}={shlex.quote(v)}' for k, v in env.items())
    command = ('cd /tmp/agent && mkdir -p vendor && tar -xf vendor_overlay.tar -C vendor && '
               'cp -a vendor/moatless-det-src/. /mnt/disk2/dyp/spr_payload/moatless-det-src/ 2>/dev/null; '
               f'PYTHONPATH=/tmp/agent/vendor PYTHONHASHSEED=0 setsid nohup {VENV_PY} /tmp/agent/vendor/mock_llm_server.py --tcp-host 127.0.0.1 --tcp-port {MOCK_PORT} '
               '--traces-root /tmp/agent/traces --message-policy audit --latency-policy recorded > /tmp/agent/mock.log 2>&1 < /dev/null & '
               f'{exports} setsid nohup {VENV_PY} /tmp/agent/guest_agent_driver.py > /tmp/agent/guest.log 2>&1 < /dev/null & echo started')
    sb.commands.run(command, timeout=60)
    end = time.time() + 180
    while time.time() < end:
        try:
            if guest(sb, 'GET', '/ping', timeout=3).get('ok'):
                load = guest(sb, 'POST', '/mock_load', {'instance_id': instance, 'variant': 'p-eagle-ms'}, timeout=60)
                return identity, load
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    tail = sb.commands.run('tail -40 /tmp/agent/guest.log; tail -20 /tmp/agent/mock.log', timeout=30)
    raise RuntimeError('guest agent did not start: ' + str(getattr(tail, 'stdout', ''))[-1500:])


def run_instance(args, row):
    instance = row['instance']
    out = Path(args.out) / instance
    out.mkdir(parents=True, exist_ok=True)
    budget = n_expansions(TRACES_ROOT / 'qwen3-coder-30b-p-eagle-ms/mcts-iter30' / instance / 'trajectory.json')
    if args.max_steps:
        budget = min(budget, args.max_steps)
    record = dict(instance=instance, group=row.get('group'), lane=args.lane, vm_cpus=args.vm_cpus, template=args.template,
                  budget_rule='trajectory build_action count', cold_rule='every rollback',
                  step_budget=budget, started_epoch=time.time(), checkpoints=[], restores=[], steps=[], mem=[], ok=False)
    sb = None
    cursor = 0
    mismatches = 0
    snapshots = []
    try:
        start = now()
        attempts = 0
        while True:
            attempts += 1
            try:
                sb = Sandbox.create(template=args.template, timeout=args.sandbox_timeout)
                break
            except Exception as error:  # noqa: BLE001  scheduling only; nothing is being measured yet
                if 'no more resource' not in str(error) or attempts >= 120:
                    raise
                time.sleep(5)
        record['create_attempts'] = attempts
        record['create_ms'] = (now() - start) * 1000.0
        record['sandbox_id'] = sb.sandbox_id
        record['pin'] = pin(sb.sandbox_id, args.vm_cpus)
        SHIM['pid'] = record['pin'][0]['pid'] if record['pin'] else None
        if os.environ.get('CUBE_RX_SHIFT'):  # moves the RX index off the wrap window; touches nothing that is timed
            neigh = subprocess.run('ip neigh show dev cube-dev nud permanent', shell=True, capture_output=True, text=True).stdout
            ips = [l.split()[0] for l in neigh.splitlines() if '20:90:6f:fc:fc:fc' in l]
            ps = {i: subprocess.Popen(['ping', '-c', '2', '-W', '1', i], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in ips}
            live = [i for i, q in ps.items() if q.wait() == 0]
            assert len(live) == 1, live
            n = int(os.environ['CUBE_RX_SHIFT'])
            record['rx_shift'] = dict(n=n, out=subprocess.run(f'ping -f -q -c {n} -s 8 -w 60 {live[0]} | tail -2', shell=True, capture_output=True, text=True).stdout[-110:])
        record['guest_identity'], record['mock_load'] = prepare_guest(sb, instance, args)
        start = now()
        init = guest(sb, 'POST', '/init', {'instance': instance, 'mock_url_base': f'http://127.0.0.1:{MOCK_PORT}'}, timeout=900)
        record['init_ms'] = (now() - start) * 1000.0
        if not init.get('ok'):
            raise RuntimeError(f'init failed: {init}')
        tree = init['tree']
        record['mem'].append(dict(seq=0, **guest(sb, 'GET', '/mem', timeout=10)))
        sid, timing = checkpoint(sb, f'{instance}-0-{uuid.uuid4().hex[:6]}')
        snapshots.append(sid)
        checkpoints = [dict(seq=0, node_id=0, snapshot_id=sid, initial=True, **timing)]
        prev_node = 0
        for seq in range(1, budget + 1):
            if cursor >= (record.get('mock_load') or {}).get('n_completions', 10**9):
                record['stop_reason'] = f'recorded completions exhausted before seq {seq}'
                break
            select = guest(sb, 'POST', '/select', {'tree': tree}, timeout=120)
            if not select.get('ok'):
                raise RuntimeError(f'select failed: {select}')
            if select.get('finished') or select.get('selected_node_id') is None:
                record['stop_reason'] = f'finished at seq {seq}'
                break
            selected = select['selected_node_id']
            if seq > 1 and selected != prev_node:
                target = next((c for c in checkpoints if c['node_id'] == selected), None)
                if target is None:
                    raise RuntimeError(f'selected node {selected} has no checkpoint')
                cold = True
                fcntl.flock(LOCK, fcntl.LOCK_EX)
                try:
                    ev = evict(target['snapshot_id']) if cold else None
                    timing = rollback(sb, target['snapshot_id'])
                finally:
                    fcntl.flock(LOCK, fcntl.LOCK_UN)
                state = guest(sb, 'GET', '/state', timeout=30)
                if state.get('checkpoint_seq') != target['seq'] or state.get('node_id') != target['node_id']:
                    raise RuntimeError(f'restored state mismatch: want {target["seq"]}/{target["node_id"]}, got {state.get("checkpoint_seq")}/{state.get("node_id")}')
                record['restores'].append(dict(cold=cold, evict=ev, before_seq=seq, target_seq=target['seq'], selected_node_id=selected, verified_state=True, **timing))
                prev_node = selected
            guest(sb, 'POST', '/mock_cursor', {'cursor': cursor}, timeout=30)
            before = (guest(sb, 'GET', '/state', timeout=30).get('mock_stats') or {})
            start = now()
            step = guest(sb, 'POST', '/step', {'seq': seq, 'tree': tree, 'selected_node_id': selected}, timeout=args.step_timeout)
            step_ms = (now() - start) * 1000.0
            if not step.get('ok'):
                raise RuntimeError(f'step failed: {str(step)[:800]}')
            tree = step['tree']
            node_id = step.get('node_id')
            after = ((step.get('state') or {}).get('mock_stats') or {})
            cursor = after.get('cursor', cursor)
            mismatches += max(0, after.get('n_mismatch', 0) - before.get('n_mismatch', 0))
            record['steps'].append(dict(seq=seq, node_id=node_id, step_ms=step_ms, after_restore=bool(record['restores'] and record['restores'][-1]['before_seq'] == seq),
                                        mock=(step.get('state') or {}).get('mock_stats')))
            with open(Path(args.out) / f'{instance}.audit.jsonl', 'a') as af:
                af.write(json.dumps(dict(seq=seq, audit=guest(sb, 'POST', '/audit', {}, timeout=60.0))) + '\n')
            sid, timing = checkpoint(sb, f'{instance}-{seq}-{uuid.uuid4().hex[:6]}')
            snapshots.append(sid)
            checkpoints.append(dict(seq=seq, node_id=node_id, snapshot_id=sid, selected_node_id=selected, **timing))
            prev_node = node_id
            record['mem'].append(dict(seq=seq, **guest(sb, 'GET', '/mem', timeout=10)))
            print(f'[{instance}] seq={seq} node={node_id} ck={timing["blocking_ms"]:.1f} restores={len(record["restores"])}', flush=True)
        record['checkpoints'] = checkpoints
        record['pin_after'] = pin_state(sb.sandbox_id)
        record['final_mock'] = dict(cursor=cursor, total=(record.get('mock_load') or {}).get('n_completions'), n_mismatch=mismatches, n_served=cursor)
        record['ok'] = True
    except BaseException as error:  # noqa: BLE001
        record['error'] = f'{type(error).__name__}: {error}'
        record['traceback'] = traceback.format_exc()[-3000:]
        if 'checkpoints' in locals():
            record['checkpoints'] = checkpoints
        try:
            if sb is not None:
                record['guest_log_tail'] = str(getattr(sb.commands.run('tail -30 /tmp/agent/guest.log', timeout=20), 'stdout', ''))[-3000:]
        except Exception:  # noqa: BLE001
            pass
        print(f'[{instance}] FAILED {record["error"]}', flush=True)
    finally:
        record['finished_epoch'] = time.time()
        (out / 'pilot_result.json').write_text(json.dumps(record, indent=1))
        if sb is not None:
            try:
                sb.kill()
            except Exception as error:  # noqa: BLE001
                print(f'[{instance}] kill: {error}', flush=True)
        for sid in snapshots:
            try:
                Sandbox.delete_snapshot(sid)
            except Exception as error:  # noqa: BLE001
                print(f'[{instance}] delete {sid}: {error}', flush=True)
    return record


def main():
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--instances', nargs='+', required=True)
    parser.add_argument('--template', required=True)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--vm-cpus', required=True)
    parser.add_argument('--helper-cpus', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--max-steps', type=int, default=0)
    parser.add_argument('--step-timeout', type=float, default=600)
    parser.add_argument('--sandbox-timeout', type=int, default=14400)
    args = parser.parse_args()
    rows = {r['instance']: r for r in csv.DictReader(open(args.manifest), delimiter='\t')}
    failed = 0
    for instance in args.instances:
        failed += not run_instance(args, rows[instance])['ok']
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
