#!/usr/bin/env python3
"""Build local, hash-addressed input cohorts without modifying raw records.

Paths in generated CSVs are relative to the AE repository. Instance overlap is
reported separately from byte-identical trajectory overlap.
"""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
H = ROOT / 'traces/raw/history-20260625'
S = ROOT / 'traces/supplement/79-20260920'
L = ROOT / 'traces/supplement/79-lw-20260920'
OUT = ROOT / 'provenance/79-20260920'
REMOTE = '/mnt/disk2/dyp/'
R = REMOTE + 'd-overlayfs/'
F = REMOTE + 'finalbench/'


def read(path):
    return json.loads(path.read_text())


def tsv(path):
    with path.open() as f:
        return list(csv.DictReader(f, delimiter='\t'))


def csv_rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def save_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w') as f:
        writer = csv.DictWriter(f, fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def action_signature(path):
    """Tree topology and recorded actions; exclude LLM text and timing metadata."""
    data = read(path)
    nodes = []

    def visit(node, parent):
        node_id = node.get('node_id')
        steps = node.get('action_steps', [])
        actions = [step.get('action') for step in steps] if isinstance(steps, list) else steps
        nodes.append(dict(node_id=node_id, parent=parent, actions=actions))
        for child in node.get('children', []):
            visit(child, node_id)

    visit(data['root'], None)
    encoded = json.dumps(nodes, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest(), len(nodes)


def main():
    sources, inventory, histories = {}, [], {}
    for line in (OUT / 'history-files.jsonl').open():
        row = json.loads(line)
        histories[row['relative_path']] = row
    old = tsv(H / 'manifests/search_mcts_trajectory_json.tsv')
    for row in old:
        rel = row['archive_trajectory_json']
        meta = histories[rel]
        item = dict(source=row['source_trajectory_json'],
                    local=(H / rel).relative_to(ROOT).as_posix(),
                    sha256=meta['sha256'], bytes=meta['bytes'],
                    snapshot='history-20260625')
        inventory.append(item)
        sources[item['source']] = item
    source_changes = []
    for folder in [S, L]:
        index = read(folder / 'SOURCE_FILES.json')
        save_json(OUT / (folder.name + '-source-files.json'), index)
        for row in index:
            item = dict(source=row['source'],
                        local=(folder / row['path']).relative_to(ROOT).as_posix(),
                        sha256=row['sha256'], bytes=row['bytes'], snapshot=folder.name)
            key = row['source']
            if row['source_type'] == 'git_object':
                key = F + row['path'].split('git-head/finalbench/', 1)[1]
            if key in sources and sources[key]['sha256'] != item['sha256']:
                source_changes.append(dict(source=key, historical=sources[key], current=item))
            sources[key] = item
            if Path(row['path']).name == 'trajectory.json':
                inventory.append(item)

    def resolve(source):
        # Historical records retain a now-relocated finalbench/62 prefix.
        key = source.replace('/finalbench/62/', '/finalbench/')
        item = sources.get(source) or sources.get(key)
        if item:
            assert (ROOT / item['local']).is_file(), item['local']
        return item

    cohorts, unresolved = {}, []

    def entry(cohort, instance, source, **extra):
        item = resolve(source)
        row = dict(instance=instance, source=source,
                   local=item['local'] if item else '',
                   sha256=item['sha256'] if item else '',
                   present=bool(item), **extra)
        cohorts.setdefault(cohort, []).append(row)
        if not item:
            unresolved.append(dict(cohort=cohort, instance=instance, source=source))
        return row

    peagle = R + 'traces/swe-search/qwen3-coder-30b-p-eagle-ms/mcts-iter30/'
    ordinary = R + 'traces/swe-search/qwen3-coder-30b-ms/'
    portable = REMOTE + 'spr_payload/det_traces/ms/'
    main_dir = S / 'finalbench/deltabox_peagle_mcts30_2x_numa12_realrtt'
    main_rows = tsv(main_dir / 'manifest_peagle12.tsv')
    for row in main_rows:
        schedule = resolve(row['schedule'])
        entry('table2-deltabox-12', row['instance'], row['trace_dir'] + '/trajectory.json',
              n_ckpt=int(row['n_ckpt']), n_restore=int(row['n_restore']),
              schedule_local=schedule['local'] if schedule else '',
              schedule_sha256=schedule['sha256'] if schedule else '')
    selection = read(S / 'd-overlayfs/experiments/cubesandbox_table2_deltabox_canonical_serial_officialish_20260610/selection_deltabox_canonical.json')
    for row in selection['instances']:
        schedule = resolve(row['schedule'])
        assert schedule and schedule['sha256'] == row['schedule_sha256'], row['instance']
        entry('table2-cube-canonical-12', row['instance'], row['trace_dir'] + '/trajectory.json',
              schedule_local=schedule['local'], schedule_sha256=schedule['sha256'])
    e2b = read(S / 'git-head/finalbench/e2b_table2_final/e2b_sample8_table2_aggregate.json')
    for row in e2b['samples']:
        raw = resolve(row['result_path'])
        assert raw, row['result_path']
        entry('table2-e2b-original-8', row['instance'], ordinary + row['instance'] + '/trajectory.json',
              result_local=raw['local'], n_e2b_steps=row['n_e2b_steps'],
              input_binding='Current ms snapshot by recorded pool/instance; original run input hash not recorded')
    e2b_later = read(S / 'git-head/finalbench/e2b_fig6_peagle_mcts30_full12_server_012406/per_instance_fig6.json')
    for row in e2b_later:
        entry('e2b-separate-peagle-12', row['instance'], peagle + row['instance'] + '/trajectory.json',
              status=row['status'], note='Separate run; not the published Table 2 E2B row')

    lists = {}
    for name in ['fc_diff_dm', 'criu_copytree', 'replay_copytree']:
        lists[name] = (S / 'finalbench' / name / 'traces_244.txt').read_text().split()
    assert lists['fc_diff_dm'] == lists['criu_copytree'] == lists['replay_copytree']
    portable_differences, snapshot_comparisons = [], []
    def baseline_entry(name, instance, **extra):
        packaged = resolve(portable + instance + '/trajectory.json')
        return entry(name, instance, ordinary + instance + '/trajectory.json',
                     portable_candidate_local=packaged['local'] if packaged else '',
                     portable_candidate_sha256=packaged['sha256'] if packaged else '',
                     input_binding='Current ms snapshot matches real-run driver default path; historical per-run input hash unavailable; old portable snapshot also retained',
                     **extra)

    for instance in lists['replay_copytree']:
        row = baseline_entry('baseline-planned-244', instance)
        orig = resolve(ordinary + instance + '/trajectory.json')
        packaged = resolve(portable + instance + '/trajectory.json')
        if not orig or not packaged or packaged['sha256'] != orig['sha256']:
            portable_differences.append(instance)
        if orig and packaged:
            a, an = action_signature(ROOT / orig['local'])
            b, bn = action_signature(ROOT / packaged['local'])
            snapshot_comparisons.append(dict(instance=instance,
                ordinary_sha256=orig['sha256'], portable_sha256=packaged['sha256'],
                ordinary_action_sha256=a, portable_action_sha256=b,
                ordinary_nodes=an, portable_nodes=bn,
                ordinary_llm_calls=len((ROOT / orig['local']).with_name('ms_trace.jsonl').read_text().splitlines()),
                portable_llm_calls=len((ROOT / packaged['local']).with_name('ms_trace.jsonl').read_text().splitlines()),
                action_tree_equal=a == b))
    fc = csv_rows(S / 'finalbench/fc_diff_dm/results/controller_raw_per_trace_summary.csv')
    criu = csv_rows(S / 'finalbench/criu_copytree/results/_aggregate_full3_20260529_104122/per_trace_summary.csv')
    for name, rows in [('table2-fc-actual', fc), ('table2-criu-all-attempts', criu)]:
        for row in rows:
            baseline_entry(name, row['instance'],
                  ok=row['ok'], status=row['status'], n_restores=row['n_restores'])
    replay = read(S / 'finalbench/replay_copytree/results/real_run_all_summary.json')
    replay_rows = replay['summaries']
    if isinstance(replay_rows, dict):
        replay_rows = list(replay_rows.values())
    for row in replay_rows:
        instance = row.get('instance') or row.get('instance_id')
        baseline_entry('table2-replay-actual', instance,
              ok=row.get('ok', ''), status=row.get('status', ''))

    rl = tsv(S / 'd-overlayfs/benchresults/2026-06-10_rl_fanout_qwen_peagle_mcts30_table3/manifest_qwen_only_9.tsv')
    for row in rl:
        entry('rl-fanout-9', row['instance'], row['trajectory_path'],
              usage='trajectory loaded into donor heap; fork primitive, not full action replay')
    for name, rel in [('fig2-filesystem-30', 'finalcode/finaltest/motiv_totals.json'),
                      ('fig2-memory-5', 'traces/swe-search/qwen3-coder-30b-vllm18086/profile5-tree-rss-20260609_052851/tree_rss_summary.json')]:
        data = read(S / 'd-overlayfs' / rel)
        for instance in data['instances']:
            instance = instance if isinstance(instance, str) else instance['instance_id']
            possible = [x for x in sources if x.startswith(data['run_root'] + '/') and x.endswith('/' + instance + '/trajectory.json')]
            direct = data['run_root'] + '/' + instance + '/trajectory.json'
            entry(name, instance, direct if direct in sources else sorted(possible, key=len)[0])
    entry('fig6-memory-1', 'sympy__sympy-22840', peagle + 'sympy__sympy-22840/trajectory.json')

    war_actions = S / 'd-overlayfs/benchmarks/replay/swesearch_actions'
    war_results = S / 'd-overlayfs/benchresults/2026-05-11_swesearch_war'
    war_stats = Counter()
    for p in sorted(war_actions.glob('*.json')):
        data = read(p)
        results, successes = [], 0
        for arm in ['ext4', 'xfs', 'xfs_reflink']:
            output = war_results / (p.stem + '_' + arm + '.jsonl')
            if output.exists():
                results.append(output.relative_to(ROOT).as_posix())
                events = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
                ok_events = sum(bool(x.get('applied_ok')) for x in events)
                war_stats[arm + '_events'] += len(events)
                war_stats[arm + '_applied'] += ok_events
                successes += ok_events
        entry('fig9-war-inputs', data['instance_id'], R + 'traces/swe-search/' + data['pool'] + '/' + data['instance_id'] + '/trajectory.json',
              pool=data['pool'], base_commit=data['base_commit'], n_edits=data['n_edits'],
              action_local=p.relative_to(ROOT).as_posix(), result_files=len(results), applied_events=successes)
    # The older README explicitly maps these schedules to Claude/MiMo MCTS.
    lw_pools = {
        'django__django-12143': 'claude', 'django__django-12419': 'mimo',
        'django__django-13410': 'claude', 'django__django-12276': 'claude',
        'pydata__xarray-4075': 'mimo', 'pydata__xarray-4356': 'mimo',
        'pydata__xarray-4629': 'mimo', 'pydata__xarray-3677': 'mimo',
        'sympy__sympy-14711': 'claude', 'sympy__sympy-15809': 'mimo',
        'sympy__sympy-19637': 'mimo', 'sympy__sympy-12096': 'mimo'}
    for instance, pool in lw_pools.items():
        entry('fig6-adaptive-source-12', instance, R + 'traces/swe-search/' + pool + '/mcts/' + instance + '/trajectory.json', pool=pool + '/mcts')
    condition_checks = []
    for p in sorted((S / 'd-overlayfs/benchresults/2026-05-11_table3_swesearch/sys/deltabox-full').glob('*.conditions.json')):
        data = read(p)
        source = data.get('schedule_path', '').replace('/home/dong/d-overlayfs/', R)
        item = resolve(source)
        condition_checks.append(dict(condition=p.relative_to(ROOT).as_posix(),
                                     schedule=source, present=bool(item),
                                     hash_matches=bool(item and item['sha256'] == data.get('schedule_sha256'))))

    stats = {}
    for name, rows in cohorts.items():
        save_csv(OUT / 'cohorts' / (name + '.csv'), rows)
        stats[name] = dict(rows=len(rows), distinct_instances=len({x['instance'] for x in rows}),
                           distinct_sha256=len({x['sha256'] for x in rows if x['sha256']}),
                           missing=sum(not x['present'] for x in rows))
    overlap = []
    for a, b in [('table2-deltabox-12', 'table2-cube-canonical-12'),
                 ('table2-deltabox-12', 'table2-e2b-original-8'),
                 ('table2-deltabox-12', 'e2b-separate-peagle-12'),
                 ('table2-deltabox-12', 'rl-fanout-9'),
                 ('table2-deltabox-12', 'fig2-memory-5')]:
        arows, brows = cohorts[a], cohorts[b]
        overlap.append(dict(a=a, b=b,
            shared_instances=sorted({x['instance'] for x in arows} & {x['instance'] for x in brows}),
            shared_trajectory_hashes=len({x['sha256'] for x in arows} & {x['sha256'] for x in brows})))
    main_schedules = {x['instance']: x['schedule_sha256'] for x in cohorts['table2-deltabox-12']}
    cube_schedules = {x['instance']: x['schedule_sha256'] for x in cohorts['table2-cube-canonical-12']}
    summary = dict(history_trajectory_paths=len(old),
        history_distinct_trajectory_hashes=len({histories[x['archive_trajectory_json']]['sha256'] for x in old}),
        downloaded_trajectory_paths=len(inventory),
        downloaded_distinct_trajectory_hashes=len({x['sha256'] for x in inventory}),
        cohorts=stats, overlap=overlap,
        deltabox_cube_schedule_hashes_equal=main_schedules == cube_schedules,
        baseline_planned_lists_equal=True, portable_original_hash_differences=portable_differences,
        portable_ordinary_action_trees_equal=sum(x['action_tree_equal'] for x in snapshot_comparisons),
        fc_status_counts=dict(Counter(x['status'] for x in fc)),
        criu_status_counts=dict(Counter(x['status'] for x in criu)),
        criu_ok=sum(x['ok'].lower() == 'true' for x in criu),
        war_stats=dict(war_stats), source_changes=source_changes,
        lw_condition_schedule_checks=condition_checks, unresolved_inputs=unresolved)
    save_csv(OUT / 'trajectory-inventory.csv', inventory)
    save_csv(OUT / 'ordinary-vs-portable-244.csv', snapshot_comparisons)
    save_json(OUT / 'cohort-summary.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ['source_changes', 'lw_condition_schedule_checks', 'portable_original_hash_differences']}, ensure_ascii=False, indent=2))
    if unresolved:
        raise SystemExit('Unresolved cohort inputs: see cohort-summary.json')


if __name__ == '__main__':
    main()
