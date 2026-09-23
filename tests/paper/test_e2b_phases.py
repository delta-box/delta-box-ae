import unittest
from ae.repro.e2b_phases import partition, measured_phases, model_components

class PhaseTests(unittest.TestCase):
    def span(self, name, a, b):
        return dict(name=name,start_unix_ns=a*1000000,end_unix_ns=b*1000000)

    def test_overlap_nested_clipping_and_unknown_are_not_double_counted(self):
        spans=[self.span('fs',-2,5), self.span('mem',3,8), self.span('nested',4,7), self.span('outer',-5,12)]
        p=partition([0,10000000],spans,dict(fs='filesystem',mem='process',nested='process'))
        self.assertEqual(p['filesystem'],3)
        self.assertEqual(p['process'],3)
        self.assertEqual(p['overlapping_phases'],2)
        self.assertEqual(p['unclassified_api'],2)
        self.assertEqual(sum(p.values()),10)

    def test_windows_must_match_api_and_upload_is_unclassified(self):
        row=dict(pause_ms=10.,snapshot_upload_ms=5.,checkpoint_persist_ms=15.,resume_ms=10.,
                 phase_windows=dict(pause=[1000000,11000000],upload=[12000000,17000000],resume=[20000000,30000000]),
                 phase_spans=[self.span('process-memory',1,11),self.span('sandbox-wait-for-start',20,30)])
        phases=measured_phases(row)
        self.assertEqual(phases[0][2]['process'],10)
        self.assertEqual(phases[0][2]['unclassified_api'],5)
        self.assertEqual(phases[1][2]['guest_readiness'],10)
        row['pause_ms']=11
        with self.assertRaises(ValueError): measured_phases(row)

    def test_model_includes_action_llm_once_and_counts_no_action_builds(self):
        floor=dict(protocol='served-controller-build-action-v1',node_id=1,start_cursor=0,end_cursor=1,served=1,
                   recorded_ms=100,before_stats=dict(cursor=0,n_served=0),after_stats=dict(cursor=1,n_served=1))
        data=dict(iterations=[dict(node_id=1,event=dict(controller_llm_floor=floor,n_worker_actions=1,
            action_events=[dict(node_id=1,action_wall_ms=500)]),
            e2b_steps=[dict(ok=True,resume_ms=20,checkpoint_persist_ms=30,pause_ms=25)])])
        self.assertEqual(model_components(data),(600,50))
        data['iterations'][0]['event']['action_events'][0]['node_id']=2
        with self.assertRaises(ValueError): model_components(data)

if __name__=='__main__': unittest.main()
