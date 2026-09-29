"""Queue refills idle nodes without duplicate groups or service overlap."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from repro.cpu_work_queue import WorkQueue, recover_groups, permits_scope_expansion
from repro.common import file_record


class CPUWorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'cpu-work-queue.json'

    def initialize(self, names, groups=None):
        WorkQueue.initialize(self.path, names, {1: '28-31', 2: '48-51'}, groups=groups)
        return WorkQueue(self.path, 1), WorkQueue(self.path, 2)

    def test_idle_node_takes_next_group_while_peer_is_running(self):
        first, second = self.initialize(['table-02-replay', 'table-02-criu', 'table-02-fc-diff'])
        self.assertEqual(second.claim(), ('claimed', 'table-02-replay'))
        self.assertEqual(first.claim(), ('claimed', 'table-02-criu'))
        first.finish('table-02-criu', {'experiment': 'table-02-criu', 'status': 'ok'})
        self.assertEqual(first.claim(), ('claimed', 'table-02-fc-diff'))
        state = json.loads(self.path.read_text())
        self.assertEqual(state['groups']['table-02-replay']['status'], 'running')
        self.assertEqual(len([v for v in state['groups'].values() if v['status'] == 'running']), 2)

    def test_shared_services_are_serialized_but_independent_work_can_run(self):
        first, second = self.initialize(['table-02-cube', 'table-02-e2b', 'table-02-criu'])
        self.assertEqual(first.claim(), ('claimed', 'table-02-cube'))
        self.assertEqual(second.claim(), ('claimed', 'table-02-criu'))
        second.finish('table-02-criu', {'experiment': 'table-02-criu', 'status': 'ok'})
        self.assertEqual(second.claim(), ('wait', None))
        first.finish('table-02-cube', {'experiment': 'table-02-cube', 'status': 'ok'})
        self.assertEqual(second.claim(), ('claimed', 'table-02-e2b'))

    def test_partial_group_keeps_node_and_completed_group_is_not_run_again(self):
        names=['table-02-deltabox', 'table-02-replay', 'table-02-criu']
        row={'experiment':names[0], 'status':'ok'}
        groups={names[0]:{'status':'ok','node':1,'row':row}, names[1]:{'status':'pending','node':2}, names[2]:{'status':'pending','node':None}}
        first,second=self.initialize(names,groups)
        self.assertEqual(first.completed_rows(), [row])
        self.assertEqual(first.claim(), ('claimed', 'table-02-criu'))
        self.assertEqual(second.claim(), ('claimed', 'table-02-replay'))

    def test_claim_and_completion_ownership_are_enforced(self):
        first,second=self.initialize(['a','b'])
        first.claim()
        with self.assertRaises(ValueError): first.claim()
        with self.assertRaises(ValueError): second.finish('a', {'status':'ok'})
        first.finish('a', {'status':'failed'})
        self.assertEqual(second.claim(), ('done',None))

    def test_scope_expansion_never_removes_old_groups_or_accepts_gpu(self):
        cpu=['a','b','c']
        self.assertTrue(permits_scope_expansion(['a'],cpu,cpu))
        self.assertFalse(permits_scope_expansion(['a','gpu'],cpu,cpu))
        self.assertFalse(permits_scope_expansion(['a'],['a','b'],cpu))
        self.assertFalse(permits_scope_expansion([],cpu,cpu))

    def completed_fixture(self):
        config=self.root/'config.json';config.write_text('{}')
        lane=self.root/'lanes/numa1';job=lane/'runs/a/job';job.mkdir(parents=True)
        result=job/'result.json';result.write_text('{"ok":true}')
        artifact={'path':'result.json','bytes':result.stat().st_size,'sha256':hashlib.sha256(result.read_bytes()).hexdigest()}
        (job/'run.json').write_text(json.dumps({'analysis_mode':'fresh-measurement','status':'ok','experiment':'a','artifacts':[artifact]}))
        (job.parent/'suite.json').write_text(json.dumps({'status':'ok','jobs':[{'key':'job','status':'ok'}]}))
        row={'experiment':'a','status':'ok','config_source':file_record(config),'effective_config':file_record(config),'successful_jobs':1,'planned_jobs':1}
        (lane/'review.json').write_text(json.dumps({'measurement_request':{'node':1,'cpus':'28-31'},'coverage':[row]}))
        return config,result,lane

    def test_completed_manifest_hashes_checked_before_skip(self):
        config,result,lane=self.completed_fixture();checked=[]
        groups=recover_groups(self.root,['a','b'],{1:'28-31',2:'48-51'},config,lambda *a,**k:checked.append(True))
        self.assertEqual(groups['a']['status'],'ok');self.assertEqual(checked,[True]);self.assertEqual(groups['b']['status'],'pending')
        result.write_text('changed')
        with self.assertRaises(ValueError):recover_groups(self.root,['a','b'],{1:'28-31',2:'48-51'},config,lambda *a,**k:None)

    def test_old_node_binding_cannot_be_relabelled(self):
        config,_,lane=self.completed_fixture();p=lane/'review.json';j=json.loads(p.read_text());j['measurement_request']['node']=0;p.write_text(json.dumps(j))
        with self.assertRaises(ValueError):recover_groups(self.root,['a','b'],{1:'28-31',2:'48-51'},config,lambda *a,**k:None)


if __name__=='__main__':unittest.main()
