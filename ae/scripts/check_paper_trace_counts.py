#!/usr/bin/env python3
"""Check the saved inputs against the manually identified paper count claims.

This checks input availability and iteration configuration, not whether every
historical replay succeeded or whether all published timing values reproduce.
"""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROV = ROOT / 'provenance/79-20260920'
RL_RESULTS = ROOT / 'traces/supplement/79-20260920/d-overlayfs/benchresults/2026-06-10_rl_fanout_qwen_peagle_mcts30_vm_overlay'


def cohort(name):
    with (PROV / 'cohorts' / (name + '.csv')).open() as f:
        return list(csv.DictReader(f))


def inspect(row):
    path = ROOT / row['local']
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == row['sha256'], row['instance']
    data = json.loads(raw)
    stack, nodes = [data['root']], 0
    while stack:
        node = stack.pop()
        nodes += 1
        stack.extend(node.get('children', []))
    return dict(instance=row['instance'], local=row['local'], sha256=digest,
                max_iterations=data.get('max_iterations'), tree_nodes_including_root=nodes)


def main():
    detail = {}
    for name in ['table2-deltabox-12', 'table2-e2b-original-8', 'rl-fanout-9',
                 'fig2-filesystem-30', 'fig2-memory-5']:
        detail[name] = [inspect(row) for row in cohort(name)]
    rl_records = []
    for row in cohort('rl-fanout-9'):
        path = RL_RESULTS / ('fanout_modeA_' + row['instance'] + '.json')
        data = json.loads(path.read_text())
        assert data['trajectory_path'] == row['source']
        assert data['fs_mode'] == 'overlay'
        assert set(data['per_n']) == {'1', '4', '16', '64'}
        rl_records.append(dict(instance=row['instance'], result=path.relative_to(ROOT).as_posix(),
                               trajectory_path_matches_manifest=True,
                               n_values=sorted(int(n) for n in data['per_n']),
                               reps=data['reps'], warmup=data['warmup'],
                               raw_repetitions_per_n={n: len(v) for n, v in data['raw_per_rep'].items()}))
    group_counts = json.loads((PROV / 'workload-group-counts.json').read_text())['rows']
    summary = dict(
        paper='atc26-paper158.pdf',
        scope='Explicit trajectory sample count, iteration configuration, and workload coverage; not full result validation',
        explicit_rl_trajectory_count=dict(required=9, available=len(detail['rl-fanout-9']),
                                          separate_result_files=len(rl_records)),
        current_input_iteration_configs={name: dict(Counter(str(row['max_iterations']) for row in rows))
                                        for name, rows in detail.items()},
        four_workload_groups_present={row['label']: len(row['counts']) == 4 and all(row['counts'].values())
                                      for row in group_counts},
        input_detail=detail, rl_result_detail=rl_records,
        limits=['30 iterations is not 30 independent trajectories',
                'Node count includes the root and is not by itself a count of executed checkpoint events',
                'E2B ms inputs match the recorded pool and instance; historical per-run input hashes remain unavailable',
                '53 correctness checks and zero-mismatch assertions require separate run-log evidence'])
    (PROV / 'paper-trace-count-check.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k not in ['input_detail', 'rl_result_detail']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
