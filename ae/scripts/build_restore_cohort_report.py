#!/usr/bin/env python3
"""Verify the expanded restore audit, retain failures, and redraw comparisons."""
import argparse
from collections import defaultdict
import csv
import gzip
import hashlib
import json
from pathlib import Path
import re
import statistics as st
import tarfile

from build_restore_wakeup_report import measured_frequency, read, verify_lifecycle

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / 'ae/report/restore-cohort-20260921'
PAPER = {'Django': 2.23, 'SymPy': 2.21, 'Scientific': 1.82, 'Tools': 1.46}
METRICS = ('restore_api_wall_ms', 'restore_critical_ms', 'restore_kill_active_ms',
           'restore_prepare_overlapped_ms', 'checkpoint_api_wall_ms', 'worker_exec_wall_ms')
COMMITS = {'base': 'b228b18', 'readbase': 'b228b18', 'readopt': '547379f', 'race': '547379f'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def family(instance):
    return ('Django' if instance.startswith('django') else 'SymPy' if instance.startswith('sympy')
            else 'Scientific' if instance.startswith(('astropy', 'matplotlib')) else 'Tools')


def collect(source, output):
    lockpath = output / 'inputs.lock.json'
    lock = json.loads(lockpath.read_text()) if lockpath.exists() else {'host': 'spr4numa', 'files': {}, 'runs': []}
    for run in sorted(source.glob('cohort-*')):
        if not run.is_dir() or run.name.endswith('-env'):
            continue
        manifests = list(run.rglob('run.json'))
        if len(manifests) != 1:
            raise ValueError(f'Expected one manifest: {run}')
        manifest = json.loads(manifests[0].read_text())
        if manifest.get('status') not in ('ok', 'failed'):
            continue  # Never collect a live/incomplete producer.
        directory = manifests[0].parent
        instance = manifest['instance']
        names = ['run.json', f'{instance}.results.jsonl', 'schedule.jsonl', 'guest.log',
                 'host_binding.json', 'numa_maps_at_exit.txt', 'diagnostics.tar.gz',
                 'dmesg.log', 'runner.log']
        paths = [(directory / n, str(Path(manifest['guest_archive']).parent / n)) for n in names]
        envdir = source / (run.name + '-env')
        env = json.loads((envdir / 'environment.json').read_text())
        origin = Path(env['monitor_command'][-1]).parent
        paths += [(envdir / n, str(origin / n)) for n in ('environment.json', 'turbostat.tsv', 'samples.jsonl')]
        for path, origin in paths:
            if not path.exists():
                if manifest['status'] == 'ok':
                    raise ValueError(f'Missing successful-run evidence: {path}')
                continue
            raw = path.read_bytes()
            relative = f'raw/{run.name}/{path.name}'
            stored = raw
            if path.suffix in ('.log', '.jsonl'):
                relative += '.gz'; stored = gzip.compress(raw, mtime=0)
            dst = output / relative
            dst.parent.mkdir(parents=True, exist_ok=True); dst.write_bytes(stored)
            lock['files'][relative] = dict(source=origin, sha256=sha(stored), raw_sha256=sha(raw))
        if run.name not in lock['runs']:
            lock['runs'].append(run.name)
    lock['runs'].sort()
    lockpath.write_text(json.dumps(lock, indent=2) + '\n')


def summarize(output):
    lock = json.loads((output / 'inputs.lock.json').read_text())
    for relative, entry in lock['files'].items():
        assert sha((output / relative).read_bytes()) == entry['sha256'], relative
        assert sha(read(output / relative)) == entry['raw_sha256'], relative
    result = {'paper_full_cohort_verified': False, 'paper_reference_ms': PAPER,
              'runs': {}, 'groups': {}, 'historical': {}}
    pooled = defaultdict(lambda: defaultdict(list))
    configs = {}; totals = defaultdict(int)
    for name in lock['runs']:
        d = output / 'raw' / name
        cfg = json.loads(read(d / 'run.json')); env = json.loads(read(d / 'environment.json'))
        instance = cfg['instance']; arm = name.removeprefix('cohort-').split('-')[0]
        rows = [json.loads(line) for line in read(d / (instance + '.results.jsonl.gz')).splitlines()]
        record = dict(arm=arm, instance=instance, status=cfg['status'],
                      commit=cfg['source_provenance']['git_commit'],
                      started_unix=env['started_unix'],
                      prewarm_mode=cfg['guest_env'].get('DELTABOX_PREWARM_MODE', 'write'),
                      image_sha256={k: v['sha256'] for k, v in cfg['images'].items()},
                      checkpoint_n=cfg['n_ckpt'], restore_n=cfg['n_restore'])
        result['runs'][name] = record
        assert not cfg['source_provenance']['tracked_worktree_dirty'], name
        assert cfg['checkpoint_profile'] == 'historical-async-full'
        assert cfg['mode'] == 'fast' and not cfg['adaptive'] and cfg['memory_policy'] is None
        assert cfg['vcpus'] == 4 and cfg['mem_mib'] == 8192
        assert not cfg['incremental_dump_enabled'] and cfg['durable_dump_enabled']
        assert (cfg['max_events'] == 3 if arm == 'race' else cfg['max_events'] is None), name
        assert '--prewarm' in cfg['guest_flags'], name
        assert env['node'] == 2 and env['cpus'] == [52, 53, 54, 55]
        assert not env['restoration_errors']
        for p in env['policies'].values():
            assert p['applied'] == dict(scaling_governor='performance', scaling_min_freq='4000000', scaling_max_freq='4000000')
            assert p['restored'] == p['original']
        record['frequency'] = measured_frequency(d / 'turbostat.tsv')
        schedule_bytes = read(d / 'schedule.jsonl.gz')
        assert sha(schedule_bytes) == cfg['schedule_sha256']
        for artifact in cfg.get('artifacts', []):
            filename = artifact['path'] + ('.gz' if artifact['path'].endswith('.jsonl') else '')
            assert sha(read(d / filename)) == artifact['sha256'], filename
        errors = [r for r in rows if r.get('ok') is False]
        record['errors'] = errors
        dmesg = read(d / 'dmesg.log.gz').decode(errors='replace')
        record['guest_crashes'] = [line for line in dmesg.splitlines() if re.search(r'traps:|segfault|general protection', line)]
        if cfg['status'] != 'ok':
            record['excluded_from_latency_means'] = True
            continue
        assert env['status'] == 'ok' and not errors and not record['guest_crashes'], name
        binding = json.loads(read(d / 'host_binding.json'))
        assert binding['firecracker_affinity'] == [52, 53, 54, 55]
        maps = [l for l in read(d / 'numa_maps_at_exit.txt').decode().splitlines() if '/memfd:guest_mem' in l]
        assert maps and all('bind:2' in l and set(re.findall(r'\bN(\d+)=', l)) == {'2'} for l in maps)
        final = rows[-1]
        assert final['kind'] == 'run_summary'
        assert final['error_n'] == final['worker_exec_bad_n'] == final['worker_exec_test_timeout_n'] == 0
        events = [r for r in rows if r['kind'] in ('ckpt', 'restore')]
        schedule = [json.loads(l) for l in schedule_bytes.splitlines()]
        assert len(events) == len(schedule) == cfg['n_ckpt'] + cfg['n_restore']
        for i, (expected, actual) in enumerate(zip(schedule, events)):
            assert expected['type'] == actual['kind'] and actual['ev_i'] == i
            if actual['kind'] == 'restore':
                assert expected['restore_to_ckpt_id'] == actual['schedule_target_id']
                assert actual['worker_index_status_after_restore']['matches_target_ckpt']
                assert actual['path'] == 'warm-template'
            else:
                assert expected['ckpt_id'] == actual['schedule_ckpt_id']
                assert actual['worker_exec']['ok'] and actual['worker_index_status']['ok']
                actual['worker_exec_wall_ms'] = actual['worker_exec']['wall_ms']
        dumps = [r for r in rows if r['kind'] == 'dump_completion']
        assert len(dumps) == cfg['n_ckpt'] and all(r['ok'] for r in dumps)
        record['dumps_ok'] = len(dumps)
        totals['dumps_ok'] += len(dumps); totals['restores_ok'] += cfg['n_restore']
        with tarfile.open(d / 'diagnostics.tar.gz') as archive:
            members = {m.name: m for m in archive.getmembers()}
            assert 'prewarm.jsonl' in members, name
            if 'prewarm.jsonl' in members:
                warm = [json.loads(l) for l in archive.extractfile(members['prewarm.jsonl'])]
                assert warm and all(r['mode'] == record['prewarm_mode'] for r in warm)
                if record['prewarm_mode'] == 'read':
                    assert all(not r['write_cow'] for r in warm)
                record['prefetch_log_n'] = len(warm)
                record['prefetch_ok_n'] = sum(r['ok'] for r in warm)
                record['prefetch_failed_n'] = sum(not r['ok'] for r in warm)
                assert len(warm) == cfg['n_restore'] and all(r['ok'] for r in warm), name
        assert record['commit'].startswith(COMMITS[arm]), name
        if arm == 'race':
            record['lifecycle'] = verify_lifecycle(d / 'diagnostics.tar.gz')
            continue
        assert not any(v == '1' and ('PROFILE' in k or 'DIAGNOSTICS' in k) for k, v in cfg['guest_env'].items())
        if arm in ('readbase', 'readopt'):
            assert record['prewarm_mode'] == 'read'
            match_config = (cfg['guest_flags'], cfg['guest_env'], record['image_sha256'], sha(schedule_bytes))
            if instance in configs:
                assert match_config == configs[instance], name
            configs[instance] = match_config
        record['mean_ms'] = {m: st.mean(r[m] for r in events if m in r) for m in METRICS if any(m in r for r in events)}
        record['foreground_measured_sum_ms'] = sum(r.get('checkpoint_api_wall_ms',0) + r.get('restore_api_wall_ms',0) + r.get('worker_exec_wall_ms',0) for r in events)
        pooled[(arm, instance)]['foreground_measured_sum_ms'].append(record['foreground_measured_sum_ms'])
        for metric, value in record['mean_ms'].items():
            pooled[(arm, instance)][metric].append(value)
    result['successful_counts'] = dict(totals)
    for (arm, instance), metrics in pooled.items():
        result['groups'].setdefault(arm, {})[instance] = dict(
            repeats=len(metrics['restore_api_wall_ms']), family=family(instance),
            mean_ms={m: st.mean(xs) for m, xs in metrics.items()},
            run_range_ms={m: [min(xs), max(xs)] for m, xs in metrics.items()})
    hist = output / 'historical'
    sources = json.loads((hist / 'sources.json').read_text())
    hrows=[]; paper_rows=[]; paper_groups=defaultdict(lambda: defaultdict(list))
    for name, entry in sources['files'].items():
        assert sha((hist / name).read_bytes()) == entry['sha256']
        assert sha(read(hist / name)) == entry['raw_sha256']
        if name.endswith('.results.jsonl.gz'):
            rows=[json.loads(l) for l in read(hist / name).splitlines()]
            if name.startswith('paper-table2/'):
                paper_rows.extend(rows)
                instance=Path(name).name.split('.replay-')[0]
                for row in rows:
                    if row.get('kind')=='restore':paper_groups[family(instance)]['restore'].append(row['restore_critical_ms'])
                    if row.get('kind')=='ckpt':paper_groups[family(instance)]['checkpoint'].append(row['ckpt_wall_ms'])
            else:
                hrows.extend(rows)
    restores=[r for r in hrows if r.get('kind') == 'restore']
    checkpoints=[r for r in hrows if r.get('kind') == 'ckpt']
    assert len(restores) == 334 and len(checkpoints) == 317
    assert all(r['restore_wall_ms'] == r['restore_critical_ms'] for r in restores)
    result['historical'] = dict(restore_n=len(restores), checkpoint_n=len(checkpoints),
        restore_critical_ms=st.mean(r['restore_critical_ms'] for r in restores),
        ckpt_wall_ms=st.mean(r['ckpt_wall_ms'] for r in checkpoints),
        later_main_is_paper_dataset=False, exact_deployed_source_recovered=False)
    paper_restore=[r for r in paper_rows if r.get('kind')=='restore']
    paper_ckpt=[r for r in paper_rows if r.get('kind')=='ckpt']
    assert len(paper_restore)==334 and len(paper_ckpt)==317
    assert all(r['restore_wall_ms']==r['restore_critical_ms'] for r in paper_restore)
    expected_ck={'Django':12.12,'SymPy':11.96,'Scientific':10.86,'Tools':9.16}
    paper_metrics={f:{m:dict(n=len(xs),mean=st.mean(xs)) for m,xs in values.items()} for f,values in paper_groups.items()}
    for f,values in paper_metrics.items():
        assert round(values['restore']['mean'],2)==PAPER[f]
        assert round(values['checkpoint']['mean'],2)==expected_ck[f]
    mean_ck=st.mean(r['ckpt_wall_ms'] for r in paper_ckpt);mean_rs=st.mean(r['restore_critical_ms'] for r in paper_restore)
    assert round(mean_ck,2)==10.83 and round(mean_rs,2)==1.86
    plock=json.loads((hist/'paper-version-lock.json').read_text())
    assert len(plock['runs'])==12
    for run in plock['runs']:
        condition=json.loads((hist/run['conditions']).read_text())
        assert condition['git_rev']==plock['recorded_commit']=='e200a6ce7deb107ba35a770206b6392f6ef9dac3'
        assert condition['status']=='ok'
    result['historical']['paper_table2']=dict(recorded_commit=plock['recorded_commit'],
        paper_rounding_reconstructed=True,restore_n=334,checkpoint_n=317,
        ckpt_wall_ms=mean_ck,restore_critical_ms=mean_rs,groups=paper_metrics,
        exact_deployed_source_recovered=False,
        test_runner_nonzero_rc_marked_ok=sum(op.get('ok') is True and op.get('rc') not in (None,0)
            for row in paper_ckpt for op in row.get('worker_exec',{}).get('results',[]) if op.get('type')=='run_tests'))
    (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    with (output / 'per-run.csv').open('w') as stream:
        writer=csv.writer(stream, lineterminator='\n')
        writer.writerow(['run','status','mode','commit',*METRICS])
        for name,r in result['runs'].items():
            writer.writerow([name,r['status'],r['prewarm_mode'],r['commit'],*[r.get('mean_ms',{}).get(m,'') for m in METRICS]])
    return result


def plot(output, data):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    instances=list(data['groups'].get('readopt', data['groups'].get('readbase', {})))
    fig, axes = plt.subplots(1,2,figsize=(13,5.4))
    colors={'readbase':'#2b6cb0','readopt':'#2f855a'}
    labels={'readbase':'Before','readopt':'Candidate'}
    for arm,shift in [('readbase',-.19),('readopt',.19)]:
        if arm not in data['groups']:continue
        for i,inst in enumerate(instances):
            group=data['groups'][arm][inst];m=group['mean_ms'];x=i+shift
            axes[0].bar(x,m['restore_api_wall_ms'],.35,color=colors[arm],alpha=.28)
            axes[0].bar(x,m['restore_critical_ms'],.35,color=colors[arm],label=labels[arm] if i==0 else None)
            axes[1].bar(x,m['worker_exec_wall_ms'],.35,color=colors[arm],label=labels[arm] if i==0 else None)
            runs=[r for r in data['runs'].values() if r['arm']==arm and r['instance']==inst and 'mean_ms' in r]
            for j,run in enumerate(runs):
                axes[0].plot(x+(j-1)*.035,run['mean_ms']['restore_api_wall_ms'],'k.',ms=3)
                axes[1].plot(x+(j-1)*.035,run['mean_ms']['worker_exec_wall_ms'],'k.',ms=3)
    for i,inst in enumerate(instances):
        ref=PAPER[family(inst)]
        axes[0].plot([i-.43,i+.43],[ref,ref],color='#b83280',ls='--',label='Paper family critical reference' if i==0 else None)
    for ax in axes:
        ax.set_xticks(range(len(instances)),[s.split('__')[0]+'\n'+s.rsplit('-',1)[-1] for s in instances]);ax.set_ylabel('ms');ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    axes[0].set_title('Restore: full height = complete API\nsolid = internal fork/ioctl interval');handles,legend_labels=axes[0].get_legend_handles_labels()
    fig.legend(handles,legend_labels,loc='upper center',bbox_to_anchor=(.5,.925),ncol=3,fontsize=8)
    axes[1].set_title('Worker actions (same read-prefetch setting)');axes[1].set_yscale('log');axes[1].set_ylabel('ms (log scale)');axes[1].legend(fontsize=8)
    fig.suptitle('Full-trace audit: read-prefetch; not the full paper cohort',fontsize=13)
    fig.tight_layout(rect=[0,0,1,.86]);fig.savefig(output/'comparison.png',dpi=180);fig.savefig(output/'comparison.pdf');plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--collect',type=Path)
    parser.add_argument('--output',type=Path,default=DEFAULT);parser.add_argument('--no-plot',action='store_true');parser.add_argument('--partial',action='store_true',help='Inspect an unfinished campaign without final coverage assertions')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if args.collect:collect(args.collect,args.output)
    result=summarize(args.output)
    if not args.partial:
        expected={'matplotlib__matplotlib-23964','pylint-dev__pylint-7228','psf__requests-863','sympy__sympy-22840','astropy__astropy-7746'}
        for arm in ('readbase','readopt'):
            assert set(result['groups'][arm]) == expected
            assert all(r['repeats'] == 3 for r in result['groups'][arm].values())
        races=[r for r in result['runs'].values() if r['arm']=='race']
        assert len(races)==1 and races[0]['status']=='ok' and races[0].get('lifecycle')
        assert sum(r['status']=='failed' for r in result['runs'].values()) == 3
    if not args.no_plot:plot(args.output,result)
    print(json.dumps({'runs':len(result['runs']),'counts':result['successful_counts'],'groups':result['groups']},indent=2))

if __name__=='__main__':main()
