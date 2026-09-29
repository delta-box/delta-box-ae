"""Table 2 analysis for original E2B inputs, with explicit validated sources.

Reference-only and mixed-source continuations have no copied run.json files.
This adapter resolves and validates their original measurements and aggregates
actual inner C/R events, keeping each input's original path and release.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from ae.repro.common import file_record
from ae.repro.e2b_reuse import (
    EXPERIMENT, bound, load_source, validate_measured_job, verify_referenced_job,
)
from ae.scripts.e2b_paper_profile import COHORT


def discover(input_root):
    root=Path(input_root).absolute()
    candidates=[root/'runs'/EXPERIMENT/'suite.json', root/EXPERIMENT/'suite.json']
    if root.name==EXPERIMENT:
        candidates.append(root/'suite.json')
    for path in candidates:
        if path.is_file():
            suite=json.loads(path.read_text())
            if suite.get('measurement_identity',{}).get('e2b_profile')=='paper-nested':
                return path.parent.parent.parent
    return None


def collect(source):
    source=Path(source).absolute();repo=Path(__file__).resolve().parents[2]
    suite=json.loads((source/'runs'/EXPERIMENT/'suite.json').read_text())
    context=load_source(source,suite,repo,analysis=True)
    entries=[];excluded=[]
    for job in suite['jobs']:
        if job.get('status')!='ok':
            excluded.append(dict(job=job['key'],status=job.get('status'),reason=job.get('reason') or job.get('paper_context_error')))
            continue
        referenced=bool(job.get('reused_verified'))
        receipt=(verify_referenced_job(job,suite) if referenced
                 else validate_measured_job(job,job,context,suite))
        run_path=Path(receipt['run']['path']);run=json.loads(run_path.read_text())
        pilot_path,record=bound(run_path.parent,run['result'],within=run_path.parent)
        entries.append(dict(receipt=receipt,pilot=json.loads(pilot_path.read_text()),
                            pilot_path=str(pilot_path),referenced=referenced))
    if not entries:
        raise ValueError('No complete validated paper E2B input is available')
    return entries,excluded,suite['release']


def build_summary(source,entries,excluded,planner_release):
    from ae.repro import analysis as common
    records=[];origins=[];sources={};audits=[]
    for entry in entries:
        receipt,pilot=entry['receipt'],entry['pilot']
        backend,record=common.parse_pilot(pilot)
        if backend!='e2b' or record['instance']!=receipt['instance']:
            raise ValueError('Validated E2B source and pilot identity differ')
        for key,op in [('checkpoints','checkpoint'),('restores','restore')]:
            if receipt['counts'][key]!=record[op][0]:
                raise ValueError('Validated E2B counts differ from raw pilot')
        records.append(record)
        origins.append(dict(instance=receipt['instance'],source_commit=receipt['release']['source_commit'],
            release=receipt['release'],run=receipt['run']['path'],pilot=entry['pilot_path'],
            referenced=entry['referenced'],counts=receipt['counts']))
        for rec in receipt['checks']:
            if rec['path'] in sources and sources[rec['path']]!=rec:
                raise ValueError('Source path has conflicting evidence hashes')
            sources[rec['path']]=rec
        for role in ('controller','worker'):
            audits.append(dict(instance=receipt['instance'],role=role,**pilot[role+'_mock_stats']))
    instances=[r['instance'] for r in records]
    if len(instances)!=len(set(instances)):
        raise ValueError('Duplicate original E2B input')
    pairs=sum(r['checkpoint'][0] for r in records)
    complete=set(instances)=={r[0] for r in COHORT} and pairs==185 and sum(r['restore'][0] for r in records)==185
    cohort='e2b-paper-original-eight-explicit-input-sources'
    metrics=[]
    for row in common.latency_metrics(records,'e2b',common.FRESH):
        commits=sorted({o['source_commit'] for o in origins if row['group']=='All' or common.group_for(o['instance'])==row['group']})
        metrics.append(dict(row,experiment=EXPERIMENT,cohort=cohort,plot_group=cohort,
                            run_purpose='full-trace',message_policy='strict',e2b_worker_mode='cold',
                            source_identity='explicit-input-sources',source_commits=','.join(commits)))
    limitations=[
        'Each input is revalidated against its original raw files and original release; references are not relabelled as new measurements.',
        'The explicit per-input source manifest governs this event-weighted aggregate; it is not a single-binary result.',
        'Checkpoint is Pause plus local snapshot upload; restore is Factory.ResumeSandbox return latency. Root setup and CLI elapsed time are excluded.',
        'The recovered original input cohort is validated; exact original deployed binary and outer-L1 configuration remain unconfirmed.',
        'This selected paper-nested profile reports Table2 C/R measurements; it does not synthesize new Figure7 warm-worker models.',
    ]
    selection=dict(actual_run_count=len(records),actual_instance_count=len(instances),actual_instances=instances,
        new_run_count=sum(not r['referenced'] for r in origins),referenced_run_count=sum(r['referenced'] for r in origins),
        excluded_run_count=len(excluded),checkpoint_restore_pairs=pairs,paper_cohort_verified=complete,
        run_purpose='full-trace',reason='Validated original-eight C/R cohort with explicit per-input sources' if complete else 'Only completed original inputs; incomplete inputs and prefixes are excluded',
        populations=[dict(cohort=cohort,actual_run_count=len(records),actual_instance_count=len(instances),
                          actual_instances=instances,paper_cohort_verified=complete,run_purpose='full-trace')])
    results={name:common.unavailable('Outside this selected paper E2B C/R measurement scope.') for name in common.ARCHIVED}
    results['table-02']=dict(status='analyzed',metrics=metrics,series=[],selection=dict(runs=origins,**selection),
        limitations=limitations,replay_audits=audits,sources=sorted(sources),measurement_sources=origins)
    return dict(schema_version=1,source='fresh',analysis_mode='fresh-run-analysis',input_root=str(source),
        producer=file_record(Path(__file__).resolve()),sources=[dict(r,manifest_verified=True) for r in sources.values()],
        experiments=results,selection=selection,excluded_runs=excluded,planner_release=planner_release,
        measurement_sources=origins,skipped=[dict(experiment='other-experiments',reason='Not selected in the paper E2B profile')])


def analyze(source,output=None):
    from ae.repro.analysis import export_summary
    entries,excluded,release=collect(source)
    summary=build_summary(source,entries,excluded,release)
    return export_summary(summary,output) if output is not None else summary
