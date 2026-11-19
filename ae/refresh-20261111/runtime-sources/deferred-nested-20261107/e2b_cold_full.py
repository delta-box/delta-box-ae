#!/usr/bin/env python3
"""Full-agent replay on the complete self-hosted E2B stack, through the official Python SDK.

deferred-nested-20261107: real-tests-20261106/e2b_cold.py with two changes only: the step budget is the
trajectory's build_action count (same rollback events as copy+replay), and every rollback is cold.

Every call goes through the E2B API (API key auth, Postgres, placement) to the orchestrator; guest traffic
goes through client-proxy. Same loop and same windows as cube_agent_replay.py:
  checkpoint: Sandbox.create_snapshot start -> API return -> first agent /ping success
              (official Checkpoint: pause, snapshot, resume the same sandbox, upload in the background)
  rollback:   Sandbox.create(template=snapshot_id) start -> API return -> first agent /ping success
              (official way to get back to an older snapshot; the replaced sandbox is killed after the window)
"""
import subprocess, argparse, csv, http.client, json, os, shlex, sys, tarfile, tempfile, time, traceback, uuid
from pathlib import Path
from e2b import Sandbox

H = Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b')
CLIENT = json.loads((H / 'config/client.json').read_text())
OPTS = dict(api_key=CLIENT['E2B_API_KEY'], api_url=CLIENT['E2B_API_URL'], sandbox_url=CLIENT['E2B_SANDBOX_URL'])
PROXY_HOST, PROXY_PORT = '127.0.0.1', int(CLIENT['E2B_SANDBOX_URL'].rsplit(':', 1)[1])
T = Path('/home/atc-ae/delta-box-ae/ae/work/cube-full-agent-20261105')
TRACES = Path('/mnt/disk2/dyp/d-overlayfs/traces/swe-search/qwen3-coder-30b-p-eagle-ms/mcts-iter30')
PAYLOAD = Path('/mnt/disk2/dyp/spr_payload')
GUEST_PORT = 18080
MOCK_PORT = 19999
now = time.perf_counter


def guest(sb, method, path, obj=None, timeout=30.0):
    body = json.dumps(obj).encode() if obj is not None else None
    conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=timeout)
    try:
        headers = {'E2b-Sandbox-Id': sb.sandbox_id, 'E2b-Sandbox-Port': str(GUEST_PORT), 'Connection': 'close'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        if response.status >= 300:
            raise RuntimeError(f'guest {path} HTTP {response.status}: {data[-400:]!r}')
        return json.loads(data)
    finally:
        conn.close()


def first_rpc(sb, deadline_s=60.0):
    start = now(); attempts = 0; last = None
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
        try:
            fd = os.open(f, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            os.fsync(fd); os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


def build_of(sid):
    env = sid.split(':')[0]
    q = f"select a.build_id from env_build_assignments a where a.env_id='{env}' order by a.created_at desc limit 1"
    return subprocess.run(['docker', 'exec', 'ae-e2b-postgres', 'sh', '-c', f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "{q}"'], capture_output=True, text=True, timeout=30).stdout.strip()


CHAIN = set()
BASES = {Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/e2b-official-20261106/' + n).read_text().strip() for n in ('agent-build.id', 'base-4c8g.id')}

# Include immutable ancestors as well as the two named builds when evicting.
# V3 headers contain flattened byte mappings to every referenced parent object.
def immutable_template_parents(builds):
    import struct
    parents = set(builds)
    for build in builds:
        for name in ('rootfs.ext4', 'memfile'):
            data = (H / 'storage/templates' / build / (name + '.header')).read_bytes()
            if len(data) < 64 or (len(data) - 64) % 40 or struct.unpack_from('<Q', data)[0] != 3:
                raise ValueError('unsupported or incomplete template header: ' + build)
            for offset in range(64, len(data), 40):
                parent = data[offset + 16:offset + 32]
                if parent != bytes(16):
                    parents.add(str(uuid.UUID(bytes=parent)))
    return parents


BASES = immutable_template_parents(BASES)



def evict(sid):
    """E2B snapshots are layered: pages of one snapshot live in many ancestor builds, so the whole chain has to go."""
    t = now(); files = []
    for b in CHAIN | BASES | {build_of(sid)}:
        d = H / 'storage/templates' / b
        if d.is_dir():
            files += list(d.iterdir())
        files += list((H / 'orchestrator/build').glob(b + '-*'))
    files = [p for p in files if p.exists()]  # a sandbox of the other lane can remove its own files at any time
    before = resident(files); drop_files(files)
    left = resident(files); rec = None; tries = 0
    while left > (32 << 20) and tries < 3:  # pages the orchestrator has mapped: reclaim only about what is left
        tries += 1
        try:
            Path('/sys/fs/cgroup/system.slice/ae-e2b-orchestrator.service/memory.reclaim').write_text(str(int(left * 1.5)))
        except OSError as e:
            rec = str(e)[:60]
        left = resident(files)
    return dict(ms=(now() - t) * 1000.0, files=len(files), before=before, after=left, reclaim=rec, tries=tries)


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


def checkpoint(sb):
    start_ns = time.time_ns(); start = now()
    snap = sb.create_snapshot(**OPTS)
    api_end = now()
    reply, attempts = first_rpc(sb)
    end = now()
    loaded = {}
    sid = getattr(snap, 'snapshot_id', None) or getattr(snap, 'snapshotId', None)
    CHAIN.add(build_of(sid))
    return sid, dict(**loaded, start_ns=start_ns, api_ms=(api_end - start) * 1000.0, reconnect_ms=(end - api_end) * 1000.0, blocking_ms=(end - start) * 1000.0, rpc_attempts=attempts, ping=reply)


def rollback(snapshot_id, timeout):
    start_ns = time.time_ns(); start = now()
    new = Sandbox.create(template=snapshot_id, timeout=timeout, **OPTS)
    api_end = now()
    reply, attempts = first_rpc(new)
    end = now()
    loaded = {}
    return new, dict(**loaded, start_ns=start_ns, api_ms=(api_end - start) * 1000.0, reconnect_ms=(end - api_end) * 1000.0, blocking_ms=(end - start) * 1000.0, rpc_attempts=attempts, ping=reply)


def prepare_guest(sb, instance):
    identity = sb.commands.run('id && nproc && free -m | sed -n 2p', timeout=30).stdout
    with tempfile.TemporaryDirectory(dir='/mnt/disk2/dyp/deltabox-runtime/ae/work/e2b-official-20261106') as d:
        tar = Path(d) / 'payload.tar'
        with tarfile.open(tar, 'w') as t:
            t.add((PAYLOAD / 'repos' / f'swe-bench_{instance}').resolve(), arcname=f'spr_payload/repos/swe-bench_{instance}')
            t.add((PAYLOAD / 'index_store' / instance).resolve(), arcname=f'spr_payload/index_store/{instance}')
            for name in ('trajectory.json', 'ms_trace.jsonl'):
                t.add(TRACES / instance / name, arcname=f'agent/traces/qwen3-coder-30b-p-eagle-ms/mcts-iter30/{instance}/{name}')
            t.add('/mnt/disk2/dyp/spr_payload/mock_llm_server.py', arcname='agent/mock_llm_server.py')
            t.add('/mnt/disk2/dyp/deltabox-runtime/ae/work/real-tests-20261106/guest_agent_driver_touch.py', arcname='agent/guest_agent_driver.py')
            t.add('/mnt/disk2/dyp/replay-offline-nltk-20260921', arcname='spr_payload/nltk_offline')
        with open(tar, 'rb') as f:
            sb.files.write('/tmp/agent_payload.tar', f)
    setup = ('mkdir -p /tmp/agent /tmp/agent_x && tar -xf /tmp/agent_payload.tar -C /tmp/agent_x && cp -a /tmp/agent_x/spr_payload/. /mnt/disk2/dyp/spr_payload/ && '
             f'cp -a /tmp/agent_x/agent/. /tmp/agent/ && cp /tmp/agent/mock_llm_server.py /opt/agent_vendor/mock_llm_server.py && rm -rf /tmp/agent_x /tmp/agent_payload.tar && NLTK_DATA=/mnt/disk2/dyp/spr_payload/nltk_offline INSTANCE={shlex.quote(instance)} sh /opt/agent_start.sh')
    sb.commands.run(setup, timeout=240, user='root')
    load = guest(sb, 'POST', '/mock_load', {'instance_id': instance, 'variant': 'p-eagle-ms'}, timeout=60)
    return identity, load


def run_instance(args, row):
    instance = row['instance']
    CHAIN.clear()
    out = Path(args.out) / instance; out.mkdir(parents=True, exist_ok=True)
    budget = n_expansions(TRACES / instance / 'trajectory.json')
    if args.max_steps:
        budget = min(budget, args.max_steps)
    rec = dict(instance=instance, group=row.get('group'), lane=args.lane, template=args.template, step_budget=budget, budget_rule='trajectory build_action count', cold_rule='every rollback', started_epoch=time.time(), checkpoints=[], restores=[], steps=[], kills=[], mem=[], ok=False)
    sb = None; cursor = 0; mismatches = 0; snapshots = []; checkpoints = []
    try:
        start = now()
        sb = Sandbox.create(template=args.template, timeout=args.sandbox_timeout, **OPTS)
        rec['create_ms'] = (now() - start) * 1000.0
        rec['sandbox_id'] = sb.sandbox_id
        rec['guest_identity'], rec['mock_load'] = prepare_guest(sb, instance)
        start = now()
        init = guest(sb, 'POST', '/init', {'instance': instance, 'mock_url_base': f'http://127.0.0.1:{MOCK_PORT}'}, timeout=900)
        rec['init_ms'] = (now() - start) * 1000.0
        if not init.get('ok'):
            raise RuntimeError(f'init failed: {str(init)[:400]}')
        tree = init['tree']
        rec['mem'].append(dict(seq=0, **guest(sb, 'GET', '/mem', timeout=10)))
        sid, timing = checkpoint(sb)
        snapshots.append(sid)
        checkpoints = [dict(seq=0, node_id=0, snapshot_id=sid, initial=True, **timing)]
        prev_node = 0
        for seq in range(1, budget + 1):
            if cursor >= (rec.get('mock_load') or {}).get('n_completions', 10**9):
                rec['stop_reason'] = f'recorded completions exhausted before seq {seq}'; break
            select = guest(sb, 'POST', '/select', {'tree': tree}, timeout=120)
            if not select.get('ok'):
                raise RuntimeError(f'select failed: {select}')
            if select.get('finished') or select.get('selected_node_id') is None:
                rec['stop_reason'] = f'finished at seq {seq}'; break
            selected = select['selected_node_id']
            if seq > 1 and selected != prev_node:
                target = next((c for c in checkpoints if c['node_id'] == selected), None)
                if target is None:
                    raise RuntimeError(f'selected node {selected} has no checkpoint')
                old = sb
                cold = True
                fcntl.flock(LOCK, fcntl.LOCK_EX)
                try:
                    ev = evict(target['snapshot_id']) if cold else None
                    sb, timing = rollback(target['snapshot_id'], args.sandbox_timeout)
                finally:
                    fcntl.flock(LOCK, fcntl.LOCK_UN)
                state = guest(sb, 'GET', '/state', timeout=30)
                if state.get('checkpoint_seq') != target['seq'] or state.get('node_id') != target['node_id']:
                    raise RuntimeError(f'restored state mismatch: want {target["seq"]}/{target["node_id"]}, got {state.get("checkpoint_seq")}/{state.get("node_id")}')
                k0 = now(); old.kill(); rec['kills'].append((now() - k0) * 1000.0)
                rec['restores'].append(dict(cold=cold, evict=ev, before_seq=seq, target_seq=target['seq'], selected_node_id=selected, verified_state=True, sandbox_id=sb.sandbox_id, **timing))
                prev_node = selected
            guest(sb, 'POST', '/mock_cursor', {'cursor': cursor}, timeout=30)
            before = (guest(sb, 'GET', '/state', timeout=30).get('mock_stats') or {})
            start = now()
            step = guest(sb, 'POST', '/step', {'seq': seq, 'tree': tree, 'selected_node_id': selected}, timeout=args.step_timeout)
            step_ms = (now() - start) * 1000.0
            if not step.get('ok'):
                raise RuntimeError(f'step failed: {str(step)[:600]}')
            tree = step['tree']; node_id = step.get('node_id')
            after = ((step.get('state') or {}).get('mock_stats') or {})
            cursor = after.get('cursor', cursor)
            mismatches += max(0, after.get('n_mismatch', 0) - before.get('n_mismatch', 0))
            rec['steps'].append(dict(seq=seq, node_id=node_id, step_ms=step_ms, after_restore=bool(rec['restores'] and rec['restores'][-1]['before_seq'] == seq)))
            sid, timing = checkpoint(sb)
            snapshots.append(sid)
            checkpoints.append(dict(seq=seq, node_id=node_id, snapshot_id=sid, selected_node_id=selected, **timing))
            prev_node = node_id
            rec['mem'].append(dict(seq=seq, **guest(sb, 'GET', '/mem', timeout=10)))
            last = rec['restores'][-1]['blocking_ms'] if rec['restores'] else 0
            print(f'[{instance}] seq={seq} node={node_id} ck={timing["blocking_ms"]:.1f} rb={last:.1f} restores={len(rec["restores"])}', flush=True)
        rec['checkpoints'] = checkpoints
        rec['final_mock'] = dict(cursor=cursor, total=(rec.get('mock_load') or {}).get('n_completions'), n_mismatch=mismatches, n_served=cursor)
        rec['ok'] = True
    except BaseException as error:  # noqa: BLE001
        rec['error'] = f'{type(error).__name__}: {error}'; rec['traceback'] = traceback.format_exc()[-2500:]
        rec['checkpoints'] = checkpoints
        print(f'[{instance}] FAILED {rec["error"][:300]}', flush=True)
    finally:
        rec['finished_epoch'] = time.time()
        (out / 'pilot_result.json').write_text(json.dumps(rec, indent=1))
        if sb is not None:
            try: sb.kill()
            except Exception as error: print(f'[{instance}] kill: {error}', flush=True)  # noqa: BLE001
        for sid in snapshots:
            try: Sandbox.delete_snapshot(sid, **OPTS)
            except Exception as error: print(f'[{instance}] delete {sid}: {error}', flush=True)  # noqa: BLE001
    return rec


def main():
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)  # a background shell starts us with SIGINT ignored
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', required=True); p.add_argument('--instances', nargs='+', required=True); p.add_argument('--template', required=True)
    p.add_argument('--lane', required=True); p.add_argument('--out', required=True); p.add_argument('--max-steps', type=int, default=0)
    p.add_argument('--step-timeout', type=float, default=600); p.add_argument('--sandbox-timeout', type=int, default=3500); p.add_argument('--start-delay', type=float, default=0)
    args = p.parse_args()
    rows = {r['instance']: r for r in csv.DictReader(open(args.manifest), delimiter='\t')}
    time.sleep(args.start_delay)
    failed = 0
    for i in args.instances:
        result = run_instance(args, rows[i])
        failed += not result['ok']
        if result.get('error', '').startswith('KeyboardInterrupt:'):
            break  # A disk guard must stop the lane, not start the next trace.
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
