#!/usr/bin/env python3
"""Verify original Table 2 batch identity against the repository's fixed source lock.

Only the standard library is required. Input-directory manifests are never trusted
for source identity. This verifies historical data, not a new system execution.
"""
import argparse
import collections
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

REPOSITORY = Path(__file__).resolve().parents[2]
LOCK_PATH = REPOSITORY / 'ae/paper/table-02/paper-source-lock.json'
DEFAULT_INPUT = REPOSITORY / 'ae/paper/table-02/data/records/deltabox-paper/results'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def event_number(row, field):
    value = row[field]
    require(isinstance(value, (int, float)) and not isinstance(value, bool),
            'Invalid numeric timing field: ' + field)
    require(math.isfinite(value) and value >= 0, 'Invalid timing value: ' + field)
    return value


def verify(input_directory):
    lock_bytes = LOCK_PATH.read_bytes()
    lock = json.loads(lock_bytes)
    runs = lock['runs']
    require(lock['schema_version'] == 1, 'Unsupported source lock schema')
    require(len(runs) == lock['expected_counts']['runs'] == 12, 'Invalid source lock run count')
    require(len({r['instance'] for r in runs}) == 12, 'Duplicate locked instances')
    require(len({r['run_id'] for r in runs}) == 12, 'Duplicate locked run IDs')
    input_directory = Path(input_directory)
    require(input_directory.is_dir(), 'Input directory does not exist: ' + str(input_directory))
    record_directory = input_directory / 'records'
    if not record_directory.is_dir():
        record_directory = input_directory
    files = sorted(p for p in record_directory.rglob('*') if p.is_file()
                   and (p.name.endswith('.results.jsonl') or p.name.endswith('.results.jsonl.gz')))
    require(len(files) == len(runs),
            f'BATCH MISMATCH: expected exactly {len(runs)} historical result files; found {len(files)}')
    by_instance = collections.defaultdict(list)
    for path in files:
        by_instance[path.name.split('.replay', 1)[0]].append(path)
    require(set(by_instance) == {r['instance'] for r in runs}, 'BATCH MISMATCH: instance set differs')
    groups = collections.defaultdict(lambda: {'checkpoint': [], 'restore': []})
    verified_runs = []
    for run in runs:
        instance = run['instance']
        require(len(by_instance[instance]) == 1, 'BATCH MISMATCH: duplicate result for ' + instance)
        path = by_instance[instance][0]
        expected_name = instance + '.' + run['run_id'] + '.results.jsonl'
        require(path.name in (expected_name, expected_name + '.gz'),
                'BATCH MISMATCH: run ID differs for ' + instance + '; expected ' + run['run_id'])
        data = path.read_bytes()
        raw = gzip.decompress(data) if path.name.endswith('.gz') else data
        actual_sha = hashlib.sha256(raw).hexdigest()
        require(actual_sha == run['raw_sha256'] and len(raw) == run['raw_bytes'],
                'BATCH MISMATCH: raw-content SHA-256/size differs for ' + instance
                + '; expected ' + run['raw_sha256'] + ', got ' + actual_sha)
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line]
        checkpoints = [row for row in rows if row.get('kind') == 'ckpt']
        restores = [row for row in rows if row.get('kind') == 'restore']
        summaries = [row for row in rows if row.get('kind') == 'run_summary']
        require(len(summaries) == 1 and rows[-1].get('kind') == 'run_summary',
                'Missing historical final summary for ' + instance)
        require(summaries[0].get('error_n') == 0 and summaries[0].get('worker_exec_bad_n') == 0,
                'Historical summary reports errors for ' + instance)
        require(len(checkpoints) == run['checkpoint_events'] and len(restores) == run['restore_events'],
                'Historical event count mismatch for ' + instance)
        ck = [event_number(row, 'ckpt_wall_ms') for row in checkpoints]
        rs = [event_number(row, 'restore_critical_ms') for row in restores]
        require(all(event_number(row, 'restore_wall_ms') == value for row, value in zip(restores, rs)),
                'Historical restore_wall_ms/restore_critical_ms mismatch for ' + instance)
        for family in (run['family'], 'TOTAL'):
            groups[family]['checkpoint'].extend(ck)
            groups[family]['restore'].extend(rs)
        verified_runs.append({'instance': instance, 'run_id': run['run_id'], 'raw_sha256': actual_sha})
    require(len(groups['TOTAL']['checkpoint']) == lock['expected_counts']['checkpoint_events'] == 317,
            'Expected 317 checkpoint events')
    require(len(groups['TOTAL']['restore']) == lock['expected_counts']['restore_events'] == 334,
            'Expected 334 restore events')
    metrics = []
    for family in ('Django', 'SymPy', 'Scientific', 'Tools/Small', 'TOTAL'):
        metrics.append({'family': family, **{operation: {'events': len(groups[family][operation]),
                        'mean_ms': statistics.mean(groups[family][operation])}
                        for operation in ('checkpoint', 'restore')}})
    return {'status': 'verified_historical_source', 'dataset_id': lock['dataset_id'],
            'source_lock_sha256': hashlib.sha256(lock_bytes).hexdigest(),
            'aggregation': 'Event-weighted arithmetic means; no target latency fitting.',
            'metrics': metrics, 'verified_runs': verified_runs, 'limitations': lock['limitations']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=DEFAULT_INPUT,
                        help='Directory containing the original historical records; defaults to the imported original Table 2 paper data')
    parser.add_argument('--json', action='store_true', help='Print verification and computed metrics as JSON')
    args = parser.parse_args(argv)
    try:
        result = verify(args.input)
    except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        print('Table 2 source verification failed: ' + str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print('Verified original historical Table 2 source: 12 runs, 317 checkpoints, 334 restores.')
        print('Restore is the historical internal interval, not full external API latency.')
        for row in result['metrics']:
            print(f"{row['family']:<12} checkpoint {row['checkpoint']['mean_ms']:.6f} ms "
                  f"(n={row['checkpoint']['events']}); restore {row['restore']['mean_ms']:.6f} ms "
                  f"(n={row['restore']['events']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
