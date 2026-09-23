#!/usr/bin/env python3
"""Read-only collector, run on spr4numa with stdout redirected to a local tar.gz.

Select trace inputs and narrowly scoped provenance/results. Never modify the
measurement host. Missing tracked sources are read from Git HEAD into the stream.
"""
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time

BASE = Path('/mnt/disk2/dyp')
R = BASE / 'd-overlayfs'
F = BASE / 'finalbench'
selected = set()
missing = []
git_objects = {}


def add(path):
    path = Path(path)
    if path.is_file() and not path.is_symlink():
        selected.add(path)
    else:
        missing.append(str(path))


def tree(path, suffixes):
    path = Path(path)
    if not path.exists():
        missing.append(str(path))
        return
    for p in path.rglob('*'):
        if any(part in {'.git', '__pycache__', '.cache'} for part in p.parts):
            continue
        if p.is_file() and not p.is_symlink() and p.suffix in suffixes:
            selected.add(p)


def git_read(rel):
    out = subprocess.run(['git', '-C', str(F), 'show', 'HEAD:' + rel],
                         capture_output=True)
    if out.returncode:
        missing.append('git:finalbench:HEAD:' + rel)
        return None
    git_objects[rel] = out.stdout
    return out.stdout


def git_prefix(prefix, suffixes, exclude=()):
    out = subprocess.run(['git', '-C', str(F), 'ls-tree', '-r', '--name-only',
                          'HEAD', prefix], capture_output=True, text=True, check=True)
    for rel in out.stdout.splitlines():
        if Path(rel).suffix in suffixes and not any(x in rel for x in exclude):
            if (F / rel).is_file():
                add(F / rel)
            else:
                git_read(rel)


def add_manifest_dir(name):
    p = F / name
    for pattern in ['manifest*.tsv', 'manifest*.json', 'PEAGLE12_SELECTION.md',
                    'batch_summary.json', 'run_config.json']:
        for f in p.glob(pattern):
            add(f)
    for child in ['schedules', 'llm_rtt', 'summaries']:
        tree(p / child, {'.json', '.jsonl', '.csv', '.tsv'})


def main():
    # Raw/portable pools absent from the MCTS-only archive, plus current P-EAGLE.
    for rel in ['traces/swe-search/qwen3-coder-30b-ms',
                'traces/swe-search/qwen3-coder-30b-p-eagle-ms/mcts-iter30',
                'traces/swe-search/claude/linear', 'traces/swe-search/mimo/linear']:
        tree(R / rel, {'.json', '.jsonl', '.diff'})
    tree(BASE / 'spr_payload/det_traces/ms', {'.json', '.jsonl', '.diff'})
    tree(R / 'benchmarks/replay/swesearch_actions', {'.json', '.jsonl'})
    add(R / 'traces/SUMMARY.md')
    add(R / 'traces/swe-search/qwen3-coder-30b-ms/README.md')
    # Paper-specific input manifests and raw measurements; no VM/workdir images.
    for rel in ['benchresults/2026-06-10_rl_fanout_qwen_peagle_mcts30_table3',
                'benchresults/2026-06-10_rl_fanout_qwen_peagle_mcts30_vm_overlay',
                'benchresults/2026-05-11_table3_swesearch/sys',
                'experiments/table2_deltabox_canonical_alignment_20260610',
                'experiments/cubesandbox_table2_deltabox_canonical_serial_officialish_20260610']:
        tree(R / rel, {'.json', '.jsonl', '.csv', '.tsv', '.md', '.txt'})
    war = R / 'benchresults/2026-05-11_swesearch_war'
    for p in war.glob('*.jsonl'):
        add(p)
    for name in ['README.md', 'aggregate.json']:
        add(war / name)
    for rel in ['finalcode/finaltest/motiv_totals.json',
                'finalcode/finaltest/measure_motiv_totals.py',
                'experiments/end2end_real_replay/end2end_2sys.json',
                'benchmarks/bench_fanout_n_real.py',
                'benchmarks/replay/swesearch_to_actions.py',
                'benchmarks/replay/run_swesearch_all.sh']:
        add(R / rel)
    mem = R / 'experiments/memcurve_forkonly_write_sympy22840_20260611'
    for p in mem.glob('*'):
        if p.is_file() and p.suffix in {'.json', '.csv', '.md', '.sh'}:
            add(p)
        elif p.is_dir() and p.name.startswith('results_'):
            tree(p, {'.json', '.jsonl'})
    # Fig.2 data beside the archived trajectories, with no copied testbeds.
    prof = R / 'traces/swe-search/qwen3-coder-30b-vllm18086/profile5-tree-rss-20260609_052851'
    add(prof / 'tree_rss_summary.json')
    for p in prof.glob('*/per_step_memory_dirty.json'):
        add(p)
    for name in ['deltabox_peagle_mcts30_2x_numa12_realrtt',
                 'deltabox_peagle_mcts30_1x_numa2_realrtt_rs_slow_lazy',
                 'deltabox_peagle_mcts30_2x_numa12_realrtt_rs_slow_lazy',
                 'cube_cow_peagle_mcts30_2x_numa12_realrtt']:
        add_manifest_dir(name)
    tree(F / 'deltabox_peagle_mcts30_2x_numa12_realrtt/results/deltabox-no-adapt', {'.jsonl'})
    tree(F / 'deltabox_peagle_mcts30_1x_numa2_realrtt_rs_slow_lazy/results/deltabox-no-adapt', {'.jsonl'})
    for rel in ['fc_diff_dm/traces_244.txt', 'criu_copytree/traces_244.txt',
                'replay_copytree/traces_244.txt',
                'fc_diff_dm/results/controller_raw_aggregate_table2.json',
                'fc_diff_dm/results/controller_raw_per_trace_summary.csv',
                'criu_copytree/results/_aggregate_full3_20260529_104122/aggregate.json',
                'criu_copytree/results/_aggregate_full3_20260529_104122/per_trace_summary.csv',
                'replay_copytree/results/real_run_all_aggregate.json',
                'replay_copytree/results/real_run_all_summary.json',
                'replay_copytree/results/table2_family_aggregate_zero_llm.json',
                '_paper_data_sources/PAPER_DATA_PROVENANCE.md']:
        add(F / rel)
    # Only paper-relevant, explicitly selected Git data, excluding credential files.
    git_prefix('e2b_table2_final', {'.json', '.md'})
    rel = 'e2b_table2_final/e2b_sample8_table2_aggregate.json'
    data = (F / rel).read_bytes() if (F / rel).exists() else git_objects.get(rel)
    if data:
        for sample in json.loads(data)['samples']:
            p = Path(sample['result_path'])
            if p.is_file():
                add(p)
            else:
                git_read(p.relative_to(F).as_posix())
    git_prefix('e2b_fig6_peagle_mcts30_full12_server_012406',
               {'.json', '.tsv', '.csv', '.md'})
    git_prefix('official_sandbox_fork', {'.json', '.jsonl', '.md', '.py'})
    git_prefix('e2b_peagle_mcts30_2x_numa12_lw_realrtt',
               {'.tsv', '.json', '.jsonl'},
               exclude=('/payload/', '/results/', '/work/', '/logs/'))
    # Keep original bytes. The entire downloaded supplement is gitignored.
    index = []
    revision = subprocess.check_output(['git', '-C', str(F), 'rev-parse', 'HEAD'], text=True).strip()
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as out:
        for p in sorted(selected):
            relative = p.relative_to(BASE).as_posix()
            digest = hashlib.sha256()
            with p.open('rb') as src:
                for block in iter(lambda: src.read(1 << 20), b''):
                    digest.update(block)
            out.add(p, arcname=relative, recursive=False)
            index.append({'path': relative, 'source': str(p), 'source_type': 'working_tree',
                          'bytes': p.stat().st_size, 'sha256': digest.hexdigest()})
        for rel, data in sorted(git_objects.items()):
            relative = 'git-head/finalbench/' + rel
            member = tarfile.TarInfo(relative)
            member.size = len(data)
            member.mode = 0o644
            out.addfile(member, io.BytesIO(data))
            index.append({'path': relative, 'source': 'git:finalbench:' + revision + ':' + rel,
                          'source_type': 'git_object', 'git_revision': revision,
                          'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        for name, obj in [('SOURCE_FILES.json', index), ('MISSING_SOURCES.json', sorted(set(missing)))]:
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            out.addfile(member, io.BytesIO(data))
    print(json.dumps({'files': len(index), 'bytes': sum(x['bytes'] for x in index),
                      'missing': len(set(missing))}), file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
