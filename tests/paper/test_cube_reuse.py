"""Cube imports preserve original bytes and reject incomplete/mutated evidence."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae')]
from ae.repro import cube_reuse as reuse
from ae.repro.common import file_record, write_json
from ae.runners import cube_phases
from ae.scripts.cube_paper_profile import validate as validate_profile
from tests.paper.test_cube_paper_phase_capture import event, checkpoint, restore

OLD = dict(source_commit='a'*40, source_sha256='1'*64)
NEW = dict(source_commit='b'*40, source_sha256='2'*64)


class CubeReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / 'old'
        self.dest = self.base / 'new/runs/table-02-cube'
        self.instance = 'test'
        self.key = 'table-02-cube__test'
        self.root = self.source / 'runs/table-02-cube' / self.key
        (self.root / 'process').mkdir(parents=True)
        self.trace = self.base / 'trace.json'
        self.trace.write_text('{}')
        self.schedule = self.base / 'schedule.jsonl'
        self.schedule_rows = [dict(type='ckpt', ckpt_id=0, node_id=0, worker_ops=[]),
            dict(type='ckpt', ckpt_id=1, node_id=1, worker_ops_required=True,
                 worker_ops=[dict(type='action', action_class='Edit', action_step_idx=1)]),
            dict(type='restore', restore_to_ckpt_id=0, node_id=0)]
        self.schedule.write_text(''.join(json.dumps(x)+'\n' for x in self.schedule_rows))
        events = [event(), event('ckpt', 200, 300, 1), event('restore', 400, 500, 2)]
        for e, expected in zip(events, self.schedule_rows):
            e.update(agent_mode='real', require_real_agent=True, node_id=expected['node_id'])
            if e['kind'] == 'ckpt':
                e.update(ckpt_id=expected['ckpt_id'], worker_ops_n=len(expected['worker_ops']))
                e['snapshot']['api_retries'] = 0
            else:
                e['restore_to_ckpt_id'] = expected['restore_to_ckpt_id']
                e['cube_steps'][0].update(ok=True, rollback_response={'status':'READY'})
        events[1].update(action_step_idx=1, cube_steps=[{'ok':True}],
            action_response=dict(ok=True,event=dict(worker_ops_ok=True,
                worker_results=[dict(type='action',action_class='Edit',ok=True)])))
        self.pilot = dict(ok=True, status='ok', instance='test', schedule_sha256=file_record(self.schedule)['sha256'],
            warm_action_worker=True, llm_replay_mode='schedule_latency_sleep', template=reuse.TEMPLATE,
            measurement_scope='host-side CubeSandbox SDK API elapsed time', n_schedule_events=3,
            n_ckpt_events=2, n_restore_events=1, iterations=events)
        self.pilot_path = self.root / 'pilot_result.json'
        write_json(self.pilot_path, self.pilot)
        log = self.root / 'service-source.log'
        log.write_text('')
        start = cube_phases.begin(log)
        more = checkpoint()
        for row in more:
            row['startUnixNs'] += 200_000_000
            row['endUnixNs'] += 200_000_000
        log.write_text('\n'.join(json.dumps(x) for x in checkpoint()+more+restore(400))+'\n')
        self.capture = cube_phases.collect(start, self.pilot_path, self.root/'cubelet-phases.log', strict=True)
        write_json(self.root/'cube_phases.json', self.capture)
        self.service = self.source/'environment/attempt-001/cube-disk/storage.json'
        identity = dict(profile='paper-disk', disk={'physical_disks':[{'serial':'disk-test'}]})
        write_json(self.service, dict(profile='paper-disk',identity=identity,service_pid=123))
        write_json(self.service.parent/'restored.json',dict(ActiveState='active', override_removed=True,
            cleanup_errors=[], readiness=dict(ready=True,idle=True)))
        proof = dict(profile='paper-disk', node=2, runner_cpus='48-51', service_cpus='48-71',
            paths={'/data/cubelet/storage':{'fstype':'xfs'}},
            workspace_disk={'mount':{'fstype':'ext4'},'physical_disks':[{'serial':'disk-test'}]},
            capacity=dict(reserve_bytes=10*1024**3,available_bytes=20*1024**3,required_bytes=10*1024**3),
            manifest=file_record(self.service),identity=identity,service_pid=123,loop={'name':'/dev/loop1'})
        for name in ('cube_disk_before.json','cube_disk_after.json'):
            write_json(self.root/name,proof)
        self.command = ['python','baseline.py','--backend','cube','--instance',self.instance,'--trace',str(self.trace),
            '--schedule',str(self.schedule),'--config',str(self.source/'config.json'),'--out',str(self.root),
            '--collect-phases','--experiment-id','table-02-cube']
        self.outer = self.source/'runs/table-02-cube/logs/job/process.json'
        write_json(self.outer,dict(status='ok',returncode=0,finished_at='now',command=self.command))
        write_json(self.root/'process/process.json',dict(status='ok',returncode=0,finished_at='now'))
        self.measurement = dict(node=2,cpus='48-51',cube_profile='paper-disk',service_cpus='48-71',
            frequency_policy_cpus='48-71',frequency_policy='maximum-pstate',trace_workers=1,storage_mode='disk')
        self.run = dict(status='ok',experiment='table-02-cube',backend='cube',instance=self.instance,
            analysis_mode='fresh-measurement',run_purpose='full-cohort',release=OLD,
            runtime=dict(commit=OLD['source_commit'],status='',tracked_diff_sha256=reuse.EMPTY_SHA256),
            measurement_identity=self.measurement,storage_mode='disk-backed-xfs', input=file_record(self.trace),
            schedule=file_record(self.schedule), sources=[file_record(self.trace)],
            disk_backing=proof,disk_backing_after=copy.deepcopy(proof),
            cube_environment=dict(template_cpu_millicores=2000,template_memory_mb=2048,
                template={'artifact_sha256':reuse.TEMPLATE_SHA,'artifact_id':'rfs-abc','artifact_size_bytes':'1073741824'}),phase_instrumentation={'sha256':'3'*64},
            counts={'checkpoints':2,'restores':1})
        self.save_run()
        prior = dict(key=self.key,experiment='table-02-cube',status='ok',command=self.command,
            inputs=['trace'],run_purpose='full-cohort',process_manifest=str(self.outer),staging_cleanup={'status':'ok'})
        self.suite = dict(status='failed',experiments=['table-02-cube'],release=OLD,
            measurement_identity=self.measurement,effective_config_sha256='config',run_purpose='full-cohort',
            jobs=[prior]+[dict(key=f'pending-{i}',experiment='table-02-cube',status='not-run') for i in range(11)])
        self.review = dict(status='failed',finished_at='now',release=OLD,cube_disk_service=file_record(self.service))
        write_json(self.source/'review.json',self.review)
        self.save_suite()
        self.plan = copy.deepcopy(self.suite)
        self.plan.update(status='planned',release=NEW)
        c = self.plan['jobs'][0]['command']
        c[c.index('--out')+1] = str(self.dest/self.key)
        c[c.index('--config')+1] = str(self.base/'new/config.json')
        for name in ('measurement_fingerprint','source_commit_identity'):
            mock = patch.object(reuse,name,return_value={'sha256':'f'*64})
            mock.start();self.addCleanup(mock.stop)

    def save_suite(self):
        write_json(self.source/'runs/table-02-cube/suite.json',self.suite)

    def save_run(self):
        self.run['result'] = dict(file_record(self.pilot_path),path='pilot_result.json')
        self.run['artifacts'] = [dict(file_record(self.root/name),path=name) for name in (
            'pilot_result.json','cube_phases.json','cubelet-phases.log','cube_disk_before.json','cube_disk_after.json')]
        self.run['phase_evidence'] = dict(strict=True,events=3,raw_phase_records=16,
            captured_log=file_record(self.root/'cubelet-phases.log'),phase_json=file_record(self.root/'cube_phases.json'))
        write_json(self.root/'run.json',self.run)

    def execute(self, **kw):
        return reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.base,verify_images=lambda x:None,check_active=lambda p:[],**kw)

    def test_copy_preserves_original_bytes_and_only_marks_completed_jobs(self):
        before = (self.root/'run.json').read_bytes()
        old_suite = (self.source/'runs/table-02-cube/suite.json').read_bytes()
        receipt = self.execute()
        self.assertEqual(receipt['reused_jobs'],1)
        self.assertEqual((self.dest/self.key/'run.json').read_bytes(),before)
        self.assertEqual((self.source/'runs/table-02-cube/suite.json').read_bytes(),old_suite)
        self.assertEqual(self.plan['jobs'][0]['measurement_release'],OLD)
        self.assertEqual(sum(bool(j.get('reused_verified')) for j in self.plan['jobs']),1)
        reuse.verify_imported_job(self.plan['jobs'][0],self.plan)
        self.assertNotEqual((self.dest/self.key/'run.json').stat().st_ino,(self.root/'run.json').stat().st_ino)

    def test_validate_only_writes_nothing(self):
        self.assertEqual(self.execute(validate_only=True)['status'],'validated')
        self.assertFalse(self.dest.exists())

    def test_changed_import_is_rejected_on_execution(self):
        self.execute()
        (self.dest/self.key/'run.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'imported bytes'):reuse.verify_imported_job(self.plan['jobs'][0],self.plan)

    def test_nonterminal_source_refused(self):
        self.review['status']='running';write_json(self.source/'review.json',self.review)
        with self.assertRaisesRegex(ValueError,'terminal'):self.execute()

    def test_old_service_cleanup_must_be_complete(self):
        write_json(self.service.parent/'restored.json',dict(ActiveState='active',cleanup_errors=['mount busy']))
        with self.assertRaisesRegex(ValueError,'restore cleanly'):self.execute()

    def test_partial_cohort_and_changed_resource_refused(self):
        for field,value in [('jobs',self.plan['jobs'][:1]),('measurement_identity',{'cube_profile':'paper-disk'})]:
            old=self.plan[field];self.plan[field]=value
            with self.assertRaises(ValueError):self.execute()
            self.plan[field]=old
        self.assertFalse(self.dest.exists())

    def test_active_source_refused_before_any_copy(self):
        with self.assertRaisesRegex(ValueError,'active file'):
            reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.base,check_active=lambda p:['pid'])
        self.assertFalse(self.dest.exists())

    def test_mutated_trace_refused(self):
        self.trace.write_text('changed')
        with self.assertRaisesRegex(ValueError,'trace bytes'):self.execute()

    def test_failed_outer_process_refused(self):
        write_json(self.outer,dict(status='failed',returncode=1,finished_at='now',command=self.command))
        with self.assertRaisesRegex(ValueError,'outer process'):self.execute()

    def test_missing_worker_action_refused_even_with_updated_artifact_hash(self):
        self.pilot['iterations'][1]['action_response']['event']['worker_results']=[]
        write_json(self.pilot_path,self.pilot);self.save_run()
        with self.assertRaisesRegex(ValueError,'worker actions'):self.execute()

    def test_phase_success_not_inferred_from_suite_success(self):
        log=self.root/'cubelet-phases.log'
        rows=[json.loads(x) for x in log.read_text().splitlines()];rows[0]['success']=False
        log.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
        self.capture['captured_log']=file_record(log)
        write_json(self.root/'cube_phases.json',self.capture);self.save_run()
        with self.assertRaisesRegex(ValueError,'success=true'):self.execute()

    def test_duplicate_phase_not_deduplicated(self):
        log=self.root/'cubelet-phases.log';log.write_text(log.read_text()+log.read_text().splitlines()[0]+'\n')
        self.capture['captured_log']=file_record(log)
        write_json(self.root/'cube_phases.json',self.capture);self.save_run()
        with self.assertRaisesRegex(ValueError,'Duplicate'):self.execute()

    def test_saved_stage_timer_cannot_be_forged(self):
        self.capture['events'][0]['process_ms']+=1
        write_json(self.root/'cube_phases.json',self.capture);self.save_run()
        with self.assertRaisesRegex(ValueError,'saved phase'):self.execute()

    def test_api_attempts_must_be_zero(self):
        self.pilot['iterations'][0]['snapshot']['api_retries']=1
        write_json(self.pilot_path,self.pilot);self.capture['pilot']=file_record(self.pilot_path)
        write_json(self.root/'cube_phases.json',self.capture);self.save_run()
        with self.assertRaisesRegex(ValueError,'more than once'):self.execute()

    def test_wrong_template_or_disk_transition_refused(self):
        self.run['cube_environment']['template_memory_mb']=4096;self.save_run()
        with self.assertRaisesRegex(ValueError,'template'):self.execute()

    def test_raw_evidence_symlink_refused(self):
        path=self.root/'unexpected';path.symlink_to(self.trace)
        with self.assertRaisesRegex(ValueError,'link'):self.execute()

    def test_dependency_link_is_preserved_not_followed_into_copied_results(self):
        dep=self.base/'dependency';dep.mkdir();(dep/'code.py').write_text('pass')
        (self.root/'payload').mkdir();(self.root/'payload/moatless-det-src').symlink_to(dep)
        receipt=self.execute()
        self.assertEqual((self.dest/self.key/'payload/moatless-det-src').readlink(),dep)
        self.assertIn('payload/moatless-det-src',receipt['jobs'][0]['files'])
        reuse.verify_imported_job(self.plan['jobs'][0],self.plan)


    def test_actual_image_verifier_is_used(self):
        seen=[]
        reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.base,
            verify_images=lambda x:seen.append(x),check_active=lambda p:[],validate_only=True)
        image=seen[0]['images']['cube_template']
        self.assertEqual(image['sha256'],reuse.TEMPLATE_SHA)
        self.assertEqual(image['path'],'/data/CubeMaster/storage/rfs-abc/rfs-abc.ext4')

    def test_changed_actual_image_refuses_before_copy(self):
        def changed(x): raise ValueError('image changed')
        with self.assertRaisesRegex(ValueError,'image changed'):
            reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.base,
                verify_images=changed,check_active=lambda p:[])
        self.assertFalse(self.dest.exists())

    def test_source_becomes_active_during_copy_no_success_receipt(self):
        checks=iter([[],[],['new-active-owner']])
        with self.assertRaisesRegex(ValueError,'during copy'):
            reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.base,
                verify_images=lambda x:None,check_active=lambda p:next(checks))
        self.assertFalse((self.dest/'reuse-manifest.json').exists())

    def test_failed_job_is_never_imported(self):
        self.suite['jobs'][0]['status']='failed';self.save_suite()
        with self.assertRaisesRegex(ValueError,'no completed'):self.execute()

    def test_all_candidates_verified_before_destination_created(self):
        self.suite['jobs'][1]=dict(self.suite['jobs'][0],key='pending-0')
        self.plan['jobs'][1]=dict(self.plan['jobs'][0],key='pending-0')
        self.save_suite()
        with self.assertRaisesRegex(ValueError,'location'):self.execute()
        self.assertFalse(self.dest.exists())


class CubeReuseEntryTests(unittest.TestCase):

    def test_public_entry_forwards_cube_reuse(self):
        from ae.scripts import hosted_launcher as hosted
        args=hosted.parse_arguments(['--checkout','/repo','--experiment','table-02-cube',
            '--cube-profile','paper-disk','--reuse-completed-from','/old','--output','/new'])
        command=hosted.command_line(dict(python=Path('/python'),runtime_root=Path('/repo'),
            config=Path('/fixed.json')),args,Path('/new'))
        self.assertEqual(command[command.index('--reuse-completed-from')+1],'/old')

    def test_profile_accepts_explicit_reuse_but_still_rejects_resume(self):
        args=argparse.Namespace(cube_profile='paper-disk',experiment=['table-02-cube'],
                                reuse_completed_from=Path('/old'))
        validate_profile(args)
        args.resume=Path('/old')
        with self.assertRaises(ValueError):validate_profile(args)


if __name__ == '__main__':
    unittest.main()
