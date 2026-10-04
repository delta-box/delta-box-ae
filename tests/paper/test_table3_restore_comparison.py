"""Table3 restores retain raw windows; archive values only form a separate reference."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from ae.repro import paper_tables


def row(metric,value,n,instance,cohort='one'):
    return dict(metric=metric,value=value,n=n,unit='ms',statistic='mean',
                backend='deltabox',mode='slow',experiment='table-03-slow',
                checkpoint_profile='async-incremental-lazy',run_purpose='full-trace',
                cohort=cohort,plot_group=cohort,evidence_kind='fresh_raw_events',instance=instance)

class RestoreWindows(unittest.TestCase):
    def fixture(self):
        return {'metrics':[row(k,v,n,i) for i,n,values in [
            ('a',1,{'restore_critical_ms':8.,'restore_table3_total_ms':13.,'restore_api_wall_ms':80.,'restore_slow_lazy_daemon_ms':4.}),
            ('b',3,{'restore_critical_ms':10.,'restore_table3_total_ms':15.,'restore_api_wall_ms':100.,'restore_slow_lazy_daemon_ms':5.})]
            for k,v in values.items()]}
    def test_four_direct_windows_are_event_weighted_without_subtraction(self):
        data=self.fixture();old=copy.deepcopy(data)
        groups=paper_tables.slow_restore_windows(data)
        self.assertEqual(len(groups),1);windows=groups[0]['windows']
        self.assertEqual({k:windows[k]['value'] for k in windows},
                         {'critical':9.5,'component':14.5,'api':95.,'lazy_daemon':4.75})
        self.assertTrue(all(v['n']==4 for v in windows.values()))
        self.assertEqual(data,old)
    def test_prestart_window_is_reported_only_when_recorded(self):
        data=self.fixture()
        self.assertNotIn('lazy_prestart',paper_tables.slow_restore_windows(data)[0]['windows'])
        data['metrics']+=[row('restore_slow_lazy_daemon_prestart_ms',v,n,i) for i,n,v in [('a',1,5.),('b',2,6.)]]
        window=paper_tables.slow_restore_windows(data)[0]['windows']['lazy_prestart']
        self.assertEqual((window['value'],window['n']),(17/3,3))
        self.assertEqual(paper_tables.slow_restore_windows(data)[0]['windows']['component']['n'],4)
    def test_missing_critical_never_uses_component_minus_daemon_or_api(self):
        data=self.fixture();data['metrics']=[r for r in data['metrics'] if r['metric']!='restore_critical_ms']
        windows=paper_tables.slow_restore_windows(data)[0]['windows']
        self.assertIsNone(windows['critical']['value']);self.assertEqual(windows['component']['value'],14.5)
    def test_missing_one_input_does_not_silently_shorten_window_population(self):
        data=self.fixture();data['metrics']=[r for r in data['metrics'] if not(r['metric']=='restore_critical_ms' and r['instance']=='b')]
        self.assertIsNone(paper_tables.slow_restore_windows(data)[0]['windows']['critical']['value'])
    def test_different_populations_remain_separate(self):
        data=self.fixture();extra=copy.deepcopy(data['metrics'])
        for r in extra:r['cohort']=r['plot_group']='two';r['value']*=2
        data['metrics']+=extra
        self.assertEqual(len(paper_tables.slow_restore_windows(data)),2)
    def test_duplicate_input_is_not_double_counted(self):
        data=self.fixture();data['metrics'].append(copy.deepcopy(data['metrics'][0]))
        self.assertIsNone(paper_tables.slow_restore_windows(data)[0]['windows']['critical']['value'])
    def test_fast_rows_do_not_enter_slow_windows(self):
        data=self.fixture()
        for r in data['metrics']:r['mode']='fast'
        self.assertEqual(paper_tables.slow_restore_windows(data),[])

class MatchedReference(unittest.TestCase):
    def setUp(self):
        from ae.repro.table3_restore import compare_windows
        self.compare=compare_windows
        self.data={'metrics':[row(k,v,n,i) for i,n,v in [('a',2,8.),('b',2,10.),('extra',5,100.)]
                    for k in ['restore_critical_ms']]}
        self.reference=dict(status='verified',metric='restore_critical_ms',
            inputs=[dict(instance='a',n=2,value=10.),dict(instance='b',n=2,value=10.)],
            value=10.,n=4,reference_kind='synthetic test reference')
    def test_matched_subset_is_by_reference_names_not_fastest_new_results(self):
        result=self.compare(paper_tables.slow_restore_windows(self.data),self.reference)[0]
        self.assertEqual(result['status'],'compared')
        self.assertEqual((result['current']['value'],result['current']['n']),(9.,4))
        self.assertEqual(result['percent_change'],-10.)
        self.assertEqual(result['reference_instances'],['a','b'])
        self.assertEqual(result['additional_current_instances'],['extra'])
    def test_missing_reference_input_cannot_compare_partial_to_whole(self):
        self.data['metrics']=[r for r in self.data['metrics'] if r['instance']!='b']
        r=self.compare(paper_tables.slow_restore_windows(self.data),self.reference)[0]
        self.assertEqual(r['status'],'unavailable');self.assertNotIn('percent_change',r)
    def test_event_count_change_cannot_claim_same_event_population(self):
        self.data['metrics'][0]['n']=3
        r=self.compare(paper_tables.slow_restore_windows(self.data),self.reference)[0]
        self.assertEqual(r['status'],'unavailable')
    def test_exact_twenty_percent_is_not_flagged(self):
        self.data={'metrics':[row('restore_critical_ms',.6,1,'a')]}
        self.reference.update(inputs=[dict(instance='a',n=1,value=.75)],value=.75,n=1)
        r=self.compare(paper_tables.slow_restore_windows(self.data),self.reference)[0]
        self.assertEqual(r['percent_change'],-20.);self.assertFalse(r['absolute_over_20_percent'])
    def test_reference_aggregate_cannot_disagree_with_input_weights(self):
        self.reference['value']=123.
        with self.assertRaisesRegex(ValueError,'aggregate'):
            self.compare(paper_tables.slow_restore_windows(self.data),self.reference)

    def test_unavailable_reference_keeps_current_windows_but_no_percentage(self):
        result=self.compare(paper_tables.slow_restore_windows(self.data),{'status':'unavailable','reason':'missing reference'})[0]
        self.assertEqual(result['status'],'unavailable');self.assertNotIn('percent_change',result)


class ReferenceFiles(unittest.TestCase):
    def setUp(self):
        from ae.repro.table3_restore import load_reference
        self.load=load_reference
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);base=self.root/'table-03';base.mkdir()
        (base/'cohort-deltabox.csv').write_text('instance,n_ckpt,n_restore\na,1,2\nb,1,2\nc,1,2\n')
        self.records=[];self.paths=[]
        for name,values,error in [('a',[8.,10.],0),('b',[10.,12.],0),('c',[1.,1.],1)]:
            path=base/'data/records/deltabox-slow/results'/('test/'+name+'.replay-test.results.jsonl');path.parent.mkdir(parents=True,exist_ok=True)
            rows=[{'kind':'ckpt'}]+[dict(kind='restore',restore_critical_ms=v,restore_wall_ms=v,restore_cleanup_ms=None) for v in values]+[dict(kind='run_summary',error_n=error)]
            raw = (chr(10).join(json.dumps(r) for r in rows) + chr(10)).encode()
            path.write_bytes(raw);self.paths.append(path)
            self.records.append(dict(target='paper/'+str(path.relative_to(self.root)),sha256=hashlib.sha256(raw).hexdigest()))
        self.metadata_hashes = {
            'table-03/cohort-deltabox.csv': hashlib.sha256((base/'cohort-deltabox.csv').read_bytes()).hexdigest()}
        from unittest.mock import patch
        metadata_patch = patch('ae.repro.table3_restore.REFERENCE_METADATA_SHA256',
                               self.metadata_hashes, create=True)
        metadata_patch.start();self.addCleanup(metadata_patch.stop)
        self.manifest=base/'files.jsonl';self.save_manifest()
    def save_manifest(self):
        self.manifest.write_text(chr(10).join(json.dumps(r) for r in self.records)+chr(10))
        self.metadata_hashes['table-03/files.jsonl'] = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
    def test_hash_verified_complete_reference_only_and_null_cleanup_retained(self):
        result=self.load(self.root)
        self.assertEqual(result['status'],'verified');self.assertEqual(result['value'],10.)
        self.assertEqual((result['input_count'],result['n']),(2,4))
        self.assertEqual(result['cleanup_missing_events'],4)
        self.assertEqual(result['selection']['missing_complete'],['c'])
    def test_all_reference_sources_have_verified_metadata_or_result_hashes(self):
        result=self.load(self.root)
        self.assertTrue(all(source['manifest_verified'] for source in result['sources']))
    def test_removed_cohort_input_is_rejected_instead_of_shortening_reference(self):
        cohort=self.root/'table-03/cohort-deltabox.csv'
        cohort.write_text(cohort.read_text().replace('a,1,2\n',''))
        with self.assertRaisesRegex(ValueError,'SHA-256'):self.load(self.root)
    def test_changed_cohort_event_count_is_rejected(self):
        cohort=self.root/'table-03/cohort-deltabox.csv'
        cohort.write_text(cohort.read_text().replace('a,1,2','a,1,3'))
        with self.assertRaisesRegex(ValueError,'SHA-256'):self.load(self.root)
    def test_removed_manifest_record_cannot_redefine_reference(self):
        self.manifest.write_text(chr(10).join(json.dumps(r) for r in self.records[1:])+chr(10))
        self.paths[0].unlink()
        with self.assertRaisesRegex(ValueError,'SHA-256'):self.load(self.root)
    def test_changed_reference_bytes_are_rejected(self):
        self.paths[0].write_text('{}\n')
        with self.assertRaisesRegex(ValueError,'SHA-256'):self.load(self.root)
    def test_missing_declared_file_does_not_shrink_reference_cohort(self):
        self.paths[0].unlink();result=self.load(self.root)
        self.assertEqual(result['status'],'unavailable');self.assertNotIn('value',result)
    def test_unmanifested_measurement_is_rejected(self):
        p=self.paths[0].with_name('extra.replay-test.results.jsonl');p.write_bytes(self.paths[0].read_bytes())
        with self.assertRaisesRegex(ValueError,'Unmanifested'):self.load(self.root)
    def test_bad_reference_is_rejected_before_any_report_output(self):
        from unittest.mock import patch
        from ae.scripts import build_review_comparison as report
        self.paths[0].write_text('{}'+chr(10))
        summary={'experiments':{'table-03':{'metrics':[row('restore_critical_ms',8.,2,'a'),row('restore_critical_ms',9.,2,'b')]}}}
        output=self.root/'report-output'
        with patch.object(report,'verify_inputs',return_value=(summary,[],{}, {}, {})),patch.object(report,'Canvas') as canvas:
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                report.build(coverage_path=self.root/'unused.json',output=output,table3_reference_root=self.root)
            canvas.assert_not_called()
        self.assertFalse(output.exists())

    def test_missing_bundle_has_no_printed_constant_fallback(self):
        self.assertEqual(self.load(self.root/'absent')['status'],'unavailable')

class TimingPage(unittest.TestCase):
    def test_global_aggregate_keeps_window_but_has_unknown_input_count(self):
        from unittest.mock import patch
        from ae.repro.table3_restore import build_timing_report,timing_markdown
        metric=row('restore_critical_ms',8.,2,'a');metric.pop('instance')
        with patch('ae.repro.table3_restore.load_reference',side_effect=AssertionError('No per-input identity')):
            report=build_timing_report({'metrics':[metric]},Path('/unused'))
        group=report['windows'][0]
        self.assertIsNone(group['instance_count'])
        self.assertEqual((group['windows']['critical']['value'],group['windows']['critical']['n']),(8.,2))
        self.assertEqual(report['comparisons'][0]['status'],'unavailable')
        self.assertIn('All input count: not recorded','\n'.join(timing_markdown(report)))
        self.assertIn('全部输入数：未记录','\n'.join(timing_markdown(report,language='zh')))
    def test_missing_fresh_critical_does_not_read_archive(self):
        from unittest.mock import patch
        from ae.repro.table3_restore import build_timing_report
        data={'metrics':[row('restore_table3_total_ms',13.,2,'a')]}
        with patch('ae.repro.table3_restore.load_reference',side_effect=AssertionError('No archive read for missing fresh field')):
            report=build_timing_report(data,Path('/unused'))
        self.assertIsNone(report['windows'][0]['windows']['critical']['value'])
    def test_both_languages_show_distinct_values_and_matched_reference(self):
        from ae.repro.table3_restore import compare_windows,timing_markdown
        data=RestoreWindows().fixture();groups=paper_tables.slow_restore_windows(data)
        ref={'status':'verified','inputs':[dict(instance='a',n=1,value=10.),dict(instance='b',n=3,value=10.)],'n':4,'value':10.}
        report={'windows':groups,'reference':ref,'comparisons':compare_windows(groups,ref)}
        for lang in ('en','zh'):
            text='\n'.join(timing_markdown(report,language=lang))
            for value in ('9.500000','14.500000','95.000000','4.750000','-5.00%'):
                self.assertIn(value,text)

if __name__=='__main__':unittest.main()
