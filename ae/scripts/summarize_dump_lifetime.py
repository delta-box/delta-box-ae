#!/usr/bin/env python3
"""Archive and verify the dump-lifetime race proof and uninstrumented replays."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import statistics
import tarfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'ae/report/dump-lifetime-20260921'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def collect(source, output):
    lock = {'source_host': 'spr4numa', 'source_root': '/mnt/disk2/dyp/ae-dump-lifetime-20260921/ae/results', 'runs': [], 'files': {}}
    for directory in sorted(source.iterdir()):
        if not directory.is_dir() or directory.name.endswith('-env'):
            continue
        configs = list(directory.glob('**/run.json'))
        if len(configs) != 1:
            raise ValueError('expected one run config: ' + str(directory))
        run = configs[0].parent
        paths = [run / n for n in ('run.json', 'guest.log', 'django__django-14997.results.jsonl',
                 'schedule.jsonl', 'host_binding.json', 'numa_maps_at_exit.txt', 'diagnostics.tar.gz')]
        env = source / (directory.name + '-env')
        paths += [env / n for n in ('environment.json', 'turbostat.tsv', 'samples.jsonl')]
        for path in paths:
            raw = path.read_bytes()
            relative = 'raw/' + directory.name + '/' + path.name
            if path.suffix in ('.log', '.jsonl'):
                relative += '.gz'
                stored = gzip.compress(raw, mtime=0)
            else:
                stored = raw
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stored)
            lock['files'][relative] = {'sha256': digest(stored), 'original_sha256': digest(raw),
                                      'source': str(path.relative_to(source))}
        lock['runs'].append(directory.name)
    (output / 'inputs.lock.json').write_text(json.dumps(lock, indent=2) + '\n')


def summarize(output):
    lock = json.loads((output / 'inputs.lock.json').read_text())
    for relative, record in lock['files'].items():
        assert digest((output / relative).read_bytes()) == record['sha256'], relative
    result = {'runs': {}}
    reference = json.loads((ROOT / 'ae/report/numa2-max-20260921/summary.json').read_text())
    pooled = {'checkpoint_api_wall_ms': [], 'restore_api_wall_ms': [], 'restore_critical_ms': [], 'restore_kill_active_ms': []}
    for name in lock['runs']:
        directory = output / 'raw' / name
        config = json.loads((directory / 'run.json').read_text())
        env = json.loads((directory / 'environment.json').read_text())
        data = [json.loads(line) for line in gzip.decompress((directory / 'django__django-14997.results.jsonl.gz').read_bytes()).decode().splitlines()]
        measured = [row for row in data if row['kind'] in ('ckpt', 'restore')]
        dumps = [row for row in data if row['kind'] == 'dump_completion']
        assert len(measured) == config['n_ckpt'] + config['n_restore']
        assert len(dumps) == config['n_ckpt']
        assert data[-1]['worker_exec_bad_n'] == data[-1]['worker_exec_test_timeout_n'] == 0
        assert not config['source_provenance']['tracked_worktree_dirty']
        assert config['checkpoint_profile'] == 'historical-async-full'
        assert {key: item['sha256'] for key, item in config['images'].items()} == reference['image_sha256']
        binding = json.loads((directory / 'host_binding.json').read_text())
        assert binding['firecracker_affinity'] == [52, 53, 54, 55]
        guest_maps = [line for line in (directory / 'numa_maps_at_exit.txt').read_text().splitlines() if '/memfd:guest_mem' in line]
        assert guest_maps and all('bind:2' in line and set(re.findall(r'\bN(\d+)=', line)) == {'2'} for line in guest_maps)
        for row in measured:
            if row['kind'] == 'restore':
                assert row['worker_index_status_after_restore']['matches_target_ckpt']
        assert env['node'] == 2 and env['cpus'] == [52, 53, 54, 55]
        assert not env['restoration_errors']
        for policy in env['policies'].values():
            assert policy['applied']['scaling_min_freq'] == policy['applied']['scaling_max_freq'] == '4000000'
            assert policy['restored'] == policy['original']
        record = {'source_commit': config['source_provenance']['git_commit'], 'status': config['status'],
                  'error_n': data[-1]['error_n'], 'checkpoint_n': config['n_ckpt'], 'restore_n': config['n_restore'],
                  'dump_success_n': sum(row['ok'] for row in dumps), 'dump_failure_n': sum(not row['ok'] for row in dumps),
                  'diagnostic_environment': {k: v for k, v in config['guest_env'].items() if 'DUMP_DIAG' in k}}
        with tarfile.open(directory / 'diagnostics.tar.gz') as archive:
            member = next((m for m in archive.getmembers() if m.name == 'dump_lifecycle.jsonl'), None)
            if member:
                trace = [json.loads(line) for line in archive.extractfile(member).read().decode().splitlines()]
                kill_sets = [row for row in trace if row['kind'] == 'kill_set']
                intersections = []
                for row in kill_sets:
                    pending = {d['pid'] for d in row['dumps'].values() if not d['done']}
                    intersections.extend(sorted(pending & {int(pid) for pid in row['targets']}))
                record['pending_dump_pids_in_kill_sets'] = intersections
                record['criu_start_states'] = [r for r in trace if r['kind'] == 'criu_start']
                record['kill_sets'] = kill_sets
                record['signals_to_pending_dumps'] = [r for r in trace if r['kind'] == 'signal' and r['signal'] == 9
                    and r['target'] in intersections and r['caller'] == '_kill_pids_and_wait']
                if name == 'race-before-001':
                    assert intersections and record['signals_to_pending_dumps'] and record['dump_failure_n'] == 1
                    assert any(r['target_state'].get('State', '').startswith('Z') for r in record['criu_start_states'])
                else:
                    assert not intersections and record['dump_failure_n'] == 0
            else:
                assert name.startswith('lifetime-full-') and not record['diagnostic_environment']
        if name.startswith('lifetime-full-'):
            assert config['status'] == env['status'] == 'ok' and data[-1]['error_n'] == 0
            assert config['n_ckpt'] == 29 and config['n_restore'] == 28
            record['mean_ms'] = {}
            for key in pooled:
                values = [row[key] for row in measured if key in row]
                record['mean_ms'][key] = statistics.mean(values)
                pooled[key].extend(values)
        result['runs'][name] = record
    result['full_trace_means_ms'] = {key: statistics.mean(v) for key, v in pooled.items()}
    result['full_trace_event_counts'] = {key: len(v) for key, v in pooled.items()}
    (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', type=Path)
    parser.add_argument('--output', type=Path, default=DEFAULT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.collect:
        collect(args.collect, args.output)
    result = summarize(args.output)
    print(json.dumps({'means_ms': result['full_trace_means_ms'], 'runs': {
        k: {n: v[n] for n in ('status', 'error_n', 'dump_success_n', 'dump_failure_n')} for k, v in result['runs'].items()}}, indent=2))


if __name__ == '__main__':
    main()
