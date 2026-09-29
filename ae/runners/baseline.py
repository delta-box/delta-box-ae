#!/usr/bin/env python3
"""Execute pinned baseline drivers against bundled inputs in a fresh workspace."""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repro.common import host_state, run_purpose, artifact_records, AE_ROOT, configured_path, configured_value, file_record, load_config, number, public_config, repository_state, write_json
from release.lock import from_environment
from repro.process import execute
from repro.replay_audit import message_policy, summarize, validate_stats

VENDOR = AE_ROOT / 'vendor'
DRIVERS = {'replay': ('replay_copytree', 'real_trace_runner.py'),
           'criu': ('criu_copytree', 'criu_copytree_pilot.py'),
           'fc-diff': ('fc_diff_dm', 'fc_dm_controller_pilot.py'),
           'e2b': ('e2b_finalbench', 'e2b_slim_finalbench_pilot.py'),
           'cube': ('cube_cow_peagle_mcts30_2x_numa12_realrtt', 'scripts/cube_cow_schedule_replay.py')}



def resolve_experiment(backend, collect_phases=False, explicit_id=None):
    if backend not in DRIVERS:
        raise ValueError('Unknown baseline backend')
    expected = 'table-02-' + backend
    allowed = {expected}
    if backend == 'cube':
        allowed.add('figure-01-cube')
    selected = explicit_id or ('figure-01-cube' if collect_phases else expected)
    if selected not in allowed:
        raise ValueError('Experiment identity does not match baseline backend')
    if collect_phases and backend != 'cube':
        raise ValueError('--collect-phases requires Cube')
    if selected == 'figure-01-cube' and not collect_phases:
        raise ValueError('Figure 1 requires Cube phase collection')
    return selected


def paper_cube_profile(config, backend, experiment, collect_phases):
    enabled = config.get('cube', {}).get('profile') == 'paper-disk'
    if enabled and (backend != 'cube' or experiment != 'table-02-cube'
                    or not collect_phases or config.get('baseline_storage') != 'disk'):
        raise ValueError('Cube paper-disk requires Table 2 Cube, phase collection and disk storage')
    if enabled and os.environ.get('AE_MEMORY_JOB'):
        raise ValueError('Cube paper-disk cannot run inside a memory-storage job')
    return enabled


def configure_mock_latency(backend, env, replay_policy="zero"):
    """Pin the paper's Replay policy without changing other backends' RTT."""
    if replay_policy not in ('zero', 'recorded'):
        raise ValueError('Replay mock latency policy must be zero or recorded')
    policy = replay_policy if backend == 'replay' else 'recorded'
    env['MOCK_LATENCY_POLICY'] = policy  # Override any inherited shell setting.
    return {'mock_latency_policy': policy,
            **({'replay_timing_method': 'zero-latency-wall' if policy == 'zero' else 'recorded-sleep-subtracted'} if backend == 'replay' else {})}


def validate_mock_latency(reports, policy):
    for report in reports:
        stats = report['stats']
        if report.get('latency_policy') != policy or stats.get('latency_policy') != policy:
            raise ValueError('Mock latency policy differs from requested configuration')
        if policy == 'zero' and stats.get('sleep_wall_s') != 0.0:
            raise ValueError('Zero-latency mock reported injected sleep')


def stage_local_dependencies(config, output, env):
    """Use real, hashed local NLTK resources and packaged model metadata."""
    venv = configured_path(config, 'moatless_venv')
    cost_maps = list(venv.glob('lib/python*/site-packages/litellm/model_prices_and_context_window_backup.json'))
    if len(cost_maps) != 1:
        raise ValueError('Require exactly one installed LiteLLM local model cost map')
    cache = configured_path(config, 'nltk_data', required=False)
    if cache is None and env.get('NLTK_DATA'):
        cache = Path(env['NLTK_DATA']).resolve()
    if cache is None:
        cache = cost_maps[0].parents[1] / 'llama_index/core/_static/nltk_cache'
    # LlamaIndex checks legacy punkt, while recent NLTK uses punkt_tab. Both
    # must be real datasets; an empty marker directory would hide this bug.
    for name in ('tokenizers/punkt/english.pickle', 'corpora/stopwords/english',
                 'tokenizers/punkt_tab/english/abbrev_types.txt',
                 'tokenizers/punkt_tab/english/collocations.tab',
                 'tokenizers/punkt_tab/english/ortho_context.tab',
                 'tokenizers/punkt_tab/english/sent_starters.txt'):
        path = cache / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Incomplete offline NLTK data: {path}; configure nltk_data with real punkt, punkt_tab and stopwords')
    staged = output / 'nltk_data'
    shutil.copytree(cache, staged, symlinks=False)
    env.update(NLTK_DATA=str(staged), LITELLM_LOCAL_MODEL_COST_MAP='True')
    return {'nltk_data': {'source': str(cache), 'staged': str(staged),
                         'files': [file_record(path) for path in sorted(staged.rglob('*')) if path.is_file()]},
            'litellm_local_model_cost_map': file_record(cost_maps[0]),
            'litellm_source': file_record(cost_maps[0].parent / '__init__.py')}


def configure_test_runtime(config, backend, env):
    """Validate before measuring; never silently downgrade real test execution."""
    sys.path.insert(0, str(VENDOR / 'spr_payload'))
    from baseline_runtime import ENV, describe, settings
    chosen = settings(config.get('baseline_test_runtime', {'backend': 'none'}))
    if chosen['backend'] != 'none' and backend not in ('replay', 'criu', 'fc-diff', 'profile'):
        raise ValueError(f'{backend} has no verified bound test runtime; use a supported baseline')
    provenance = describe(chosen)
    if chosen['backend'] == 'local-pytest':
        python = Path(chosen['python'])
        if not python.is_file():
            raise FileNotFoundError(python)
        clean = dict(env)
        for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
            clean.pop(name, None)
        output = subprocess.check_output([str(python), '-c',
            'import sys, json, importlib.metadata as m, pytest; '
            'print(json.dumps({"python": sys.version, "pytest": pytest.__version__, '
            '"packages": sorted((d.metadata["Name"], d.version) for d in m.distributions())}))'],
            env=clean, text=True, timeout=30)
        provenance['environment_identity'] = json.loads(output)
        provenance['python_binary'] = file_record(python.resolve())
    env.pop(ENV + '_FILE', None)
    env[ENV] = json.dumps(chosen)
    if chosen['backend'] == 'local-pytest':
        env['DELTABOX_TEST_STEP_TIMEOUT'] = str(max(240, 5 * chosen['timeout_s'] + 120))
    else:
        env.pop('DELTABOX_TEST_STEP_TIMEOUT', None)
    return provenance


def select_criu_binary(config, env):
    """Keep the historical default; an explicit private binary is a new condition."""
    selected = configured_path(config, 'criu_bin', required=False)
    if selected is None:
        choice = env.get('FINALBENCH_CRIU')
        if choice and '/' not in choice:
            resolved = shutil.which(choice, path=env.get('PATH'))
            if resolved is None:
                raise FileNotFoundError(f'FINALBENCH_CRIU command not found in PATH: {choice}')
            choice = resolved
        selected = Path(choice or shutil.which('criu', path=env.get('PATH')) or '/usr/sbin/criu')
    selected = selected.resolve(strict=True)
    version = subprocess.check_output([str(selected), '--version'], text=True, timeout=10).strip()
    env['FINALBENCH_CRIU'] = str(selected)
    return dict(file_record(selected), version=version,
                selection='config.criu_bin' if configured_path(config, 'criu_bin', required=False) else 'historical FINALBENCH_CRIU/PATH default')


def stage_payload(config, trace, instance, output, *, allow_missing_rtt=False,
                  restore_history_order=False, repository_commit=None):
    source = configured_path(config, 'payload')
    payload = output / 'payload'
    payload.mkdir()
    records = []
    for name in ('index_store', 'moatless-det-src'):
        target = source / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        (payload / name).symlink_to(target.resolve(), target_is_directory=True)
    repository = payload / 'repos' / ('swe-bench_' + instance)
    repository.parent.mkdir()
    trace_commit = json.loads(trace.read_text()).get('repository', {}).get('commit')
    if trace_commit and repository_commit and trace_commit != repository_commit:
        raise ValueError('Explicit repository commit differs from the trace')
    commit = trace_commit or repository_commit
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit.lower()):
        raise ValueError('Trace requires a full repository commit')
    subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout',
                    str(source / 'repos' / repository.name), str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), 'checkout', '--quiet', '--detach', commit], check=True)
    write_json(output / 'repository.json', {'instance': instance, 'commit': commit,
               'source': str(source / 'repos' / repository.name), 'checkout': 'fresh git clone, detached recorded base commit',
               'commit_binding': 'trace' if trace_commit else 'explicit cohort base-commit binding'})
    for path in sorted((source / 'moatless-det-src').rglob('*.py')):
        if '__pycache__' not in path.parts:
            records.append(file_record(path))
    index = source / 'index_store' / instance
    if not index.is_dir():
        raise FileNotFoundError(f'Prebuilt index is required (no automatic download): {index}')
    for path in sorted(index.rglob('*')):
        if path.is_file():
            records.append(file_record(path))
    for path in sorted((VENDOR / 'spr_payload').glob('*.py')):
        shutil.copy2(path, payload / path.name)
        records.append(file_record(path))
    (payload / '__init__.py').touch()
    traces = output / 'mock_traces'
    for target in (payload / 'det_traces/ms' / instance, traces / 'qwen3-coder-30b-ms' / instance):
        target.mkdir(parents=True)
        shutil.copyfile(trace, target / 'trajectory.json')
        rtt = trace.parent / 'ms_trace.jsonl'
        if not rtt.is_file() and not allow_missing_rtt:
            raise FileNotFoundError(f'Missing recorded RTT: {rtt}')
        if rtt.is_file():
            shutil.copyfile(rtt, target / rtt.name)
    if (trace.parent / 'ms_trace.jsonl').is_file():
        records.append(file_record(trace.parent / 'ms_trace.jsonl'))
    if restore_history_order:
        sys.path.insert(0, str(AE_ROOT.parent))
        from replay.history_order import stage_history_order
        adaptation = stage_history_order(payload, trace,
                                         message_policy=config.get('replay_message_policy', 'audit'))
        write_json(output / 'history-serialization.json', adaptation)
        records.append(file_record(AE_ROOT.parent / 'replay/history_order.py'))
        for name in ('message_history.py', '_replay_history_order.py', '_replay_history_order.json'):
            records.append(file_record(payload / 'moatless-det-src/moatless' / name))
        records.append(file_record(output / 'history-serialization.json'))
    if config.get('recorded_search_order', False):
        sys.path.insert(0, str(AE_ROOT.parent))
        from replay.search_order import stage_search_order
        adaptation = stage_search_order(payload, trace)
        write_json(output / 'search-file-priority.json', adaptation)
        records.append(file_record(AE_ROOT.parent / 'replay/search_order.py'))
        for name in ('repository/file.py', '_replay_search_order.py', '_replay_search_order.json'):
            records.append(file_record(payload / 'moatless-det-src/moatless' / name))
        records.append(file_record(output / 'search-file-priority.json'))
    return payload, traces, records


def pilot_paths(base, backend, instance):
    if backend == 'replay':
        return [base / 'results/real_per_restore' / instance / 'summary.json']
    return sorted((base / 'results').glob('*/pilot_result.json'))


def test_execution_summary(path, backend):
    """Report actual cases separately from mere successful replay completion."""
    pilot = json.loads(path.read_text())
    rows = []
    if backend == 'replay':
        for item in sorted(path.parent.rglob('*.driver.json')):
            rows.extend(json.loads(item.read_text()).get('test_runtime_records', []))
    elif backend in ('criu', 'fc-diff'):
        for checkpoint in pilot.get('ckpts', []):
            rows.extend(checkpoint.get('test_runtime_records',
                (checkpoint.get('event') or {}).get('test_runtime_records', [])))
    statuses = {'PASSED': 0, 'FAILED': 0, 'ERROR': 0, 'SKIPPED': 0}
    for row in rows:
        for result in row['results']:
            statuses[result['status']] += 1
    return {'invocations': len(rows), 'actual_case_status_counts': statuses,
            'all_selected_cases_passed': bool(sum(statuses.values())) and
                statuses['PASSED'] == sum(statuses.values()),
            'semantics': 'Actual selected cases only; replay completion does not mean all repository tests passed'}


def validate_pilot(path, backend, policy='strict', *, latency_policy=None):
    data = json.loads(path.read_text())
    if data.get('ok') is not True:
        raise ValueError(f'{path}: benchmark did not succeed: {data.get("status")}')
    if backend == 'replay':
        if latency_policy is not None and (data.get('mock_latency_policy') != latency_policy
                or data.get('replay_timing_method') != ('zero-latency-wall' if latency_policy == 'zero' else 'recorded-sleep-subtracted')):
            raise ValueError('Replay summary differs from requested timing identity')
        expected = data.get('requested_restores', 0)
        if expected <= 0 or data.get('completed_restores') != expected:
            raise ValueError('Replay incomplete or empty restore set')
        with (path.parent / 'restores.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != expected or any(r.get('ok') != 'True' or r.get('rc') != '0' for r in rows):
            raise ValueError('Replay event evidence incomplete')
        for row in rows:
            wall = number(float(row['restore_ms']), 'restore_ms')
            sleep = number(float(row['mock_sleep_ms']), 'mock_sleep_ms')
            adjusted = number(float(row['restore_zero_llm_ms']), 'restore_zero_llm_ms')
            if abs(wall - sleep - adjusted) > 1e-6:
                raise ValueError('Replay zero-LLM timing is inconsistent')
            if latency_policy == 'zero' and (row.get('mock_latency_policy') != 'zero'
                    or row.get('replay_timing_method') != 'zero-latency-wall' or sleep != 0
                    or float(row['replay_ms']) != float(row['replay_zero_llm_ms'])):
                raise ValueError('Replay duration with zero LLM delay is inconsistent')
            validate_stats({'message_policy': row.get('message_policy', 'strict'),
                            'n_mismatch': int(row.get('mock_mismatch') or 0) if row.get('mock_mismatch') != 'None' else 0,
                            'n_protocol_errors': int(row.get('mock_protocol_errors') or 0)}, policy)
        return {'checkpoints': 1, 'restores': len(rows), 'checkpoint_kind': 'pristine-copy proxy'}
    if backend in ('criu', 'fc-diff'):
        if data.get('tail_error') or data.get('status') == 'TAIL_CRASH_OK':
            raise ValueError('Tail crash is an incomplete run, not full-trace success')
        checkpoints, restores = data['ckpts'], data['restore_events']
        ckfield = 'checkpoint_total_ms' if backend == 'criu' else 'fc_total_ms'
        for row in checkpoints:
            number(row[ckfield], ckfield)
        if not checkpoints:
            raise ValueError('Missing checkpoint evidence')
        for row in restores:
            if backend == 'criu':
                number(row['restore_total_ms'], 'restore_total_ms')
            else:
                number(row['load']['fc_load_ms'], 'fc_load_ms')
                number(row['dm_restore']['dm_restore_ms'], 'dm_restore_ms')
                number(row['merge']['merge_ms'], 'merge_ms')
        return {'checkpoints': len(checkpoints), 'restores': len(restores)}
    if backend == 'cube':
        iterations = data['iterations']
        if not iterations or any(row.get('ok') is not True for row in iterations):
            raise ValueError('Cube empty or incomplete event evidence')
        for row in iterations:
            number(row['checkpoint_wall_ms' if row['kind'] == 'ckpt' else 'restore_wall_ms'], 'latency')
        return {'checkpoints': sum(r['kind'] == 'ckpt' for r in iterations),
                'restores': sum(r['kind'] == 'restore' for r in iterations)}
    steps = [step for row in data['iterations'] for step in row.get('e2b_steps', [])]
    if not steps or any(s.get('ok') is not True for s in steps):
        raise ValueError('E2B empty or incomplete step evidence')
    for step in steps:
        number(step['checkpoint_persist_ms'], 'checkpoint_persist_ms')
        number(step['resume_ms'], 'resume_ms')
    return {'checkpoints': len(steps), 'restores': len(steps)}



def validate_trace_events(path, backend, trace, limit, schedule=None, policy='strict'):
    data = json.loads(path.read_text())
    sys.path.insert(0, str(VENDOR / 'finalbench/replay_copytree'))
    from walker import parse_trajectory
    parsed = parse_trajectory(trace)
    if backend == 'cube':
        expected = [json.loads(line) for line in schedule.read_text().splitlines() if line.strip()]
        if limit: expected = expected[:limit]
        observed = data['iterations']
        if len(expected) != len(observed): raise ValueError('Cube missing scheduled events')
        for event, row in zip(expected, observed):
            if row['kind'] != event['type']: raise ValueError('Cube event order changed')
            key = 'ckpt_id' if event['type'] == 'ckpt' else 'restore_to_ckpt_id'
            # Vendor uses schedule_ckpt_id / schedule_target_id in the raw records.
            raw_key = 'ckpt_id' if event['type'] == 'ckpt' else 'restore_to_ckpt_id'
            if row.get(raw_key) != event[key]: raise ValueError('Cube target changed')
    elif backend == 'replay':
        expected = [e for e in parsed['events'] if e['event'] == 'restore']
        if limit: expected = expected[:limit]
        with (path.parent / 'restores.csv').open() as stream:
            observed = list(csv.DictReader(stream))
        if len(expected) != len(observed): raise ValueError('Replay missing trace restores')
        for event, row in zip(expected, observed):
            if int(row['target_node']) != event['target_node']: raise ValueError('Replay restored wrong target')
    elif backend in ('criu', 'fc-diff'):
        budget = limit or 29
        expansions = [e for e in parsed['events'] if e['event'] == 'expand'][:budget]
        expected_rs = [e for e in parsed['events'] if e['event'] == 'restore' and e['iter'] < len(expansions)]
        ckpts, restores = data['ckpts'], data['restore_events']
        if len(ckpts) != len(expansions) + 1 or len(restores) != len(expected_rs):
            raise ValueError('Checkpoint/restore count does not cover selected trace prefix')
        for event, row in zip(expansions, ckpts[1:]):
            actual = row.get('node_id', (row.get('state') or {}).get('node_id'))
            if actual != event['new_node']: raise ValueError('Checkpoint node differs from input trace')
        for event, row in zip(expected_rs, restores):
            if row.get('selected_node_id') != event['target_node']: raise ValueError('Restore target differs from input trace')
    else:
        iterations = data['iterations']
        expected = [e for e in parsed['events'] if e['event'] == 'expand'][:limit or 29]
        if len(iterations) != len(expected) or any(it.get('ok') is not True for it in iterations):
            raise ValueError('E2B incomplete iteration sequence')
        nodes = {}
        pending = [json.loads(trace.read_text())['root']]
        while pending:
            node = pending.pop()
            nodes[node['node_id']] = node
            pending.extend(node.get('children') or [])
        step_count = 0
        for event, observed in zip(expected, iterations):
            evidence = observed['event']
            if (observed['node_id'] != event['new_node'] or evidence['new_node_id'] != event['new_node']
                    or evidence['selected_node_id'] != event['parent']):
                raise ValueError('E2B node or restore target differs from trace')
            node = nodes[event['new_node']]
            actions = [] if node.get('is_duplicate') else node.get('action_steps') or []
            rows, steps = evidence['action_events'], observed['e2b_steps']
            if not len(actions) == len(rows) == len(steps) == evidence['n_worker_actions']:
                raise ValueError('E2B missing action checkpoint/restore evidence')
            for action, row in zip(actions, rows):
                if row['action_args_class'] != action['action']['action_args_class'] or row['node_id'] != event['new_node']:
                    raise ValueError('E2B action evidence differs from trace')
            step_count += len(steps)
        if data['n_e2b_steps'] != step_count:
            raise ValueError('E2B step total differs from trace')
        validate_stats(data['mock_stats'], policy)



def validate_paper_e2b(path, trace, instance, proof, environment, output):
    """Independently bind the complete original action order to real transfers."""
    from ae.scripts.e2b_paper_profile import _root_read
    sys.path.insert(0, str(VENDOR / 'finalbench/e2b_finalbench'))
    from e2b_paper_action import recorded_action_payload
    data = json.loads(path.read_text())
    contract = json.loads(_root_read(proof['contract']['path']))
    expected = next(x for x in contract['inputs'] if x['instance'] == instance)
    input_row = next(x for x in proof['inputs'] if x['instance'] == instance)
    actions = {(a['node_id'], a['action_index']): a for a in contract['ordered_measured_actions']
               if a['instance'] == instance}
    nodes = {}; pending = [json.loads(trace.read_text())['root']]
    while pending:
        node = pending.pop(); nodes[node['node_id']] = node
        pending.extend(node.get('children') or [])
    binding = data.get('paper_input_contract', {})
    if (data.get('instance') != instance or data.get('message_policy') != 'strict'
            or binding.get('contract') != proof['contract']
            or binding.get('manifest') != proof['manifest']
            or binding.get('trajectory_sha256') != input_row['trajectory']['sha256']
            or binding.get('repository_commit') != input_row['repository_commit']
            or binding.get('expected_expansions') != input_row['expansions']
            or binding.get('expected_actions') != input_row['actions']):
        raise ValueError('Paper pilot is not bound to the frozen input/contract')
    create = data.get('create', {})
    if (create.get('ok') is not True or create.get('reused') is not False
            or create.get('fresh_for_input') != instance
            or create.get('build_id') != environment['from_build']
            or create.get('fresh_base_manifest') != environment['fresh_base_manifest']['path']):
        raise ValueError('Paper pilot used an unverified/reused base')

    def receipt(step):
        if (step.get('ok') is not True or step.get('host_rc') != 0
                or step.get('timing_present') is not True):
            raise ValueError('Paper physical step has no successful real Go result')
        target = Path(step['transport_receipt'])
        if (not target.is_absolute() or target.resolve() != target
                or not target.is_relative_to(output)):
            raise ValueError('Paper transport receipt escapes current input')
        value = json.loads(target.read_text())
        if (value.get('kind') != 'ssh-files-outside-inner-timers'
                or value.get('returncode') != 0 or value.get('transfer_errors')):
            raise ValueError('Paper transfer failed')
        for row in value.get('uploads', []) + value.get('downloads', []):
            item = Path(row['path'])
            if item.resolve() != item or not item.is_relative_to(output):
                raise ValueError('Paper action evidence escapes current input')
            actual = file_record(item)
            if any(actual[k] != row[k] for k in ('bytes', 'sha256')):
                raise ValueError('Paper transferred bytes changed')
        timings = [x for x in value.get('downloads', []) if x['path'].endswith('.timing.json')]
        if len(timings) != 1:
            raise ValueError('Missing independently hashed real timing result')
        timing = json.loads(Path(timings[0]['path']).read_text())
        if timing.get('ok') is not True or any(timing.get(k) != step.get(k)
                  for k in ('checkpoint_persist_ms', 'resume_ms', 'pause_ms', 'snapshot_upload_ms', 'to_build')):
            raise ValueError('Pilot timing differs from real Go result')
        values = {k: number(timing.get(k), k) for k in
                  ('checkpoint_persist_ms', 'resume_ms', 'pause_ms', 'snapshot_upload_ms')}
        if (timing.get('to_build') != value.get('to_build')
                or abs(values['checkpoint_persist_ms'] - values['pause_ms'] - values['snapshot_upload_ms']) > 0.003000001
                or timing.get('pause_error') or timing.get('command_error')):
            raise ValueError('Raw Go checkpoint source, success or pause+upload timing window differs')
        return value

    root = receipt(data['root_setup'])
    if root['from_build'] != environment['from_build']:
        raise ValueError('Root setup did not consume the freshly bound base')
    resource_files = [r for r in root['downloads'] if Path(r['path']).name == 'root-l2-proof.json']
    if len(resource_files) != 1:
        raise ValueError('Root setup lacks transferred actual L2 resource proof')
    resource = json.loads(Path(resource_files[0]['path']).read_text())
    if (resource != data.get('guest_resource_proof') or type(resource.get('nproc')) is not int
            or resource['nproc'] != 1 or len(resource.get('cpu_affinity', [])) != 1
            or type(resource.get('mem_total_kib')) is not int
            or not 1.8 * 1024**2 <= resource['mem_total_kib'] <= 2 * 1024**2
            or type(resource.get('swap_total_kib')) is not int or resource['swap_total_kib'] != 0
            or set(resource.get('sidecars', {})) != {'worker', 'index'}
            or any(v.get('ok') is not True for v in resource['sidecars'].values())):
        raise ValueError('Actual L2 CPU/memory/swap/sidecar conditions failed')
    builds = {json.loads(trace.read_text())['root']['node_id']: root['to_build']}
    seen_builds = {environment['from_build'], root['to_build']}
    observed = data.get('iterations', [])
    if len(observed) != len(expected['observed_expansions']):
        raise ValueError('Paper expansion count differs')
    count = 0
    for wanted, row in zip(expected['observed_expansions'], observed):
        ev = row.get('event', {}); steps = row.get('e2b_steps', [])
        if (row.get('ok') is not True or row.get('node_id') != wanted['node_id']
                or ev.get('new_node_id') != wanted['node_id']
                or ev.get('selected_node_id') != wanted['parent_node_id']
                or row.get('finished') is not wanted['finished']
                or ev.get('is_duplicate') is not wanted['duplicate']
                or len(steps) != wanted['actual_n_actions']
                or ev.get('n_worker_actions') != len(steps)):
            raise ValueError('Paper node/parent/terminal/action count differs')
        parent_build = builds[wanted['parent_node_id']]
        if row.get('selected_build_id') != parent_build:
            raise ValueError('Paper selected build does not restore the recorded parent')
        event_actions = ev.get('action_events', [])
        if len(event_actions) != len(steps):
            raise ValueError('Missing real action event')
        for index, (step, event) in enumerate(zip(steps, event_actions)):
            action = actions[(wanted['node_id'], index)]
            rec = receipt(step)
            if rec['from_build'] != parent_build or rec['to_build'] in seen_builds:
                raise ValueError('Paper physical restore/checkpoint chain differs')
            seen_builds.add(rec['to_build']); parent_build = rec['to_build']
            requests = [x for x in rec.get('uploads', []) if x['path'].endswith('.req.json')]
            responses = [x for x in rec.get('downloads', []) if x['path'].endswith('.resp.json')]
            if len(requests) != 1 or len(responses) != 1:
                raise ValueError('Physical action lacks complete request/response')
            request = json.loads(Path(requests[0]['path']).read_text())
            response = json.loads(Path(responses[0]['path']).read_text())
            raw_action = recorded_action_payload(
                nodes[wanted['node_id']], index, action['action_class'], action['action'])
            if (request.get('instance') != instance or request.get('seq') != wanted['seq']
                    or request.get('node_id') != wanted['node_id'] or request.get('action') != raw_action
                    or response.get('ok') is not True
                    or event.get('node_id') != wanted['node_id']
                    or event.get('action_args_class') != action['action_class']):
                raise ValueError('Real paper action differs from the recovered ordered contract')
            count += 1
        if row.get('build_id') != parent_build:
            raise ValueError('Paper node build does not equal the final real checkpoint')
        builds[wanted['node_id']] = parent_build
    if not observed[-1].get('finished') or count != input_row['actions'] or data.get('n_e2b_steps') != count:
        raise ValueError('Paper input did not naturally complete all original actions')
    # Replay calls remain split between two servers. Do not sum cursor values
    # or demand the worker consume controller-only calls.
    sys.path.insert(0, str(VENDOR / 'spr_payload'))
    from trajectory_index import load_trajectory
    completions = load_trajectory(str(trace))
    for role, purpose in (('controller', 'build_action'), ('worker', 'exec.')):
        stats = data[role + '_mock_stats']
        validate_stats(stats, 'strict')
        wanted = sum(c.purpose == purpose if role == 'controller' else c.purpose.startswith(purpose)
                     for c in completions)
        if (stats.get('ok') is not True or type(stats.get('n_served')) is not int
                or stats['n_served'] != wanted or stats.get('total') != len(completions)):
            raise ValueError('Paper dual-mock actual served counts differ')
        if role == 'controller' and stats.get('cursor') != len(completions):
            raise ValueError('Paper controller did not consume the complete recording')
        audit_path = path.parent / (role + '_mock_audit.json')
        audit = json.loads(audit_path.read_text())
        stable = ('ok', 'instance_id', 'variant', 'cursor', 'total', 'n_served',
                  'n_mismatch', 'n_protocol_errors', 'message_policy', 'latency_policy',
                  'audit_records_dropped', 'audit_payloads_omitted', 'sleep_wall_s')
        if (any(audit.get('stats', {}).get(k) != stats.get(k) for k in stable)
                or data.get('mock_audits', {}).get(role) != str(audit_path)):
            raise ValueError('Paper dual-mock audit differs from terminal statistics')
    return {'status': 'verified', 'expansions': len(observed), 'actions': count,
            'checkpoint_restore_pairs': count, 'mock_servers': 2, 'actual_l2_resources': resource,
            'args_normalization': 'Full original functional arguments plus exact Thought from the same frozen ReAct completion; retained nonempty thoughts must agree',
            'original_request_bytes': 'not retained; comparison is to recovered recorded action contract',
            'contract': proof['contract']}


def run(args):
    config = load_config(args.config)
    policy = message_policy(config.get('replay_message_policy', 'audit'))
    experiment = resolve_experiment(args.backend, args.collect_phases, getattr(args, 'experiment_id', None))
    cube_disk_profile = paper_cube_profile(config, args.backend, experiment, args.collect_phases)
    paper_e2b = config.get('e2b', {}).get('profile') == 'paper-nested'
    if paper_e2b:
        from ae.scripts.e2b_paper_profile import verify_inputs
        if args.backend != 'e2b' or experiment != 'table-02-e2b' or args.limit or os.environ.get('AE_MEMORY_JOB'):
            raise ValueError('Paper nested profile requires the complete E2B-only disk suite')
        paper_inputs = verify_inputs(config, guest_ready=True)
        selected = next((r for r in paper_inputs['inputs'] if r['instance'] == args.instance), None)
        if (selected is None or args.trace.absolute() != Path(selected['trajectory']['path'])
                or getattr(args, 'repository_commit', None) != selected['repository_commit']):
            raise ValueError('Paper runner input/base commit differs from frozen selection')
        policy = 'strict'

    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    trace = args.trace.absolute()  # Keep the logical bundle directory beside ms_trace.jsonl.
    record = {'schema_version': 1, 'experiment': experiment,
              'backend': args.backend, 'instance': args.instance, 'status': 'preparing',
              'run_purpose': run_purpose('quick-check' if args.limit else 'full-trace'),
              'input': file_record(trace), 'config': public_config(config),
              'runtime': repository_state(), 'release': from_environment(),
              'host': host_state(), 'analysis_mode': 'fresh-measurement'}
    if args.backend != 'cube':
        record['message_policy'] = policy
    record['recorded_search_order'] = bool(config.get('recorded_search_order', False))
    record['measurement_identity'] = json.loads(os.environ.get('AE_MEASUREMENT_IDENTITY', '{}'))
    manifest = output / 'run.json'
    write_json(manifest, record)
    try:
        if cube_disk_profile:
            from cube_disk import verify as verify_cube_disk
            record['disk_backing'] = verify_cube_disk(config)
            record['storage_mode'] = 'disk-backed-xfs'
            write_json(output / 'cube_disk_before.json', record['disk_backing'])
        elif os.environ.get('AE_MEMORY_JOB'):
            record['memory_backing'] = json.loads(os.environ['AE_MEMORY_JOB'])
            record['storage_mode'] = 'tmpfs-noswap'
        elif args.backend == 'cube' and config.get('baseline_storage') == 'tmpfs':
            from cube_memory import verify as verify_cube_memory
            record['memory_backing'] = verify_cube_memory(config)
            record['storage_mode'] = 'tmpfs-noswap'
            write_json(output / 'cube_memory_before.json', record['memory_backing'])
        elif config.get('baseline_storage') == 'tmpfs':
            raise ValueError('Memory baseline must run through the isolated one-click memory wrapper')
        phase_start = None
        if args.collect_phases:
            if args.backend != 'cube':
                raise ValueError('--collect-phases requires Cube')
            from cube_phases import begin
            phase_start = begin(configured_path(config, 'cube.phase_log'))
            record['phase_instrumentation'] = file_record(configured_path(config, 'cube.phase_binary'))
        payload, traces, records = stage_payload(
            config, trace, args.instance, output,
            restore_history_order=args.backend in ('replay', 'criu', 'fc-diff'),
            repository_commit=getattr(args, 'repository_commit', None))
        if paper_e2b:
            from ae.scripts.e2b_paper_search import stage_historical_search
            search_proof = stage_historical_search(payload)
            search_proof_path = output / 'paper-search-protocol.json'
            write_json(search_proof_path, search_proof)
            record['paper_search_protocol'] = file_record(search_proof_path)
            records.extend([file_record(AE_ROOT / 'scripts/e2b_paper_search.py'),
                            file_record(Path(search_proof['staged_path'])),
                            file_record(search_proof_path)])
        if args.backend in ('replay', 'fc-diff', 'criu'):
            record['history_serialization'] = file_record(output / 'history-serialization.json')
        dirname, entry = DRIVERS[args.backend]
        if paper_e2b:
            entry = 'e2b_paper_nested_driver.py'
        base = output / 'driver'
        shutil.copytree(VENDOR / 'finalbench' / dirname, base)
        record['sources'] = records + [file_record(p) for p in sorted(base.rglob('*.py'))]
        env = os.environ.copy()
        env.update(AE_BASE=str(base), SPR_PAYLOAD=str(payload), MOCK_TRACES_ROOT=str(traces),
                   MOCK_MESSAGE_POLICY=policy,
                   MOCK_MISMATCH_DIR=str(output / "diagnostics"),
                   MOATLESS_VENV=str(configured_path(config, 'moatless_venv')),
                   PYTHONPATH=os.pathsep.join([str(base), str(payload), str(payload / 'moatless-det-src')]))
        record.update(configure_mock_latency(args.backend, env, config.get('replay_mock_latency_policy', 'zero')))
        record['baseline_test_runtime'] = configure_test_runtime(config, args.backend, env)
        if args.backend != 'cube':
            record['local_dependencies'] = stage_local_dependencies(config, output, env)
        if args.backend == 'criu':
            record['criu_binary'] = select_criu_binary(config, env)
        python = configured_path(config, 'moatless_venv') / 'bin/python'
        if not python.is_file():
            raise FileNotFoundError(python)
        command = [str(python), *(['-B'] if paper_e2b else []), str(base / entry)]
        prefix = 'ae_' + uuid.uuid4().hex[:12]
        steps = 30 if paper_e2b else args.limit or 29
        if args.backend == 'replay':
            command += [args.instance]
            if args.limit:
                command += ['--max-restores', str(args.limit)]
        elif args.backend == 'criu':
            command += ['--instance', args.instance, '--max-steps', str(steps),
                        '--run-id-prefix', prefix, '--cleanup-large-artifacts']
        elif args.backend == 'fc-diff':
            sys.path.insert(0, str(Path(__file__).resolve().parent / 'deltabox'))
            from provenance import cached_digest
            record['images'] = {key: cached_digest(configured_path(config, key), AE_ROOT / 'work/image-hashes.json')
                                for key in ('kernel', 'base_xfs')}
            record['images']['data_xfs'] = cached_digest(configured_path(config, 'images_dir') / 'data-django.xfs', AE_ROOT / 'work/image-hashes.json')
            env.update(AE_FC_DATA_XFS=str(configured_path(config, "images_dir") / "data-django.xfs"), AE_D_OVERLAY=str(configured_path(config, 'deltafs')),
                       AE_KERNEL=str(configured_path(config, 'kernel')),
                       AE_BASE_XFS=str(configured_path(config, 'base_xfs')),
                       FCDM_WORK_BASE=str(output / 'work'))
            command += ['--instance', args.instance, '--max-steps', str(steps), '--mem-mib', '8192',
                        '--vcpus', '4', '--snapshot-mode', 'diff', '--run-id-prefix', prefix,
                        '--cleanup-large-artifacts']
            # Fixed guest addresses/TAP/routes are private to this execution.
            command = ['unshare', '--mount', '--net', '--propagation', 'private',
                       str(sys.executable), str(Path(__file__).with_name('namespace_exec.py')), *command]
        elif args.backend == 'cube':
            if args.schedule is None:
                raise ValueError('Cube needs the same bundled --schedule as DeltaBox')
            record['schedule'] = file_record(args.schedule)
            schedule = base / 'schedule.jsonl'
            shutil.copyfile(args.schedule, schedule)
            with (base / 'manifest.tsv').open('w') as stream:
                writer = csv.DictWriter(stream, ['instance', 'group', 'schedule'], delimiter='\t')
                writer.writeheader(); writer.writerow({'instance': args.instance, 'group': args.instance.split('__')[0], 'schedule': schedule})
            cube = config['cube']
            from cube_environment import capture_cube_environment
            observed_cube = capture_cube_environment(
                api_url=cube['api_url'], template=cube['template'],
                sdk_path=configured_path(config, 'cube.sdk'),
                phase_binary=configured_path(config, 'cube.phase_binary'))
            record['cube_environment'] = observed_cube
            env.update(CUBE_SDK_PATH=str(configured_path(config, 'cube.sdk')),
                       CUBE_API_URL=cube['api_url'], CUBE_PROXY_NODE_IP=str(cube['proxy_node_ip']))
            if cube.get('proxy_port_http'):
                env['CUBE_PROXY_PORT_HTTP'] = str(cube['proxy_port_http'])
            command += ['--manifest', str(base / 'manifest.tsv'), '--template', cube['template'], '--run-id-prefix', prefix, '--fail-fast']
            command += ['--template-cpu-millicores', str(observed_cube['template_cpu_millicores']),
                        '--template-memory-mb', str(observed_cube['template_memory_mb'])]
            if observed_cube['template'].get('writable_layer_size'):
                command += ['--writable-layer-size', observed_cube['template']['writable_layer_size']]
            if args.limit:
                command += ['--max-events', str(args.limit)]
        else:
            e2b = config['e2b']
            from_build = configured_value(config, 'e2b.from_build')
            from e2b_environment import configure
            record['e2b_environment'] = configure(config, env, instance=args.instance)
            env.update(E2B_FINALBENCH_BASE=str(base), DELTABOX_STD_BASE=str(VENDOR / 'finalbench/deltabox_std'))
            command += ['--instance', args.instance, '--max-steps', str(steps),
                        '--traces-root', str(traces), '--trace-variant', 'ms',
                        '--run-id-prefix', prefix, '--storage', record['e2b_environment']['storage'], '--from-build', from_build]
            if paper_e2b:
                record['paper_input_contract'] = paper_inputs
                command += ['--mem-mib', '2048', '--disk-mb', '4096',
                            '--fc-version', 'v1.14.1_458ca91']
                for key, flag in (('worker_mock_port', '--worker-mock-port'), ('index_port', '--index-port')):
                    value = e2b.get(key)
                    if value is not None:
                        if type(value) is not int or not 1024 <= value <= 65535:
                            raise ValueError('Invalid paper sidecar port')
                        command += [flag, str(value)]
        if args.backend == 'e2b':
            warm_worker = config['e2b'].get('warm_action_worker', True)
            if type(warm_worker) is not bool:
                raise ValueError('e2b.warm_action_worker must be a boolean')
            record['e2b_worker_mode'] = 'warm' if warm_worker else 'cold'
            if warm_worker:
                command.append('--warm-action-worker')
        record.update(command=command, status='planned' if args.dry_run else 'running')
        write_json(manifest, record)
        if args.dry_run:
            print(json.dumps(record, indent=2)); return 0
        result = execute(command, output / 'process', cwd=base, env=env, timeout=args.timeout)
        if cube_disk_profile:
            record['disk_backing_after'] = verify_cube_disk(config)
            write_json(output / 'cube_disk_after.json', record['disk_backing_after'])
        if args.backend == 'e2b':
            from e2b_environment import verify_snapshot_inputs
            verify_snapshot_inputs(record['e2b_environment'])
            checked_inputs = record['e2b_environment'].get('snapshot_dependencies',
                record['e2b_environment'].get('base_build', []))
            if checked_inputs:
                record['e2b_environment'].update(snapshot_inputs_unchanged=True,
                                               verified_snapshot_input_count=len(checked_inputs))
        paths = pilot_paths(base, args.backend, args.instance)
        if result['status'] != 'ok' or len(paths) != 1:
            raise RuntimeError(f'Driver failed or expected one result, got {len(paths)}; see {output / "process/stdout.log"}')
        counts = validate_pilot(paths[0], args.backend, policy,
                                latency_policy=record['mock_latency_policy'] if args.backend == 'replay' else None)
        record['test_runtime_execution'] = test_execution_summary(paths[0], args.backend)
        if paper_e2b:
            record['paper_contract_validation'] = validate_paper_e2b(
                paths[0], trace, args.instance, paper_inputs, record['e2b_environment'], output)
        else:
            validate_trace_events(paths[0], args.backend, trace, args.limit, args.schedule, policy)
        if args.backend != 'cube':
            audit_paths = sorted((base / 'results').rglob('*mock_audit*.json'))
            expected_reports = 2 if paper_e2b else 1
            if args.backend == 'replay':
                with (paths[0].parent / 'restores.csv').open() as stream:
                    expected_reports = sum(int(row['target_expansions']) > 0 for row in csv.DictReader(stream))
            if len(audit_paths) != expected_reports:
                raise ValueError(f'Expected {expected_reports} post-measurement replay audit exports, got {len(audit_paths)}')
            audit_reports = [json.loads(p.read_text()) for p in audit_paths]
            validate_mock_latency(audit_reports, record['mock_latency_policy'])
            record['replay_audit'] = summarize(audit_reports, policy)
            audit = record['replay_audit']
            # The driver has exited: formatting and console I/O cannot enter
            # any checkpoint/restore/replay timer.
            print(f"[replay audit] completed; policy={policy}; message differences={audit['n_mismatch']}; "
                  f"dropped={audit['audit_records_dropped']}; omitted={audit['audit_payloads_omitted']}; "
                  f"evidence: {base / 'results'}", flush=True)
        extra_artifacts = []
        if paper_e2b:
            # Preserve and bind the actual requests/responses and transport
            # evidence separately from the (large) reconstructable payload tar.
            extra_artifacts += [p for p in (base / 'work').rglob('*')
                                if p.is_file() and p.suffix in ('.json', '.jsonl', '.log', '.txt')]
        if args.backend in ('replay', 'fc-diff', 'criu'):
            extra_artifacts += [output / 'history-serialization.json',
                                payload / 'moatless-det-src/moatless/_replay_history_order.json']
        if config.get('recorded_search_order', False):
            extra_artifacts += [output / 'search-file-priority.json',
                               payload / 'moatless-det-src/moatless/_replay_search_order.json']
        if phase_start:
            from cube_phases import collect
            phase_log = output / 'cubelet-phases.log'
            phases = collect(phase_start, paths[0], phase_log, strict=cube_disk_profile)
            if cube_disk_profile and (len(phases['events']) != sum(counts.values()) or
                    phases['phase_record_count'] != counts['checkpoints'] * 3 + counts['restores'] * 10):
                raise ValueError('Cube phase count does not match complete measured event counts')
            write_json(output / 'cube_phases.json', phases)
            record['phase_evidence'] = {'strict': cube_disk_profile,
                'events': len(phases['events']), 'raw_phase_records': phases['phase_record_count'],
                'captured_log': file_record(phase_log),
                'phase_json': file_record(output / 'cube_phases.json')}
            extra_artifacts += [phase_log, output / 'cube_phases.json']
        if cube_disk_profile:
            extra_artifacts += [output / 'cube_disk_before.json', output / 'cube_disk_after.json']
        if args.backend == 'cube' and config.get('baseline_storage') == 'tmpfs':
            write_json(output / 'cube_memory_after.json', verify_cube_memory(config))
            extra_artifacts += [output / 'cube_memory_before.json', output / 'cube_memory_after.json']
        record.update(status='ok', counts=counts, result=dict(file_record(paths[0]),path=str(paths[0].relative_to(output))),
            artifacts=artifact_records(output, extra_artifacts + [p for p in (base / 'results').rglob('*') if p.suffix in ('.json', '.jsonl', '.csv')]))
        if args.backend == 'cube':
            expected = sum(bool(line.strip()) for line in args.schedule.read_text().splitlines())
            expected = min(expected, args.limit) if args.limit else expected
            if sum(counts.values()) != expected:
                raise ValueError('Cube event count does not match selected schedule')
    except BaseException as error:
        record.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        write_json(manifest, record)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=DRIVERS, required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--repository-commit', help='Frozen base commit for portable traces without a commit field')
    parser.add_argument('--schedule', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--timeout', type=int, default=14400)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--collect-phases', action='store_true', help='Require fresh instrumented Cubelet log evidence')
    parser.add_argument('--experiment-id', help='Explicit baseline identity; must match the backend')
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be positive')
    return run(args)

if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
