"""Offline paper-suite integration tests: no QEMU, SSH, leases or benchmarks.

The real serial execute_plan callback loop is exercised against small temporary
files and fake per-input producers. Every external boundary fails closed.
"""
from contextlib import contextmanager, ExitStack, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from ae.scripts import e2b_paper_suite as m
from ae.scripts import e2b_paper_profile as profile
from ae.scripts import run_review as review


class SuiteFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.work=self.root/'work';self.work.mkdir()
        self.planpath=self.root/'plan.json'
        self.configpath=self.root/'config.json'
        self.output=self.root/'runs'/'table-02-e2b'
        self.output.parent.mkdir()
        self.config=profile.effective({},profile.PROFILE)
        self.configpath.write_text(json.dumps(self.config))
        self.plan={'review_config':str(self.configpath),'review_output':str(self.output),
                   'workers':1,'review_timeout':99,
                   'measurement_identity':{'e2b_profile':'paper-nested'},
                   'jobs':[{'key':'table-02-e2b__'+r[0],'experiment':'table-02-e2b',
                            'status':'planned','command':['fake-producer','--config',str(self.configpath)],
                            'run_purpose':'test-only'} for r in m.COHORT]}
        self.save_plan()
        self.evidence=self.output.parent/'e2b-paper-l1'
        self.vm=types.SimpleNamespace(
            verify=Mock(),manifest_path=self.work/'lifecycle.json',
            identity={'pid':123,'starttime':456,'cgroup':'/owned'},
            ssh_port=57785,folder=self.work/'l1')
        self.events=[];self.owned_calls=0;self.exits=0
        self.prepare_error=None;self.create_fail_at=None;self.after_change_at=None
        self.bad_count_at=None;self.producer_fail_at=None;self.interrupt_at=None
        self.runtime_fail_at=None;self.base_records={};self.created_commands=[]
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m,'WORK',self.work))
        self.stack.enter_context(patch.object(m,'verify_inputs',return_value={
            'profile':'paper-nested','measured_actions':185,'observed_expansions':227}))
        self.stack.enter_context(patch.object(m,'checked_deployment',return_value=(
            {'files':[],'oci_manifest':'sha256:'+'a'*64},'b'*64,self.work/'l1-measurement-assets.json')))
        self.owned=self.stack.enter_context(patch.object(m,'owned_l1',side_effect=self.owned_context))
        self.stack.enter_context(patch.object(m,'install_runtime',side_effect=self.install))
        self.stack.enter_context(patch.object(m,'ssh',side_effect=self.ssh))
        self.stack.enter_context(patch.object(m,'capture',side_effect=self.capture))
        self.stack.enter_context(patch.object(m,'guest_json',side_effect=self.list_builds))
        self.stack.enter_context(patch.object(m,'verify_runtime',side_effect=self.verify_runtime))
        self.stack.enter_context(patch.object(m,'free_ports',return_value=(40001,40002)))
        self.stack.enter_context(patch.object(review,'repository_state',return_value={'fake':'source'}))
        self.stack.enter_context(patch.object(review,'host_state',return_value={'fake':'host'}))
        self.stack.enter_context(patch.object(review,'from_environment',return_value={'fake':'release'}))
        self.stack.enter_context(patch.object(review,'make_output_accessible'))
        self.producer=self.stack.enter_context(patch.object(review,'execute_review_job',side_effect=self.produce))
        self.stack.enter_context(patch.object(m.subprocess,'run',side_effect=AssertionError('External command forbidden in suite fixture')))
        self.quiet=self.stack.enter_context(patch('sys.stdout',new_callable=io.StringIO))

    def save_plan(self):self.planpath.write_text(json.dumps(self.plan))
    def suite(self):return json.loads((self.output/'suite.json').read_text())
    def state(self):return json.loads((self.evidence/'state.json').read_text())
    def row_index(self,path):
        name=Path(path).parent.name
        return [r[0] for r in m.COHORT].index(name)+1

    @contextmanager
    def owned_context(self,cfg):
        self.owned_calls+=1
        self.assertEqual(cfg.workspace,self.work/'l1-work')
        if self.prepare_error:raise self.prepare_error
        self.events.append(('l1-enter',0))
        try:yield self.vm
        finally:self.events.append(('l1-exit',0));self.exits+=1

    def install(self,vm,data,evidence):
        self.assertIs(vm,self.vm)
        self.events.append(('install',0))
        m.write(evidence/'runtime-before.json',{'status':'verified'})

    def ssh(self,vm,command,**kw):
        log=kw.get('log')
        if log:
            log=Path(log);log.parent.mkdir(parents=True,exist_ok=True)
            log.with_suffix('.command').write_text(command+'\n')
            log.with_suffix('.stdout').write_text('fake retained output')
        if log and log.name=='create-base':
            idx=self.row_index(log);self.events.append(('create',idx))
            self.created_commands.append(command)
            if self.create_fail_at==idx:raise RuntimeError('fixture base create failure')
            if self.interrupt_at==idx:raise KeyboardInterrupt('fixture cancellation')
        return ''

    def capture(self,vm,guest_root,builds,output):
        output=Path(output);idx=self.row_index(output)
        build=builds[0] if builds else 'missing'
        if output.name=='base-before.json':
            raw={'status':'verified','build_count':1,
                 'files':[{'path':guest_root+'/storage/templates/'+build+'/'+name,
                           'bytes':4096,'sha256':hashlib.sha256(name.encode()).hexdigest(),
                           'mtime_ns':1,'ctime_ns':1} for name in
                          ('metadata.json','snapfile','memfile','memfile.header','rootfs.ext4','rootfs.ext4.header')],
                 'builds':{build:{'headers':[{'file':'memfile.header','logical_bytes':2048*1024**2},
                                            {'file':'rootfs.ext4.header','logical_bytes':5899000000}]}}}
            self.base_records[guest_root]=copy.deepcopy(raw)
        elif output.name=='base-after.json':
            self.events.append(('after',idx))
            raw=copy.deepcopy(self.base_records[guest_root])
            if self.after_change_at==idx:raw['files'][0]['sha256']='f'*64
        else:
            raw=copy.deepcopy(self.base_records[guest_root])
            raw['build_count']=m.COHORT[idx-1][5]+2-(1 if self.bad_count_at==idx else 0)
        m.write(output,raw)
        return raw

    def list_builds(self,vm,source,**kw):
        idx=self.row_index(kw['log'])
        self.assertIn('/storage/templates',source)
        return ['fake-build-'+str(i) for i in range(m.COHORT[idx-1][5]+2)]

    def verify_runtime(self,vm,data,result):
        idx=self.row_index(result)
        self.events.append(('runtime-check',idx))
        if self.runtime_fail_at==idx:raise ValueError('fixture runtime hash mismatch')
        proof={'status':'verified','files':[]}
        m.write(result,proof);return proof

    def produce(self,index,job,plan,output):
        self.events.append(('produce',index))
        target=Path(output)/job['key']
        self.assertFalse(target.exists(),'before callback must not precreate producer output')
        target.mkdir()
        (target/'raw-response.json').write_text('{"retained": true}')
        effective=json.loads(Path(job['command'][2]).read_text())
        self.assertEqual(effective['e2b']['execution'],'paper-nested-ready')
        self.assertFalse(effective['e2b']['warm_action_worker'])
        self.assertEqual(effective['e2b']['vcpus'],1)
        return dict(job,status='failed' if self.producer_fail_at==index else 'ok')

    def assert_statuses(self,statuses):
        self.assertEqual([j['status'] for j in self.suite()['jobs']],statuses)


class Lifecycle(SuiteFixture):
    def test_one_l1_eight_unique_bases_and_outputs(self):
        self.assertEqual(m.run(self.planpath),0)
        self.assertEqual((self.owned_calls,self.exits),(1,1))
        self.assert_statuses(['ok']*8)
        self.assertEqual(self.state()['status'],'completed')
        self.assertEqual(self.state()['expected_actions'],185)
        self.assertEqual(len(self.base_records),8)
        roots=set();builds=set()
        for idx,row in enumerate(m.COHORT,1):
            evidence=self.evidence/row[0]
            transport=json.loads((evidence/'transport.json').read_text())
            proof=json.loads((evidence/'fresh-base.json').read_text())
            roots.add(transport['guest_root']);builds.add(transport['fresh_base_build_id'])
            self.assertEqual(transport['instance'],row[0])
            self.assertEqual(Path(transport['host_output_root']),self.output/('table-02-e2b__'+row[0]))
            self.assertEqual(proof['guest_proof']['vcpus'],1)
            self.assertEqual({r['file'] for r in proof['guest_proof']['snapshot_files']},
                {'metadata.json','snapfile','memfile','memfile.header','rootfs.ext4','rootfs.ext4.header'})
            self.assertTrue(all('path' in r for r in proof['guest_proof']['snapshot_files']))
            for marker in ('base-before.json','base-after.json','all-builds-after.json','runtime-after.json'):
                self.assertTrue((evidence/marker).exists())
            job=self.suite()['jobs'][idx-1]
            self.assertEqual(job['paper_post_validation']['build_count'],row[5]+2)
            self.assertEqual(job['paper_preparation']['sha256'],m.digest(evidence/'fresh-base.json'))
        self.assertEqual(len(roots),8);self.assertEqual(len(builds),8)
        self.assertEqual(self.events[0],('l1-enter',0));self.assertEqual(self.events[-1],('l1-exit',0))
        for idx in range(1,9):
            order=[self.events.index((kind,idx)) for kind in ('create','produce','after','runtime-check')]
            self.assertEqual(order,sorted(order))
            if idx<8:self.assertLess(order[-1],self.events.index(('create',idx+1)))

    def test_new_runs_parent_is_created_without_precreating_suite(self):
        self.assertFalse(self.output.exists())
        self.output.parent.rmdir()
        self.assertFalse(self.output.parent.exists())
        self.assertEqual(m.run(self.planpath),0)
        self.assert_statuses(['ok']*8)
        self.assertEqual((self.owned_calls,self.exits),(1,1))
        self.assertEqual(self.producer.call_count,8)
        self.assertEqual(len(self.base_records),8)
        self.assertEqual(self.state()['status'],'completed')
        self.assertTrue((self.evidence/'inputs.json').is_file())
        self.assertTrue((self.output/'suite.json').is_file())
        # The existing produce() guard also verifies each producer output is
        # absent when that producer begins, including this fresh-parent case.

    def test_original_effective_config_and_plan_are_not_rewritten(self):
        oldconfig=self.configpath.read_bytes();oldplan=self.planpath.read_bytes()
        m.run(self.planpath)
        self.assertEqual(self.configpath.read_bytes(),oldconfig)
        self.assertEqual(self.planpath.read_bytes(),oldplan)
        self.assertEqual(json.loads(oldconfig)['e2b']['execution'],'paper-nested-pending')

    def test_l1_setup_failure_records_all_not_run_and_first_error(self):
        self.prepare_error=RuntimeError('exact original readiness failure')
        with self.assertRaisesRegex(RuntimeError,'exact original readiness'):m.run(self.planpath)
        self.assert_statuses(['not-run']*8)
        self.assertIn('exact original readiness',self.state()['error'])
        self.assertIn('preparation_error',self.suite())
        self.producer.assert_not_called()
        self.assertTrue((self.evidence/'runtime-deployment.json').exists())

    def test_install_failure_closes_l1_and_records_all_not_run(self):
        with patch.object(m,'install_runtime',side_effect=RuntimeError('install failed')):
            with self.assertRaisesRegex(RuntimeError,'install failed'):m.run(self.planpath)
        self.assertEqual(self.exits,1)
        self.assert_statuses(['not-run']*8)
        self.producer.assert_not_called()

    def test_before_second_failure_keeps_first_and_marks_rest_not_run(self):
        self.create_fail_at=2
        self.assertEqual(m.run(self.planpath),1)
        self.assert_statuses(['ok','failed']+['not-run']*6)
        self.assertEqual(self.producer.call_count,1)
        self.assertEqual(self.exits,1)
        self.assertTrue((self.output/self.plan['jobs'][0]['key']/'raw-response.json').exists())
        failed=self.evidence/m.COHORT[1][0]
        self.assertTrue((failed/'create-base.stdout').exists())
        self.assertNotIn(('after',2),self.events)
        self.assertIn('fixture base create failure',self.suite()['jobs'][1]['paper_context_error'])

    def test_failed_producer_is_still_audited_then_no_more_input(self):
        self.producer_fail_at=2
        self.assertEqual(m.run(self.planpath),1)
        self.assert_statuses(['ok','failed']+['not-run']*6)
        self.assertIn(('after',2),self.events)
        self.assertEqual(self.producer.call_count,2)
        self.assertEqual(self.exits,1)
        self.assertTrue((self.output/self.plan['jobs'][1]['key']/'raw-response.json').exists())

    def test_changed_old_base_is_not_reported_success(self):
        self.after_change_at=2
        self.assertEqual(m.run(self.planpath),1)
        self.assert_statuses(['ok','failed']+['not-run']*6)
        self.assertIn('Fresh base changed',self.suite()['jobs'][1]['paper_context_error'])
        self.assertNotIn('paper_post_validation',self.suite()['jobs'][1])
        for name in ('base-before.json','base-after.json'):
            self.assertTrue((self.evidence/m.COHORT[1][0]/name).exists())

    def test_incomplete_successful_snapshot_count_fails(self):
        self.bad_count_at=1
        self.assertEqual(m.run(self.planpath),1)
        self.assert_statuses(['failed']+['not-run']*7)
        self.assertIn('Snapshot count differs',self.suite()['jobs'][0]['paper_context_error'])

    def test_runtime_after_hash_failure_fails_and_preserves_all_closure(self):
        self.runtime_fail_at=1
        self.assertEqual(m.run(self.planpath),1)
        self.assert_statuses(['failed']+['not-run']*7)
        self.assertIn('runtime hash mismatch',self.suite()['jobs'][0]['paper_context_error'])
        self.assertTrue((self.evidence/m.COHORT[0][0]/'all-builds-after.json').exists())

    def test_cancellation_marks_interrupted_and_unstarted_jobs(self):
        self.interrupt_at=2
        with self.assertRaises(KeyboardInterrupt):m.run(self.planpath)
        statuses=[j['status'] for j in self.suite()['jobs']]
        self.assertEqual(statuses[0],'ok')
        self.assertIn(statuses[1],('interrupted','failed','cancelled'))
        self.assertEqual(statuses[2:],['not-run']*6)
        self.assertEqual(self.exits,1)
        self.assertEqual(self.state()['status'],'failed')
        self.assertIn('KeyboardInterrupt',self.state()['error'])

    def test_existing_evidence_is_not_clobbered_or_reused(self):
        self.evidence.mkdir();(self.evidence/'sentinel').write_bytes(b'old-evidence')
        with self.assertRaises(FileExistsError):m.run(self.planpath)
        self.owned.assert_not_called()
        self.assertEqual((self.evidence/'sentinel').read_bytes(),b'old-evidence')

    def test_existing_suite_output_is_rejected(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):m.run(self.planpath)
        self.producer.assert_not_called()


class Scope(SuiteFixture):
    def test_missing_input_rejected_before_l1(self):
        self.plan['jobs'].pop();self.save_plan()
        with self.assertRaises(ValueError):m.run(self.planpath)
        self.owned.assert_not_called()

    def test_reordered_input_rejected_before_l1(self):
        self.plan['jobs'][0],self.plan['jobs'][1]=self.plan['jobs'][1],self.plan['jobs'][0];self.save_plan()
        with self.assertRaises(ValueError):m.run(self.planpath)
        self.owned.assert_not_called()

    def test_reused_input_rejected_before_l1(self):
        self.plan['jobs'][0]['reused_verified']=True;self.save_plan()
        with self.assertRaises(ValueError):m.run(self.planpath)
        self.owned.assert_not_called()

    def test_parallel_input_rejected_before_l1(self):
        self.plan['workers']=2;self.save_plan()
        with self.assertRaises(ValueError):m.run(self.planpath)
        self.owned.assert_not_called()

    def test_input_hash_verification_failure_never_starts_l1(self):
        with patch.object(m,'verify_inputs',side_effect=ValueError('input hash differs')):
            with self.assertRaisesRegex(ValueError,'input hash'):m.run(self.planpath)
        self.owned.assert_not_called()

    def test_callbacks_cannot_enter_nonpaper_plan(self):
        self.configpath.write_text('{}')
        with self.assertRaisesRegex(ValueError,'owned serial nested'):
            review.execute_plan(self.planpath,paper_context_ready=True,paper_before_job=lambda *a:None)
        self.producer.assert_not_called()


class Commands(unittest.TestCase):
    def test_create_command_has_fixed_old_dimensions_no_flags_and_private_paths(self):
        storage='/var/tmp/e2b-paper-'+'a'*32+'/storage'
        command=m.create_command(storage,'11111111-1111-1111-1111-111111111111','sha256:'+'b'*64)
        words=shlex.split(command.split(' && ',1)[1])
        for flag,value in (('-memory','2048'),('-vcpu','1'),('-disk','4096'),
                           ('-template','e2b-paper-original-input'),
                           ('-sandbox-dir',storage.rsplit('/',1)[0]+'/sandboxes'),
                           ('-storage',storage),('-firecracker','v1.14.1_458ca91'),
                           ('-kernel','vmlinux-6.1.158'),('-oci-layout','/opt/e2b-paper/oci')):
            self.assertEqual(words[words.index(flag)+1],value)
        self.assertIn('-hugepages=false',words)
        self.assertEqual(words[:5],['sudo','-n','env','-u','LAUNCH_DARKLY_API_KEY'])
        self.assertFalse(any('e2b-table2-storage' in v for v in words))

    def test_ready_config_does_not_mutate_pending_config(self):
        original=profile.effective({},profile.PROFILE);before=copy.deepcopy(original)
        ready=m.ready_config(original,'/owned/transport.json','new-build','/var/tmp/owned',40001,40002)
        self.assertEqual(original,before)
        self.assertEqual(ready['e2b']['execution'],'paper-nested-ready')
        self.assertEqual(ready['e2b']['from_build'],'new-build')
        self.assertFalse(ready['e2b']['warm_action_worker'])

    def test_ready_config_keeps_strict_profile_validation(self):
        config=profile.effective({},profile.PROFILE);config['e2b']['vcpus']=2
        with self.assertRaises(ValueError):m.ready_config(config,'/owned/m','build','/owned',40001,40002)

    def test_base_proof_rejects_non2g_memory(self):
        raw={'status':'verified','files':[],'builds':{'b':{'headers':[
            {'file':'memfile.header','logical_bytes':1024**3},
            {'file':'rootfs.ext4.header','logical_bytes':10}]}}}
        with self.assertRaises(ValueError):m.base_proof('i','b','/owned',raw,'a'*64)

    def test_base_proof_rejects_unverified_closure(self):
        raw={'status':'failed','files':[],'builds':{'b':{'headers':[
            {'file':'memfile.header','logical_bytes':2*1024**3},
            {'file':'rootfs.ext4.header','logical_bytes':10}]}}}
        with self.assertRaises(ValueError):m.base_proof('i','b','/owned',raw,'a'*64)


class SSHBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.log=self.root/'command'
        self.vm=types.SimpleNamespace(verify=Mock(),_ssh_argv=Mock(return_value=['fake-only']))
        self.process=patch.object(m.subprocess,'run');self.call=self.process.start()
        self.addCleanup(self.process.stop)

    def test_nonzero_keeps_stdout_stderr_and_command(self):
        self.call.return_value=subprocess.CompletedProcess([],23,'kept stdout','kept stderr')
        with self.assertRaisesRegex(RuntimeError,'code 23'):m.ssh(self.vm,'fixed command',log=self.log)
        self.assertEqual(self.log.with_suffix('.stdout').read_text(),'kept stdout')
        self.assertEqual(self.log.with_suffix('.stderr').read_text(),'kept stderr')
        self.assertEqual(self.log.with_suffix('.command').read_text(),'fixed command\n')
        self.vm.verify.assert_called_once()

    def test_failed_identity_does_not_execute(self):
        self.vm.verify.side_effect=RuntimeError('identity drift')
        with self.assertRaisesRegex(RuntimeError,'identity'):m.ssh(self.vm,'fixed',log=self.log)
        self.call.assert_not_called()

    def test_timeout_retains_partial_bytes_and_command(self):
        self.call.side_effect=subprocess.TimeoutExpired(['fake-only'],20,output=b'partial stdout',stderr=b'partial stderr')
        with self.assertRaises(subprocess.TimeoutExpired):m.ssh(self.vm,'fixed timed command',timeout=20,log=self.log)
        self.assertEqual(self.log.with_suffix('.stdout').read_text(),'partial stdout')
        self.assertEqual(self.log.with_suffix('.stderr').read_text(),'partial stderr')
        self.assertEqual(self.log.with_suffix('.command').read_text(),'fixed timed command\n')

    def test_command_evidence_cannot_be_overwritten_or_reexecuted(self):
        self.log.with_suffix('.command').write_text('previous command')
        with self.assertRaises(FileExistsError):m.ssh(self.vm,'new command',log=self.log)
        self.call.assert_not_called()
        self.assertEqual(self.log.with_suffix('.command').read_text(),'previous command')

    def test_malformed_guest_json_not_success(self):
        self.call.return_value=subprocess.CompletedProcess([],0,'not JSON','')
        with self.assertRaises(json.JSONDecodeError):m.guest_json(self.vm,'fixed source',log=self.log)



class RuntimeFiles(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.guest=self.root/'guest'
        (self.guest/'runtime/bin').mkdir(parents=True)
        (self.guest/'oci/blobs').mkdir(parents=True)
        self.rows=[]
        for name in ('runtime/bin/create-build','runtime/bin/resume-build','oci/blobs/layer'):
            data=('test:'+name).encode()
            (self.guest/name).write_bytes(data)
            (self.guest/name).chmod(0o444)
            self.rows.append({'path':name,'sha256':hashlib.sha256(data).hexdigest()})
        self.result=self.root/'proof.json'
        self.external=patch.object(m.subprocess,'run',side_effect=AssertionError('No external commands allowed'))
        self.external.start();self.addCleanup(self.external.stop)

    def execute_guest_source(self,vm,source,**kw):
        # Run only this module's generated verifier against this test's tiny
        # fixture root, not the real L1 or host runtime.
        old="root=pathlib.Path('/opt/e2b-paper')"
        self.assertIn(old,source)
        source=source.replace(old,'root=pathlib.Path('+repr(str(self.guest))+')',1)
        output=io.StringIO()
        with redirect_stdout(output):exec(compile(source,'<fixture-runtime-verifier>','exec'),{})
        return json.loads(output.getvalue())

    def verify(self):
        with patch.object(m,'guest_json',side_effect=self.execute_guest_source):
            return m.verify_runtime(object(),{'files':self.rows},self.result)

    def test_exact_known_files_hashes_verified(self):
        self.assertEqual(self.verify()['status'],'verified')
        self.assertEqual(json.loads(self.result.read_text())['files'],self.rows)

    def test_extra_regular_file_rejected(self):
        (self.guest/'runtime/extra').write_text('extra')
        with self.assertRaisesRegex(AssertionError,'file set differs'):self.verify()
        self.assertFalse(self.result.exists())

    def test_missing_regular_file_rejected(self):
        (self.guest/'runtime/bin/create-build').unlink()
        with self.assertRaisesRegex(AssertionError,'file set differs'):self.verify()

    def test_wrong_hash_rejected(self):
        (self.guest/'runtime/bin/create-build').write_text('changed')
        with self.assertRaises(AssertionError):self.verify()

    def test_group_writable_runtime_rejected(self):
        (self.guest/'runtime/bin/create-build').chmod(0o664)
        with self.assertRaises(AssertionError):self.verify()

    def test_file_symlink_rejected(self):
        target=self.guest/'runtime/bin/create-build';target.unlink()
        target.symlink_to(self.guest/'runtime/bin/resume-build')
        with self.assertRaisesRegex(AssertionError,'symlink'):self.verify()

    def test_extra_directory_symlink_rejected(self):
        external=self.root/'external';external.mkdir();(external/'invisible').write_text('unlisted')
        (self.guest/'runtime/extra-link').symlink_to(external,target_is_directory=True)
        with self.assertRaisesRegex(AssertionError,'symlink'):self.verify()

    def test_runtime_top_level_symlink_rejected(self):
        external=self.root/'external-runtime'
        (self.guest/'runtime').rename(external)
        (self.guest/'runtime').symlink_to(external,target_is_directory=True)
        with self.assertRaisesRegex(AssertionError,'symlink'):self.verify()

    def test_oci_top_level_symlink_rejected(self):
        external=self.root/'external-oci'
        (self.guest/'oci').rename(external)
        (self.guest/'oci').symlink_to(external,target_is_directory=True)
        with self.assertRaisesRegex(AssertionError,'symlink'):self.verify()

    def test_result_file_is_never_overwritten(self):
        self.result.write_text('previous proof')
        with self.assertRaises(FileExistsError):self.verify()
        self.assertEqual(self.result.read_text(),'previous proof')



class Deployment(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'runtime-deployment.json'
        self.manifest=self.root/'l1-measurement-assets.json'
        self.data={'schema_version':1,'kind':'e2b-paper-runtime-deployment-v1',
            'share_tag':'ae_runtime_v1','l1_manifest':{'path':str(self.manifest),'sha256':'a'*64},
            'oci_manifest':'sha256:9da1d3aecd725a91d879bbc59e9872ed4cab3b98d21b802426a24f877d69ee12',
            'files':[{'path':x,'sha256':'b'*64} for x in (
                'runtime/bin/create-build','runtime/bin/resume-build','runtime/envd/envd',
                'runtime/fc-versions/v1.14.1_458ca91/amd64/firecracker',
                'runtime/kernels/vmlinux-6.1.158/amd64/vmlinux.bin',
                'runtime/busybox/1.36.1/amd64/busybox','oci/index.json')]}
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m,'WORK',self.root))
        self.stack.enter_context(patch.object(m,'DEPLOYMENT',self.path))
        self.stack.enter_context(patch.object(m,'trusted',side_effect=lambda x:Path(x)))
        self.asset=self.stack.enter_context(patch.object(m,'file_asset',return_value=self.manifest))

    def checked(self):
        self.path.write_text(json.dumps(self.data))
        return m.checked_deployment()

    def test_exact_deployment_content_hash_and_manifest_returned(self):
        data,sha,l1=self.checked()
        self.assertEqual(data,self.data)
        self.assertEqual(sha,hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(l1,self.manifest)
        self.asset.assert_called_once_with(self.data['l1_manifest'])

    def test_changed_oci_manifest_rejected(self):
        self.data['oci_manifest']='sha256:'+'e'*64
        with self.assertRaises(ValueError):self.checked()

    def test_other_share_tag_rejected(self):
        self.data['share_tag']='whole-host'
        with self.assertRaises(ValueError):self.checked()

    def test_other_l1_manifest_rejected(self):
        self.asset.return_value=self.root/'unreviewed.json'
        with self.assertRaises(ValueError):self.checked()

    def test_missing_runtime_dependency_rejected(self):
        self.data['files']=self.data['files'][1:]
        with self.assertRaises(ValueError):self.checked()

    def test_duplicate_runtime_file_rejected(self):
        self.data['files'].append(dict(self.data['files'][0]))
        with self.assertRaises(ValueError):self.checked()

    def test_unsafe_or_unrelated_relative_file_rejected(self):
        original=copy.deepcopy(self.data)
        for path in ('/etc/shadow','runtime/../../escape','private/id_ed25519'):
            with self.subTest(path=path):
                self.data=copy.deepcopy(original)
                self.data['files'].append({'path':path,'sha256':'d'*64})
                with self.assertRaises(ValueError):self.checked()

    def test_missing_manifest_identity_rejected(self):
        self.data['files'][0]['sha256']=''
        with self.assertRaises(ValueError):self.checked()


if __name__=='__main__':
    unittest.main()
