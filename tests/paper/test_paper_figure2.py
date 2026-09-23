"""Verify Figure 2's unit conversion, population boundaries and missing slots."""
import copy
import importlib.util
import json
import math
import unittest

from ae.repro.paper_figure2 import figure2_metadata


def fixture():
    result = dict(metrics=[], series=[])
    for domain, delta, total, unit in (("filesystem", 2., 4096., "KiB"),
                                        ("memory", 4., 64., "MiB")):
        identity = dict(plot_group=domain + "-population", source_identity="fresh-release",
                        evidence_kind="fresh_raw_events")
        for metric, value, n in (("step_delta", delta, 3), ("total", total, 2)):
            result["metrics"].append(dict(domain=domain, panel="a", metric=metric, value=value,
                                           unit=unit, n=n, statistic="mean", **identity))
        for step, value in ((1, 0.), (2, 1.), (3, 8.)):
            result["series"].append(dict(domain=domain, panel="b", metric="step_delta", x=step,
                                          x_unit="step", y=value, unit=unit, n=1, **identity))
    return result


class Figure2MappingTests(unittest.TestCase):
    def test_separate_domains_use_explicit_decimal_unit_conversions(self):
        data = figure2_metadata(fixture())
        json.dumps(data, allow_nan=False)
        fs, mem = data["domains"]["filesystem"], data["domains"]["memory"]
        self.assertEqual(fs["status"], "measured")
        self.assertEqual(mem["status"], "measured")
        self.assertEqual(fs["metrics"]["step_delta"]["displayed_value"], 2048.)
        self.assertEqual(fs["metrics"]["total"]["value_bytes"], 4096 * 1024)
        self.assertEqual(fs["series"]["points"][1]["y"], 1.024)
        self.assertEqual(fs["series"]["points"][0]["y"], 0.)
        self.assertAlmostEqual(mem["metrics"]["step_delta"]["displayed_value"], 4.194304)
        self.assertAlmostEqual(mem["series"]["points"][2]["y"], 8.388608)
        self.assertEqual(mem["metrics"]["total"]["conversion_factor"], 1.048576)
        self.assertEqual(mem["total_to_delta_ratio"], 16.)
        self.assertEqual(mem["series"]["points"][2]["n"], 1)

    def test_conflicting_sources_are_not_averaged_or_joined(self):
        for field in ("plot_group", "source_identity", "checkpoint_profile", "input_summary"):
            with self.subTest(field=field):
                result = fixture()
                result["series"][0][field] = "another-population"
                data = figure2_metadata(result)["domains"]
                fs = data["filesystem"]
                self.assertTrue(fs["population_conflict"])
                self.assertEqual(fs["status"], "unavailable")
                self.assertEqual(fs["series"]["points"], [])
                self.assertIsNone(fs["metrics"]["total"]["displayed_value"])
                self.assertIsNone(fs["total_to_delta_ratio"])
                self.assertIn("Multiple populations", fs["metrics"]["total"]["reason"])
                self.assertEqual(data["memory"]["status"], "measured")

    def test_unavailable_total_does_not_hide_measured_deltas_or_infer_a_ratio(self):
        result = fixture()
        result["metrics"][1].update(value=None, evidence_kind="unavailable", n=0,
                                    reason="No baseline bytes for this population.")
        fs = figure2_metadata(result)["domains"]["filesystem"]
        self.assertEqual(fs["status"], "partial")
        self.assertEqual(fs["metrics"]["step_delta"]["displayed_value"], 2048.)
        self.assertEqual(len(fs["series"]["points"]), 3)
        self.assertIsNone(fs["total_to_delta_ratio"])
        self.assertEqual(fs["metrics"]["total"]["reason"], "No baseline bytes for this population.")

    def test_duplicate_points_and_unmeasured_or_invalid_values_are_explicitly_unavailable(self):
        result = fixture()
        result["series"].append(copy.deepcopy(result["series"][0]))
        data = figure2_metadata(result)
        self.assertEqual(data["domains"]["filesystem"]["series"]["points"], [])
        self.assertIn("Duplicate step", data["domains"]["filesystem"]["series"]["reason"])
        for change in (dict(value=float("nan")), dict(unit="MB_archived"), dict(n=0),
                       dict(evidence_kind="published_adjustment"), dict(statistic="median")):
            with self.subTest(change=change):
                result = fixture()
                result["metrics"][0].update(change)
                data = figure2_metadata(result)
                json.dumps(data, allow_nan=False)
                self.assertEqual(data["domains"]["filesystem"]["metrics"]["step_delta"]["status"], "unavailable")
                self.assertIsNone(data["domains"]["filesystem"]["total_to_delta_ratio"])


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
class Figure2LayoutTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt

    def tearDown(self):
        self.plt.close("all")

    def test_paper_axes_keep_real_bar_endpoints_and_all_steps(self):
        from ae.repro.paper_figure2 import figure2
        result = fixture()
        result["metrics"][0].update(value=1., unit="bytes")
        result["series"][-1].update(x=45, y=1200.)
        figure = figure2(self.plt, result)
        figure.canvas.draw()
        axes = {axis.get_label(): axis for axis in figure.axes}
        self.assertEqual(list(figure.get_size_inches()), [3.4, 1.4])
        self.assertEqual(len(axes), 4)
        fs, mem = axes["figure2-a-filesystem"], axes["figure2-a-memory"]
        self.assertEqual(fs.get_xscale(), "log")
        self.assertEqual(mem.get_xscale(), "linear")
        self.assertLess(fs.get_xlim()[0], 1.)
        self.assertAlmostEqual(fs.patches[0].get_x() + fs.patches[0].get_width(), 1.)
        self.assertAlmostEqual(fs.patches[1].get_x() + fs.patches[1].get_width(), 4096 * 1024)
        self.assertAlmostEqual(mem.patches[0].get_width(), 4.194304)
        fs_steps, dirty = axes["figure2-b-filesystem"], axes["figure2-b-memory"]
        self.assertEqual([patch.get_height() for patch in fs_steps.patches], [0., 1.024, 8.192])
        self.assertGreaterEqual(fs_steps.get_xlim()[1], 45.5)
        self.assertGreater(dirty.get_ylim()[1], 1200 * 1.048576)
        self.assertEqual(list(dirty.lines[0].get_xdata())[:2], [1, 2])
        self.assertTrue(math.isnan(dirty.lines[0].get_xdata()[2]))
        self.assertEqual(dirty.lines[0].get_xdata()[-1], 45)
        self.assertAlmostEqual(mem.get_position().y0, fs_steps.get_position().y0)
        self.assertLess(mem.get_position().height, fs_steps.get_position().height)

    def test_missing_slots_keep_axes_and_legends_without_fake_numeric_bars(self):
        from ae.repro.paper_figure2 import figure2
        figure = figure2(self.plt, dict(metrics=[], series=[]))
        figure.canvas.draw()
        self.assertEqual(len(figure.axes), 4)
        self.assertTrue(all(not axis.patches and not axis.lines for axis in figure.axes))
        texts = [text.get_text() for axis in figure.axes for text in axis.texts]
        self.assertEqual(sum("N/A" in text for text in texts), 6)
        self.assertEqual(sum(axis.get_legend() is not None for axis in figure.axes), 2)
        self.assertEqual(figure2_metadata(dict())["axis_limits"]["step"], [0., 30.5])


if __name__ == "__main__":
    unittest.main()
