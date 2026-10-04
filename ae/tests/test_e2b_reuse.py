import copy
import importlib.util
from pathlib import Path
import unittest
from ae.scripts.e2b_paper_profile import COHORT

ROOT=Path(__file__).resolve().parents[2]
class E2BReusePlanTests(unittest.TestCase):
    def setUp(self):
        p=ROOT/'ae/repro/e2b_reuse.py'
        self.assertTrue(p.is_file(),'E2B continuation must validate the original complete eight-input plan')
        spec=importlib.util.spec_from_file_location('e2b_reuse_test',p)
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.plan={'experiments':['table-02-e2b'],'workers':1,'measurement_identity':{'e2b_profile':'paper-nested'},'jobs':[{'key':'table-02-e2b__'+r[0],'experiment':'table-02-e2b','run_purpose':'full-trace'} for r in COHORT]}
    def test_original_complete_plan_is_accepted(self):
        self.mod.validate_plan(self.plan)
    def test_shortened_plan_is_rejected(self):
        self.plan['jobs'].pop()
        with self.assertRaises(ValueError):self.mod.validate_plan(self.plan)
    def test_reordered_plan_is_rejected(self):
        self.plan['jobs'].reverse()
        with self.assertRaises(ValueError):self.mod.validate_plan(self.plan)
    def test_other_profile_is_rejected(self):
        self.plan['measurement_identity']={}
        with self.assertRaises(ValueError):self.mod.validate_plan(self.plan)
    def test_nonserial_plan_is_rejected(self):
        self.plan['workers']=2
        with self.assertRaises(ValueError):self.mod.validate_plan(self.plan)
    def test_destination_alias_cannot_write_into_original_results(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'original';source.mkdir();alias=root/'alias';alias.symlink_to(source,target_is_directory=True)
            plan=copy.deepcopy(self.plan);plan['release']={'source_commit':'new'}
            old=copy.deepcopy(plan['jobs'])
            for job in old:job['status']='failed'
            old[0]['status']='ok'
            context={'suite':{'jobs':old,'release':{'source_commit':'old'}},'controls':[{},{}]}
            receipt={'job':old[0]['key'],'release':{'source_commit':'old'},'run':{'path':'/old/run.json'},'process':{'path':'/old/process.json'}}
            with mock.patch.object(self.mod,'load_source',return_value=context), mock.patch.object(self.mod,'validate_measured_job',return_value=receipt):
                with self.assertRaisesRegex(ValueError,'destination'):
                    self.mod.prepare_reuse(plan,source,alias/'continuation',repo=ROOT,check_active=lambda p:[])
            self.assertFalse((source/'continuation').exists())

    def test_reference_only_source_does_not_need_a_synthetic_lifecycle(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plan=copy.deepcopy(self.plan);plan['e2b_paper_inputs']={}
            suite=copy.deepcopy(plan);suite.update(status='ok',release={'source_commit':'old'})
            for job in suite['jobs']:job.update(status='ok',reused_verified=True,execution=self.mod.EXECUTION)
            review={'status':'ok','finished_at':'done','experiments':['table-02-e2b'],'e2b_profile':'paper-nested','release':suite['release']}
            def read(path):
                if Path(path).name=='review.json':return review,{'path':str(path)}
                if Path(path).name=='suite.json':return suite,{'path':str(path)}
                self.fail('Reference-only output must not require local L1 evidence')
            with mock.patch.object(self.mod,'_bound_json',side_effect=read),mock.patch.object(self.mod,'frozen_controls',return_value=[]),mock.patch.object(self.mod,'source_commit_identity',return_value={}):
                result=self.mod.load_source(root,plan,ROOT)
            self.assertIsNone(result['state'])

    def test_unbound_reuse_flag_is_rejected(self):
        job=self.plan['jobs'][0];job['reused_verified']=True
        with self.assertRaises((ValueError,KeyError,OSError)):self.mod.verify_referenced_job(job,self.plan)

class FrozenControlTests(unittest.TestCase):
    def setUp(self):
        import tempfile,hashlib
        from unittest import mock
        from ae.scripts import e2b_paper_profile as profile
        from ae.repro import e2b_reuse
        self.mod=e2b_reuse
        self.assertTrue(hasattr(e2b_reuse,'frozen_controls'),'Reference verification must bind immutable contract and manifest bytes')
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        self.contract=root/'e2b-paper-185-input-action-contract.json';self.contract.write_text('{"inputs":[]}')
        self.manifest=root/'manifest.json';self.manifest.write_text('{"profile":"paper-nested"}')
        digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        self.proof={'contract':{'path':str(self.contract),'sha256':digest(self.contract)},'manifest':{'path':str(self.manifest),'sha256':digest(self.manifest),'bytes':self.manifest.stat().st_size}}
        for key,value in [('INPUT_ROOT',root),('MANIFEST',self.manifest),('CONTRACT_SHA256',digest(self.contract))]:
            patch=mock.patch.object(profile,key,value);patch.start();self.addCleanup(patch.stop)
    def test_changed_contract_is_rejected(self):
        self.contract.write_text('{"inputs":[],"changed":true}')
        with self.assertRaises(ValueError):self.mod.frozen_controls(self.proof)
    def test_changed_manifest_is_rejected(self):
        self.manifest.write_text('{"profile":"changed"}')
        with self.assertRaises(ValueError):self.mod.frozen_controls(self.proof)

class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        from ae.repro import e2b_reuse
        self.mod=e2b_reuse
        self.assertTrue(hasattr(self.mod,'validate_runtime_identity'))
        self.identity={'pid':123,'ppid':12,'starttime':456,'cgroup':'/system.slice/owned.service','exe':'/qemu'}
        self.state={'identity':self.identity,'deployment_sha256':'a'*64}
        self.life={'ownership':dict(self.identity),'resources':{'cgroup':self.identity['cgroup']}}
        self.files=[{'path':'runtime/bin/resume-build','sha256':'b'*64}]
        self.deployment={'schema_version':1,'kind':'e2b-paper-runtime-deployment-v1','files':self.files}
        self.before={'status':'verified','files':copy.deepcopy(self.files)};self.after=copy.deepcopy(self.before)
        self.fresh={'runtime_manifest_sha':'a'*64}
    def test_lifecycle_cannot_be_borrowed_from_another_guest(self):
        self.mod.validate_lifecycle_identity(self.state,self.life)
        self.life['ownership']['pid']=124
        with self.assertRaises(ValueError):self.mod.validate_lifecycle_identity(self.state,self.life)
    def test_lifecycle_resource_cgroup_must_match(self):
        self.life['resources']['cgroup']='/system.slice/other.service'
        with self.assertRaises(ValueError):self.mod.validate_lifecycle_identity(self.state,self.life)
    def test_runtime_files_are_bound_to_the_complete_deployment(self):
        self.mod.validate_runtime_identity(self.before,self.after,self.deployment,'a'*64,self.state,self.fresh)
        self.before['files']=[];self.after['files']=[]
        with self.assertRaises(ValueError):self.mod.validate_runtime_identity(self.before,self.after,self.deployment,'a'*64,self.state,self.fresh)
    def test_empty_deployment_is_rejected(self):
        self.before['files']=[];self.after['files']=[];self.deployment['files']=[]
        with self.assertRaises(ValueError):self.mod.validate_runtime_identity(self.before,self.after,self.deployment,'a'*64,self.state,self.fresh)
    def test_deployment_and_fresh_base_hashes_must_match_state(self):
        with self.assertRaises(ValueError):self.mod.validate_runtime_identity(self.before,self.after,self.deployment,'c'*64,self.state,self.fresh)
        self.fresh['runtime_manifest_sha']='c'*64
        with self.assertRaises(ValueError):self.mod.validate_runtime_identity(self.before,self.after,self.deployment,'a'*64,self.state,self.fresh)

class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        from ae.repro import e2b_reuse
        self.mod=e2b_reuse
        self.assertTrue(hasattr(self.mod,'validate_source_inventory'))
        self.root=Path('/result/input')
        self.records=[{'path':str(self.root/'driver'/name),'sha256':'a'*64,'bytes':1} for name in
            ('e2b_paper_nested_driver.py','e2b_slim_finalbench_pilot.py','e2b_slim_action_runner.py','e2b_paper_action.py')]
    def test_required_driver_inventory_is_accepted(self):
        self.mod.validate_source_inventory(self.records,self.root)
    def test_empty_and_partial_source_inventory_are_rejected(self):
        for records in ([],self.records[:-1]):
            with self.assertRaises(ValueError):self.mod.validate_source_inventory(records,self.root)
    def test_duplicate_source_paths_are_rejected(self):
        with self.assertRaises(ValueError):self.mod.validate_source_inventory(self.records+[self.records[0]],self.root)

if __name__=='__main__':unittest.main()
