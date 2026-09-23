"""Figure 7 retains the paper's slots without fabricating missing measurements."""
import importlib.util
import json
import unittest

from ae.repro.paper_figure7 import figure7_metadata


def metrics(ratio=1.234, *, group="Django", backend="deltabox", **labels):
    return [dict(metric=name, value=value, unit=unit, backend=backend, group=group,
                 n=3, statistic="mean", evidence_kind="derived_model", modeled=True, **labels)
            for name, value, unit in (("floor_s", 100., "s"), ("wall_s", 123.4, "s"),
                                      ("ratio", ratio, "ratio"))]


class Figure7MappingTests(unittest.TestCase):
    def test_saved_ratio_and_provenance_are_retained_without_recalculation(self):
        rows = metrics(1.234567, source_identity="source-a", plot_group="a")
        original = json.dumps(rows, sort_keys=True)
        metadata = figure7_metadata(dict(metrics=rows))
        self.assertEqual(len(metadata["slots"]), 8)
        slot = metadata["slots"][0]
        self.assertEqual(slot["displayed_ratio"], 1.234567)
        self.assertEqual(slot["status"], "derived_model")
        self.assertEqual((slot["n"], slot["statistic"]), (3, "mean"))
        self.assertEqual(slot["available_metrics"][2]["source_identity"], "source-a")
        self.assertEqual(sum(s["status"] == "unavailable" for s in metadata["slots"]), 7)
        self.assertEqual(original, json.dumps(rows, sort_keys=True))
        self.assertIn("component-sum models", " ".join(metadata["notes"]))
        self.assertIn("not a measured end-to-end", " ".join(metadata["notes"]))
        json.dumps(metadata, allow_nan=False)

    def test_ratio_of_sums_matches_summed_components_but_not_mixed_means(self):
        rows = metrics(1.234567)
        for row in rows:
            row["statistic"] = "ratio-of-sums" if row["metric"] == "ratio" else "sum"
        slot = figure7_metadata(dict(metrics=rows))["slots"][0]
        self.assertEqual(slot["status"], "derived_model")
        self.assertEqual(slot["displayed_ratio"], 1.234567)
        rows[0]["statistic"] = "mean"
        slot = figure7_metadata(dict(metrics=rows))["slots"][0]
        self.assertEqual(slot["status"], "unavailable")

    def test_conflicting_population_is_not_selected_or_averaged(self):
        for field in ("plot_group", "source_identity", "cohort", "experiment", "mode", "source_summary"):
            with self.subTest(field=field):
                rows = metrics(1.2, **{field: "a"}) + metrics(1.8, **{field: "b"})
                rows += metrics(1.4, group="SymPy")
                metadata = figure7_metadata(dict(metrics=rows))
                slot = metadata["slots"][0]
                self.assertIsNone(slot["displayed_ratio"])
                self.assertEqual(slot["status"], "unavailable")
                self.assertIn("Multiple populations", slot["reason"])
                self.assertEqual(len(slot["available_metrics"]), 6)
                self.assertEqual(metadata["slots"][2]["displayed_ratio"], 1.4)

    def test_missing_duplicate_or_invalid_ratio_is_never_inferred(self):
        rows = metrics()
        for candidate, reason in ((rows[:2], "No supplied ratio"),
                                  (rows + rows[-1:], "Duplicate aggregate"),
                                  (metrics(-1), "finite nonnegative")):
            with self.subTest(reason=reason):
                slot = figure7_metadata(dict(metrics=candidate))["slots"][0]
                self.assertIsNone(slot["displayed_ratio"])
                self.assertIn(reason, slot["reason"])
        rows[0]["source_identity"] = "different-floor"
        slot = figure7_metadata(dict(metrics=rows))["slots"][0]
        self.assertIsNone(slot["displayed_ratio"])
        self.assertIn("Multiple populations", slot["reason"])


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
class Figure7PaperLayoutTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt

    def tearDown(self):
        self.plt.close("all")

    def test_one_populated_slot_keeps_all_groups_and_both_system_positions(self):
        from ae.repro.paper_figure7 import figure7
        figure = figure7(self.plt, dict(metrics=metrics(1.234567)))
        figure.canvas.draw()
        axis = figure.axes[0]
        self.assertEqual(list(figure.get_size_inches()), [3.4, 1.62])
        self.assertEqual([label.get_text() for label in axis.get_xticklabels()],
                         ["Django", "SymPy", "Scientific", "Tools/Small"])
        self.assertEqual(axis.get_ylim(), (0., 2.38))
        self.assertEqual(sum(text.get_text() == "N/A" for text in axis.texts), 7)
        self.assertEqual(len(axis.patches), 2)
        self.assertAlmostEqual(sum(patch.get_height() for patch in axis.patches), 1.234567)
        for patch in axis.patches:
            self.assertAlmostEqual(patch.get_x() + patch.get_width() / 2, -.18)
            self.assertAlmostEqual(patch.get_width(), .36)
        self.assertEqual("time / LLM+action", axis.get_ylabel())

    def test_missing_slots_have_no_bars_and_no_reference_numbers(self):
        from ae.repro.paper_figure7 import figure7
        figure = figure7(self.plt, dict(metrics=[]))
        axis = figure.axes[0]
        self.assertEqual(len(axis.patches), 0)
        placeholders = [text for text in axis.texts if text.get_text() == "N/A"]
        self.assertEqual(len(placeholders), 8)
        self.assertEqual(len({text.get_position()[0] for text in placeholders}), 8)

    def test_supplied_ratios_below_and_above_reference_scale_are_not_clipped(self):
        from ae.repro.paper_figure7 import figure7
        figure = figure7(self.plt, dict(metrics=metrics(.75) + metrics(9., backend="e2b")))
        figure.canvas.draw()
        axis = figure.axes[0]
        self.assertGreater(axis.get_ylim()[1], 9.)
        self.assertEqual([patch.get_height() for patch in axis.patches], [.75, 1., 8.])
        renderer = figure.canvas.get_renderer()
        for text in axis.texts:
            if text.get_text().endswith("×"):
                self.assertLess(text.get_window_extent(renderer).y1, axis.get_window_extent(renderer).y1)


if __name__ == "__main__":
    unittest.main()
