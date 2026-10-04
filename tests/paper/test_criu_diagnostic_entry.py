"""Complete CRIU/FC diagnostic entry: private locks/mocks, no measurements."""
import argparse
import ast
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('diagnostic_hosted_fixture',ROOT/'tests/paper/test_hosted_launcher.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
hosted=fixture.hosted


class DiagnosticHostedTests(unittest.TestCase):
    setUp=fixture.HostedTests.setUp
    owned_fixture=fixture.HostedTests.owned_fixture
    launcher=fixture.HostedTests.launcher

    def flags(self,node=0,experiment='table-02-criu'):
        return ['--checkout',str(self.runtime),'--isolated-validation','--cpu-layout','numa03',
                '--experiment',experiment,'--baseline-inputs','44','--limit','2' if experiment=='table-02-fc-diff' else '1',
                '--numa-node',str(node),'--cpus','0-3' if node==0 else '72-75','--output','selected/diagnostic']

    def test_serial_diagnostic_uses_background_owned_unit_without_reviewer_lease(self):
        def inspect(policy,caller,args,output,environment,priority,**kwargs):
            self.assertFalse(args.cpu_parallel)
            self.assertEqual(args.cpu_layout,'numa03')
            self.assertFalse(priority.reviewer_waiting())
            with self.assertRaisesRegex(ValueError,'Another hosted AE run'):
                hosted.acquire_lock(policy['lock_file'])
            command=hosted.command_line(policy,args,output)
            self.assertIn('--isolated-validation',command)
            self.assertNotIn('--cpu-parallel',command)
            self.assertEqual(command[command.index('--cpu-layout')+1],'numa03')
            self.assertEqual(command[command.index('--limit')+1],str(args.limit))
            self.assertNotIn('--max-events',command)
            service=hosted.cpu_service_command(policy,caller,command,'deltabox-ae-cpu-'+'a'*32+'.service')
            self.assertIn('--property=AllowedMemoryNodes=0 3',service)
            self.assertNotIn('fixed-secret',' '.join(service))
            return 7
        for experiment in ('table-02-criu','table-02-fc-diff'):
            with self.subTest(experiment=experiment),self.launcher() as (execute,_,stderr),patch.object(hosted,'run_background_cpu',side_effect=inspect):
                self.assertEqual(hosted.main(self.flags(experiment=experiment)),7,stderr.getvalue())
                execute.assert_not_called()
                self.priority.acquire_reviewer.assert_not_called()

    def test_service_helper_repeats_trust_without_taking_reviewer_priority(self):
        unit='deltabox-ae-cpu-'+'b'*32+'.service'
        for experiment,node,cpus in ((backend,node,cpus) for backend in ('table-02-criu','table-02-fc-diff')
                                    for node,cpus in ((0,'0-3'),(3,'72-75'))):
            with self.subTest(experiment=experiment,node=node), self.launcher() as (execute,_,stderr), \
                    patch.object(hosted,'cpu_service_identity',return_value={'unit':unit,'cgroup':'/owned','memory_swap_max':'0'}), \
                    patch.object(hosted,'background_cpu_binding',return_value={'cpu_layout':'numa03'}) as binding:
                self.assertEqual(hosted.main(self.flags(node,experiment),service_context=(fixture.REVIEWER.pw_uid,unit)),0,stderr.getvalue())
                self.priority.acquire_reviewer.assert_not_called()
                binding.assert_called_once_with('/owned')
                executable,command,_=execute.call_args.args
                self.assertEqual(executable,'/usr/bin/numactl')
                self.assertEqual(command[:4],['/usr/bin/numactl','--all','--physcpubind='+cpus,'--membind='+str(node)])
                self.assertEqual(command[4:9],[str(self.policy['python']),'-I',str(self.runtime/'ae/scripts/run_review.py'),'--config',str(self.policy['config'])])
                self.assertEqual(command[command.index('--cpu-layout')+1],'numa03')
                self.assertEqual(command[command.index('--cpus')+1],cpus)
                self.assertEqual(command[command.index('--experiment')+1],experiment)
                self.assertEqual(command[command.index('--limit')+1],'2' if experiment=='table-02-fc-diff' else '1')
                self.assertNotIn('--max-events',command)

    def test_parallel_service_keeps_existing_python_exec_and_controller_affinity(self):
        unit='deltabox-ae-cpu-'+'c'*32+'.service'
        flags=['--checkout',str(self.runtime),'--group','cpu','--cpu-parallel','--cpu-layout','numa03',
               '--output','selected/full']
        with self.launcher() as (execute,_,stderr), \
                patch.object(hosted,'cpu_service_identity',return_value={'unit':unit,'cgroup':'/owned','memory_swap_max':'0'}), \
                patch.object(hosted,'background_cpu_binding',return_value={'cpu_layout':'numa03'}):
            self.assertEqual(hosted.main(flags,service_context=(fixture.REVIEWER.pw_uid,unit)),0,stderr.getvalue())
            executable,command,_=execute.call_args.args
            self.assertEqual(executable,str(self.policy['python']))
            self.assertEqual(command[0],str(self.policy['python']))
            self.assertNotIn('/usr/bin/numactl',command)

    def test_scope_rejects_gpu_other_backend_event_prefix_wrong_node_and_reuse(self):
        changes=(('experiment','table-02-e2b'),('experiment','table-02-deltabox'),
                 ('experiment','figure-08-gpu'),('limit','2'),('numa-node','1'),
                 ('cpus','28-31'),('cpu-layout','numa12'),('baseline-inputs','all'))
        for key,value in changes:
            flags=self.flags();flags[flags.index('--'+key)+1]=value
            with self.subTest(key=key,value=value),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                hosted.parse_arguments(flags)
        for extra in (['--max-events','1'],['--reuse-completed-from','selected/foreign'],
                      ['--group','cpu'],['--cpu-parallel'],['--test'],['--list'],
                      ['--experiment','table-02-criu']):
            with self.subTest(extra=extra),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                hosted.parse_arguments(self.flags()+extra)

    def test_failed_serial_diagnostic_is_not_retried_as_a_reviewer_yield(self):
        output=self.runtime/'ae/results/selected/diagnostic'
        output.mkdir(parents=True)
        command=['python','run_review.py','--output',str(output)]
        for backend in ('criu','fc-diff'):
            with self.subTest(backend=backend):
                record={'status':'failed','concurrency_policy':{'lane':'isolated-background-'+backend+'-validation'}}
                (output/'review.json').write_text(json.dumps(record))
                with self.assertRaisesRegex(RuntimeError,'failed baseline diagnostic'):
                    hosted.verify_background_cleanup(self.policy,command)
                hosted.verify_background_cleanup(self.policy,command,check_experiment_failure=False)
                record['status']='interrupted'
                (output/'review.json').write_text(json.dumps(record))
                hosted.verify_background_cleanup(self.policy,command)
                record['coverage']=[{'experiment':'table-02-'+backend,'status':'failed'}]
                (output/'review.json').write_text(json.dumps(record))
                with self.assertRaisesRegex(RuntimeError,'failed baseline diagnostic'):
                    hosted.verify_background_cleanup(self.policy,command)

    def test_fc_requires_two_complete_inputs_on_background_nodes(self):
        for key,value in (('limit','1'),('limit','3'),('numa-node','1'),('numa-node','2'),
                          ('cpus','28-31'),('cpu-layout','numa12')):
            flags=self.flags(experiment='table-02-fc-diff');flags[flags.index('--'+key)+1]=value
            with self.subTest(key=key,value=value),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                hosted.parse_arguments(flags)
        for extra in (['--max-events','1'],['--reuse-completed-from','selected/foreign'],['--experiment','table-02-criu']):
            with self.subTest(extra=extra),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                hosted.parse_arguments(self.flags(experiment='table-02-fc-diff')+extra)

    def test_full_numa03_entry_keeps_all16_path_without_diagnostic_or_limit(self):
        args=hosted.parse_arguments(['--checkout',str(self.runtime),'--group','cpu','--cpu-parallel','--cpu-layout','numa03'])
        self.assertTrue(hosted.managed_cpu_execution(args))
        self.assertFalse(args.isolated_validation)
        self.assertIsNone(args.limit)
        self.assertIsNone(args.max_events)


def review_contract():
    """Execute production parser/main/validators without importing VM backends."""
    source=ROOT/'ae/scripts/run_review.py'
    names={'parser','GPUCases','gpu_case_selection','gpu_device_selection','validate_gpu_selection','isolated_background_baseline',
           'validate_isolated_baseline_resume','validation_job_limit','apply_validation_defaults',
           'isolated_validation_output','main'}
    tree=ast.parse(source.read_text())
    selected=ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names],type_ignores=[])
    namespace=dict(argparse=argparse,Path=Path,re=re,sys=SimpleNamespace(platform='linux',modules={}),
                   json=json,os=os,subprocess=subprocess,EXPERIMENTS=hosted.EXPERIMENTS,GROUPS={},GPU='figure-08-gpu',
                   GPU_CASES=('generation-B1',),ISOLATED_VALIDATION_EXPERIMENTS=frozenset({'correctness'}))
    exec(compile(selected,str(source),'exec'),namespace)
    return namespace


class DiagnosticReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve();(self.root/'ae/work').mkdir(parents=True)
        self.contract=review_contract();self.contract['REPO']=self.root
        self.contract['no_symlink_parents']=lambda p:p
        self.contract['parallel_output']=Mock()
        self.config={'review':{'validation_max_jobs':10,'parallel_quick_check':True}}
        self.contract['load_config']=Mock(return_value=self.config)
        self.locks=[]
        self.contract['run_lock']=self.lock
        self.selected=Mock(side_effect=self.select)
        self.contract['run_selected']=self.selected

    def flags(self,experiment='table-02-criu'):
        return ['--isolated-validation','--cpu-layout','numa03','--experiment',experiment,
                '--limit','2' if experiment=='table-02-fc-diff' else '1','--numa-node','0','--cpus','0-3','--output',str(self.root/'ae/results/diagnostic')]

    @contextlib.contextmanager
    def lock(self,path,*,shared=False,wait=False):
        fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(fd,fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        self.locks.append({'path':path,'shared':shared,'wait':wait})
        try:yield fd
        finally:os.close(fd)

    def select(self,args,parser):
        other=os.open(self.root/'ae/work/.results.lock',os.O_RDONLY)
        try:
            if args.cpu_layout=='numa03':
                with self.assertRaises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_SH|fcntl.LOCK_NB)
            else:fcntl.flock(other,fcntl.LOCK_SH|fcntl.LOCK_NB)
        finally:os.close(other)
        self.assertIsNone(args.max_events)
        return 0

    def call_main(self,flags):
        cube=ModuleType('ae.scripts.cube_paper_profile');cube.validate=Mock()
        e2b=ModuleType('ae.scripts.e2b_paper_profile');e2b.validate=Mock()
        with patch.dict(sys.modules,{'ae.scripts.cube_paper_profile':cube,'ae.scripts.e2b_paper_profile':e2b}):
            return self.contract['main'](flags)

    def test_diagnostic_obtains_exclusive_waiting_results_lock_before_selection(self):
        for experiment,limit in (('table-02-criu',1),('table-02-fc-diff',2)):
            with self.subTest(experiment=experiment):
                self.locks.clear()
                self.assertEqual(self.call_main(self.flags(experiment)),0)
                self.assertEqual(self.locks,[{'path':self.root/'ae/work/.results.lock','shared':False,'wait':True}])
                args=self.selected.call_args.args[0]
                self.assertEqual(args.experiment,[experiment])
                self.assertEqual(args.limit,limit)
                self.assertIsNone(args.max_events)

    def test_legacy_isolated_vm_keeps_existing_shared_lock(self):
        flags=self.flags();flags.remove('--cpu-layout');flags.remove('numa03')
        flags[flags.index('--experiment')+1]='correctness'
        self.assertEqual(self.call_main(flags),0)
        self.assertTrue(self.locks[0]['shared'])
        self.assertFalse(self.locks[0]['wait'])

    def test_foreign_complete_source_or_binding_resume_is_rejected(self):
        for experiment in ('table-02-criu','table-02-fc-diff'):
            args=self.contract['parser']().parse_args(self.flags(experiment))
            release={'source_sha256':'a'*64}
            previous={'experiments':[experiment],
                      'concurrency_policy':{'lane':'isolated-background-'+experiment.removeprefix('table-02-')+'-validation'},
                      'status':'interrupted','release':release,'measurement_request':{'node':0,'cpus':'0-3'}}
            self.contract['validate_isolated_baseline_resume'](args,previous,release)
            for altered in ({'status':'ok'},{'experiments':['table-02-e2b']},
                            {'experiments':['table-02-fc-diff' if experiment=='table-02-criu' else 'table-02-criu']},
                            {'concurrency_policy':{'lane':'main'}},{'release':{'source_sha256':'b'*64}},
                            {'measurement_request':{'node':1,'cpus':'28-31'}}):
                with self.subTest(experiment=experiment,altered=altered),self.assertRaisesRegex(ValueError,'same source and binding'):
                    self.contract['validate_isolated_baseline_resume'](args,{**previous,**altered},release)

    def test_direct_review_unsafe_modes_are_refused_before_lock_or_measurement(self):
        for experiment in ('table-02-criu','table-02-fc-diff'):
            for extra in (['--max-events','1'],['--available'],['--no-pin'],
                          ['--experiment-config',experiment+'=/other'],['--reuse-completed-from','/foreign'],
                          ['--baseline-inputs','all'],['--execute-plan','/foreign'],
                          ['--limit','1' if experiment=='table-02-fc-diff' else '2']):
                with self.subTest(experiment=experiment,extra=extra),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                    self.call_main(self.flags(experiment)+extra)
        self.assertFalse(self.locks)
        self.selected.assert_not_called()


if __name__=='__main__':unittest.main()
