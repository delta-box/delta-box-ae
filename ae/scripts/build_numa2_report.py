#!/usr/bin/env python3
"""Collect/hash controlled NUMA runs, then regenerate statistics and plots.

--collect accepts a local rsync mirror containing numa2-max-*-NNN{,-env}.
Without it, this reads only committed, hash-checked report snapshots.
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import statistics as st

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'ae/report/numa2-max-20260921'
METRICS = ('checkpoint_api_wall_ms', 'ckpt_wall_ms', 'checkpoint_fork_ms',
           'checkpoint_overlay_ms', 'restore_api_wall_ms', 'restore_critical_ms',
           'restore_kill_active_ms', 'restore_other_ms')
HISTORICAL = ('ae/paper/table-02/data/records/deltabox-fast/results/deltabox-no-adapt/'
              'django__django-14997.replay-deltabox-no-adapt-92cef3.results.jsonl')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def collect(source, out):
    lock = {'host': 'spr4numa', 'files': {}, 'runs': []}
    def copy(path, relative, origin):
        raw = path.read_bytes()
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        stored = gzip.compress(raw, mtime=0) if relative.endswith('.gz') else raw
        target.write_bytes(stored)
        lock['files'][relative] = dict(source=origin, sha256=sha(stored),
                                      uncompressed_sha256=sha(raw), bytes=len(raw))
    for run in sorted(source.glob('numa2-max-*')):
        if run.name.endswith('-env') or not run.is_dir():
            continue
        configs = list(run.glob('**/run.json'))
        if len(configs) != 1:
            raise ValueError(f'expected one run.json: {run}')
        config_path = configs[0]
        config = json.loads(config_path.read_text())
        origin_dir = str(Path(config['guest_archive']).parent)
        env = source / (run.name + '-env')
        env_record = json.loads((env / 'environment.json').read_text())
        env_origin = str(Path(env_record['monitor_command'][-1]).parent)
        for name in ('run.json', 'host_binding.json', 'numa_maps_at_exit.txt',
                     'schedule.jsonl', 'django__django-14997.results.jsonl', 'guest.log', 'runner.log'):
            p = config_path.parent / name
            suffix = '.gz' if name.endswith(('.jsonl', '.log')) else ''
            copy(p, f'raw/{run.name}/{name}{suffix}', origin_dir + '/' + name)
        for name in ('environment.json', 'turbostat.tsv', 'samples.jsonl', 'command.log'):
            suffix = '.gz' if name.endswith(('.jsonl', '.log')) else ''
            copy(env / name, f'raw/{run.name}/{name}{suffix}', env_origin + '/' + name)
        lock['runs'].append(run.name)
    copy(ROOT / HISTORICAL, 'raw/historical.results.jsonl.gz', HISTORICAL)
    copy(ROOT / 'ae/reference/paper158.txt', 'raw/paper158.txt.gz', 'ae/reference/paper158.txt')
    lock['paper_sha256'] = sha((ROOT / 'ae/reference/paper158.pdf').read_bytes())
    (out / 'inputs.lock.json').write_text(json.dumps(lock, indent=2) + '\n')


def read(path):
    data = path.read_bytes()
    return (gzip.decompress(data) if path.suffix == '.gz' else data).decode()


def rows(path):
    return [json.loads(line) for line in read(path).splitlines() if line.strip()]


def stats(values):
    values = sorted(values)
    if not values:
        return None
    return dict(n=len(values), mean=st.mean(values), median=st.median(values),
                p95_nearest_rank=values[math.ceil(.95 * len(values)) - 1],
                min=values[0], max=values[-1])


def event_keys(data):
    return [[r.get(k) for k in ('ev_i', 'kind', 'schedule_ckpt_id', 'schedule_target_id', 'step_idx')]
            for r in data if r.get('kind') in ('ckpt', 'restore')]


def analyze(out):
    lock = json.loads((out / 'inputs.lock.json').read_text())
    for name, record in lock['files'].items():
        assert sha((out / name).read_bytes()) == record['sha256'], name
        assert sha(read(out / name).encode()) == record['uncompressed_sha256'], name
    probe_lock = out / 'probe/inputs.lock.json'
    if probe_lock.exists():
        for name, record in json.loads(probe_lock.read_text()).items():
            assert sha((out / 'probe' / name).read_bytes()) == record['sha256'], name
    historical = rows(out / 'raw/historical.results.jsonl.gz')
    result = dict(paper_cohort_verified=False, unit='ms',
                  paper_django={'checkpoint_ms': 12.12, 'restore_ms': 2.23},
                  paper_event_avg={'checkpoint_ms': 10.83, 'restore_ms': 1.86},
                  historical={k: stats([r[k] for r in historical if k in r]) for k in
                              ('ckpt_wall_ms', 'restore_wall_ms', 'restore_critical_ms', 'checkpoint_fork_ms')},
                  runs={}, groups={})
    paper = read(out / 'raw/paper158.txt.gz')
    assert re.search(r'Django\s+568\.1.*12\.12\s+2\.23', paper)
    pooled = {}
    expected_schedule = None
    expected_images = None
    for name in lock['runs']:
        directory = out / 'raw' / name
        config = json.loads(read(directory / 'run.json'))
        env = json.loads(read(directory / 'environment.json'))
        binding = json.loads(read(directory / 'host_binding.json'))
        data = rows(directory / 'django__django-14997.results.jsonl.gz')
        summary = data[-1]
        assert event_keys(data) == event_keys(historical), name
        assert config['n_ckpt'] == 29 and config['n_restore'] == 28
        assert config['checkpoint_profile'] == 'historical-async-full'
        assert not config['source_provenance']['tracked_worktree_dirty']
        images = {k: v['sha256'] for k, v in config['images'].items()}
        expected_images = expected_images or images
        assert images == expected_images, 'image drift'
        schedule = sha(read(directory / 'schedule.jsonl.gz').encode())
        expected_schedule = expected_schedule or schedule
        assert schedule == expected_schedule == config['schedule_sha256']
        assert env['node'] == 2 and env['cpus'] == [52, 53, 54, 55]
        assert binding['firecracker_affinity'] == [52, 53, 54, 55]
        assert not env['restoration_errors']
        for policy in env['policies'].values():
            assert policy['applied'] == dict(scaling_governor='performance',
                                             scaling_min_freq='4000000', scaling_max_freq='4000000')
            assert policy['restored'] == policy['original']
        pages = {}
        for line in read(directory / 'numa_maps_at_exit.txt').splitlines():
            if '/memfd:guest_mem' in line:
                assert 'bind:2' in line
                for node, n in re.findall(r'\bN(\d+)=(\d+)', line):
                    pages[node] = pages.get(node, 0) + int(n)
        assert set(pages) == {'2'} and pages['2'] > 0
        for row in data:
            if row.get('kind') == 'restore':
                assert row['worker_index_status_after_restore']['matches_target_ckpt']
                row['restore_other_ms'] = (row['restore_api_wall_ms'] - row['restore_critical_ms']
                                           - row['restore_kill_active_ms'])
        assert summary['worker_exec_bad_n'] == summary['worker_exec_test_timeout_n'] == 0
        for key in ('fingerprint', 'n_files', 'n_classes', 'n_functions', 'total_bytes'):
            assert summary['worker_index_initial'][key] == historical[-1]['worker_index_initial'][key]
        valid = env['status'] == config['status'] == 'ok' and summary['error_n'] == 0
        metrics = {k: stats([r[k] for r in data if k in r]) for k in METRICS}
        freq = [list(map(float, line.split())) for line in read(directory / 'turbostat.tsv').splitlines()
                if line.split() and line.split()[0] in ('52', '53', '54', '55')]
        # Columns are CPU, Avg_MHz, Busy%, Bzy_MHz, TSC_MHz. Includes VM startup.
        weighted = sum(r[2] * r[3] for r in freq) / sum(r[2] for r in freq)
        group = name.removeprefix('numa2-max-').rsplit('-', 1)[0]
        result['runs'][name] = dict(group=group, valid=valid, metrics=metrics,
            source_commit=config['source_provenance']['git_commit'],
            error_n=summary['error_n'], first_error=summary['first_error'],
            started_unix=env['started_unix'], guest_pages_by_node=pages,
            schedule_sha256=schedule, achieved_busy_weighted_mhz=weighted,
            achieved_sample_range_mhz=[min(r[3] for r in freq), max(r[3] for r in freq)],
            prefix_first_3={k: stats([r[k] for r in data if r.get('ev_i', 999) < 3 and k in r])
                            for k in METRICS})
        group_record = result['groups'].setdefault(group, {'attempts': 0, 'passed': 0, 'failed': 0})
        group_record['attempts'] += 1
        group_record['passed' if valid else 'failed'] += 1
        if valid:
            for k in METRICS:
                pooled.setdefault(group, {}).setdefault(k, []).extend(r[k] for r in data if k in r)
    for group, values in pooled.items():
        g = result['groups'][group]
        g['metrics_successful_runs_only'] = {k: stats(v) for k, v in values.items()}
        g['per_run_mean_range'] = {k: [min(v), max(v)] for k in METRICS
            if (v := [r['metrics'][k]['mean'] for r in result['runs'].values() if r['group'] == group and r['valid']])}
    result['image_sha256'] = expected_images
    (out / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    with (out / 'per-run.csv').open('w') as f:
        writer = csv.writer(f)
        writer.writerow(['run', 'valid', 'error_n', 'busy_weighted_MHz', *METRICS])
        for name, r in result['runs'].items():
            writer.writerow([name, r['valid'], r['error_n'], r['achieved_busy_weighted_mhz'],
                             *[r['metrics'][k]['mean'] for k in METRICS]])
    return result


def plot(out, result):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    colors = ['#366b96', '#cf9c37', '#368b77']
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.7), constrained_layout=True)
    groups = [g for g in ('baseline', 'pidfd', 'compat') if g in result['groups']]
    labels = {'baseline': 'Original', 'pidfd': 'pidfd patch\n(fell back)', 'compat': 'pidfd + compat'}
    for ax, key, title, paper in zip(axes[:2], ('checkpoint_api_wall_ms', 'restore_api_wall_ms'),
                                    ('Checkpoint: complete API', 'Restore: complete API'), (12.12, 2.23)):
        means = [result['groups'][g]['metrics_successful_runs_only'][key]['mean'] for g in groups]
        bars = ax.bar(range(len(groups)), means, color=colors[:len(groups)], alpha=.8)
        for i, g in enumerate(groups):
            passed = [r['metrics'][key]['mean'] for r in result['runs'].values() if r['group'] == g and r['valid']]
            failed = [r['metrics'][key]['mean'] for r in result['runs'].values() if r['group'] == g and not r['valid']]
            ax.scatter([i] * len(passed), passed, color='black', s=22, zorder=5)
            ax.scatter([i] * len(failed), failed, color='red', marker='x', s=65, zorder=6)
        ax.bar_label(bars, fmt='%.2f', padding=10)
        ax.axhline(paper, color='#9a4261', ls='--', label=f'Paper Django: {paper} (different cohort)')
        ax.set(title=title, ylabel='Mean latency (ms)', ylim=(0, max(means) * 1.32))
        ax.legend(fontsize=8, loc='upper right')
    ax = axes[2]
    bottom = [0.] * len(groups)
    for key, label, color in [('restore_critical_ms', 'fork/ioctl critical path', '#366b96'),
                               ('restore_kill_active_ms', 'kill + wait', '#cf9c37'),
                               ('restore_other_ms', 'remaining API work', '#368b77')]:
        values = [result['groups'][g]['metrics_successful_runs_only'][key]['mean'] for g in groups]
        ax.bar(range(len(groups)), values, bottom=bottom, label=label, color=color)
        bottom = [b + v for b, v in zip(bottom, values)]
    ax.set(title='Restore decomposition', ylabel='Mean latency (ms)', ylim=(0, max(bottom) * 1.32))
    ax.legend(fontsize=8)
    for ax in axes:
        ax.set_xticks(range(len(groups)), [f'{labels[g]}\n{result["groups"][g]["passed"]}/{result["groups"][g]["attempts"]} runs passed' for g in groups])
        ax.spines[['top', 'right']].set_visible(False)
    fig.suptitle('Django-14997 full trace | NUMA 2, CPUs 52-55, maximum P-state request\n'
                 '29 checkpoints + 28 restores/run; bars = passed runs only; red x = rejected run', fontsize=12)
    fig.savefig(out / 'comparison.png', dpi=180)
    fig.savefig(out / 'comparison.pdf')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect', type=Path)
    parser.add_argument('--output', type=Path, default=DEFAULT)
    parser.add_argument('--plot', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.collect:
        collect(args.collect, args.output)
    result = analyze(args.output)
    if args.plot:
        plot(args.output, result)
    print(json.dumps(result['groups'], indent=2))


if __name__ == '__main__':
    main()
