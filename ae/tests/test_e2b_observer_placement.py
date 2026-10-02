"""Hosted observer identity, thread-only setup and fail-closed handshakes.

Kernel APIs are mocked here. The separate real Linux thread contract remains
required before accepting this candidate for installation.
"""
from contextlib import ExitStack
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ae.scripts import e2b_observer_placement as p
from ae.scripts import e2b_service_context as c


class Admission(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)/'runtime'
        self.unit = 'deltabox-ae-cpu-' + 'a'*32 + '.service'
        self.relative = '/system.slice/' + self.unit
        self.group = {'path': str(Path(self.tmp)/self.unit), 'device': 1, 'inode': 10,
            'cpuset.cpus.effective': '0-23,72-95', 'cpuset.mems.effective': '0,3', 'memory.swap.max': '0'}
        self.state = {'Id': self.unit, 'ControlGroup': self.relative, 'MainPID': '20',
            'ActiveState': 'active', 'SubState': 'running', 'Transient': 'yes',
            'WorkingDirectory': str(self.root), 'AllowedCPUs': '0-23 72-95',
            'AllowedMemoryNodes': '0 3', 'CPUAffinity': '4-7', 'NUMAPolicy': 'bind',
            'NUMAMask': '0', 'MemorySwapMax': '0'}
        self.chain = {pid: {'tid': pid, 'tgid': pid, 'parent_pid': pid-1, 'start_ticks': pid+1,
            'cgroup': '0::'+self.relative, 'uids': [0]*4} for pid in (20, 30, 40, 50)}
        for name, kwargs in [('ancestors', {'side_effect': lambda: copy.deepcopy(self.chain)}),
            ('task_identity', {'side_effect': lambda tid: copy.deepcopy(self.chain[tid])}),
            ('service_state', {'side_effect': lambda unit: copy.deepcopy(self.state)}),
            ('group_identity', {'side_effect': lambda path: copy.deepcopy(self.group)})]:
            self.stack.enter_context(patch.object(p, name, **kwargs))
        self.stack.enter_context(patch.object(p.os, 'getpid', return_value=50))
        self.stack.enter_context(patch.object(p.os, 'geteuid', return_value=0))

    def admit(self):
        return p.admit(self.root, 3, '72-75', {'numa_lease_owner': 40, 'results_lease_owner': 30})

    def test_exact03_layout_and_ancestry(self):
        a = self.admit()
        self.assertEqual(a['layout'], 'numa03')
        self.assertEqual(a['controller_cpus'], [4,5,6,7])
        self.assertEqual(a['controller_node'], 0)
        self.assertEqual(a['workload_cpus'], [72,73,74,75])

    def test_freeform_environment_does_not_select_cpus(self):
        with patch.dict(p.os.environ, {'AE_CPU_LAYOUT':'numa12', 'AE_OBSERVER_CPUS':'0-95'}):
            self.assertEqual(self.admit()['controller_cpus'], [4,5,6,7])

    def test_wrong_workload_lane_rejected(self):
        with self.assertRaises(RuntimeError):
            p.admit(self.root, 3, '4-7', {'numa_lease_owner':40,'results_lease_owner':30})

    def test_ancestor_main_pid_required(self):
        self.state['MainPID'] = '999'
        with self.assertRaisesRegex(RuntimeError, 'ancestor'): self.admit()

    def test_both_lease_owners_must_be_in_same_unit(self):
        self.chain[30]['cgroup'] = '0::/system.slice/foreign.service'
        with self.assertRaisesRegex(RuntimeError, 'lease owners'): self.admit()

    def test_non_root_unit_ancestor_rejected(self):
        self.chain[40]['uids'] = [1000]*4
        with self.assertRaisesRegex(RuntimeError, 'lease owners'): self.admit()

    def test_foreign_unit_rejected(self):
        self.chain[50]['cgroup'] = '0::/system.slice/foreign.service'
        with self.assertRaisesRegex(RuntimeError, 'owned hosted'): self.admit()

    def test_effective_mask_cannot_silently_intersect(self):
        self.group['cpuset.cpus.effective'] = '72-75'
        with self.assertRaisesRegex(RuntimeError, 'exact hosted'): self.admit()

    def test_forbidden_controller_memory_rejected(self):
        self.group['cpuset.mems.effective'] = '3'
        with self.assertRaisesRegex(RuntimeError, 'exact hosted'): self.admit()

    def test_policy_and_swap_are_exact(self):
        for key, value in [('NUMAPolicy','preferred'),('MemorySwapMax','max'),('Transient','no')]:
            with self.subTest(key=key):
                old=self.state[key];self.state[key]=value
                with self.assertRaises(RuntimeError): self.admit()
                self.state[key]=old

    def test_cgroup_or_pid_replacement_rejected_by_recheck(self):
        a=self.admit(); self.group['inode']+=1
        with self.assertRaisesRegex(RuntimeError, 'cgroup identity'): p.recheck(a)
        self.group['inode']-=1;self.chain[40]['start_ticks']+=1
        with self.assertRaisesRegex(RuntimeError, 'ancestor changed'): p.recheck(a)


class ThreadSetup(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.pid=100;self.tid=100;self.affinity=[72,73,74,75]
        self.policy={'mode':2,'nodes':[3]}
        self.identity=lambda tid: {'tid':tid,'tgid':100,'parent_pid':50,'start_ticks':tid+5,
            'cgroup':'0::/system.slice/owned.service','uids':[0]*4}
        self.a={'leader':self.identity(100),'workload_cpus':[72,73,74,75], 'workload_node':3,
            'controller_cpus':[4,5,6,7], 'controller_node':0,'group':{'path':'/group','inode':1}}
        mem=Mock(get=Mock(side_effect=lambda:copy.deepcopy(self.policy)))
        mem.bind.side_effect=lambda node:setattr(self,'policy',{'mode':2,'nodes':[node]})
        self.mem=mem
        self.stack.enter_context(patch.object(p,'MemoryPolicy',return_value=mem))
        p.MemoryPolicy.MPOL_BIND=2
        self.stack.enter_context(patch.object(p.os,'getpid',return_value=100))
        self.stack.enter_context(patch.object(p.threading,'get_native_id',side_effect=lambda:self.tid))
        self.stack.enter_context(patch.object(p,'task_identity',side_effect=lambda tid:self.identity(tid)))
        self.stack.enter_context(patch.object(p.os,'sched_getaffinity',side_effect=lambda tid:set(self.affinity),create=True))
        self.setter=self.stack.enter_context(patch.object(p.os,'sched_setaffinity',side_effect=lambda tid, cpus:setattr(self,'affinity',list(cpus)),create=True))
        self.stack.enter_context(patch.object(p,'recheck'))
        self.stack.enter_context(patch.object(p,'group_identity',side_effect=lambda path:copy.deepcopy(self.a['group'])))
        self.subject=p.ThreadPlacement(self.a)

    def test_only_current_worker_task_is_bound_with_actual_readbacks(self):
        self.tid=101;self.subject.setup()
        self.setter.assert_called_once_with(0,[4,5,6,7])
        self.mem.bind.assert_called_once_with(0)
        self.assertEqual(self.subject.receipt['ready']['identity']['tid'],101)
        self.assertEqual(self.subject.receipt['ready']['memory_policy'],{'mode':2,'nodes':[0]})

    def test_setup_on_main_is_rejected_before_any_mutation(self):
        with self.assertRaisesRegex(RuntimeError,'own hosted native'):self.subject.setup()
        self.setter.assert_not_called()

    def test_silent_cpu_intersection_rejected(self):
        self.tid=101;self.setter.side_effect=lambda *args:setattr(self,'affinity',[4])
        with self.assertRaisesRegex(RuntimeError,'placement changed'):self.subject.setup()

    def test_memory_policy_call_failure_is_fatal(self):
        self.tid=101;self.mem.bind.side_effect=PermissionError(1,'not permitted')
        with self.assertRaises(PermissionError):self.subject.setup()

    def test_memory_policy_readback_is_not_cgroup_allowance(self):
        self.tid=101;self.mem.bind.side_effect=None
        with self.assertRaisesRegex(RuntimeError,'placement changed'):self.subject.setup()

    def test_identity_reuse_and_affinity_changes_fail_scan_boundary(self):
        self.tid=101;self.subject.setup();self.affinity=[72,73,74,75]
        with self.assertRaisesRegex(RuntimeError,'placement changed'):self.subject.check()
        self.affinity=[4,5,6,7];self.tid=102
        with self.assertRaisesRegex(RuntimeError,'placement changed'):self.subject.check()

    def test_main_after_is_checked_by_main_thread_directly(self):
        self.tid=101;self.subject.setup();self.tid=100
        self.affinity=[72,73,74,75];self.policy={'mode':2,'nodes':[3]}
        self.subject.check_main()
        self.assertEqual(self.subject.receipt['main_before'],self.subject.receipt['main_after'])
        self.policy={'mode':2,'nodes':[0]}
        with self.assertRaisesRegex(RuntimeError,'changed the workload main'):self.subject.check_main()


class Handshake(unittest.TestCase):
    def test_ready_is_required_before_start_returns(self):
        entered, release=threading.Event(),threading.Event()
        observer=Mock(receipt={})
        def setup():entered.set();release.wait(2)
        observer.setup.side_effect=setup
        proof=c.VMProof(3,'72-75',observer=observer)
        proof.sample=Mock(side_effect=lambda:proof.stop.set())
        done=threading.Event()
        caller=threading.Thread(target=lambda:(proof.start(),done.set()))
        caller.start();self.assertTrue(entered.wait(1));self.assertFalse(done.is_set())
        release.set();caller.join(2);self.assertTrue(done.is_set());proof.stop_worker()

    def test_setup_error_records_separate_error_and_joins(self):
        observer=Mock(receipt={});observer.setup.side_effect=OSError(38,'unsupported')
        proof=c.VMProof(3,'72-75',observer=observer);proof.sample=Mock()
        with self.assertRaisesRegex(RuntimeError,'setup failed'):proof.start()
        self.assertFalse(proof.stop_worker());proof.sample.assert_not_called()
        self.assertIn('unsupported',proof.errors[0]);self.assertEqual(observer.receipt['error']['type'],'OSError')

    def test_readiness_timeout_does_not_allow_driver(self):
        proof=c.VMProof(3,'72-75',observer=Mock(receipt={}))
        proof.worker=Mock(ident=None);proof.ready=Mock(wait=Mock(return_value=False))
        with self.assertRaisesRegex(RuntimeError,'not ready'):proof.start()
        self.assertIn('readiness timed out',proof.errors[0])

    def test_stuck_worker_is_recorded_without_changing_prior_error(self):
        proof=c.VMProof(3,'72-75',observer=Mock(receipt={}))
        proof.worker=Mock(ident=123,is_alive=Mock(return_value=True))
        proof.errors=['original placement failure']
        self.assertTrue(proof.stop_worker())
        self.assertEqual(proof.errors,['original placement failure','VM sampling did not terminate'])

    def test_sampling_error_is_preserved_and_finish_fails(self):
        proof=c.VMProof(3,'72-75',observer=Mock(receipt={}))
        proof.sample=Mock(side_effect=ValueError('known violation'))
        proof.worker.start();proof.worker.join(2)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError,'placement verification'):proof.finish(Path(d)/'proof.json')
            saved=json.loads((Path(d)/'proof.json').read_text())
            self.assertIn('known violation',saved['errors'][0]);self.assertIn('observer',saved)


class Restoration(unittest.TestCase):
    def workflow(self, folder, *, setup_error=False, stuck=False, body_error=False):
        from tests.paper import test_e2b_service_context as fixture
        class Fake(fixture.FakeProof):
            def start(self):
                if setup_error:
                    self.errors.append('synthetic setup failure')
                    raise RuntimeError('observer setup failed')
                super().start()
            def stop_worker(self):
                self.stop.set()
                return stuck
        case=fixture.E2BServiceTests()
        with patch.object(fixture,'FakeProof',Fake):
            result=case.workflow(folder,storage=True,body_error=body_error)
        return case,result

    def test_setup_failure_restores_services_storage_and_preserves_primary(self):
        with tempfile.TemporaryDirectory() as folder:
            case,(commands,guard,error,root)=self.workflow(folder,setup_error=True)
            self.assertEqual(error,'observer setup failed');self.assertFalse(guard)
            case.owned_storage.restore_stopped.assert_called_once()
            result=json.loads((root/'evidence/transaction-result.json').read_text())
            self.assertEqual(result['original_error']['message'],'observer setup failed')
            self.assertEqual(result['restoration_errors'],[])
            self.assertEqual(sum(row[:2]==('systemctl','stop') for row in commands),2)

    def test_stuck_observer_does_not_skip_restoration_but_retains_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            case,(commands,guard,error,root)=self.workflow(folder,stuck=True)
            self.assertTrue(guard);self.assertIn('remains alive',error)
            case.owned_storage.restore_stopped.assert_called_once()
            case.owned_storage.verify_restored.assert_called_once()
            self.assertEqual(sum(row[:2]==('systemctl','start') for row in commands),4)

    def test_original_driver_error_and_stuck_observer_are_both_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            case,(commands,guard,error,root)=self.workflow(folder,stuck=True,body_error=True)
            self.assertTrue(guard)
            self.assertEqual(str(case.last_error.__cause__),'producer failed')
            result=json.loads((root/'evidence/transaction-result.json').read_text())
            self.assertEqual(result['original_error']['message'],'producer failed')
            self.assertIn('remains alive',result['restoration_errors'][0]['message'])
            case.owned_storage.restore_stopped.assert_called_once()


if __name__ == '__main__':
    unittest.main()
