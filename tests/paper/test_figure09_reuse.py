import copy
import hashlib
import importlib.util
import json
import io
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ae.repro import figure09_reuse as reuse
from ae.repro.analysis import analyze_fresh
from ae.repro.common import file_record, write_json

OLD = {'source_commit': 'a' * 40, 'source_sha256': '1' * 64}
NEW = {'source_commit': 'b' * 40, 'source_sha256': '2' * 64}


class ReuseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'old'
        self.dest = self.root / 'new/runs/figure-09'
        self.key = 'figure-09__pool__test__ext4'
        self.old_root = self.source / 'runs/figure-09' / self.key
        (self.old_root / 'measurements').mkdir(parents=True)
        (self.old_root / 'process').mkdir()
        self.actions = self.root / 'actions.json'
        write_json(self.actions, dict(base_commit='c' * 40, edits=[dict(edit_idx=0, file_path='a.py')]))
        self.driver = self.root / 'driver.py'; self.driver.write_text('driver')
        self.command = ['python', 'runner', '--experiment', 'figure-09', '--actions', str(self.actions),
                        '--input-key', 'pool__test', '--arm', 'ext4', '--config', str(self.source / 'config.json'),
                        '--out', str(self.old_root)]
        self.process = self.source / 'runs/figure-09/logs/process.json'
        write_json(self.process, dict(status='ok', returncode=0, finished_at='now', command=self.command))
        write_json(self.old_root / 'process/process.json', dict(status='ok', returncode=0, finished_at='now'))
        self.rows = [dict(instance='pool__test', fs_arm='ext4', edit_idx=0, file_path='a.py', file_size_bytes=16384,
                          copyup_bytes=4096, phys_bytes=8192, applied_ok=True)]
        (self.old_root / 'measurements/pool__test_ext4.jsonl').write_text(json.dumps(self.rows[0])+'\n')
        write_json(self.old_root / 'measurements/input-audit.json', dict(requested_edits=1, eligible_edits=1, excluded=[]))
        write_json(self.old_root / 'measurements/storage.json', dict(backing_fstype='tmpfs', mount_options=['noswap'],
                   filesystem_arm='ext4', logical_bytes=4*1024**3))
        write_json(self.old_root / 'host-storage.json', dict(fstype='tmpfs', mount={'options':'rw,noswap'}))
        artifacts = []
        for path in [self.old_root / 'host-storage.json', *sorted((self.old_root / 'measurements').iterdir())]:
            item = file_record(path); item['path'] = str(path.relative_to(self.old_root)); artifacts.append(item)
        self.config = dict(experiment='figure-09', status='ok', analysis_mode='fresh-measurement',
                          run_purpose='full-cohort', release=OLD, runtime=dict(commit=OLD['source_commit'], status='',
                          tracked_diff_sha256=reuse.EMPTY_SHA256), host={'cpu_affinity':[28,29,30,31]},
                          input_key='pool__test', arm='ext4', expected_edits=1,
                          filesystem_geometry={'logical_bytes':4*1024**3},
                          repository_source=dict(commit='c'*40, files=['a.py']),
                          extra_sources=[file_record(self.actions)], artifacts=artifacts,
                          images={key:{'path':str(self.driver),'sha256':'3'*64} for key in ('kernel','base_xfs','data_xfs')},
                          sources=dict(tracked_worktree_dirty=False, files={'driver':dict(source='driver.py',
                                  size=6, sha256=hashlib.sha256(b'driver').hexdigest())}))
        self.make_archives()
        self.save_run()
        prior = dict(key=self.key, experiment='figure-09', status='ok', command=self.command,
                     inputs=['actions'], run_purpose='full-cohort', process_manifest=str(self.process),
                     staging_cleanup={'status':'not-applicable'})
        self.suite = dict(experiments=['figure-09'], status='failed', release=OLD, jobs=[prior],
                          effective_config_sha256='cfg', measurement_identity={'node':1,'cpus':'28-31'},
                          run_purpose='full-cohort', host={'cpu_affinity':[28,29,30,31]})
        write_json(self.source / 'review.json', dict(status='interrupted', finished_at='now', release=OLD))
        self.save_suite()
        job = copy.deepcopy(prior);job.update(status='pending',run_purpose='full-trace',expected_edits=1)
        job.pop('process_manifest');job.pop('staging_cleanup')
        job['command'][-1] = str(self.dest / self.key)
        job['command'][job['command'].index('--config')+1] = str(self.root/'new/config.json')
        self.plan = dict(experiments=['figure-09'], release=NEW, jobs=[job], run_purpose='full-cohort',
                         effective_config_sha256='cfg', measurement_identity={'node':1,'cpus':'28-31'})
        self.fingerprint = patch.object(reuse, 'measurement_fingerprint', return_value={'sha256':'f'*64,'files':{}})
        self.fingerprint.start();self.addCleanup(self.fingerprint.stop)
        self.space = patch.object(reuse, 'MIN_FREE_BYTES', 0);self.space.start();self.addCleanup(self.space.stop)

    def make_archives(self):
        lower=self.old_root/'lower.tar'
        with tarfile.open(lower,'w') as tar:
            info=tarfile.TarInfo('a.py');info.size=1;tar.addfile(info,io.BytesIO(b'a'))
        experiment=self.old_root/'experiment.json';write_json(experiment,dict(expected_edits=1))
        self.config['extra_sources'].extend([file_record(lower),file_record(experiment)])
        guest=self.old_root/'guest.tar'
        with tarfile.open(guest,'w') as tar:
            for name,raw in [('driver',b'driver'),('guest_manifest.json',json.dumps(self.config['sources']['files']).encode())]:
                info=tarfile.TarInfo(name);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
        self.config['sources']['archive']=dict(path=str(guest),size=guest.stat().st_size,sha256=reuse._sha(guest))
        with tarfile.open(self.old_root/'extra.tar','w') as tar:
            for path,name in [(self.actions,'actions.json'),(lower,'lower.tar'),(experiment,'experiment.json')]:
                tar.add(path,arcname=name,recursive=False)

    def save_run(self): write_json(self.old_root/'run.json', self.config)
    def save_suite(self): write_json(self.source/'runs/figure-09/suite.json', self.suite)
    def run_reuse(self, **kwargs):
        return reuse.prepare_reuse(self.plan, self.source, self.dest, repo=self.root,
                                  verify_images=lambda value:None, check_active=lambda value:[], **kwargs)

    def test_copy_retains_old_bytes_and_new_plan_source(self):
        before=(self.old_root/'run.json').read_bytes()
        receipt=self.run_reuse()
        self.assertEqual(receipt['reused_jobs'],1)
        target=self.dest/self.key
        self.assertEqual((target/'run.json').read_bytes(),before)
        self.assertEqual(self.plan['release'],NEW)
        self.assertEqual(self.plan['jobs'][0]['measurement_release'],OLD)
        self.assertEqual(json.loads((target/'reuse-origin.json').read_text())['release'],OLD)
        self.assertNotEqual((target/'run.json').stat().st_ino,(self.old_root/'run.json').stat().st_ino)
        self.assertEqual((target/'run.json').stat().st_nlink,1)

    def test_validate_only_does_not_copy(self):
        self.run_reuse(validate_only=True);self.assertFalse(self.dest.exists())

    def test_incomplete_old_job_not_reused(self):
        self.suite['jobs'][0]['status']='failed';self.save_suite()
        with self.assertRaisesRegex(ValueError,'no complete'):self.run_reuse()

    def test_running_source_rejected(self):
        write_json(self.source/'review.json',dict(status='running',release=OLD))
        with self.assertRaisesRegex(ValueError,'terminal'):self.run_reuse()

    def test_different_policy_rejected(self):
        self.plan['measurement_identity']['node']=2
        with self.assertRaisesRegex(ValueError,'resource policy'):self.run_reuse()

    def test_different_config_rejected(self):
        self.plan['effective_config_sha256']='other'
        with self.assertRaisesRegex(ValueError,'config changed'):self.run_reuse()

    def test_different_command_arm_rejected(self):
        self.plan['jobs'][0]['command'][self.command.index('--arm')+1]='xfs'
        with self.assertRaisesRegex(ValueError,'command/input/arm'):self.run_reuse()

    def test_changed_input_rejected(self):
        self.actions.write_text(self.actions.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'dependency changed'):self.run_reuse()

    def test_changed_guest_source_uses_the_recorded_archive(self):
        self.driver.write_text('changed')
        self.run_reuse()
        self.assertTrue(self.dest.exists())

    def test_changed_measurement_fingerprint_does_not_block_reuse(self):
        with patch.object(reuse,'measurement_fingerprint', return_value={'sha256':'e'*64,'files':{}}):
            self.run_reuse()
        self.assertTrue(self.dest.exists())

    def test_bad_process_rejected(self):
        write_json(self.process,dict(status='failed',returncode=1,finished_at='now',command=self.command))
        with self.assertRaisesRegex(ValueError,'outer job'):self.run_reuse()

    def test_partial_actions_rejected(self):
        self.plan['jobs'][0]['expected_edits']=2
        with self.assertRaisesRegex(ValueError,'action count'):self.run_reuse()

    def test_symlink_rejected(self):
        (self.old_root/'link').symlink_to(self.actions)
        with self.assertRaisesRegex(ValueError,'non-regular'):self.run_reuse()

    def test_dirty_original_source_remains_reusable(self):
        self.config['runtime']['status']=' M core';self.save_run()
        self.run_reuse()
        self.assertTrue(self.dest.exists())

    def test_original_source_identity_mismatch_remains_reusable(self):
        self.config['release']=NEW;self.save_run()
        self.run_reuse()
        self.assertTrue(self.dest.exists())

    def test_active_source_rejected(self):
        with self.assertRaisesRegex(ValueError,'active file'):
            reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.root,
                                verify_images=lambda value:None,check_active=lambda value:['pid:fd'])

    def test_actual_copy_space_reserve(self):
        with patch.object(reuse,'MIN_FREE_BYTES',10**30):
            with self.assertRaisesRegex(ValueError,'free-space reserve'):self.run_reuse()
        self.assertFalse(self.dest.exists())

    def test_all_jobs_validate_before_any_copy(self):
        bad=copy.deepcopy(self.plan['jobs'][0]);bad['key']='unknown';bad['command'][-1]=str(self.dest/'unknown')
        oldbad=copy.deepcopy(self.suite['jobs'][0]);oldbad['key']='unknown'
        self.plan['jobs'].append(bad);self.suite['jobs'].append(oldbad);self.save_suite()
        with self.assertRaises(ValueError):self.run_reuse()
        self.assertFalse(self.dest.exists())

    def test_corrupt_guest_archive_rejected(self):
        (self.old_root/'guest.tar').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'guest archive SHA'):self.run_reuse()
        self.assertFalse(self.dest.exists())

    def test_corrupt_extra_member_rejected(self):
        with tarfile.open(self.old_root/'extra.tar','w') as tar:
            for name,raw in [('actions.json',b'{}'),('lower.tar',(self.old_root/'lower.tar').read_bytes()),
                             ('experiment.json',(self.old_root/'experiment.json').read_bytes())]:
                info=tarfile.TarInfo(name);info.size=len(raw);tar.addfile(info,io.BytesIO(raw))
        with self.assertRaisesRegex(ValueError,'archive member bytes'):self.run_reuse()
        self.assertFalse(self.dest.exists())

    def test_changed_control_file_during_validation_rejected(self):
        def mutate(value):
            self.suite['new-field']='changed';self.save_suite()
        with self.assertRaisesRegex(ValueError,'control files changed'):
            reuse.prepare_reuse(self.plan,self.source,self.dest,repo=self.root,
                                verify_images=mutate,check_active=lambda value:[])
        self.assertFalse(self.dest.exists())

    def normal_resume(self):
        from ae.scripts import run_review
        self.plan['status']='ok';write_json(self.dest/'suite.json',self.plan)
        runner=run_review.Review.__new__(run_review.Review)
        runner.output=self.root/'new';runner.attempt='attempt-002'
        nextplan=copy.deepcopy(self.plan)
        for job in nextplan['jobs']:job.pop('reused_verified',None)
        row={'experiment':'figure-09'}
        with patch.object(run_review,'verify_reused_images'):
            runner.prepare_resume(nextplan,self.dest,row)
        return nextplan

    def test_normal_resume_retains_import_source(self):
        self.run_reuse();plan=self.normal_resume()
        self.assertEqual(plan['jobs'][0]['measurement_release'],OLD)
        self.assertEqual(plan['measurement_sources'],[OLD,NEW])
        self.assertTrue(plan['jobs'][0]['reused_verified'])

    def test_normal_resume_rejects_changed_import_source(self):
        self.run_reuse();path=self.dest/self.key/'run.json'
        config=json.loads(path.read_text());config['release']=NEW;write_json(path,config)
        with self.assertRaisesRegex(ValueError,'imported job files changed'):self.normal_resume()

    def test_normal_resume_rejects_deleted_import_archive(self):
        self.run_reuse();(self.dest/self.key/'guest.tar').unlink()
        with self.assertRaisesRegex(ValueError,'imported job files changed'):self.normal_resume()

    def test_normal_resume_rejects_changed_reuse_manifest(self):
        self.run_reuse();path=self.dest/'reuse-manifest.json';path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'reuse manifest changed'):self.normal_resume()

    def mixed_summary(self):
        self.run_reuse()
        new=self.dest/'new-measurement';shutil=__import__('shutil');shutil.copytree(self.old_root,new)
        c=json.loads((new/'run.json').read_text());c['release']=NEW;c['runtime']['commit']=NEW['source_commit']
        c['run_purpose']='full-trace';write_json(new/'run.json',c)
        summary=analyze_fresh(self.dest)
        series=summary['experiments']['figure-09']['series']
        self.assertEqual({row['source_identity'] for row in series},
                         {'release-sha256:'+OLD['source_sha256'],'release-sha256:'+NEW['source_sha256']})
        self.assertEqual(len(summary['experiments']['figure-09']['selection']),2)
        return summary

    def test_analysis_keeps_two_source_populations(self):
        self.mixed_summary()

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'plotting extra unavailable')
    def test_mixed_source_real_plots_and_bilingual_comparison(self):
        from ae.repro.analysis import export_summary
        from ae.repro.plot import render
        from ae.scripts.build_review_comparison import build, ITEMS, PAPER_SHA256
        from PIL import Image
        summary=self.mixed_summary()
        analysis=self.root/'analysis';export_summary(summary,analysis)
        plots=self.root/'plots';render(analysis/'summary.json',plots)
        paper=self.root/'paper';paper.mkdir();refs=[]
        for key in ITEMS:
            path=paper/(key+'.png');Image.new('RGB',(10,10),'white').save(path)
            refs.append(dict(file=path.name,sha256=reuse._sha(path),pdf_page=1))
        write_json(paper/'manifest.json',dict(source_sha256=PAPER_SHA256,artifacts=refs))
        coverage=self.root/'coverage.json'
        write_json(coverage,dict(release=NEW,completed_job_reuse=dict(reused_jobs=1,original_release=OLD),
             coverage=[dict(experiment='figure-09',status='ok',planned_jobs=2,successful_jobs=2)]))
        output=self.root/'comparison'
        result=build(analysis/'summary.json',plots/'plots.json',coverage_path=coverage,output=output,paper_dir=paper)
        item=next(item for item in result['items'] if item['experiment']=='figure-09')
        self.assertEqual(len(item['populations']),2)
        for filename, phrase in [('README.md','multi-source campaign'),('README-zh.md','多来源测评')]:
            text=(output/filename).read_text();self.assertIn(phrase,text);self.assertIn(OLD['source_sha256'],text)
        self.assertIn('explicitly imported completed measurements', (output/'README.md').read_text())
        self.assertEqual(result['release'],NEW)


if __name__=='__main__':unittest.main()
