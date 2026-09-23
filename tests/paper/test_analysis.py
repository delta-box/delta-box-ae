"""Evidence binding, cohort isolation and paper aggregation regression checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ae.repro.analysis import analyze, analyze_fresh, aggregate_war, bin_for, order_percentile
from ae.repro.common import AE_ROOT


def write(path, data, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r)+'\n' for r in data) if jsonl else json.dumps(data))


def manifest(root, name, config, artifacts):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for relative, (data, is_jsonl) in artifacts.items():
        path = directory / relative
        write(path, data, is_jsonl)
        raw = path.read_bytes()
        records.append({'path': relative, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
    document = dict(analysis_mode='fresh-measurement', status='ok', artifacts=records, **config)
    write(directory/'run.json', document)
    return directory, document


def delta(root, name, *, experiment='table-02-deltabox', profile='historical-async-full', purpose='full-trace',
          adaptive=False, memory_policy=None, checkpoint=10., bootstrap=False, strategies=None):
    instance = 'sympy__sympy-22840'
    strategies = strategies or ['standard']
    schedule, rows = [], []
    for index, strategy in enumerate(strategies):
        is_bootstrap = bootstrap and index == 0
        schedule.append(dict(type='ckpt', ckpt_id=str(index), bootstrap=is_bootstrap))
        rows.append(dict(kind='ckpt', ev_i=index, schedule_ckpt_id=str(index), agent_mode='real', require_real_agent=True,
                         bootstrap=is_bootstrap, strategy=strategy, checkpoint_api_wall_ms=checkpoint,
                         ckpt_wall_ms=checkpoint-1, checkpoint_sync_no_dump_ms=checkpoint-2,
                         worker_exec={'ok':True,'wall_ms':3.}, worker_index_status={'ok':True}, latency_ms=5.))
        if memory_policy:
            rows.append(dict(kind='memcurve', ev_i=index, after='ckpt', snapshot_tmpfs_bytes=1<<20,
                             templates_pss_kb=1024*(index+1), active_pss_kb=2048))
    index = len(strategies)
    schedule.append(dict(type='restore', restore_to_ckpt_id='0'))
    rows.append(dict(kind='restore', ev_i=index, schedule_target_id='0', agent_mode='real', require_real_agent=True,
                     restore_api_wall_ms=4., restore_wall_ms=3., worker_index_status_after_restore={'matches_target_ckpt':True}))
    rows.append(dict(kind='run_summary', error_n=0, worker_index_loaded=True, worker_exec_required=True))
    directory = root/name
    write(directory/'schedule.jsonl', schedule, True)
    schedule_path = directory/'schedule.jsonl'
    return manifest(root, name, dict(experiment=experiment, instance=instance, mode='fast', checkpoint_profile=profile,
         adaptive=adaptive, memory_policy=memory_policy, run_purpose=purpose,
         n_ckpt=len(strategies), n_restore=1, schedule=str(schedule_path),
         schedule_sha256=hashlib.sha256(schedule_path.read_bytes()).hexdigest()),
         {instance+'.results.jsonl':(rows,True)})


def fanout(root, backend, *, name=None, forks=(1,4,16,64)):
    relative = 'measurements/fanout.json' if backend == 'deltabox' else 'fanout.json'
    return manifest(root, name or backend, dict(experiment='figure-08-'+backend,
         **{('forks' if backend == 'deltabox' else 'expected_forks'):list(forks)}),
         {relative:([{'forks':n,'success':True,'success_count':n,'ready_e2e_ms':n*3.} for n in forks],False)})


class FreshAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_audit_difference_survives_analysis_with_explicit_warning(self):
        from ae.repro.replay_audit import summarize
        import csv
        report = dict(ok=True, schema_version=1, message_policy='audit',
                      stats=dict(message_policy='audit', n_mismatch=2, n_protocol_errors=0))
        summary = summarize([report], 'audit')
        pilot = dict(instance='sympy__sympy-22840', ok=True, requested_restores=1, completed_restores=1)
        directory, config = manifest(self.root, 'audited',
            dict(experiment='table-02-replay', backend='replay', instance=pilot['instance'],
                 counts=dict(checkpoints=1, restores=1), message_policy='audit', replay_audit=summary),
            {'results/summary.json': (pilot, False), 'results/restore_000.mock_audit.json': (report, False)})
        row = dict(instance=pilot['instance'], ok='True', rc='0', mock_mismatch=2,
                   mock_protocol_errors=0, message_policy='audit', target_expansions=13,
                   restore_index=0, copytree_ms=2, restore_ms=20, replay_ms=18,
                   mock_sleep_ms=10, restore_zero_llm_ms=10, rmtree_ms=0, replay_zero_llm_ms=8)
        path = directory / 'results/restores.csv'
        with path.open('w') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader(); writer.writerow(row)
        raw = path.read_bytes()
        config['artifacts'].append(dict(path='results/restores.csv', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        config['result'] = config['artifacts'][0]
        write(directory / 'run.json', config)
        result = analyze_fresh(self.root)['experiments']['table-02']
        self.assertEqual(result['selection']['runs'][0]['replay_audit']['n_mismatch'], 2)
        self.assertTrue(any('message differences' in text for text in result['limitations']))
        self.assertTrue(all(row['message_policy'] == 'audit' for row in result['metrics']))
        from ae.repro.plot import audit_note
        phase = analyze_fresh(self.root)['experiments']['figure-01']
        self.assertIn('2 message differences', audit_note(phase))
        self.assertTrue(any('message differences' in text for text in phase['limitations']))
        self.assertEqual(audit_note(dict(phase, metrics=[dict(plot_group='another population')], series=[])), '')
        config['replay_audit']['n_mismatch'] = 0
        write(directory / 'run.json', config)
        with self.assertRaisesRegex(ValueError, 'audit summary'):
            analyze_fresh(self.root)

    def test_successful_fresh_manifest_is_required(self):
        directory, config = fanout(self.root,'cube')
        for field, invalid in (('analysis_mode','archived-data-analysis'),('status','failed'),('artifacts',[])):
            with self.subTest(field=field):
                document = dict(config, **{field:invalid})
                write(directory/'run.json',document)
                with self.assertRaises(ValueError): analyze_fresh(self.root)
        write(directory/'run.json',config)
        self.assertEqual(analyze_fresh(self.root)['source'],'fresh')

    def test_diagnostic_instrumentation_is_not_a_performance_sample(self):
        directory, config = delta(self.root, 'delta')
        for key in ('DELTABOX_API_PROFILE', 'DELTABOX_DUMP_DIAGNOSTICS', 'DELTABOX_RESTORE_DIAGNOSTICS'):
            with self.subTest(key=key):
                write(directory/'run.json', dict(config, guest_env={key:'1'}))
                with self.assertRaisesRegex(ValueError, 'Diagnostic instrumentation'):
                    analyze_fresh(self.root)
                write(directory/'run.json', dict(config, guest_env={key:'0'}))
                self.assertEqual(analyze_fresh(self.root)['source'], 'fresh')

    def test_reject_altered_size_and_digest(self):
        directory, config = fanout(self.root,'cube')
        path=directory/'fanout.json'
        original=path.read_bytes()
        path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'bytes/SHA-256'): analyze_fresh(self.root)
        path.write_bytes(original)
        config['artifacts'][0]['bytes'] += 1
        write(directory/'run.json',config)
        with self.assertRaisesRegex(ValueError,'bytes/SHA-256'): analyze_fresh(self.root)

    def test_reject_unbound_raw_even_beside_valid_run(self):
        fanout(self.root,'cube')
        write(self.root/'orphan/pilot_result.json',{'ok':True})
        with self.assertRaisesRegex(ValueError,'Orphan'): analyze_fresh(self.root)

    def test_reject_archive_or_absolute_artifact_paths(self):
        write(self.root/'pilot_result.json',{'ok':True})
        with self.assertRaisesRegex(ValueError,'manifest-bound'): analyze_fresh(self.root)
        (self.root/'pilot_result.json').unlink()
        directory, config=fanout(self.root,'cube')
        config['artifacts'][0]['path']=str(directory/'fanout.json')
        write(directory/'run.json',config)
        with self.assertRaisesRegex(ValueError,'relative'): analyze_fresh(self.root)

    def test_reject_symlink_outside_run(self):
        directory,config=fanout(self.root,'cube')
        path=directory/'fanout.json';external=self.root/'outside.json'
        path.rename(external);path.symlink_to(external)
        with self.assertRaisesRegex(ValueError,'escapes'):analyze_fresh(self.root)

    def test_distinct_delta_profiles_purposes_not_pooled(self):
        delta(self.root,'historical',checkpoint=10.)
        delta(self.root,'runtime',profile='runtime-default',checkpoint=30.)
        delta(self.root,'quick-check',purpose='quick-check',checkpoint=100.)
        delta(self.root,'adaptive',experiment='figure-06-adaptive',adaptive=True,checkpoint=300.)
        summary=analyze_fresh(self.root, self.root/'analysis')
        rows=[r for r in summary['experiments']['table-02']['metrics'] if r['group']=='All' and r['metric']=='checkpoint_ms']
        self.assertEqual(sorted(r['value'] for r in rows),[10.,30.,100.])
        self.assertEqual(len({r['cohort'] for r in rows}),3)
        self.assertTrue(all(r['n']==1 for r in rows))
        self.assertEqual(summary['experiments']['figure-01']['status'],'unavailable')
        self.assertTrue(all(r['evidence_kind']=='derived_model' for r in summary['experiments']['figure-07']['metrics']))
        self.assertTrue((self.root/'analysis/metrics.csv').is_file())

    def test_duplicate_delta_within_population_rejected(self):
        delta(self.root,'one');delta(self.root,'two')
        with self.assertRaisesRegex(ValueError,'Duplicate'): analyze_fresh(self.root)

    def test_schedule_hash_and_order_checked(self):
        directory,config=delta(self.root,'delta')
        with (directory/'schedule.jsonl').open('a') as stream:stream.write('\n')
        with self.assertRaisesRegex(ValueError,'schedule SHA-256'):analyze_fresh(self.root)

    def test_memory_and_adaptive_bootstrap_rules(self):
        delta(self.root,'none',experiment='figure-06-memory',memory_policy='none',bootstrap=True,strategies=['standard','standard'])
        delta(self.root,'skip',experiment='figure-06-memory',memory_policy='skip',bootstrap=True,strategies=['standard','standard'])
        delta(self.root,'adaptive',experiment='figure-06-adaptive',adaptive=True,bootstrap=True,strategies=['standard','lightweight','standard'])
        delta(self.root,'standard',experiment='figure-06-adaptive',bootstrap=True,strategies=['standard','standard','standard'])
        result=analyze_fresh(self.root)['experiments']['figure-06']
        memory=[r for r in result['series'] if r['panel']=='a']
        self.assertEqual([r['y'] for r in memory],[4.,5.,4.,5.])
        populations={r['arm']:r['n'] for r in result['metrics'] if r['panel']=='b'}
        self.assertEqual(populations,{'adaptive_lightweight':1,'adaptive_standard':1,'standard_only':2})
        self.assertTrue(all(v['status']=='analyzed' for v in result['panels'].values()))
        self.assertEqual(analyze_fresh(self.root)['experiments']['table-02']['status'],'unavailable')

    def test_all_cpu_fanout_backends_measured(self):
        for backend in ('deltabox','cube','e2b'):fanout(self.root,backend)
        result=analyze_fresh(self.root)['experiments']['figure-08']
        self.assertEqual(len(result['series']),12)
        self.assertEqual(result['selection']['backends'],['cube','deltabox','e2b'])
        self.assertTrue(all(not row['estimated'] for row in result['series']))
        fanout(self.root,'e2b',name='duplicate')
        with self.assertRaisesRegex(ValueError,'Duplicate'):analyze_fresh(self.root)

    def test_failed_child_or_estimate_not_fresh(self):
        directory,config=fanout(self.root,'e2b')
        rows=json.loads((directory/'fanout.json').read_text());rows[0]['estimated']=True
        manifest(self.root,'e2b',{k:v for k,v in config.items() if k not in ('artifacts','status','analysis_mode')},{'fanout.json':(rows,False)})
        with self.assertRaisesRegex(ValueError,'estimate'):analyze_fresh(self.root)

    def test_profiles_use_measured_fs_total_and_separate_purposes(self):
        for purpose,value in [('full-trace',2048),('quick-check',4096)]:
            manifest(self.root,purpose,dict(experiment='figure-02-filesystem',instance='sympy__sympy-22840',
                     step_count=2,rss_sample_count=1,run_purpose=purpose,filesystem_baseline_bytes=value),
                     {'step_metrics.jsonl':([dict(soft_dirty_bytes=1024,action_write_bytes=0),dict(soft_dirty_bytes=2048,action_write_bytes=value)],True),
                      'tree_rss_samples.json':([dict(rss_kb_total=1024)],False),'mock_stats.json':(dict(cursor=2,total=2,n_mismatch=0),False)})
        result=analyze_fresh(self.root)['experiments']['figure-02']
        totals=[r for r in result['metrics'] if r['metric']=='total']
        self.assertEqual(sorted(r['value'] for r in totals),[2048.,4096.])
        self.assertTrue(all(r['n']==1 and r['unit']=='bytes' for r in totals))

    def test_war_pool_identity_and_no_historical_annotation(self):
        for pool in ('claude','mimo'):
            key=pool+'__sympy__sympy-22840'
            manifest(self.root,pool,dict(experiment='figure-09',input_key=key,arm='xfs',expected_edits=1),
                     {'measurements/'+key+'_xfs.jsonl':([dict(instance=key,fs_arm='xfs',applied_ok=True,file_path='same.py',
                         file_size_bytes=4096,copyup_bytes=8192,phys_bytes=16384)],True)})
        result=analyze_fresh(self.root)['experiments']['figure-09']
        population=next(iter(result['selection'].values()))
        self.assertEqual(population['arms']['xfs']['grouped_units'],2)
        self.assertIs(result['historical_annotation'],False)

    def test_war_disjoint_quick_check_and_full_inputs_are_separate_populations(self):
        for purpose,value in [('quick-check',10),('full-cohort',100)]:
            key=purpose+'__sympy__sympy-22840'
            manifest(self.root,purpose,dict(experiment='figure-09',input_key=key,arm='xfs',expected_edits=1,run_purpose=purpose),
                     {'measurements/'+key+'_xfs.jsonl':([dict(instance=key,fs_arm='xfs',applied_ok=True,file_path='same.py',
                         file_size_bytes=4096,copyup_bytes=value,phys_bytes=2*value)],True)})
        result=analyze_fresh(self.root)['experiments']['figure-09']
        rows=[row for row in result['series'] if row['metric']=='copyup_bytes' and row['n_units']]
        self.assertEqual({row['run_purpose']:row['y'] for row in rows},{'quick-check':10.,'full-cohort':100.})
        self.assertEqual(len({row['cohort'] for row in rows}),2)
        self.assertEqual(len({row['plot_group'] for row in rows}),2)
        self.assertTrue(all(row['n_units']==1 for row in rows))
        self.assertEqual({row['run_purpose'] for row in result['selection'].values()},{'quick-check','full-cohort'})
        self.assertEqual(len({row['plot_group'] for row in result['selection'].values()}),2)

    def test_war_same_input_allowed_across_purposes_but_not_within_population(self):
        key='claude__sympy__sympy-22840'
        for name,purpose in [('quick-check','quick-check'),('full','full-cohort')]:
            manifest(self.root,name,dict(experiment='figure-09',input_key=key,arm='xfs',expected_edits=1,run_purpose=purpose),
                     {'measurements/'+key+'_xfs.jsonl':([dict(instance=key,fs_arm='xfs',applied_ok=True,file_path='same.py',
                         file_size_bytes=4096,copyup_bytes=10,phys_bytes=20)],True)})
        result=analyze_fresh(self.root)['experiments']['figure-09']
        self.assertEqual(len(result['selection']),2)
        manifest(self.root,'duplicate',dict(experiment='figure-09',input_key=key,arm='xfs',expected_edits=1,run_purpose='full-cohort'),
                 {'measurements/'+key+'_xfs.jsonl':([dict(instance=key,fs_arm='xfs',applied_ok=True,file_path='same.py',
                     file_size_bytes=4096,copyup_bytes=10,phys_bytes=20)],True)})
        with self.assertRaisesRegex(ValueError,'duplicate fresh WAR'):analyze_fresh(self.root)

    def test_portable_bound_schedule_and_relative_baseline_result(self):
        original=self.root/'original'
        directory,config=delta(original,'delta')
        schedule_path=directory/'schedule.jsonl'
        raw=schedule_path.read_bytes()
        config['schedule_artifact']='schedule.jsonl'
        config['artifacts'].append({'path':'schedule.jsonl','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        write(directory/'run.json',config)
        data=dict(ok=True,instance='sympy__sympy-22840',ckpts=[dict(checkpoint_total_ms=10.,fs_checkpoint_ms=3.,criu_dump_ms=7.)],
                  restore_events=[dict(restore_total_ms=20.,fs_restore_ms=4.,criu_restore_ms=10.)])
        base,document=manifest(original,'criu',dict(experiment='table-02-criu',backend='criu',instance=data['instance'],
                         counts={'checkpoints':1,'restores':1},run_purpose='quick-check'),{'pilot_result.json':(data,False)})
        document['result']=dict(document['artifacts'][0])
        write(base/'run.json',document)
        moved=self.root/'moved'
        original.rename(moved)
        self.assertFalse(schedule_path.exists())
        summary=analyze_fresh(moved)
        self.assertEqual(summary['selection']['actual_run_count'],2)
        self.assertEqual(summary['selection']['actual_instance_count'],1)
        self.assertFalse(summary['selection']['paper_cohort_verified'])
        self.assertIn('未验证论文完整 cohort',summary['selection']['reason'])
        values={r['backend']:r['value'] for r in summary['experiments']['table-02']['metrics']
                if r['group']=='All' and r['metric']=='checkpoint_ms'}
        self.assertEqual(values,{'criu':10.,'deltabox':10.})

    def test_bound_schedule_does_not_fall_back_to_absolute_copy(self):
        directory,config=delta(self.root,'delta')
        config['schedule_artifact']='unbound-schedule.jsonl'
        write(directory/'run.json',config)
        with self.assertRaisesRegex(ValueError,'not bound'):analyze_fresh(self.root)

    def test_keep_going_success_and_non_success_manifests(self):
        delta(self.root,'good',purpose='quick-check')
        for status in ('failed','preparing','prepared','planned','cancelled'):
            directory=self.root/status
            write(directory/'run.json',dict(analysis_mode='fresh-measurement',experiment='table-02-deltabox',
                                           status=status,instance='sympy__sympy-22840',run_purpose='quick-check',error='injected '+status))
            # Incomplete output is never parsed or promoted to measurements.
            (directory/'incomplete.results.jsonl').write_text('{incomplete output')
        summary=analyze_fresh(self.root)
        self.assertEqual(summary['selection']['actual_run_count'],1)
        self.assertEqual(summary['selection']['excluded_run_count'],5)
        self.assertEqual({r['status'] for r in summary['excluded_runs']},{'failed','preparing','prepared','planned','cancelled'})
        self.assertTrue(all(r['error']=='injected '+r['status'] for r in summary['excluded_runs']))
        rows=[r for r in summary['experiments']['table-02']['metrics'] if r['group']=='All' and r['metric']=='checkpoint_ms']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['n'],1)
        write(self.root/'orphan/pilot_result.json',{'ok':True})
        with self.assertRaisesRegex(ValueError,'Orphan'):analyze_fresh(self.root)

    def test_non_success_manifest_cannot_hide_archive(self):
        fanout(self.root,'cube')
        write(self.root/'failed/run.json',dict(analysis_mode='archived-data-analysis',experiment='table-02-criu',status='failed'))
        with self.assertRaisesRegex(ValueError,'not fresh-measurement'):analyze_fresh(self.root)

    def test_no_successful_runs_still_rejected(self):
        for status in ('failed','cancelled'):
            write(self.root/status/'run.json',dict(analysis_mode='fresh-measurement',experiment='figure-08-cube',status=status))
        with self.assertRaisesRegex(ValueError,'No successful.*2 non-successful'):analyze_fresh(self.root)

    def test_declared_full_cohort_is_not_coverage_proof(self):
        delta(self.root,'partial',purpose='full-cohort')
        delta(self.root,'quick-check',purpose='quick-check',checkpoint=30.)
        selection=analyze_fresh(self.root)['selection']
        self.assertFalse(selection['paper_cohort_verified'])
        self.assertEqual(selection['actual_run_count'],2)
        self.assertEqual(selection['actual_instance_count'],1)
        self.assertEqual({r['run_purpose'] for r in selection['populations']},{'quick-check','full-cohort'})
        self.assertTrue(all(r['actual_instance_count']==1 and not r['paper_cohort_verified'] for r in selection['populations']))

    def test_cube_control_and_unclassified_must_close(self):
        self.test_actual_baseline_phase_components()
        directory=self.root/'cube'
        path=directory/'cube_phases.json'
        data=json.loads(path.read_text())
        data['events'][1]['unclassified_ms']+=.0001
        write(path,data)
        config=json.loads((directory/'run.json').read_text())
        for record in config['artifacts']:
            if record['path']=='cube_phases.json':
                raw=path.read_bytes();record.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        write(directory/'run.json',config)
        with self.assertRaisesRegex(ValueError,'Cube phase sum'):analyze_fresh(self.root)

    def test_actual_baseline_phase_components(self):
        instance='sympy__sympy-22840'
        pilots={
          'criu':dict(ok=True,instance=instance,ckpts=[dict(checkpoint_total_ms=10.,fs_checkpoint_ms=3.,criu_dump_ms=7.)],
                      restore_events=[dict(restore_total_ms=20.,fs_restore_ms=4.,criu_restore_ms=10.)]),
          'fc-diff':dict(ok=True,instance=instance,ckpts=[dict(fc_total_ms=7.,dm_snapshot_ms=3.)],
                        restore_events=[dict(load={'fc_load_ms':10.},dm_restore={'dm_restore_ms':4.},merge={'merge_ms':6.})]),
          'e2b':dict(ok=True,instance=instance,iterations=[dict(ok=True,e2b_steps=[dict(ok=True,checkpoint_persist_ms=10.,
                     pause_ms=7.,snapshot_upload_ms=3.,resume_ms=20.)])]),
          'cube':dict(ok=True,instance=instance,iterations=[dict(ok=True,kind='ckpt',ev_i=0,snapshot_id='one',checkpoint_wall_ms=10.),
                      dict(ok=True,kind='restore',ev_i=1,snapshot_id='one',restore_wall_ms=20.)])}
        for backend,data in pilots.items():
            artifacts={'pilot_result.json':(data,False)}
            if backend=='cube':
                artifacts['cube_phases.json']=(dict(events=[dict(event_index=i,kind=kind,snapshot_id='one',sandbox_id='box',
                  wall_ms=total,filesystem_ms=3.,process_ms=5.,other_ms=total-8.-.386,unclassified_ms=.386,phase_names=['measured'])
                  for i,(kind,total) in enumerate([('ckpt',10.),('restore',20.)])]),False)
            directory,config=manifest(self.root,backend,dict(experiment='figure-01-cube' if backend=='cube' else 'table-02-'+backend,
                  backend=backend,instance=instance,counts={'checkpoints':1,'restores':1},run_purpose='full-trace'),artifacts)
            config['result']=dict(config['artifacts'][0],path=str(directory/'pilot_result.json'));write(directory/'run.json',config)
        result=analyze_fresh(self.root)['experiments']['figure-01']
        self.assertEqual(result['missing_backends'],['replay'])
        values={(r['backend'],r['operation'],r['metric']):r['value'] for r in result['metrics']}
        self.assertEqual(values['criu','restore','unclassified_api'],6.)
        self.assertAlmostEqual(values['cube','restore','control_plane'],11.614)
        self.assertAlmostEqual(values['cube','restore','unclassified_api'],.386)
        self.assertEqual(values['fc-diff','restore','memory_merge'],6.)
        self.assertEqual(values['e2b','checkpoint','pause'],7.)
        self.assertNotIn(('e2b','checkpoint','process'),values)
        self.assertTrue(all(r['evidence_kind']=='fresh_raw_events' for r in result['metrics']))

    def test_replay_correction_is_measured_and_validated(self):
        directory=self.root/'replay';result_path=directory/'results/summary.json'
        document=dict(instance='sympy__sympy-22840',ok=True,requested_restores=1,completed_restores=1)
        manifest(self.root,'replay',dict(experiment='table-02-replay',backend='replay',instance=document['instance'],
                 counts={'checkpoints':1,'restores':1},run_purpose='full-trace'),{'results/summary.json':(document,False)})
        csv_path=directory/'results/restores.csv'
        csv_path.write_text('instance,ok,rc,mock_mismatch,restore_index,copytree_ms,restore_ms,replay_ms,mock_sleep_ms,restore_zero_llm_ms,rmtree_ms,replay_zero_llm_ms\n'+
                           document['instance']+',True,0,0,0,2,20,18,10,10,0,8\n')
        config=json.loads((directory/'run.json').read_text())
        raw=result_path.read_bytes();config['result']={'path':str(result_path),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
        raw=csv_path.read_bytes();config['artifacts'].append({'path':'results/restores.csv','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
        write(directory/'run.json',config)
        result=analyze_fresh(self.root)['experiments']['table-02']
        values={r['metric']:r['value'] for r in result['metrics'] if r['group']=='All'}
        self.assertEqual(values['restore_ms'],10.)
        self.assertEqual(values['restore_raw_api_wall_ms'],20.)
        self.assertEqual(values['mock_sleep_ms'],10.)
        phases=analyze_fresh(self.root)['experiments']['figure-01']
        self.assertEqual(next(r['value'] for r in phases['metrics'] if r['operation']=='restore' and r['metric']=='replay'),8.)
        csv_path.write_text(csv_path.read_text().replace(',18,10,10',',18,30,10'))
        raw=csv_path.read_bytes();config['artifacts'][-1].update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw));write(directory/'run.json',config)
        with self.assertRaisesRegex(ValueError,'correction'):analyze_fresh(self.root)


class AggregationTests(unittest.TestCase):
    def test_war_two_stage_order_statistics_and_bin_edges(self):
        self.assertEqual(order_percentile([1,2,3,4],50),3)
        self.assertIsNone(bin_for(1023))
        self.assertEqual(bin_for(1024),0)
        self.assertEqual(bin_for(8*1024),1)
        self.assertIsNone(bin_for(256*1024))
        rows=[dict(instance=pool,file_path='f',applied_ok=True,file_size_bytes=4096,copyup_bytes=cu,phys_bytes=cu)
              for pool in ('a','b') for cu in (10,20)]
        series,selection=aggregate_war({'xfs':rows})
        self.assertEqual(selection['xfs']['grouped_units'],2)
        self.assertEqual(series[0]['y'],20.)

    @unittest.skipUnless((AE_ROOT/'paper/table-02/data').exists(),'import bundled archive to run its invariants')
    def test_archive_denominators_and_evidence_tags(self):
        summary=analyze()
        self.assertEqual(summary['analysis_mode'],'archived-data-analysis')
        table=summary['experiments']['table-02']
        all_rows={(r['backend'],r['metric']):r for r in table['metrics'] if r['group']=='All'}
        for backend,counts in {'deltabox':(317,334),'cube':(317,334),'e2b':(185,185),'fc-diff':(7093,6518),'criu':(7123,6546),'replay':(244,6606)}.items():
            self.assertEqual(tuple(all_rows[backend,op+'_ms']['n'] for op in ('checkpoint','restore')),counts)
        self.assertAlmostEqual(all_rows['deltabox','checkpoint_ms']['value'],8.32461483471025)
        slow=summary['experiments']['table-03']['selection']['slow'];self.assertEqual(slow['complete_runs'],8)
        fig6=summary['experiments']['figure-06'];self.assertEqual(fig6['selection']['population_counts'],{'adaptive_lightweight':831,'adaptive_standard':250,'standard_only':1081})
        estimated=[r for r in summary['experiments']['figure-08']['series'] if r.get('estimated')]
        self.assertEqual(len(estimated),1);self.assertEqual(estimated[0]['evidence_kind'],'derived_model')
        for selection in summary['experiments']['figure-09']['selection'].values():
            self.assertEqual((selection['applied_ok'],selection['grouped_units'],selection['plotted_units']),(603,183,180))


if __name__=='__main__':unittest.main()
