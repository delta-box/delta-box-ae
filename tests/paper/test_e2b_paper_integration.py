"""Paper integration rejects forged fresh bases and retains physical event evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'ae'), str(ROOT/'ae/runners')]
from ae.scripts import e2b_paper_profile as profile
from ae.scripts import run_review as review
from ae.scripts import e2b_paper_transport as transport_module
from ae.runners import baseline, e2b_environment as environment


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.config = profile.effective({}, profile.PROFILE)
        self.manifest = self.root/'transport.json'
        self.fresh = self.root/'fresh.json'
        self.config['e2b'].update(execution='paper-nested-ready', transport_manifest=str(self.manifest),
            from_build='fresh', storage='/owned/storage')
        self.proof = {'status':'verified','instance':'input','fresh_base_build_id':'fresh',
            'guest_storage':'/owned/storage','runtime_manifest_sha':'1'*64,
            'guest_proof':{'header_closure_verified':True,'vcpus':1,'mem_mib':2048,
                'requested_disk_mib':4096,'actual_rootfs_bytes':5899*1024**2,
                'snapshot_files':[{'file':n,'bytes':1,'sha256':'2'*64} for n in environment.SNAPSHOT_FILES]}}
        self.stub = SimpleNamespace(manifest={'instance':'input','fresh_base_build_id':'fresh',
            'fresh_base_manifest':str(self.fresh)},storage='/owned/storage',key=Path('/key'),
            port=56789,runtime='/opt/e2b-paper/runtime',guard=mock.Mock())
        self.manifest.write_text('{}')

    def configured(self, env=None):
        self.fresh.write_text(json.dumps(self.proof))
        with mock.patch.object(profile,'_root_read',side_effect=lambda p:Path(p).read_bytes()), mock.patch.object(transport_module,'Transport',return_value=self.stub):
            return environment.configure(self.config, env if env is not None else {}, instance='input')

    def test_real_rootfs_size_is_preserved_not_forced_to_requested(self):
        env={'E2B_RESUME_BINARY':'/foreign','E2B_L1_HOST':'foreign','AE_E2B_PAPER_TRANSPORT':'bad'}
        result=self.configured(env)
        self.assertEqual(result['fresh_base_proof']['guest_proof']['actual_rootfs_bytes'],5899*1024**2)
        self.assertEqual(env['E2B_L1_HOST'],'ubuntu@127.0.0.1')
        self.assertEqual(env['AE_E2B_PAPER_TRANSPORT'],str(self.manifest))
        self.assertNotIn('E2B_RESUME_BINARY',env)

    def test_fresh_base_mismatch_resources_and_missing_snapshot_rejected(self):
        for mutate in (
            lambda p:p.update(instance='foreign'),
            lambda p:p.update(fresh_base_build_id='old'),
            lambda p:p.update(guest_storage='/old'),
            lambda p:p.update(runtime_manifest_sha='invalid'),
            lambda p:p['guest_proof'].update(header_closure_verified=False),
            lambda p:p['guest_proof'].update(vcpus=4),
            lambda p:p['guest_proof'].update(mem_mib=8192),
            lambda p:p['guest_proof']['snapshot_files'].pop(),
            lambda p:p['guest_proof']['snapshot_files'][0].update(sha256='wrong')):
            original=copy.deepcopy(self.proof)
            mutate(self.proof)
            with self.assertRaises(ValueError):self.configured()
            self.proof=original

    def test_manifest_change_after_execution_rejected(self):
        evidence=self.configured()
        self.fresh.write_text(self.fresh.read_text()+' ')
        with mock.patch.object(profile,'_root_read',side_effect=lambda p:Path(p).read_bytes()), mock.patch.object(transport_module,'Transport',return_value=self.stub), self.assertRaises(ValueError):
            environment.verify_snapshot_inputs(evidence)

    def test_paper_job_preserves_staging(self):
        plan={'jobs':[{}],'review_timeout':10,'measurement_identity':{'e2b_profile':'paper-nested'}}
        job={'key':'job','command':['unused'],'run_purpose':'full-trace'}
        with mock.patch.object(review,'from_environment',return_value={}), mock.patch.object(review,'execute',return_value={'status':'ok'}), mock.patch('repro.staging_cleanup.cleanup_reconstructable_staging') as cleanup:
            result=review.execute_review_job(1,job,plan,self.root)
        cleanup.assert_not_called()
        self.assertEqual(result['staging_cleanup']['status'],'retained')


class PhysicalEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.out=Path(tmp.name).resolve()
        self.path=self.out/'pilot_result.json'
        self.trace=self.out/'trajectory.json'
        self.action={'action_args_class':'pkg.Action','thoughts':None,'query':'exact'}
        self.trace.write_text(json.dumps({'root':{'node_id':0,'children':[{'node_id':30,'children':[],
            'action_steps':[{'action':self.action}]}]}}))
        self.contract={'inputs':[{'instance':'input','observed_expansions':[
            {'seq':1,'node_id':30,'parent_node_id':0,'duplicate':False,'finished':True,'actual_n_actions':1}]}],
            'ordered_measured_actions':[{'instance':'input','node_id':30,'action_index':0,'action_class':'pkg.Action'}]}
        self.proof={'contract':{'path':'/contract'},'manifest':{'sha256':'manifest'},'inputs':[
            {'instance':'input','repository_commit':'base','trajectory':{'sha256':'trace'},'expansions':1,'actions':1}]}
        self.env={'from_build':'base-id','fresh_base_manifest':{'path':'/fresh'}}
        self.root_step=self.make_step('root','base-id','root-id')
        resource={'sidecars':{'worker':{'ok':True},'index':{'ok':True}},'nproc':1,
                  'cpu_affinity':[0],'mem_total_kib':2000000,'swap_total_kib':0}
        resource_path=self.out/'root-l2-proof.json';resource_path.write_text(json.dumps(resource))
        receipt=self.out/'root.receipt.json';value=json.loads(receipt.read_text())
        value['downloads'].append(baseline.file_record(resource_path));receipt.write_text(json.dumps(value))
        self.step=self.make_step('action','root-id','child-id',action=True)
        self.data={'instance':'input','message_policy':'strict','create':{'ok':True,'reused':False,
            'fresh_for_input':'input','build_id':'base-id','fresh_base_manifest':'/fresh'},
            'root_setup':self.root_step,'guest_resource_proof':resource,'n_e2b_steps':1,
            'paper_input_contract':{'contract':self.proof['contract'],'manifest':self.proof['manifest'],
                'trajectory_sha256':'trace','repository_commit':'base','expected_expansions':1,'expected_actions':1},
            'iterations':[{'ok':True,'node_id':30,'finished':True,'selected_build_id':'root-id','build_id':'child-id',
                'event':{'new_node_id':30,'selected_node_id':0,'is_duplicate':False,'n_worker_actions':1,
                    'action_events':[{'node_id':30,'action_args_class':'pkg.Action'}]},'e2b_steps':[self.step]}],
            'mock_audits':{}}
        for role,served in [('controller',1),('worker',0)]:
            stats={'ok':True,'cursor':1,'total':1,'n_served':served,'n_mismatch':0,'n_protocol_errors':0,
                   'message_policy':'strict','audit_events_pending':2}
            self.data[role+'_mock_stats']=stats
            audit=self.out/(role+'_mock_audit.json')
            audit.write_text(json.dumps({'stats':dict(stats,audit_events_pending=0)}))
            self.data['mock_audits'][role]=str(audit)

    def make_step(self,prefix,parent,child,action=False):
        timing=self.out/(prefix+'.timing.json')
        value={'ok':True,'checkpoint_persist_ms':12.3,'resume_ms':45.6,
               'pause_ms':10.0,'snapshot_upload_ms':2.3,'to_build':child}
        timing.write_text(json.dumps(value))
        receipt={'kind':'ssh-files-outside-inner-timers','returncode':0,'transfer_errors':[],
            'from_build':parent,'to_build':child,'uploads':[],'downloads':[baseline.file_record(timing)]}
        if action:
            req=self.out/'action.req.json';resp=self.out/'action.resp.json'
            req.write_text(json.dumps({'instance':'input','seq':1,'node_id':30,
                'action':dict(self.action,thoughts='')}))
            resp.write_text(json.dumps({'ok':True}))
            receipt['uploads']=[baseline.file_record(req)]
            receipt['downloads'].append(baseline.file_record(resp))
        path=self.out/(prefix+'.receipt.json');path.write_text(json.dumps(receipt))
        return dict(value,host_rc=0,timing_present=True,transport_receipt=str(path))

    def validate(self):
        self.path.write_text(json.dumps(self.data))
        index=SimpleNamespace(load_trajectory=lambda _: [SimpleNamespace(purpose='build_action')])
        with mock.patch.object(profile,'_root_read',return_value=json.dumps(self.contract).encode()), mock.patch.dict(sys.modules,{'trajectory_index':index}):
            return baseline.validate_paper_e2b(self.path,self.trace,'input',self.proof,self.env,self.out)

    def test_complete_physical_evidence_and_drained_audit_are_accepted(self):
        result=self.validate()
        self.assertEqual(result['checkpoint_restore_pairs'],1)

    def test_parent_build_and_terminal_mismatch_rejected(self):
        row=self.data['iterations'][0]
        for key,bad in [('selected_build_id','wrong'),('build_id','wrong'),('finished',False),('node_id',31)]:
            saved=row[key];row[key]=bad
            with self.subTest(key=key),self.assertRaises(ValueError):self.validate()
            row[key]=saved

    def test_tampered_action_with_updated_transfer_hash_still_rejected(self):
        req=self.out/'action.req.json';value=json.loads(req.read_text())
        value['action']['query']='changed';req.write_text(json.dumps(value))
        rec=self.out/'action.receipt.json';value=json.loads(rec.read_text())
        value['uploads']=[baseline.file_record(req)];rec.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'Real paper action'):self.validate()

    def test_go_timing_mismatch_rejected(self):
        timing=self.out/'action.timing.json';value=json.loads(timing.read_text())
        value['resume_ms']=1;timing.write_text(json.dumps(value))
        rec=self.out/'action.receipt.json';value=json.loads(rec.read_text())
        value['downloads'][0]=baseline.file_record(timing);rec.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'timing differs'):self.validate()

    def test_wrong_build_or_nonfinite_and_invalid_timer_window_rejected(self):
        timing=self.out/'action.timing.json'
        receipt=self.out/'action.receipt.json'
        original=json.loads(timing.read_text());oldstep=dict(self.step)
        for fields in [{'to_build':'another-build'},{'resume_ms':float('nan')},
                       {'pause_ms':-1},{'snapshot_upload_ms':99},
                       {'checkpoint_persist_ms':False}]:
            value=dict(original,**fields);timing.write_text(json.dumps(value))
            self.step.update(fields)
            rec=json.loads(receipt.read_text());rec['downloads'][0]=baseline.file_record(timing)
            receipt.write_text(json.dumps(rec))
            with self.subTest(fields=fields),self.assertRaises(ValueError):self.validate()
            self.step.clear();self.step.update(oldstep)

    def test_actual_guest_resources_fail_even_if_pilot_and_transfer_are_consistent(self):
        target=self.out/'root-l2-proof.json';receipt=self.out/'root.receipt.json'
        original=json.loads(target.read_text())
        for fields in [{'nproc':2,'cpu_affinity':[0,1]}, {'swap_total_kib':1024},
                       {'mem_total_kib':4000000}, {'sidecars':{'worker':{'ok':True},'index':{'ok':False}}}]:
            value=dict(original,**fields);target.write_text(json.dumps(value))
            self.data['guest_resource_proof']=value
            rec=json.loads(receipt.read_text())
            rec['downloads'][-1]=baseline.file_record(target);receipt.write_text(json.dumps(rec))
            with self.subTest(fields=fields),self.assertRaisesRegex(ValueError,'Actual L2'):
                self.validate()

    def test_worker_audit_or_fresh_base_mismatch_rejected(self):
        self.data['create']['reused']=True
        with self.assertRaisesRegex(ValueError,'reused base'):self.validate()
        self.data['create']['reused']=False
        audit=self.out/'worker_mock_audit.json';value=json.loads(audit.read_text())
        value['stats']['n_served']=1;audit.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'audit differs'):self.validate()


if __name__=='__main__':unittest.main()
