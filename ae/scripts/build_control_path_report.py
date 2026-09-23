#!/usr/bin/env python3
"""Verify the controlled PID-lookup/prewarm experiment and redraw its report."""
import argparse
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import tarfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'ae/report/control-path-20260921'
METRICS = ('checkpoint_api_wall_ms', 'restore_api_wall_ms', 'restore_critical_ms',
           'restore_kill_active_ms', 'restore_other_ms', 'worker_exec_wall_ms')
COMMITS = {'base': '0c66d86', 'hint': '7b712ce', 'off': '7b712ce', 'scoped': '227ceb1',
           'helper': '3f58fcf'}
LABELS = {'base': 'Before / prewarm on', 'hint': 'PID hint / prewarm on',
          'off': 'Prewarm off', 'scoped': 'Reaper lookup / prewarm off',
          'helper': 'Helper lookup / prewarm on'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.name.endswith(('.jsonl.gz', '.log.gz')) else raw


def collect(source, output):
    lock = {'host': 'spr4numa', 'runs': [], 'files': {}}
    for run in sorted(source.iterdir()):
        if not run.is_dir() or run.name.endswith('-env'):
            continue
        if not run.name.startswith(('control-', 'api-profile-', 'profile-helper-', 'race-helper-')):
            continue
        configs = list(run.glob('**/run.json'))
        if len(configs) != 1:
            raise ValueError(f'Expected one run.json in {run}')
        directory = configs[0].parent
        config = json.loads(configs[0].read_text())
        env_dir = source / (run.name + '-env')
        env = json.loads((env_dir / 'environment.json').read_text())
        origin = Path(config['guest_archive']).parent
        env_origin = Path(env['monitor_command'][-1]).parent
        paths = [(directory / n, origin / n) for n in
                 ('run.json', 'django__django-14997.results.jsonl', 'schedule.jsonl',
                  'guest.log', 'host_binding.json', 'numa_maps_at_exit.txt', 'diagnostics.tar.gz')]
        paths += [(env_dir / n, env_origin / n) for n in
                  ('environment.json', 'turbostat.tsv', 'samples.jsonl')]
        for path, remote in paths:
            raw = path.read_bytes()
            relative = f'raw/{run.name}/{path.name}'
            if path.suffix in ('.jsonl', '.log'):
                relative += '.gz'
                stored = gzip.compress(raw, mtime=0)
            else:
                stored = raw
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stored)
            lock['files'][relative] = dict(source=str(remote), sha256=sha(stored),
                                          raw_sha256=sha(raw))
        lock['runs'].append(run.name)
    (output / 'inputs.lock.json').write_text(json.dumps(lock, indent=2) + '\n')


def profile(archive):
    with tarfile.open(archive) as saved:
        item = next((m for m in saved.getmembers() if m.name == 'api_profile.jsonl'), None)
        if item is None:
            return None
        rows = [json.loads(line) for line in saved.extractfile(item)]
    result = {}
    for operation in ('checkpoint_action', 'restore_action'):
        selected = [r for r in rows if r['operation'] == operation]
        totals = defaultdict(lambda: [0., 0., 0])
        for row in selected:
            for entry in row['records']:
                key = Path(entry['file']).name + ':' + entry['name']
                for i, field in enumerate(('self_ms', 'total_ms', 'calls')):
                    totals[key][i] += entry[field]
        result[operation] = dict(n=len(selected), functions={k: dict(zip(
            ('self_ms', 'inclusive_ms', 'calls'), [v / len(selected) for v in values]))
            for k, values in sorted(totals.items(), key=lambda item: -item[1][0])})
    return result


def measured_frequency(path):
    samples = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[0] in ('52', '53', '54', '55'):
            samples.append((float(fields[2]), float(fields[3])))
    assert samples and sum(busy for busy, _ in samples) > 0, path
    # turbostat samples all four CPUs at the same fixed interval. Ignore its
    # '-' aggregate rows to avoid counting the same measurement twice.
    return dict(cpu_sample_n=len(samples), busy_weighted_mhz=
                math.fsum(busy*mhz for busy, mhz in samples)/math.fsum(busy for busy, _ in samples))


def verify_lifecycle(archive):
    with tarfile.open(archive) as saved:
        item = next((m for m in saved.getmembers() if m.name == 'dump_lifecycle.jsonl'), None)
        assert item, archive
        rows = [json.loads(line) for line in saved.extractfile(item)]
    kills = [r for r in rows if r['kind'] == 'kill_set']
    assert kills, archive
    pending_sets = []
    for row in kills:
        pending = {d['pid'] for d in row['dumps'].values() if not d['done']}
        assert not pending & {int(pid) for pid in row['targets']}, row
        pending_sets.append(sorted(pending))
    assert any(pending_sets), 'Injection did not exercise a pending dump'
    return dict(kill_sets=kills, pending_dump_pids=pending_sets,
                pending_dump_pids_in_kill_sets=[],
                criu_start_states=[r for r in rows if r['kind'] == 'criu_start'])


def summarize(output):
    lock = json.loads((output / 'inputs.lock.json').read_text())
    for relative, record in lock['files'].items():
        path = output / relative
        assert sha(path.read_bytes()) == record['sha256'], relative
        assert sha(read(path)) == record['raw_sha256'], relative
    reference = json.loads((ROOT / 'ae/report/numa2-max-20260921/summary.json').read_text())
    result = dict(paper_cohort_verified=False, paper_django_ms={'checkpoint': 12.12, 'restore': 2.23},
                  runs={}, groups={}, profiles={})
    pooled = defaultdict(lambda: defaultdict(list))
    schedule_hash = None
    for name in lock['runs']:
        directory = output / 'raw' / name
        config = json.loads(read(directory / 'run.json'))
        env = json.loads(read(directory / 'environment.json'))
        assert config['status'] == env['status'] == 'ok', name
        assert not config['source_provenance']['tracked_worktree_dirty'], name
        assert config['checkpoint_profile'] == 'historical-async-full'
        assert {k: v['sha256'] for k, v in config['images'].items()} == reference['image_sha256']
        for artifact in config['artifacts']:
            filename = artifact['path'] + ('.gz' if artifact['path'].endswith('.jsonl') else '')
            assert sha(read(directory / filename)) == artifact['sha256'], filename
        binding = json.loads(read(directory / 'host_binding.json'))
        assert binding['firecracker_affinity'] == [52, 53, 54, 55]
        mappings = [l for l in read(directory / 'numa_maps_at_exit.txt').decode().splitlines() if '/memfd:guest_mem' in l]
        assert mappings and all('bind:2' in l and set(re.findall(r'\bN(\d+)=', l)) == {'2'} for l in mappings)
        assert env['cpus'] == [52, 53, 54, 55] and env['node'] == 2 and not env['restoration_errors']
        for policy in env['policies'].values():
            assert policy['applied'] == dict(scaling_governor='performance', scaling_min_freq='4000000', scaling_max_freq='4000000')
            assert policy['original'] == policy['restored']
        rows = [json.loads(l) for l in read(directory / 'django__django-14997.results.jsonl.gz').splitlines()]
        assert rows[-1]['error_n'] == rows[-1]['worker_exec_bad_n'] == rows[-1]['worker_exec_test_timeout_n'] == 0
        dumps = [r for r in rows if r['kind'] == 'dump_completion']
        assert len(dumps) == config['n_ckpt'] and all(r['ok'] for r in dumps)
        events = [r for r in rows if r['kind'] in ('ckpt', 'restore')]
        schedule_bytes = read(directory / 'schedule.jsonl.gz')
        assert sha(schedule_bytes) == config['schedule_sha256']
        schedule = [json.loads(l) for l in schedule_bytes.splitlines()]
        assert len(schedule) == len(events)
        for i, (expected, actual) in enumerate(zip(schedule, events)):
            assert expected['type'] == actual['kind'] and actual['ev_i'] == i
            if actual['kind'] == 'restore':
                assert expected['restore_to_ckpt_id'] == actual['schedule_target_id']
                assert actual['worker_index_status_after_restore']['matches_target_ckpt']
                actual['restore_other_ms'] = actual['restore_api_wall_ms'] - actual['restore_critical_ms'] - actual['restore_kill_active_ms']
            else:
                assert expected['ckpt_id'] == actual['schedule_ckpt_id']
                assert actual['worker_exec']['ok'] and actual['worker_index_status']['ok']
                actual['worker_exec_wall_ms'] = actual['worker_exec']['wall_ms']
        record = dict(commit=config['source_provenance']['git_commit'], checkpoint_n=config['n_ckpt'],
                      restore_n=config['n_restore'], dumps_ok=len(dumps), prewarm='--prewarm' in config['guest_flags'],
                      started_unix=env['started_unix'], elapsed_s=env['finished_unix']-env['started_unix'])
        record['frequency'] = measured_frequency(directory / 'turbostat.tsv')
        result['runs'][name] = record
        prof = profile(directory / 'diagnostics.tar.gz')
        if prof:
            result['profiles'][name] = prof
        if name.startswith('race-'):
            record['lifecycle'] = verify_lifecycle(directory / 'diagnostics.tar.gz')
        match = re.fullmatch(r'control-(base|hint|off|scoped|helper)-\d+', name)
        if not match:
            continue
        group = match[1]
        assert record['commit'].startswith(COMMITS[group])
        assert record['prewarm'] == (group in ('base', 'hint', 'helper'))
        assert not prof and not any(v == '1' and ('PROFILE' in k or 'DUMP_DIAG' in k) for k, v in config['guest_env'].items())
        assert (config['n_ckpt'], config['n_restore']) == (29, 28)
        schedule_hash = schedule_hash or sha(schedule_bytes)
        assert sha(schedule_bytes) == schedule_hash
        record['group'] = group
        record['mean_ms'] = {}
        for metric in METRICS:
            values = [r[metric] for r in events if metric in r]
            record['mean_ms'][metric] = statistics.mean(values)
            pooled[group][metric].extend(values)
        record['foreground_measured_sum_ms'] = math.fsum(
            r.get('checkpoint_api_wall_ms', 0) + r.get('restore_api_wall_ms', 0)
            + r.get('worker_exec_wall_ms', 0) for r in events)
    for group, metrics in pooled.items():
        runs = [r for r in result['runs'].values() if r.get('group') == group]
        assert len(runs) == 3, (group, len(runs))
        result['groups'][group] = dict(repeats=len(runs), mean_ms={m: statistics.mean(v) for m, v in metrics.items()},
            per_run_range_ms={m: [min(r['mean_ms'][m] for r in runs), max(r['mean_ms'][m] for r in runs)] for m in METRICS})
    assert set(result['groups']) == set(COMMITS)
    result['schedule_sha256'] = schedule_hash
    (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    with (output / 'per-run.csv').open('w') as f:
        w = csv.writer(f); w.writerow(['run', 'group', 'commit', *METRICS])
        for name, run in result['runs'].items():
            if 'group' in run: w.writerow([name, run['group'], run['commit'], *[run['mean_ms'][m] for m in METRICS]])
    return result


def plot(output, data):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    groups = ['base', 'helper', 'off']
    fig, axes = plt.subplots(1, 4, figsize=(17, 4.6), constrained_layout=True)
    for ax, metric, title, paper in zip(axes[:2], METRICS[:2], ['Checkpoint: complete API', 'Restore: complete API'], [12.12, 2.23]):
        means = [data['groups'][g]['mean_ms'][metric] for g in groups]
        bars = ax.bar(range(3), means, color=['#64748b', '#377fab', '#26836b'], alpha=.8)
        ax.bar_label(bars, fmt='%.3f', padding=8)
        for i, g in enumerate(groups):
            values = [r['mean_ms'][metric] for r in data['runs'].values() if r.get('group') == g]
            ax.scatter([i]*len(values), values, c='black', s=20, zorder=4)
        ax.axhline(paper, color='#a44464', ls='--', label=f'Paper Django {paper} ms (different cohort)')
        ax.set(title=title, ylabel='Latency (ms)', ylim=(0, max(max(means),paper)*1.25))
        ax.legend(fontsize=7, loc='upper right')
    bottom = [0., 0., 0.]
    for metric, label, color in [('restore_critical_ms','fork / ioctl', '#377fab'),
                                 ('restore_kill_active_ms','kill and wait', '#d5a443'),
                                 ('restore_other_ms','other API work', '#26836b')]:
        values = [data['groups'][g]['mean_ms'][metric] for g in groups]
        axes[2].bar(range(3), values, bottom=bottom, label=label, color=color)
        bottom = [a+b for a,b in zip(bottom, values)]
    axes[2].set(title='Restore components', ylabel='Latency (ms)')
    axes[2].legend(fontsize=8)
    metric = 'worker_exec_wall_ms'
    bars = axes[3].bar(range(3), [data['groups'][g]['mean_ms'][metric] for g in groups],
                       color=['#64748b', '#377fab', '#26836b'], alpha=.8)
    axes[3].bar_label(bars, fmt='%.3f', padding=8)
    for i, g in enumerate(groups):
        values = [r['mean_ms'][metric] for r in data['runs'].values() if r.get('group') == g]
        axes[3].scatter([i]*len(values), values, c='black', s=20, zorder=4)
    axes[3].set(title='Worker action (tradeoff check)', ylabel='Latency (ms)',
                ylim=(0, max(data['groups'][g]['mean_ms'][metric] for g in groups)*1.25))
    for ax in axes:
        ax.set_xticks(range(3), [LABELS[g].replace(' / ', '\n') for g in groups], fontsize=8)
    fig.suptitle('Django-14997 / NUMA 2 / requested maximum P-state / 3 complete traces per group\n'
                 'Black points: independent run means. Historical async-full profile; GPU excluded.', fontsize=11)
    for suffix in ('png','pdf'):
        fig.savefig(output / ('comparison.'+suffix), dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--collect', type=Path); p.add_argument('--output', type=Path, default=DEFAULT)
    p.add_argument('--plot', action='store_true'); args=p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.collect: collect(args.collect, args.output)
    data=summarize(args.output)
    if args.plot: plot(args.output, data)
    print(json.dumps(data['groups'], indent=2))
