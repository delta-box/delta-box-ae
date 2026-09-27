#!/usr/bin/env python3
"""Explicit nested reconstruction with the original two sequential mock streams.

Only recorded LLM responses are mocked. Actions and checkpoint/restore calls
remain real. Synchronization moves cursors forward by proved consumed calls;
it never seeks to a node or rewinds a cursor to mask a divergent trajectory.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time
import uuid

import e2b_slim_finalbench_pilot as common

BASE, SOURCE_REPOS, INDEX_STORE = common.BASE, common.SOURCE_REPOS, common.INDEX_STORE
DEFAULT_TRACES_ROOT = common.DEFAULT_TRACES_ROOT
http_json, free_tcp_port = common.http_json, common.free_tcp_port
resolve_trajectory_path = common.resolve_trajectory_path
start_shared_mock, stop_proc = common.start_shared_mock, common.stop_proc
start_external_index_sidecar = common.start_external_index_sidecar
make_payload_tar, load_initial_tree = common.make_payload_tar, common.load_initial_tree
controller_tree_from_dict = common.controller_tree_from_dict
controller_build_action_only = common.controller_build_action_only
flush_audit, message_policy = common.flush_audit, common.message_policy

def checked_stats(port, completions):
    stats = http_json(f"http://127.0.0.1:{port}/admin/stats", timeout=2.0)
    if (stats.get('ok') is not True or type(stats.get('cursor')) is not int
            or not 0 <= stats['cursor'] <= len(completions)
            or type(stats.get('total')) is not int or stats['total'] != len(completions)
            or type(stats.get('n_served')) is not int or stats['n_served'] < 0
            or stats.get('message_policy') != 'strict'
            or type(stats.get('n_mismatch')) is not int or stats['n_mismatch'] != 0
            or type(stats.get('n_protocol_errors')) is not int or stats['n_protocol_errors'] != 0):
        raise RuntimeError('Dual mock returned invalid, non-strict or failed protocol statistics')
    return stats


def sync_forward(port, target, completions, *, purpose, node_id=None):
    """93c cursor synchronization, constrained to a proved monotonic interval."""
    if purpose not in ('build_action', 'exec'):
        raise ValueError('Unknown dual-mock synchronization purpose')
    before = checked_stats(port, completions)
    if type(target) is not int or not before['cursor'] <= target <= len(completions):
        raise RuntimeError('Dual mock synchronization would move backwards or escape the recording')
    skipped = completions[before['cursor']:target]
    if any((c.purpose != 'build_action' if purpose == 'build_action' else not c.purpose.startswith('exec.'))
           or (node_id is not None and c.node_id != node_id) for c in skipped):
        raise RuntimeError('Dual mock synchronization skips a response of the wrong node or purpose')
    if target != before['cursor']:
        result = http_json(f"http://127.0.0.1:{port}/admin/rewind", method='POST',
                           obj={'cursor': target}, timeout=2.0)
        if (result.get('ok') is not True or type(result.get('cursor')) is not int
                or result['cursor'] != target):
            raise RuntimeError('Dual mock synchronization was not acknowledged')
    after = checked_stats(port, completions)
    if after['cursor'] != target or after['n_served'] != before['n_served']:
        raise RuntimeError('Dual mock synchronization changed served-call accounting')
    return {'before': before, 'after': after, 'direction': 'forward-only',
            'skipped_purpose': purpose, 'skipped_count': len(skipped)}


def consumed(before, after, completions, *, purpose, node_id):
    start, end = before['cursor'], after['cursor']
    if end < start or end > len(completions) or after['n_served'] - before['n_served'] != end - start:
        raise RuntimeError('Mock cursor progress differs from actual served calls')
    selected = completions[start:end]
    if any(c.node_id != node_id or
           (c.purpose != 'build_action' if purpose == 'build_action' else not c.purpose.startswith('exec.'))
           for c in selected):
        raise RuntimeError('Served mock responses differ from current node/purpose')
    if purpose == 'build_action' and not selected:
        raise RuntimeError('Controller did not consume a build_action response')
    return selected



def expected_action_payload(node, index, action_class):
    """Recover the complete original ReAct action without issuing a model call.

    The recorded action object can lose thoughts during serialization. Its
    immutable build_action response retains them. Use the existing ReAct
    format/schema parsing operations, then require every functional argument
    and any retained nonempty thought to agree with the recorded action.
    """
    from moatless.actions.model import ActionArguments
    from moatless.completion.react import ReActCompletionModel
    import importlib
    if index != 0:
        raise RuntimeError('Original ReAct response must bind exactly one action')
    choices = node['completions']['build_action']['response']['choices']
    if len(choices) != 1:
        raise RuntimeError('Recorded ReAct completion must have exactly one choice')
    text = choices[0]['message']['content']
    if not isinstance(text, str):
        raise RuntimeError('Recorded ReAct completion has no text response')
    # This is the same checker and slicing/schema dispatch used by the
    # installed ReActCompletionModel.create_completion, without LLM/pricing I/O.
    ReActCompletionModel._validate_react_format(None, text)
    thought_start, action_start = text.find('Thought:'), text.find('Action:')
    if thought_start < 0 or action_start <= thought_start:
        raise RuntimeError('Invalid recorded ReAct Thought/Action order')
    thought = text[thought_start + 8:action_start].strip()
    parts = text[action_start + 7:].strip().split('\n', 1)
    if len(parts) != 2:
        raise RuntimeError('Recorded ReAct response lacks action arguments')
    module, name = action_class.rsplit('.', 1)
    schema = getattr(importlib.import_module(module), name)
    if not issubclass(schema, ActionArguments) or parts[0].strip() != schema.name:
        raise RuntimeError('Recorded completion action differs from original action class')
    body = parts[1].strip()
    if body.startswith('<') or body.startswith(chr(96)*3+'xml'):
        parsed = schema.model_validate_xml(body)
    else:
        parsed = schema.model_validate_json(body)
    parsed.thoughts = thought
    payload = parsed.model_dump()
    payload['action_args_class'] = action_class
    from e2b_paper_action import recorded_action_payload
    wanted = recorded_action_payload(node, index, action_class, schema.name)
    if payload != wanted:
        raise RuntimeError('Recorded completion changes original functional action arguments')
    return wanted


class RunContract:
    def __init__(self, args, transport):
        from ae.scripts import e2b_paper_profile as profile
        if (args.trace_variant != 'ms' or args.max_steps != 30 or args.warm_action_worker
                or args.clean_storage or args.root_build or args.materialize_file_context
                or args.mem_mib != 2048 or args.disk_mb != 4096
                or args.fc_version != 'v1.14.1_458ca91'):
            raise ValueError('Paper nested driver requires the original complete cold-worker settings')
        if (not args.from_build or transport.manifest.get('instance') != args.instance
                or transport.manifest.get('fresh_base_build_id') != args.from_build
                or args.storage != transport.storage):
            raise ValueError('Base build is not the freshly verified base for this owned input')
        proof = profile.verify_inputs(profile.effective({}, profile.PROFILE))
        row = next((x for x in proof['inputs'] if x['instance'] == args.instance), None)
        if row is None:
            raise ValueError('Instance is not in the original eight-input cohort')
        trace = resolve_trajectory_path(Path(args.traces_root), args.instance, args.trace_variant)
        if hashlib.sha256(trace.read_bytes()).hexdigest() != row['trajectory']['sha256']:
            raise ValueError('Driver trajectory differs from frozen paper input')
        rtt = trace.with_name('ms_trace.jsonl')
        if hashlib.sha256(rtt.read_bytes()).hexdigest() != row['rtt']['sha256']:
            raise ValueError('Driver recorded RTT differs from frozen paper input')
        raw_contract = profile._root_read(proof['contract']['path'])
        full = json.loads(raw_contract)
        self.input = next(x for x in full['inputs'] if x['instance'] == args.instance)
        self.actions = {(x['node_id'], x['action_index']): x for x in full['ordered_measured_actions']
                        if x['instance'] == args.instance}
        self.expansions = self.input['observed_expansions']
        tree = json.loads(trace.read_text())
        stack = [tree['root']]; self.nodes = {}
        while stack:
            node = stack.pop(); self.nodes[node['node_id']] = node
            stack.extend(node.get('children', []))
        self.binding = {'manifest': proof['manifest'], 'contract': proof['contract'],
                        'trajectory_sha256': row['trajectory']['sha256'],
                        'repository_commit': row['repository_commit'],
                        'expected_expansions': row['expansions'], 'expected_actions': row['actions'],
                        'expansion_guard': {'current_max_steps': 30, 'original_cli_bound': False,
                            'reason': 'Above the recorded natural 24/29 expansions; original pilot CLI upper bound was not retained.'},
                        'request_evidence': 'New strict requests must match recovered recording; original 185 raw request bytes were not retained.'}

    def before_build(self, seq, parent, node):
        if not 1 <= seq <= len(self.expansions):
            raise RuntimeError('Unexpected extra expansion beyond original paper contract')
        expected = self.expansions[seq - 1]
        if parent != expected['parent_node_id'] or node != expected['node_id']:
            raise RuntimeError('Selected parent/node differs from original paper contract')

    def action(self, node, index, payload):
        expected = self.actions.get((node, index))
        if expected is None:
            raise RuntimeError('Unexpected physical action outside original paper contract')
        wanted = expected_action_payload(self.nodes[node], index, expected['action_class'])
        if payload != wanted:
            keys = sorted(k for k in set(payload) | set(wanted) if payload.get(k) != wanted.get(k))
            raise RuntimeError('Action differs from the frozen original completion/argument contract: ' + ','.join(keys))

    def after_iteration(self, seq, result):
        if not result.get('ok'):
            return
        expected = self.expansions[seq - 1]
        if (result.get('node_id') != expected['node_id']
                or result.get('finished') is not expected['finished']
                or result.get('event', {}).get('is_duplicate') is not expected['duplicate']
                or len(result.get('e2b_steps', [])) != expected['actual_n_actions']):
            raise RuntimeError('Iteration structure/terminal/action count differs from original contract')

    def final(self, iterations):
        if (len(iterations) != len(self.expansions) or not iterations
                or not iterations[-1].get('finished')
                or sum(len(x.get('e2b_steps', [])) for x in iterations) != len(self.actions)):
            raise RuntimeError('Incomplete original paper input: natural termination or full action coverage missing')


def run_one_e2b_iteration(
    *,
    tree,
    instance: str,
    seq: int,
    build_by_node: dict[int, str],
    contract: RunContract,
    completions: list,
    storage: str,
    work: Path,
    controller_mock_port: int,
    worker_mock_port: int,
    index_port: int,
    materialize_file_context: bool,
    warm_action_worker: bool,
) -> tuple[dict, dict[int, str]]:
    if warm_action_worker or materialize_file_context:
        raise ValueError('Paper reconstruction requires original cold, non-materializing worker')
    from moatless.actions.model import Observation
    from moatless.file_context import FileContext

    tree.assert_runnable()
    if tree.is_finished():
        return {"ok": True, "finished": True, "node_id": None, "event": None, "e2b_steps": []}, build_by_node
    selected = tree._select(tree.root)
    if selected is None:
        return {"ok": True, "finished": True, "node_id": None, "event": {"event_type": "no_expandable_nodes"}, "e2b_steps": []}, build_by_node
    selected_node_id = selected.node_id
    if selected_node_id not in build_by_node:
        return {"ok": False, "error": f"no E2B build recorded for selected node {selected_node_id}", "node_id": selected_node_id, "e2b_steps": []}, build_by_node
    selected_build = build_by_node[selected_node_id]
    new_node = tree._expand(selected) or selected
    contract.before_build(seq, selected_node_id, new_node.node_id)
    before_build = checked_stats(controller_mock_port, completions)
    controller_build_action_only(tree, new_node)
    after_build = checked_stats(controller_mock_port, completions)
    matched = consumed(before_build, after_build, completions,
                       purpose='build_action', node_id=new_node.node_id)
    llm_floor = {"protocol": "served-controller-build-action-v1",
                 "start_cursor": before_build['cursor'], "end_cursor": after_build['cursor'],
                 "node_id": new_node.node_id, "recorded_ms": 1000 * sum(c.dur_s for c in matched),
                 "served": len(matched), "before_stats": before_build, "after_stats": after_build}
    if bool(new_node.is_duplicate) is not contract.expansions[seq - 1]['duplicate']:
        raise RuntimeError('Duplicate decision differs from original paper contract')
    action_events: list[dict] = []
    e2b_steps: list[dict] = []
    child_build = str(uuid.uuid4())

    if not new_node.is_duplicate and new_node.action_steps:
        for idx, action_step in enumerate(new_node.action_steps):
            if action_step.observation is not None:
                continue
            step_to_build = child_build if idx == len(new_node.action_steps) - 1 else str(uuid.uuid4())
            action_payload = action_step.action.model_dump()
            action_payload["action_args_class"] = (
                f"{action_step.action.__class__.__module__}.{action_step.action.__class__.__name__}"
            )
            contract.action(new_node.node_id, idx, action_payload)
            action_model = tree.agent._action_map[type(action_step.action)].model_dump()

            req = {
                "instance": instance,
                "repo_path": "/workspace/repo",
                "index_url": f"http://{common.SIDE_CAR_IP_FOR_SANDBOX}:{index_port}",
                "mock_base_url": f"http://{common.SIDE_CAR_IP_FOR_SANDBOX}:{worker_mock_port}",
                "seq": seq,
                "node_id": new_node.node_id,
                "action": action_payload,
                "action_model": action_model,
                "file_context": new_node.file_context.model_dump(),
                "materialize_file_context": materialize_file_context,
            }
            req_path = work / f"seq{seq}_action{idx}.req.json"
            resp_path = work / f"seq{seq}_action{idx}.resp.json"
            timing_path = work / f"seq{seq}_action{idx}.timing.json"
            req_path.write_text(json.dumps(req, separators=(",", ":")), encoding="utf-8")
            common_env = (
                "PYTHONPATH=/opt/finalbench/slim_shims:/opt/finalbench:/opt/spr_payload:/opt/spr_payload/moatless-det-src "
                "DELTABOX_SLIM_SHIMS=1 "
                "E2B_FINALBENCH_BASE=/opt/finalbench "
                "SPR_PAYLOAD=/opt/spr_payload "
                "PYTHONHASHSEED=0 OPENAI_API_KEY=dummy CUSTOM_LLM_API_KEY=dummy LITELLM_LOG=ERROR "
                "NLTK_DATA=/opt/nltk_data LITELLM_LOCAL_MODEL_COST_MAP=True "
                f"E2B_MATERIALIZE_FILE_CONTEXT={'1' if materialize_file_context else '0'} "
                "FAISS_OPT_LEVEL=generic FAISS_DISABLE_CPU_FEATURES=AVX512_SPR,AVX512,AVX2 "
                "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MALLOC_ARENA_MAX=1 "
            )
            command = (
                "mkdir -p /tmp/finalbench && "
                f"{common_env} "
                "/opt/moatless_det_venv/bin/python "
                "/opt/finalbench/e2b_slim_action_runner.py "
                "/tmp/finalbench/action.req.json /tmp/finalbench/action.resp.json; "
                "RC=$?; exit $RC"
            )
            controller_before_worker = checked_stats(controller_mock_port, completions)
            worker_sync = sync_forward(worker_mock_port, controller_before_worker['cursor'],
                                       completions, purpose='build_action')
            worker_before = worker_sync['after']
            e2b = common.e2b_step(
                from_build=selected_build,
                to_build=step_to_build,
                storage=storage,
                command=command,
                timings_path=timing_path,
                uploads=[(req_path, "/tmp/finalbench/action.req.json")],
                downloads=[("/tmp/finalbench/action.resp.json", resp_path)],
            )
            e2b_steps.append(e2b)
            if not e2b.get("ok"):
                return {"ok": False, "error": "e2b step failed", "e2b": e2b, "node_id": new_node.node_id, "e2b_steps": e2b_steps}, build_by_node
            worker_after = checked_stats(worker_mock_port, completions)
            executed = consumed(worker_before, worker_after, completions,
                                purpose='exec', node_id=new_node.node_id)
            controller_sync = sync_forward(controller_mock_port, worker_after['cursor'],
                                           completions, purpose='exec', node_id=new_node.node_id)
            if controller_sync['skipped_count'] != len(executed):
                raise RuntimeError('Controller synchronization differs from worker consumption')
            e2b['mock_cursor_sync'] = dict(worker=worker_sync, controller=controller_sync,
                                          actual_exec_calls=len(executed))
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            if not resp.get("ok"):
                return {"ok": False, "error": "action runner failed", "response": resp, "e2b": e2b, "node_id": new_node.node_id, "e2b_steps": e2b_steps}, build_by_node
            action_step.observation = Observation.model_validate(resp["observation"])
            if action_step.observation.execution_completion:
                action_step.completion = action_step.observation.execution_completion
            new_node.file_context = FileContext.from_dict(repo=tree.repository, runtime=None, data=resp["file_context"])
            new_node.terminal = action_step.observation.terminal
            ev = dict(resp.get("event") or {})
            ev["action_args_class"] = action_payload["action_args_class"]
            action_events.append(ev)
            selected_build = step_to_build
    else:
        child_build = selected_build

    if new_node.observation:
        new_node.terminal = new_node.observation.terminal
    tree._backpropagate(new_node)
    build_by_node[new_node.node_id] = child_build
    best = tree.get_best_trajectory()
    return {
        "ok": True,
        "finished": tree.is_finished(),
        "node_id": new_node.node_id,
        "build_id": child_build,
        "selected_build_id": build_by_node[selected_node_id],
        "event": {
            "event_type": "e2b_slim_tree_iteration",
            "selected_node_id": selected_node_id,
            "new_node_id": new_node.node_id,
            "total_nodes": len(tree.root.get_all_nodes()),
            "finished_nodes": len(tree.get_finished_nodes()),
            "best_node_id": best.node_id if best else None,
            "controller_llm_floor": llm_floor,
            "n_worker_actions": len(action_events),
            "action_events": action_events,
            "is_duplicate": bool(new_node.is_duplicate),
        },
        "e2b_steps": e2b_steps,
    }, build_by_node


def run_pilot(args: argparse.Namespace, *, transport) -> dict:
    contract = RunContract(args, transport)
    instance = args.instance
    traces_root = Path(args.traces_root)
    traj_path = resolve_trajectory_path(traces_root, instance, args.trace_variant)
    if not traj_path.exists():
        raise FileNotFoundError(traj_path)
    run_id = f"{args.run_id_prefix}_{instance}_{uuid.uuid4().hex[:6]}"
    print(f"[pilot] run_id={run_id}", flush=True)
    work = BASE / "work" / run_id
    results = BASE / "results" / run_id
    logs = work / "logs"
    for p in (work, results):
        p.mkdir(parents=True, exist_ok=False)
    logs.mkdir(parents=True, exist_ok=True)

    controller_mock_port = free_tcp_port()
    worker_mock_port = None
    index_port = None
    controller_mock_proc = worker_mock_proc = index_proc = None
    # Unlike the ordinary AE path, this reconstruction requires exact request
    # message hashes against the recovered recording on both independent mocks.
    os.environ['MOCK_MESSAGE_POLICY'] = 'strict'
    try:
        controller_mock_proc = start_shared_mock(
            instance, args.trace_variant, traces_root, controller_mock_port,
            logs / 'controller_mock.log', results / 'controller_mock_audit.json')
        worker_mock_port = args.worker_mock_port or free_tcp_port()
        if worker_mock_port == controller_mock_port:
            raise ValueError('Controller and worker mock endpoints must be distinct')
        worker_mock_proc = start_shared_mock(
            instance, args.trace_variant, traces_root, worker_mock_port,
            logs / 'worker_mock.log', results / 'worker_mock_audit.json')
        index_port = args.index_port or free_tcp_port()
        if index_port in (controller_mock_port, worker_mock_port):
            raise ValueError('Index and mock endpoints must be distinct')
        print(f'[pilot] dual mocks controller={controller_mock_port} worker={worker_mock_port} index={index_port}', flush=True)
        index_proc = start_external_index_sidecar(instance,
            SOURCE_REPOS / f'swe-bench_{instance}', index_port, logs / 'external_index_sidecar.log')
        sidecar_readiness = transport.sidecars_ready(worker_mock_port, index_port)
        base_build = args.from_build
        create = {'ok': True, 'build_id': base_build, 'reused': False,
                  'fresh_for_input': instance, 'provided_by_owned_suite': True,
                  'fresh_base_manifest': transport.manifest.get('fresh_base_manifest')}

        payload_tar = work / f"{instance}.slim_payload.tar"
        print("[pilot] building slim payload tar", flush=True)
        make_payload_tar(instance, payload_tar)
        root_build = str(uuid.uuid4())
        root_timing = work / "root_setup.timing.json"
        root_command = (
            "mkdir -p /opt /workspace /mnt/disk2/dyp && "
            "tar -xf /tmp/finalbench_payload.tar -C /opt && "
            "rm -f /tmp/finalbench_payload.tar && "
            "mv /opt/repo /workspace/repo && "
            "rm -rf /workspace/repo/.git && "
            "ln -sfn /opt/spr_payload /mnt/disk2/dyp/spr_payload && "
            "ln -sfn /opt/finalbench /mnt/disk2/dyp/finalbench && "
            "ln -sfn /opt/moatless_det_venv /mnt/disk2/dyp/moatless_det_venv && "
            "ln -sfn /opt/spr_payload/moatless-det-src /mnt/disk2/dyp/spr_payload/moatless-det-src && "
            "test -x /opt/moatless_det_venv/bin/python && "
            "test -f /opt/finalbench/e2b_slim_action_runner.py && "
            "test -d /workspace/repo"
        )
        guest_check = (
            "import json,os,urllib.request; "
            f"urls={{'worker':'http://{common.SIDE_CAR_IP_FOR_SANDBOX}:{worker_mock_port}/admin/healthz',"
            f"'index':'http://{common.SIDE_CAR_IP_FOR_SANDBOX}:{index_port}/healthz'}}; "
            "proof={k:json.load(urllib.request.urlopen(v,timeout=5)) for k,v in urls.items()}; "
            "assert all(v.get('ok') is True for v in proof.values()),proof; "
            "meminfo=open('/proc/meminfo').read(); "
            "mem={line.split(':')[0]:int(line.split()[1]) for line in meminfo.splitlines()}; "
            "cpus=sorted(os.sched_getaffinity(0)); "
            "assert len(cpus)==1,cpus; "
            "assert 1.8*1024**2<=mem['MemTotal']<=2*1024**2,mem['MemTotal']; "
            "assert mem['SwapTotal']==0,mem['SwapTotal']; "
            "value={'sidecars':proof,'cpu_affinity':cpus,'nproc':len(cpus),'meminfo':meminfo,"
            "'mem_total_kib':mem['MemTotal'],'swap_total_kib':mem['SwapTotal']}; "
            "raw=json.dumps(value,sort_keys=True); "
            "os.makedirs('/tmp/finalbench',exist_ok=True); "
            "open('/tmp/finalbench/paper-l2-proof.json','w').write(raw+chr(10)); "
            "print('E2B_PAPER_L2_PROOF='+raw)"
        )
        root_command += " && /opt/moatless_det_venv/bin/python -B -c " + shlex.quote(guest_check)
        root = common.e2b_step(
            from_build=base_build,
            to_build=root_build,
            storage=args.storage,
            command=root_command,
            timings_path=root_timing,
            uploads=[(payload_tar, "/tmp/finalbench_payload.tar")],
            downloads=[("/tmp/finalbench/paper-l2-proof.json", work / "root-l2-proof.json")],
            timeout=2400.0,
        )
        if not root.get("ok"):
            out = {"ok": False, "stage": "root_setup", "create": create, "root_setup": root}
            (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            return out
        guest_resources = json.loads((work / "root-l2-proof.json").read_text())
        if (guest_resources.get('nproc') != 1 or len(guest_resources.get('cpu_affinity', [])) != 1
                or not 1.8 * 1024**2 <= guest_resources.get('mem_total_kib', 0) <= 2 * 1024**2
                or guest_resources.get('swap_total_kib') != 0):
            raise RuntimeError('Actual L2 CPU/memory/swap differs from paper reconstruction')
        print(f"[pilot] root setup ok build={root_build}", flush=True)

        print("[pilot] loading controller tree", flush=True)
        tree_dict = load_initial_tree(
            instance, controller_mock_port, traces_root, args.trace_variant)
        controller_repo = SOURCE_REPOS / f"swe-bench_{instance}"
        tree = controller_tree_from_dict(tree_dict, controller_repo, INDEX_STORE, instance)
        build_by_node = {tree.root.node_id: root_build}
        from trajectory_index import load_trajectory
        completions = load_trajectory(str(traj_path))
        iterations = []
        for seq in range(1, args.max_steps + 1):
            it, build_by_node = run_one_e2b_iteration(
                tree=tree,
                instance=instance,
                seq=seq,
                build_by_node=build_by_node,
                contract=contract,
                completions=completions,
                storage=args.storage,
                work=work,
                controller_mock_port=controller_mock_port,
                worker_mock_port=worker_mock_port,
                index_port=index_port,
                materialize_file_context=bool(args.materialize_file_context),
                warm_action_worker=bool(args.warm_action_worker),
            )
            iterations.append(it)
            contract.after_iteration(seq, it)
            print(f"[{seq}] ok={it.get('ok')} node={it.get('node_id')} build={it.get('build_id')}", flush=True)
            if not it.get("ok") or it.get("finished"):
                break

        if all(i.get('ok') for i in iterations):
            contract.final(iterations)
        e2b_steps = [step for it in iterations for step in (it.get("e2b_steps") or [])]
        ck = [float(s.get("checkpoint_persist_ms", 0.0)) for s in e2b_steps if s.get("ok")]
        rs = [float(s.get("resume_ms", 0.0)) for s in e2b_steps if s.get("ok")]
        controller_mock_stats = checked_stats(controller_mock_port, completions)
        worker_mock_stats = checked_stats(worker_mock_port, completions)
        if all(i.get('ok') for i in iterations):
            expected_build = sum(c.purpose == 'build_action' for c in completions)
            expected_exec = sum(c.purpose.startswith('exec.') for c in completions)
            if (controller_mock_stats['cursor'] != len(completions)
                    or controller_mock_stats['n_served'] != expected_build
                    or worker_mock_stats['n_served'] != expected_exec):
                raise RuntimeError('Dual mock total served calls differ from full original recording')
        out = {
            "ok": all(i.get("ok") for i in iterations),
            "instance": instance,
            "run_id": run_id,
            "trace_variant": args.trace_variant,
            "trace_path": str(traj_path),
            "semantics": (
                "same recorded Moatless trajectory; controller/SearchTree and two sequential mock cursors stay outside E2B; "
                "the controller-host mock and index sidecars are external, while actions execute inside E2B and each node is persisted as an E2B build"
            ),
            "measurement_scope": "Table2 ck/rs use inner E2B Go API timings from resume-build finalbench-json, not CLI elapsed time",
            "checkpoint_metric": "checkpoint_persist_ms = Pause() + local snapshot upload",
            "restore_metric": "resume_ms = Factory.ResumeSandbox() return latency",
            "sidecar_note": (
                "The external CodeIndex sidecar serves the same static base index as DeltaBox slim. "
                "Moatless search hits contain locations, while code text is materialized inside the sandbox from the live repo."
            ),
            "create": create,
            "root_setup": root,
            "sidecar_readiness": sidecar_readiness,
            "guest_resource_proof": guest_resources,
            "iterations": iterations,
            "n_node_builds": len(build_by_node),
            "n_e2b_steps": len(e2b_steps),
            "ck_mean_ms": sum(ck) / len(ck) if ck else None,
            "rs_mean_ms": sum(rs) / len(rs) if rs else None,
            "controller_mock_stats": controller_mock_stats,
            "worker_mock_stats": worker_mock_stats,
            "paper_input_contract": contract.binding,
            "mock_audits": {"controller": str(results / 'controller_mock_audit.json'),
                            "worker": str(results / 'worker_mock_audit.json')},
            "message_policy": message_policy(),
            "ports": {"controller_mock": controller_mock_port, "worker_mock_l1": worker_mock_port, "index_l1": index_port},
        }
        (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    finally:
        primary = sys.exc_info()[1]
        errors = []
        for port, proc, filename in ((controller_mock_port, controller_mock_proc, 'controller_mock_audit.json'),
                                      (worker_mock_port, worker_mock_proc, 'worker_mock_audit.json')):
            if proc is not None:
                try:
                    flush_audit(f'http://127.0.0.1:{port}', results / filename, primary_error=primary)
                except BaseException as error:
                    errors.append(error)
                finally:
                    stop_proc(proc)
        stop_proc(index_proc)
        if errors and primary is None:
            raise errors[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="pytest-dev__pytest-8365")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--materialize-file-context", action="store_true",
                    help="Persist Moatless FileContext edits to the live repo for physical-FS workload runs.")
    ap.add_argument("--warm-action-worker", action="store_true",
                    help="Start a persistent in-sandbox Python action worker at root setup and send actions through a shell/FIFO hot path.")
    ap.add_argument("--trace-variant", default="ms", choices=("ms", "p-eagle-ms"))
    ap.add_argument("--traces-root", default=str(DEFAULT_TRACES_ROOT))
    ap.add_argument("--run-id-prefix", default="e2b_slim_same_trace")
    ap.add_argument("--storage", default="/var/tmp/e2b-slim-finalbench")
    ap.add_argument("--clean-storage", action="store_true")
    ap.add_argument("--from-build", default="")
    ap.add_argument("--root-build", default="")
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--disk-mb", type=int, default=4096)
    ap.add_argument("--fc-version", default="v1.14.1_458ca91")
    ap.add_argument("--worker-mock-port", type=int, default=0)
    ap.add_argument("--index-port", type=int, default=0)
    args = ap.parse_args()
    sys.path.insert(0, '/home/atc-ae/delta-box-ae')
    from ae.scripts.e2b_paper_transport import install
    transport = install(common)
    out = run_pilot(args, transport=transport)
    print(json.dumps({k: out.get(k) for k in ("ok", "instance", "run_id", "n_e2b_steps", "ck_mean_ms", "rs_mean_ms")}, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, common._ae_interrupted)
    raise SystemExit(main())
