#!/usr/bin/env python3
"""Select saved paper inputs/results and materialize per-table/figure folders.

Raw objects are copied once by SHA-256, with relative links in each data folder.
Only manifests, counts and documentation belong in Git. No experiments are run.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
PROV = ROOT / 'provenance/79-20260920'
H = ROOT / 'traces/raw/history-20260625'
S = ROOT / 'traces/supplement/79-20260920'
L = ROOT / 'traces/supplement/79-lw-20260920'
X = ROOT / 'traces/supplement/79-figures-20260920'
OBJECTS = ROOT / 'traces/objects'
PAPER = ROOT / 'paper'
R, F, G = S / 'd-overlayfs', S / 'finalbench', S / 'git-head/finalbench'
SUFFIXES = {'.json', '.jsonl', '.csv', '.tsv', '.md', '.txt', '.diff'}
COMPANIONS = ['trajectory.json', 'ms_trace.jsonl', 'eval_result.json', 'summary.json',
              'patch.diff', 'per_step_memory_dirty.json', 'step_metrics.jsonl']

SPECS = {
    'table-01': ('Mechanism comparison', 'Literature comparison; locally measured DeltaBox/Cube entries refer to Table 2/3. No independent trace cohort.'),
    'table-02': ('Checkpoint/restore comparison', 'Preserves each backend cohort separately. DeltaBox/Cube use 12 inputs; original E2B uses a different 8. FC/CRIU/Replay use a planned 244. Historical input versions and some paper timing values remain unresolved.'),
    'table-03': ('Component latency and fast/slow restore', 'Fast and slow paths come from separate runs with different NUMA/concurrency settings. Candidate raw results are preserved; the published numerical mapping is not fully resolved.'),
    'figure-01': ('Baseline phase costs', 'Phase data and the Table 2 cohorts are provided. Historical plotting used adjustments/fallback values that still require reconciliation.'),
    'figure-02': ('Per-step state changes', 'Filesystem profiling uses 30 instances; memory profiling uses 5 separately recorded inputs. Counts are distinct from the 30-iteration budget.'),
    'figure-06': ('Memory policies and lightweight skip', 'Memory curves: one SymPy input under four fork-only policies (CRIU disabled). Adaptive data: 12 Claude/MiMo inputs; saved run directory also includes an Astropy probe.'),
    'figure-07': ('MCTS end-to-end comparison', 'DeltaBox 12 and original E2B 8 inputs remain separate. Saved E2B values model a warm-worker path from measured components; reading the aggregate is not a new end-to-end execution.'),
    'figure-08': ('Sandbox fan-out and GPU timing', 'Separates the nine-input fork primitive, synthetic 64-MiB sandbox fan-out, and GPU timing inputs. E2B N=64 paper plotting used an estimate from N=16 measurements; failed native-N=64 evidence is retained.'),
    'figure-09': ('Write amplification', '185 pool+instance inputs represent 136 distinct instances. Each filesystem has 646 edit records, 603 applied_ok. Plot filtering remains part of the reproduction workflow.'),
}


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def main():
    OBJECTS.mkdir(parents=True, exist_ok=True)
    records = {name: {} for name in SPECS}
    cohorts = {name: [] for name in SPECS}
    metadata = {}

    def publish_bytes(source, raw):
        # Saved model configuration credentials are unnecessary for offline replay.
        # Change only these JSON field values; preserve prompts/actions and other bytes.
        if source.suffix not in {'.json', '.jsonl'}:
            return raw, 0
        pattern = re.compile(rb'("(?:model_api_key|api_key|openai_api_key|anthropic_api_key)"\s*:\s*)("(?:[^"\\]|\\.)*")')
        removed = 0

        def replace(match):
            nonlocal removed
            value = json.loads(match.group(2))
            if value:
                removed += 1
                return match.group(1) + b'""'
            return match.group(0)

        published = pattern.sub(replace, raw)
        return published, removed

    def add(part, source, destination, role):
        source = Path(source)
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(source)
        key = source.relative_to(ROOT).as_posix()
        if key not in metadata:
            raw = source.read_bytes()
            source_sha256 = hashlib.sha256(raw).hexdigest()
            source_size = len(raw)
            raw, removed = publish_bytes(source, raw)
            digest = hashlib.sha256(raw).hexdigest()
            obj = OBJECTS / digest
            if not obj.exists():
                obj.write_bytes(raw)
                obj.chmod(0o444)
            metadata[key] = (digest, len(raw), source_sha256, source_size, removed)
        digest, size, source_sha256, source_size, removed = metadata[key]
        target = PAPER / part / 'data' / destination
        record = dict(target=target.relative_to(ROOT).as_posix(), source=key,
                      sha256=digest, bytes=size, role=role,
                      source_sha256=source_sha256, source_bytes=source_size,
                      cleared_credential_fields=removed)
        if record['target'] in records[part]:
            assert records[part][record['target']]['sha256'] == digest
        records[part][record['target']] = record
        target.parent.mkdir(parents=True, exist_ok=True)
        link = os.path.relpath(OBJECTS / digest, target.parent)
        if target.is_symlink():
            if os.readlink(target) != link:
                # Upgrade only our previous unsanitized link; never overwrite user data.
                previous = os.path.relpath(OBJECTS / source_sha256, target.parent)
                if removed and os.readlink(target) == previous:
                    target.unlink()
                    target.symlink_to(link)
                else:
                    raise RuntimeError('Refusing to replace different link: ' + str(target))
        elif target.exists():
            raise RuntimeError('Refusing to replace existing file: ' + str(target))
        else:
            target.symlink_to(link)
        return record['target']

    def tree(part, source, destination, role):
        source = Path(source)
        if not source.is_dir():
            raise FileNotFoundError(source)
        for p in sorted(source.rglob('*')):
            if p.is_file() and not p.is_symlink() and p.suffix in SUFFIXES:
                add(part, p, str(Path(destination) / p.relative_to(source)), role)

    def attach(part, label, name):
        with (PROV / 'cohorts' / (name + '.csv')).open() as f:
            rows = list(csv.DictReader(f))
        output = []
        for row in rows:
            source = ROOT / row['local']
            unique = row.get('pool', '').replace('/', '_')
            instance = (unique + '__' if unique else '') + row['instance']
            prefix = 'inputs/' + label + '/' + instance
            new = dict(row)
            new['original_local'] = row['local']
            for filename in COMPANIONS:
                p = source.with_name(filename)
                if p.is_file():
                    target = add(part, p, prefix + '/' + filename, 'input')
                    if filename == 'trajectory.json':
                        assert records[part][target]['source_sha256'] == row['sha256']
                        new['local'] = target
                        new['original_sha256'] = row['sha256']
                        new['sha256'] = records[part][target]['sha256']
                        new['cleared_credential_fields'] = records[part][target]['cleared_credential_fields']
            for field, role in [('schedule_local', 'schedule'), ('action_local', 'edit-input'), ('result_local', 'result')]:
                if row.get(field):
                    p = ROOT / row[field]
                    new[field] = add(part, p, prefix + '/' + p.name, role)
            # Alternate historical snapshots remain in the local audit archive.
            # They are not silently selected as the paper's replay input.
            output.append(new)
        p = PAPER / part / ('cohort-' + label + '.csv')
        p.parent.mkdir(parents=True, exist_ok=True)
        fields = list(dict.fromkeys(k for row in output for k in row))
        with p.open('w') as f:
            writer = csv.DictWriter(f, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(output)
        cohorts[part].append(dict(label=label, source_cohort=name, rows=len(rows),
                                  distinct_instances=len({r['instance'] for r in rows}),
                                  manifest=p.name))

    for label, name in [('deltabox', 'table2-deltabox-12'), ('cube', 'table2-cube-canonical-12'),
                        ('e2b', 'table2-e2b-original-8'), ('fc-diff', 'table2-fc-actual'),
                        ('criu-attempts', 'table2-criu-all-attempts'), ('replay', 'table2-replay-actual')]:
        attach('table-02', label, name)
    fast = F / 'deltabox_peagle_mcts30_2x_numa12_realrtt'
    slow = F / 'deltabox_peagle_mcts30_1x_numa2_realrtt_rs_slow_lazy'
    cube = R / 'experiments/cubesandbox_table2_deltabox_canonical_serial_officialish_20260610'
    for part in ['table-02', 'table-03', 'figure-07']:
        tree(part, fast, 'records/deltabox-fast', 'run-record')
    tree('table-02', cube, 'records/cube-canonical', 'run-record')
    tree('table-02', G / 'e2b_table2_final', 'records/e2b-original', 'aggregate')
    for backend in ['fc_diff_dm', 'criu_copytree', 'replay_copytree']:
        tree('table-02', F / backend, 'records/' + backend, 'aggregate-and-cohort')
    attach('table-03', 'deltabox', 'table2-deltabox-12')
    tree('table-03', slow, 'records/deltabox-slow', 'run-record')

    tree('figure-01', X / 'd-overlayfs/experiments/table2_three_methods_breakdown_20260609', 'records/phases', 'phase-measurement')
    add('figure-01', cube / 'cube_table2_deltabox_canonical_phase_breakdown.json', 'records/cube-phases.json', 'phase-measurement')
    attach('figure-02', 'filesystem', 'fig2-filesystem-30')
    attach('figure-02', 'memory', 'fig2-memory-5')
    add('figure-02', R / 'finalcode/finaltest/motiv_totals.json', 'records/motiv_totals.json', 'aggregate')
    profile = R / 'traces/swe-search/qwen3-coder-30b-vllm18086/profile5-tree-rss-20260609_052851'
    tree('figure-02', profile, 'records/memory-profile', 'profile-record')

    attach('figure-06', 'memory', 'fig6-memory-1')
    attach('figure-06', 'adaptive', 'fig6-adaptive-source-12')
    tree('figure-06', R / 'experiments/memcurve_forkonly_write_sympy22840_20260611', 'records/memory-policies', 'run-record')
    tree('figure-06', R / 'benchresults/2026-05-11_table3_swesearch/sys', 'records/adaptive', 'run-record')
    tree('figure-06', L / 'd-overlayfs/benchresults/2026-05-11_table3_swesearch', 'records/adaptive-schedules', 'schedule-and-provenance')
    attach('figure-07', 'deltabox', 'table2-deltabox-12')
    attach('figure-07', 'e2b', 'table2-e2b-original-8')
    add('figure-07', R / 'experiments/end2end_real_replay/end2end_2sys.json', 'records/end2end_2sys.json', 'modeled-and-measured-aggregate')

    attach('figure-08', 'fork-primitive', 'rl-fanout-9')
    tree('figure-08', R / 'benchresults/2026-06-10_rl_fanout_qwen_peagle_mcts30_vm_overlay', 'records/fork-primitive', 'run-record')
    substrate = G / 'official_sandbox_fork'
    for name in ['deltabox_official_fork_readtouch_20260605_223523.json',
                 'deltabox_official_fork_readtouch_20260605_223523.meta.json',
                 'cube_official_fork_20260605_210626.json',
                 'e2b_official_fork_20260605_211829.json',
                 'e2b_official_fork16_c16_x4_20260605_215731.json',
                 'e2b_official_fork64_20260605_212120.json']:
        add('figure-08', substrate / name, 'records/substrate/' + name, 'synthetic-fanout-record')
    tree('figure-08', X / 'd-overlayfs/benchresults/2026-05-18_b1_tgpu', 'records/gpu', 'gpu-measurement')
    attach('figure-09', 'war', 'fig9-war-inputs')
    war = R / 'benchresults/2026-05-11_swesearch_war'
    for p in sorted(war.glob('*.jsonl')):
        add('figure-09', p, 'records/' + p.name, 'edit-measurement')
    add('figure-09', war / 'README.md', 'records/source-notes.md', 'provenance')

    catalog = []
    for part, (title, note) in SPECS.items():
        folder = PAPER / part
        folder.mkdir(parents=True, exist_ok=True)
        rows = sorted(records[part].values(), key=lambda r: r['target'])
        (folder / 'files.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
        entry = dict(id=part, title=title, note=note, cohorts=cohorts[part],
                     file_references=len(rows), logical_bytes=sum(r['bytes'] for r in rows))
        catalog.append(entry)
        lines = ['# ' + part + ': ' + title, '', note, '',
                 '| Cohort | Input rows | Distinct instances |', '|---|---:|---:|']
        for c in cohorts[part]:
            lines.append('| [{}]({}) | {} | {} |'.format(c['label'], c['manifest'], c['rows'], c['distinct_instances']))
        if not cohorts[part]:
            lines.append('| No independent trajectory cohort | — | — |')
        lines += ['', '`data/` contains local materialized inputs, schedules and saved measurements. It is excluded from Git. Every file is listed in `files.jsonl` with its source and SHA-256.', '',
                  'After obtaining the separate data bundle, run `python3 scripts/paper_data.py import PATH_TO_BUNDLE` at the repository root. This reconstructs these paths. To verify: `python3 scripts/paper_data.py verify`.', '',
                  'Saved JSON API-key fields are cleared in the publication copy. `source_sha256` identifies the unchanged local original; `sha256` identifies the published copy. Prompts, actions and measurements are unchanged.', '',
                  'This directory organizes existing records. It does not claim a new system execution or completed independent reproduction. Runtime/benchmark drivers are not included in the data bundle.', '']
        (folder / 'README.md').write_text('\n'.join(lines))
    dump(PAPER / 'catalog.json', catalog)
    changed = [dict(source=k, source_sha256=v[2], published_sha256=v[0], cleared_fields=v[4])
               for k, v in sorted(metadata.items()) if v[4]]
    dump(PAPER / 'data-transformations.json', dict(
        operation='Replace nonempty model_api_key/api_key/openai_api_key/anthropic_api_key JSON string values with empty strings',
        unchanged='All bytes outside these field values, including prompts, observations, actions, measurements and RTTs',
        original_storage='Unmodified originals remain in local traces/raw and traces/supplement; excluded from Git',
        changed_files=changed))
    print(json.dumps(dict(partitions=len(catalog), file_references=sum(x['file_references'] for x in catalog),
                          unique_source_paths=len(metadata), unique_objects=len({v[0] for v in metadata.values()}),
                          unique_bytes=sum({v[0]: v[1] for v in metadata.values()}.values())), indent=2))


if __name__ == '__main__':
    main()
