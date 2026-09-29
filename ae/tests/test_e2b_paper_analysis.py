import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from ae.repro import analysis
from ae.scripts.e2b_paper_profile import COHORT
ROOT=Path(__file__).resolve().parents[2]

class PaperAnalysisTests(unittest.TestCase):
    def setUp(self):
        p=ROOT/'ae/repro/e2b_paper_analysis.py';self.assertTrue(p.is_file(),'Paper analysis must include validated original references')
        spec=importlib.util.spec_from_file_location('paper_analysis_test',p);self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
    def entry(self,index,checkpoint,restore,reference=False):
        row=COHORT[index];n=row[5];instance=row[0];release={'source_commit':'old' if reference else 'new','source_sha256':'a'*64}
        receipt={'instance':instance,'release':release,'run':{'path':'/'+release['source_commit']+'/'+instance+'/run.json'},'counts':{'checkpoints':n,'restores':n},'checks':[],'original_source':'/'+release['source_commit']}
        pilot={'ok':True,'instance':instance,'n_e2b_steps':n,'iterations':[{'e2b_steps':[{'ok':True,'checkpoint_persist_ms':checkpoint,'resume_ms':restore} for _ in range(n)]}],'controller_mock_stats':{'n_mismatch':0},'worker_mock_stats':{'n_mismatch':0}}
        return {'receipt':receipt,'pilot':pilot,'pilot_path':'/'+release['source_commit']+'/'+instance+'/pilot_result.json','referenced':reference}
    def test_referenced_and_new_events_use_weighted_mean_and_keep_sources(self):
        entries=[self.entry(0,10,20,True),self.entry(1,30,60)]
        result=self.mod.build_summary(Path('/current'),entries,[],{'source_commit':'planner'})
        rows=result['experiments']['table-02']['metrics'];ck=next(r for r in rows if r['group']=='All' and r['metric']=='checkpoint_ms')
        self.assertEqual(ck['n'],40);self.assertEqual(ck['value'],17)
        self.assertEqual(result['selection']['referenced_run_count'],1);self.assertEqual(result['selection']['new_run_count'],1)
        self.assertFalse(result['selection']['paper_cohort_verified'])
        self.assertEqual([x['source_commit'] for x in result['measurement_sources']],['old','new'])
    def test_all_reference_inputs_are_complete_without_new_measurement(self):
        entries=[self.entry(i,10,20,True) for i in range(8)]
        result=self.mod.build_summary(Path('/current'),entries,[],{})
        self.assertEqual(result['selection']['new_run_count'],0)
        self.assertEqual(result['selection']['referenced_run_count'],8)
        self.assertTrue(result['selection']['paper_cohort_verified'])
        self.assertEqual(result['selection']['checkpoint_restore_pairs'],185)
    def test_default_analyzer_routes_reference_only_paper_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'runs/table-02-e2b';stage.mkdir(parents=True)
            (stage/'suite.json').write_text(json.dumps({'measurement_identity':{'e2b_profile':'paper-nested'}}))
            with mock.patch('ae.repro.e2b_paper_analysis.analyze',return_value={'routed':True}) as selected:
                self.assertEqual(analysis.analyze_fresh(root/'runs'),{'routed':True})
            selected.assert_called_once_with(root,None)
    def test_duplicate_input_is_rejected(self):
        with self.assertRaises(ValueError):self.mod.build_summary(Path('/current'),[self.entry(0,10,20),self.entry(0,10,20)],[],{})

if __name__=='__main__':unittest.main()
