"""Publication integrity checks use synthetic metadata, not measurement samples."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('publish_comparison', Path(__file__).with_name('publish_comparison.py'))
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.readme = self.root/'ae/README.md'
        self.readme.parent.mkdir()
        self.readme.write_text('KEEP ALL SEVEN EXAMPLE CELLS\n')
        self.review = self.root/'ae/report/formal'
        self.output = self.review/'comparison/attempt-001'
        self.output.mkdir(parents=True)
        self.source = 'a'*64
        self.release = dict(source_commit='b'*40, source_sha256=self.source)
        self.coverage = self.review/'coverage/attempt-001/review.json'
        self.analysis = self.review/'analysis/attempt-001/summary.json'
        self.plots = self.review/'plots/attempt-001/plots.json'
        self.write(self.coverage, {'release':self.release})
        self.write(self.analysis, {'source':'fresh','experiments':{'table-02':{
            'metrics':[{'source_identity':'release-sha256:'+self.source}]}}})
        self.write(self.plots, {'source':'fresh','input_sha256':publication.sha256(self.analysis),'artifacts':[]})
        self.manifest = dict(schema_version=1,kind='fresh-review-paper-comparison',source='fresh',release=self.release,
            coverage=self.record(self.coverage),analysis=self.record(self.analysis),plots=self.record(self.plots),items=[])
        for key in publication.ITEMS:
            original=self.readme.parent/'reference/figures'/(key+'.png')
            original.parent.mkdir(parents=True,exist_ok=True);original.write_bytes(b'synthetic paper fixture')
            artifacts=[]
            for kind in ('ae','comparison'):
                image=self.output/f'{key}-{kind}.png';image.write_bytes(b'synthetic rendering fixture')
                artifacts.append(dict(path=image.name,kind=kind,sha256=publication.sha256(image)))
            self.manifest['items'].append(dict(experiment=key,paper_item=key,status='fresh-results',
                original={'sha256':publication.sha256(original)},artifacts=artifacts,
                coverage=[dict(experiment=key,status='partial',successful_jobs=1,planned_jobs=3,failed_jobs=1)]))
        self.manifest_path=self.output/'manifest.json'
        self.write(self.manifest_path,self.manifest)

    def write(self,path,obj):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj))

    def record(self,path):
        return dict(path='/former/remote/'+path.name,sha256=publication.sha256(path))

    def generate(self,name='snippets.md',source=None):
        return publication.publish(self.manifest_path,self.readme,self.root/name,source or self.source)

    def replace_analysis(self,data):
        self.write(self.analysis,data)
        self.write(self.plots,{'source':'fresh','input_sha256':publication.sha256(self.analysis),'artifacts':[]})
        self.manifest['analysis']=self.record(self.analysis);self.manifest['plots']=self.record(self.plots)
        self.write(self.manifest_path,self.manifest)

    def test_relocated_attempt_generates_example_cells_and_preserves_readme(self):
        result=self.generate().read_text()
        self.assertEqual(result.count('<td align="center">'),len(publication.ITEMS))
        cells = result.split('<!-- AE-RESULT:')[1:]
        for cell in cells:
            if ':start -->' not in cell:
                continue
            self.assertIn('Example script output', cell)
            self.assertNotIn('成功 1/3', cell)
            self.assertNotIn('manifest.json', cell)
            self.assertNotIn('coverage/', cell)
            self.assertNotIn(self.release['source_commit'][:12], cell)
        self.assertIn('report/formal/comparison/attempt-001/table-02-ae.png',result)
        self.assertNotIn('/former/remote',result)
        self.assertEqual(self.readme.read_text(),'KEEP ALL SEVEN EXAMPLE CELLS\n')
        with self.assertRaisesRegex(ValueError,'existing output'):self.generate()

    def test_root_readmes_use_root_relative_links_and_matching_language(self):
        for name, caption in (('README-zh.md', '脚本输出示例'),
                              ('README.md', 'Example script output')):
            with self.subTest(readme=name):
                self.readme = self.root/name
                self.readme.write_text('KEEP ROOT README\n')
                result = self.generate(name + '.snippets').read_text()
                self.assertIn('href="ae/report/formal/comparison/attempt-001/table-02-ae.png"', result)
                self.assertIn(caption, result)
                self.assertEqual(self.readme.read_text(), 'KEEP ROOT README\n')

    def test_source_or_image_change_refuses_to_publish(self):
        with self.assertRaisesRegex(ValueError,'source SHA'):self.generate(source='c'*64)
        (self.output/'table-02-ae.png').write_bytes(b'changed bytes')
        with self.assertRaisesRegex(ValueError,'changed publication'):self.generate()
        self.assertFalse((self.root/'snippets.md').exists())

    def test_changed_coverage_or_missing_fresh_data_refuses_to_publish(self):
        self.write(self.coverage,{'release':self.release,'changed':True})
        with self.assertRaisesRegex(ValueError,'changed publication'):self.generate()
        self.manifest['source']='no-fresh-results'
        self.write(self.manifest_path,self.manifest)
        with self.assertRaisesRegex(ValueError,'completed fresh'):self.generate()

    def test_different_measured_source_cannot_hide_behind_top_level_release(self):
        data=json.loads(self.analysis.read_text())
        data['experiments']['table-02']['metrics'][0]['source_identity']='release-sha256:'+'c'*64
        self.replace_analysis(data)
        with self.assertRaisesRegex(ValueError,'different measured'):self.generate()

    def test_unpublished_correctness_without_identity_does_not_reject_figures(self):
        data=json.loads(self.analysis.read_text())
        data['experiments']['correctness']={
            'metrics':[{'metric':'suite_pass','value':1} for _ in range(3)],
            'series':[],
        }
        self.replace_analysis(data)
        result=self.generate().read_text()
        self.assertEqual(result.count('<td align="center">'),len(publication.ITEMS))

    def test_every_published_metric_and_series_rejects_unknown_or_mixed_source(self):
        original=json.loads(self.analysis.read_text())
        for key in publication.ITEMS:
            for collection in ('metrics','series'):
                for identity in (None,'release-sha256:'+'c'*64):
                    with self.subTest(experiment=key,collection=collection,identity=identity):
                        data=json.loads(json.dumps(original))
                        row={} if identity is None else {'source_identity':identity}
                        data['experiments'].setdefault(key,{}).setdefault(collection,[]).append(row)
                        self.replace_analysis(data)
                        with self.assertRaisesRegex(ValueError,'unknown or different measured'):
                            self.generate()
                        self.assertFalse((self.root/'snippets.md').exists())


if __name__ == '__main__':
    unittest.main()
