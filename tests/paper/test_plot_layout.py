"""Paper layouts retain independent values while sharing the original axes/table."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'Plotting extra is not installed')
class PaperPlotIntegrationTests(unittest.TestCase):
    def test_explicit_archive_keeps_its_historical_renderer(self):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from ae.repro.plot import figure1
        rows = [dict(backend='e2b', operation='checkpoint', metric=name,
                     value=value, evidence_kind='published_adjustment')
                for name, value in (('total', 100.), ('filesystem', 40.), ('process', 60.))]
        figure = figure1(plt, dict(source='archived', metrics=rows))
        try:
            self.assertEqual([bar.get_height() for bar in figure.axes[0].patches], [40., 60.])
        finally:
            plt.close(figure)

    def test_multiple_systems_share_one_fixed_table_without_pooling(self):
        from ae.repro.plot import render
        rows = [dict(backend=backend, group='Django', metric='checkpoint_ms', value=value,
                     unit='ms', n=n, statistic='mean', plot_group=group,
                     experiment=experiment, mode=mode, source_identity='release-sha256:fixture')
                for backend, value, n, group, experiment, mode in (
                    ('deltabox', 12., 29, 'fast', 'table-02-deltabox', 'fast'),
                    ('cube', 1200., 17, 'cube', 'table-02-cube', None),
                    ('deltabox', 99., 12, 'slow', 'table-03-slow', 'slow'))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / 'summary.json'
            summary.write_text(json.dumps(dict(schema_version=1, source='fresh',
                experiments={'table-02': dict(metrics=rows, series=[])})))
            result = render(summary, root / 'plots')
            self.assertEqual(len(result['artifacts']), 2)  # one PNG and one PDF
            for artifact in result['artifacts']:
                self.assertEqual(artifact['layout'], 'paper')
                self.assertEqual(artifact['populations'], ['cube', 'fast', 'slow'])
                cells = artifact['data_mapping']['cells']
                def value(backend):
                    return next(c for c in cells if c['row']=='Django' and c['backend']==backend and c['column']=='ck')
                self.assertEqual((value('deltabox')['value'], value('deltabox')['n']), (12., 29))
                self.assertEqual((value('cube')['value'], value('cube')['n']), (1200., 17))
                self.assertEqual(len(cells), 60)
                self.assertNotIn('population-', Path(artifact['path']).name)
            self.assertEqual({Path(row['path']).name for row in result['rendering']['files']},
                             {'plot.py', 'paper_tables.py', 'paper_figure1.py',
                              'paper_figure2.py', 'paper_figure6.py', 'paper_figure7.py', 'paper_figure9.py'})

if __name__ == '__main__':
    unittest.main()
