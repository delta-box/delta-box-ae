"""Figure 9 must retain measured points, missing bins, and population identity."""
import math
import unittest
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from ae.repro.paper_figure9 import figure9, figure9_metadata


class Figure9PlotTests(unittest.TestCase):
    def tearDown(self):
        plt.close('all')

    def row(self, lo, hi, y, n=1):
        return dict(panel='a',arm='ext4',bin_lo=lo*1024,bin_hi=hi*1024,
                    y=y,n_units=n,n_edits=n,source_identity='fresh',plot_group='one')

    def test_gap_is_not_interpolated_or_replaced_with_zero(self):
        result=dict(series=[self.row(1,8,8192),self.row(8,16,None,0),self.row(16,32,32768)])
        figure=figure9(plt,result)
        values=figure.axes[0].lines[0].get_ydata()
        self.assertEqual(values[0],8192)
        self.assertTrue(math.isnan(values[1]))
        self.assertEqual(values[2],32768)
        self.assertEqual(len(values),6)
        self.assertFalse(figure9_metadata(result)['historical_shading'])

    def test_conflicting_population_and_nonpositive_log_value_rejected(self):
        row=self.row(1,8,8192)
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            figure9_metadata(dict(series=[row,dict(row,plot_group='other')]))
        with self.assertRaisesRegex(ValueError,'positive'):
            figure9_metadata(dict(series=[self.row(1,8,0)]))

    def test_counts_and_source_identity_preserved(self):
        row=self.row(1,8,8192,7)
        point=figure9_metadata(dict(series=[row]))['points'][0]
        self.assertEqual(point['n_units'],7)
        self.assertEqual(point['source_identity'],'fresh')
        self.assertEqual(point['y'],8192)


if __name__=='__main__':
    unittest.main()
