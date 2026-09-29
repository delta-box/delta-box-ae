"""Published CPU panels, fixed cohorts, and concrete driver command construction."""
from __future__ import annotations
import csv
import json
import math
import tempfile
import os
from pathlib import Path
import sys
from .common import AE_ROOT, REPO_ROOT, configured_path, digest
from .figure09_cohort import figure09_rows

EXPERIMENTS = {
 'table-02-deltabox': 'Table 2 DeltaBox fast / Table 3 components / Figure 7 components',
 'table-03-slow': 'Table 3 forced CRIU restore (eager/lazy follows checkpoint profile)',
 'table-02-replay': 'Table 2 copytree + actual Moatless replay',
 'table-02-criu': 'Table 2 CRIU + filesystem copy',
 'table-02-fc-diff': 'Table 2 Firecracker Diff + dm-thin',
 'table-02-cube': 'Table 2 Cube CoW canonical 12',
 'table-02-e2b': 'Table 2 E2B original 8',
 'figure-02-filesystem': 'Figure 2 filesystem state / per-step writes, 30 traces',
 'figure-02-memory': 'Figure 2 process-tree RSS / soft dirty, 5 traces',
 'figure-06-memory': 'Figure 6 no-dump memory policies, four arms',
 'figure-06-adaptive': 'Figure 6 lightweight vs standard, 12 traces x two arms',
 'figure-08-deltabox': 'Figure 8 CPU 64 MiB inherited-memory fanout',
 'figure-08-cube': 'Figure 8 official Cube sandbox fanout',
 'figure-08-e2b': 'Figure 8 official E2B sandbox fanout',
 'figure-09': 'Figure 9 measured write amplification, fixed 80 inputs x three filesystems',
 'correctness': 'Recovered filesystem correctness tests (not a recovered 53-case suite)',
}
SKIPPED = [{'experiment':'figure-08-gpu','status':'not-run',
            'reason':'Figure 8(b) uses the separate opt-in GPU runner; hardware allocation is required',
            'entrypoint':'ae/runners/gpu_timing.py'}]
DERIVED = {'figure-07':'End-to-end component model from fresh traces; separate from measured total',
           'table-03-fast':'Derived from table-02-deltabox',
           'figure-08-theory':'Figure 8(c), CPU Equation 1 calculation via ae/repro/gpu_occupation.py; explicit timing inputs required'}


def cohort(path):
    with (AE_ROOT/path).open(newline='') as source:rows=list(csv.DictReader(source))
    for row in rows:
        path=AE_ROOT/row['local']
        if not path.is_file():raise FileNotFoundError(f'{path}; run prepare first')
        if digest(path)!=row['sha256']:raise ValueError(f'Input hash mismatch: {path}')
    return rows


BASELINE_44 = frozenset(('replay', 'criu', 'fc-diff'))


def baseline_rows(backend, rows, config):
    """Select the same fixed instances without truncating their trajectories."""
    mode = str(config.get('baseline_inputs', 'all'))
    if mode not in ('44', 'all'):
        raise ValueError('baseline_inputs must be 44 or all')
    if backend not in BASELINE_44 or mode == 'all':
        return rows, None
    path = AE_ROOT / 'paper/table-02/cohort-44.json'
    manifest = json.loads(path.read_text())
    selected = manifest.get('instances')
    if (manifest.get('schema_version') != 1 or not isinstance(selected, list)
            or len(selected) != 44 or any(not isinstance(i, str) for i in selected)
            or len(set(selected)) != 44):
        raise ValueError('The baseline-44 manifest must contain exactly 44 unique instances')
    by_id = {row['instance']: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError('Duplicate baseline instance')
    missing = set(selected) - set(by_id)
    if missing:
        raise ValueError('Baseline-44 inputs missing from ' + backend + ': ' + ', '.join(sorted(missing)))
    expected = manifest.get('input_sha256', {}).get(backend, {})
    if any(expected.get(i) != by_id[i]['sha256'] for i in selected):
        raise ValueError('Baseline-44 input binding differs for ' + backend)
    selection = dict(name='baseline-44', instances=44,
                     path='paper/table-02/cohort-44.json', sha256=digest(path))
    return [by_id[i] for i in selected], selection


def data_image(config,instance):
    overrides=config.get('instance_data_images',{})
    if not isinstance(overrides,dict):raise ValueError('instance_data_images must be an object')
    if instance in overrides:
        return configured_path({'image':overrides[instance],'_config_dir':config.get('_config_dir','.')},'image')
    family=instance.split('__')[0]
    group='django' if family=='django' else 'sympy' if family=='sympy' else 'sci' if family in ('astropy','matplotlib','pydata','scikit-learn') else 'tools'
    return configured_path(config,'images_dir')/('data-'+group+'.xfs')


def vm_flags(config):
    flags = ['--kernel',str(configured_path(config,'kernel')),'--base-xfs',str(configured_path(config,'base_xfs')),
            '--vcpus',str(config.get('vcpus',4)),'--mem-mib',str(config.get('mem_mib',8192)),
            '--image-hash-cache',str(AE_ROOT/'work/image-hashes.json'),
            '--checkpoint-profile',config.get('checkpoint_profile','runtime-default'),
            '--prewarm-policy',config.get('prewarm_policy','off')]
    flags += ['--storage-mode', config.get('vm_storage', 'disk')]
    if config.get('vm_work_dir'):
        flags += ['--work-dir', str(configured_path(config, 'vm_work_dir'))]
    if config.get('criu_dump_binary'):
        flags += ['--criu-dump-binary', str(configured_path(config, 'criu_dump_binary'))]
    return flags



def adaptive_guest_resources(config):
    """Plan equal memory for both Figure 6(b) arms without changing their method.

    The largest fixed trace keeps 184 checkpoints. Its observed near-full images,
    incremental images and CoW templates give a 14272 MiB planning envelope.
    16 GiB is a minimum to validate, not a bound on every possible runtime peak.
    The guest's existing 14 GiB noswap store is only a capacity ceiling: the
    nominal 2 GiB difference is shared working space, not a protected reservation.
    """
    minimum_mib = 16384
    configured_mib = config.get('mem_mib', 8192)
    if type(configured_mib) is not int or configured_mib <= 0:
        raise ValueError('mem_mib must be a positive integer')
    guest_mib = config.get('figure06_adaptive_mem_mib', max(configured_mib, minimum_mib))
    if type(guest_mib) is not int or guest_mib < minimum_mib:
        raise ValueError(
            'figure-06-adaptive requires figure06_adaptive_mem_mib >= 16384 '
            'for both complete standard/adaptive arms; snapshots and templates '
            'are retained and the 8192 MiB guest exhausted memory')
    effective = dict(config, mem_mib=guest_mib)
    resources = dict(
        guest_mem_mib=guest_mib,
        minimum_guest_mem_mib=minimum_mib,
        guest_snapshot_store_cap_mib=14336,
        nominal_non_store_mib=guest_mib - 14336,
        planning_envelope_mib=14272,
        planning_basis='70 x 96 MiB near-full images + 114 x 16 MiB incremental '
                       'images + 184 x 20 MiB templates + 2048 MiB guest working space',
        scope='Both Figure 6 adaptive comparison arms; no checkpoint or template pruning',
        limitation='Empirical capacity plan, not a strict peak-memory upper bound. '
                   'Snapshot tmpfs is not preallocated; nominal non-store space '
                   'is not an enforced or protected reservation.',
        host_admission='Existing NUMA-bound RAM image and full guest allocation checks '
                       'with the unchanged 2 GiB host reserve')
    return effective, resources

def replay_wait_budget(trace_dir, instance, *, legacy=False, max_events=None, legacy_timing_policy='recorded-wall'):
    """Sum the actual replay schedule, preserving recorded pacing and prefixes."""
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(REPO_ROOT / 'replay'))
        from legacy_schedule import convert_legacy
        from make_schedule import make_schedule
        with tempfile.TemporaryDirectory(prefix='deltabox-ae-budget-') as directory:
            path = Path(directory) / 'schedule.jsonl'
            if legacy:
                convert_legacy(trace_dir, instance, path, adaptive=False, timing_policy=legacy_timing_policy)
            else:
                make_schedule(trace_dir, instance, path, adaptive=False)
            events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    finally:
        sys.path[:] = old_path
    if max_events is not None:
        events = events[:max_events]
    waits = [event.get('latency_ms', 0) / 1000 for event in events if event['type'] == 'ckpt']
    if any(not math.isfinite(wait) or wait < 0 for wait in waits):
        raise ValueError('Scheduled replay waits must be finite and nonnegative')
    source = ('paper Figure 6: zero injected pacing; recorded intervals retained as diagnostic metadata' if legacy_timing_policy=='paper-zero'
              else 'runtime legacy schedule: recorded inter-transition elapsed, not isolated LLM RTT') if legacy else 'runtime standard schedule: recorded ms_trace durations and declared mean-fill policy'
    return dict(recorded_wait_s=math.fsum(waits), scheduled_events=len(events),
                scheduled_checkpoints=sum(event['type'] == 'ckpt' for event in events),
                scheduled_restores=sum(event['type'] == 'restore' for event in events),
                wait_source=source, legacy_timing_policy=legacy_timing_policy if legacy else None)


def build_jobs(experiments,config,config_path,output,limit=None,max_events=None):
    jobs=[];runner=AE_ROOT/'runners';python=sys.executable
    paper_e2b = config.get('e2b', {}).get('profile') == 'paper-nested'
    if paper_e2b and (list(experiments) != ['table-02-e2b'] or limit is not None or max_events is not None):
        raise ValueError('E2B paper profile requires all eight inputs without limits or mixed experiments')
    action_budget=float(config.get('timeout',14400))
    if not math.isfinite(action_budget) or action_budget<=0:raise ValueError('config timeout must be finite and positive')
    def add(experiment,key,command,inputs=(),timing=None):
        if not all(c not in key for c in ('/', '\\')) or key in ('.','..'):raise ValueError('unsafe job key')
        jobs.append({'experiment':experiment,'key':key,'command':list(map(str,command)),
                     'inputs':list(inputs),'run_purpose':'quick-check' if limit or max_events else 'full-cohort',
                     'timeout_s':math.ceil(action_budget)+(300 if timing else 0),
                     **(timing or {})})
    for experiment in experiments:
        if experiment not in EXPERIMENTS:raise ValueError(f'Unknown experiment {experiment}')
        if experiment in ('table-02-deltabox','table-03-slow','figure-06-memory','figure-06-adaptive'):
            vm_config, resources = (adaptive_guest_resources(config)
                                    if experiment == 'figure-06-adaptive' else (config, None))
            if experiment=='figure-06-memory':
                source='paper/figure-06/cohort-memory.csv'
                arms=config.get('figure06_memory_policies',['none','skip','gc','warm'])
                if (not isinstance(arms,list) or not arms or any(not isinstance(a,str) or a not in ('none','skip','gc','warm') for a in arms)
                        or len(arms)!=len(set(arms))):
                    raise ValueError('figure06_memory_policies must be a nonempty unique list of none/skip/gc/warm')
            elif experiment=='figure-06-adaptive':
                source='paper/figure-06/cohort-adaptive.csv'
                arms=config.get('figure06_adaptive_arms',['standard','adaptive'])
                if (not isinstance(arms,list) or not arms
                        or any(not isinstance(arm,str) or arm not in ('standard','adaptive') for arm in arms)
                        or len(set(arms))!=len(arms)):
                    raise ValueError('figure06_adaptive_arms must select unique standard/adaptive arms')
            else:source='paper/table-02/cohort-deltabox.csv';arms=['slow' if experiment=='table-03-slow' else 'fast']
            rows=cohort(source)
            if limit:rows=rows[:limit]
            for row in rows:
                instance=row['instance']
                timing=replay_wait_budget((AE_ROOT/row['local']).parent,instance,legacy=experiment=='figure-06-adaptive',max_events=max_events,
                                          legacy_timing_policy=config.get('legacy_timing_policy','paper-zero'))
                workload_timeout=math.ceil(action_budget+timing['recorded_wait_s'])
                timing.update(action_margin_s=action_budget,workload_timeout_s=workload_timeout,
                              startup_cleanup_margin_s=300,timeout_s=workload_timeout+300)
                for arm in arms:
                    key=experiment+'__'+row.get('pool','').replace('/','_')+'__'+instance+'__'+arm
                    cmd=[python,AE_ROOT.parent/'replay/run_instance.py','--instance',instance,'--experiment-id',experiment,'--trace-dir',(AE_ROOT/row['local']).parent,
                         '--timeout',str(workload_timeout),'--data-xfs',data_image(config,instance),'--mode','slow' if arm=='slow' else 'fast','--out',output/key,*vm_flags(vm_config)]
                    if arm=='adaptive':cmd+=['--adaptive']
                    if experiment=='figure-06-memory':cmd+=['--memory-policy',arm]
                    if experiment=='figure-06-adaptive':cmd+=['--trajectory-timing','--legacy-timing-policy',timing['legacy_timing_policy']]
                    if max_events:cmd+=['--max-events',str(max_events)]
                    add(experiment,key,cmd,[row['local']],timing=timing)
                    if resources is not None:
                        jobs[-1]['resources'] = dict(resources)
        elif experiment.startswith('table-02-') or experiment=='figure-01-cube':
            backend='cube' if experiment=='figure-01-cube' else experiment.removeprefix('table-02-')
            suffix='criu-attempts' if backend=='criu' else backend
            if paper_e2b:
                from ae.scripts.e2b_paper_profile import input_rows
                rows = input_rows(config)
                selection = dict(name='e2b-paper-original-eight', instances=8,
                                 expansions=227, actions=185,
                                 manifest=rows[0]['paper_manifest'], contract=rows[0]['paper_contract'])
            else:
                rows=cohort('paper/table-02/cohort-'+suffix+'.csv')
                rows, selection = baseline_rows(backend, rows, config)
            if limit:rows=rows[:limit]
            for row in rows:
                key=experiment+'__'+row['instance']
                cmd=[python,runner/'baseline.py','--backend',backend,'--instance',row['instance'],'--trace',AE_ROOT/row['local'],
                     '--config',config_path,'--out',output/key]
                if (backend=='replay' or paper_e2b) and row.get('repository_commit'):cmd+=['--repository-commit',row['repository_commit']]
                if backend=='cube':cmd+=['--schedule',AE_ROOT/row['schedule_local']]
                if experiment=='figure-01-cube':cmd+=['--collect-phases']
                elif backend=='cube' and config.get('cube', {}).get('profile') == 'paper-disk':
                    cmd+=['--collect-phases','--experiment-id','table-02-cube']
                if max_events:cmd+=['--limit',str(max_events)]
                add(experiment,key,cmd,[row['local']])
                if paper_e2b:
                    jobs[-1]['paper_contract'] = dict(expected_expansions=row['expected_expansions'],
                        expected_actions=row['expected_actions'], trajectory_sha256=row['sha256'],
                        repository_commit=row['repository_commit'], rtt=row['rtt'],
                        contract=row['paper_contract'])
                if selection:
                    jobs[-1]['input_selection'] = selection
                    if not limit and not max_events:
                        jobs[-1]['run_purpose'] = 'full-trace'
        elif experiment.startswith('figure-02-'):
            panel=experiment.removeprefix('figure-02-');rows=cohort('paper/figure-02/cohort-'+panel+'.csv')
            if limit:rows=rows[:limit]
            for row in rows:
                key=experiment+'__'+row['instance']
                add(experiment,key,[python,runner/'profile.py','--panel',panel,'--instance',row['instance'],
                    '--trace',AE_ROOT/row['local'],'--config',config_path,'--out',output/key],[row['local']])
        elif experiment=='figure-09':
            rows, selection = figure09_rows(cohort('paper/figure-09/cohort-war.csv'), config)
            if limit:rows=rows[:limit]
            for row in rows:
                for arm in ('ext4','xfs','xfs_reflink'):
                    input_key=row['pool'].replace('/','_')+'__'+row['instance'];key=experiment+'__'+input_key+'__'+arm
                    add(experiment,key,[python,runner/'vm_experiment.py','--experiment',experiment,'--actions',AE_ROOT/row['action_local'],
                        '--input-key',input_key,'--arm',arm,'--config',config_path,'--out',output/key],[row['action_local']])
                    jobs[-1]['input_selection'] = dict(selection)
                    jobs[-1]['expected_edits'] = int(row['n_edits'])
                    if not limit and not max_events:
                        jobs[-1]['run_purpose'] = 'full-trace'
        elif experiment in ('figure-08-deltabox','correctness'):
            cmd=[python,runner/'vm_experiment.py','--experiment',experiment,'--config',config_path,'--out',output/experiment]
            if experiment.startswith('figure-08') and max_events:cmd+=['--forks','1']
            add(experiment,experiment,cmd)
        else:
            backend=experiment.removeprefix('figure-08-')
            forks = config.get('cube', {}).get('fanout_forks', [1,16]) if backend == 'cube' else [1,4,16,64]
            if (not isinstance(forks, list) or not forks or
                    any(type(n) is not int or n not in (1,4,16,64) for n in forks) or len(set(forks)) != len(forks)):
                raise ValueError('Cube fanout_forks must select unique counts from 1,4,16,64')
            add(experiment,experiment,[python,runner/'fanout.py','--backend',backend,'--config',config_path,'--out',output/experiment,
                '--forks','1' if max_events else ','.join(map(str, forks))])
    return jobs
