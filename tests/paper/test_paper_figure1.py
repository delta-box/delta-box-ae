"""Prevent paper styling from inventing phase attribution or hiding missing systems."""
import importlib.util
import unittest

from ae.repro.paper_figure1 import figure1_metadata


def metrics(backend, operation, values, **extra):
    return [dict(backend=backend, operation=operation, metric=name, value=value,
                 unit="ms", n=3, statistic="mean", evidence_kind="fresh_raw_events", **extra)
            for name, value in values.items()]


class Figure1PhaseMappingTests(unittest.TestCase):
    def test_e2b_api_boundaries_are_not_filesystem_or_memory(self):
        rows = metrics("e2b", "checkpoint", dict(total=150., pause=110., snapshot_upload=40.))
        rows += metrics("e2b", "restore", dict(total=220., resume=220.))
        metadata = figure1_metadata(dict(metrics=rows))
        for slot in metadata["slots"]:
            self.assertEqual(slot["status"], "unavailable")
            self.assertEqual(slot["displayed_phases_ms"], {})
            self.assertIsNone(slot["displayed_total_ms"])
        self.assertIn("API timers", metadata["slots"][0]["reason"])
        self.assertEqual(metadata["slots"][0]["available_metrics"], rows[:3])

    def test_cube_unclassified_api_is_not_relabelled_control_plane(self):
        rows = metrics("cube", "checkpoint", dict(total=100., filesystem=10., process=40.,
                                                   control_plane=20., unclassified_api=30.))
        slot = figure1_metadata(dict(metrics=rows))["slots"][1]
        self.assertEqual(slot["status"], "measured")
        self.assertEqual(slot["displayed_phases_ms"]["unclassified_api"], 30.)
        self.assertEqual(slot["displayed_phases_ms"]["control_plane"], 20.)
        rows[-1]["value"] = 0.
        rows[0]["value"] = 70.
        slot = figure1_metadata(dict(metrics=rows))["slots"][1]
        self.assertEqual(slot["status"], "measured")
        self.assertEqual(slot["displayed_phases_ms"], dict(filesystem=10., process=40., control_plane=20.))

    def test_no_residual_or_cross_population_backfill(self):
        rows = metrics("replay", "restore", dict(total=100., filesystem=10., replay=80.))
        slot = figure1_metadata(dict(metrics=rows))["slots"][5]
        self.assertEqual(slot["status"], "unavailable")
        self.assertIn("no residual", slot["reason"])
        rows = metrics("replay", "restore", dict(total=100., filesystem=10., replay=90.), cohort="a")
        rows += metrics("replay", "restore", dict(total=200., filesystem=20., replay=180.), cohort="b")
        slot = figure1_metadata(dict(metrics=rows))["slots"][5]
        self.assertEqual(slot["status"], "unavailable")
        self.assertIn("Multiple populations", slot["reason"])

    def test_historical_adjustments_and_unmatched_counts_are_not_drawn(self):
        rows = metrics("replay", "checkpoint", dict(total=100., filesystem=100.))
        rows[1]["evidence_kind"] = "published_adjustment"
        self.assertEqual(figure1_metadata(dict(metrics=rows))["slots"][2]["status"], "unavailable")
        rows[1]["evidence_kind"] = "fresh_raw_events"
        rows[1]["n"] = 2
        self.assertEqual(figure1_metadata(dict(metrics=rows))["slots"][2]["status"], "unavailable")

    def test_phase_identity_is_checked_without_combined_population_labels(self):
        for field in ("source_identity", "mode", "checkpoint_profile", "run_purpose",
                      "mock_latency_policy", "replay_timing_method"):
            with self.subTest(field=field):
                rows = metrics("replay", "checkpoint", dict(total=100., filesystem=100.))
                rows[0][field] = "population-a"
                rows[1][field] = "population-b"
                slot = figure1_metadata(dict(metrics=rows))["slots"][2]
                self.assertEqual(slot["status"], "unavailable")
                self.assertIn("Multiple populations", slot["reason"])
                self.assertEqual(slot["displayed_phases_ms"], {})
                rows[1][field] = "population-a"
                self.assertEqual(figure1_metadata(dict(metrics=rows))["slots"][2]["status"], "measured")


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
class Figure1PaperLayoutTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt

    def tearDown(self):
        self.plt.close("all")

    def test_measured_replay_keeps_paper_slots_and_phase_values(self):
        from ae.repro.paper_figure1 import figure1
        rows = metrics("replay", "checkpoint", dict(total=121.3, filesystem=121.3))
        rows += metrics("replay", "restore", dict(total=8305.3, filesystem=153.5, replay=8151.8))
        rows += metrics("criu", "checkpoint", dict(total=100., filesystem=50., process=50.))
        figure = figure1(self.plt, dict(metrics=rows))
        figure.canvas.draw()
        self.assertEqual(list(figure.get_size_inches()), [3.4, 1.9])
        self.assertEqual(len(figure.axes), 2)
        self.assertEqual(figure.axes[0].get_yscale(), "linear")
        self.assertEqual(figure.axes[1].get_yscale(), "log")
        self.assertEqual(figure.axes[0].get_ylim(), (0., 3500.))
        self.assertEqual(list(figure.axes[0].get_yticks()), list(range(0, 3500, 500)))
        for axis in figure.axes:
            self.assertEqual([label.get_text() for label in axis.get_xticklabels()],
                             ["E2B", "CubeSandbox", "copy+replay"])
            self.assertEqual(sum(text.get_text() == "N/A" for text in axis.texts), 2)
            boxes = [label.get_window_extent(figure.canvas.get_renderer())
                     for label in axis.get_xticklabels()]
            self.assertTrue(all(left.x1 < right.x0 for left, right in zip(boxes, boxes[1:])))
        self.assertEqual([patch.get_height() for patch in figure.axes[0].patches], [121.3])
        for patch, expected in zip(figure.axes[1].patches, (153.5, 8151.8)):
            self.assertAlmostEqual(patch.get_height(), expected)
        self.assertEqual([patch.get_hatch() for patch in figure.axes[1].patches], ["xxxx", "++"])
        self.assertEqual([text.get_text() for text in figure.legends[0].texts],
                         ["filesystem", "memory", "guest-ready", "control-plane", "replay"])
        self.assertEqual(figure1_metadata(dict(metrics=rows))["excluded_backends"], ["criu"])

    def test_empty_slots_have_no_numeric_bars(self):
        from ae.repro.paper_figure1 import figure1
        figure = figure1(self.plt, dict(metrics=[]))
        figure.canvas.draw()
        for axis in figure.axes:
            self.assertEqual(len(axis.patches), 0)
            self.assertEqual(sum(text.get_text() == "N/A" for text in axis.texts), 3)


if __name__ == "__main__":
    unittest.main()
