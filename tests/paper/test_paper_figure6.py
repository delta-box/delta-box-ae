"""Keep Figure 6's independent populations and binned event counts intact."""
import copy
import importlib.util
import json
import unittest

from ae.repro.paper_figure6 import figure6_metadata


def sample_result():
    result = dict(metrics=[], series=[], selection={})
    for arm in ("skip", "gc", "none", "warm"):
        identity = dict(panel="a", arm=arm, instance="sympy__sympy-22840",
                        experiment="figure-06-memory", checkpoint_profile="runtime-default",
                        source_identity="release-a", evidence_kind="fresh_raw_events",
                        cohort="memory-" + arm, plot_group="memory-population",
                        memory_policy=arm, adaptive=False)
        for x, value in enumerate((100., 200.), 1):
            result["series"].append(dict(identity, metric="memory", x=x, y=value,
                                         x_unit="checkpoint", unit="MiB"))
        result["metrics"].append(dict(identity, metric="final_memory", value=200., n=2,
                                      unit="MiB", statistic="mean"))
        result["selection"][identity["cohort"] + ";instance=" + identity["instance"]] = dict(
            checkpoints=2, bootstrap_included=True)
    for arm, counts in (("standard_only", (0, 1, 7)),
                        ("adaptive_lightweight", (2, 3, 0)),
                        ("adaptive_standard", (0, 0, 2))):
        adaptive = arm != "standard_only"
        identity = dict(panel="b", arm=arm, experiment="figure-06-adaptive",
                        checkpoint_profile="runtime-default", source_identity="release-a",
                        evidence_kind="fresh_raw_events", adaptive=adaptive,
                        cohort="adaptive" if adaptive else "standard", plot_group="latency-population")
        edges = (.05, 1., 10., 500.)
        for lo, hi, count in zip(edges, edges[1:], counts):
            result["series"].append(dict(identity, metric="checkpoint_histogram", x=lo,
                                         x_unit="ms", y=count, unit="events", bin_lo=lo, bin_hi=hi))
        result["metrics"].append(dict(identity, metric="checkpoint_ms", value=20., n=sum(counts),
                                      unit="ms", statistic="mean"))
        selected = result["selection"].setdefault(identity["cohort"], dict(excluded_bootstrap=1,
                                                                         histogram_overflow={}))
        selected["histogram_overflow"][arm] = 0
    return result


def slots(result):
    return {slot["arm"]: slot for slot in figure6_metadata(result)["slots"]}


class Figure6DataMappingTests(unittest.TestCase):
    def test_independent_panels_and_standard_population_keep_their_counts(self):
        result = sample_result()
        before = copy.deepcopy(result)
        metadata = figure6_metadata(result)
        mapped = {slot["arm"]: slot for slot in metadata["slots"]}
        self.assertTrue(all(slot["status"] == "measured" for slot in mapped.values()))
        self.assertEqual([mapped[arm]["event_count"] for arm in
                          ("standard_only", "adaptive_lightweight", "adaptive_standard")], [8, 5, 2])
        self.assertEqual([b["count"] for b in mapped["standard_only"]["bins"]], [0, 1, 7])
        self.assertEqual(metadata["panels"]["a"]["xlabel"], "Checkpoint index")
        self.assertIn("including bootstrap", metadata["caption"])
        self.assertEqual(result, before)
        json.dumps(metadata, allow_nan=False)

    def test_memory_unit_conversion_is_numeric_and_recorded(self):
        mapped = slots(sample_result())["skip"]
        self.assertEqual([point["x"] for point in mapped["points"]], [1, 2])
        for point, expected in zip(mapped["points"], (104.8576, 209.7152)):
            self.assertAlmostEqual(point["y"], expected)
        self.assertEqual(mapped["supplied_series"][0]["y"], 100.)
        self.assertEqual(mapped["conversions"], [dict(from_unit="MiB", to_unit="MB", factor=1.048576)])

    def test_same_arm_conflict_cannot_overwrite_a_curve(self):
        result = sample_result()
        conflicts = [dict(row, source_identity="release-b") for row in result["series"]
                     if row.get("arm") == "skip"]
        result["series"].extend(conflicts)
        mapped = slots(result)
        self.assertEqual(mapped["skip"]["status"], "unavailable")
        self.assertIn("Multiple populations", mapped["skip"]["reason"])
        self.assertEqual(mapped["skip"]["points"], [])
        self.assertEqual(mapped["gc"]["status"], "measured")
        self.assertEqual(mapped["standard_only"]["status"], "measured")

    def test_adaptive_components_from_different_sources_are_not_stacked(self):
        result = sample_result()
        for row in result["series"] + result["metrics"]:
            if row.get("arm") == "adaptive_standard":
                row["source_identity"] = "release-b"
        mapped = slots(result)
        for arm in ("adaptive_lightweight", "adaptive_standard"):
            self.assertEqual(mapped[arm]["status"], "unavailable")
            self.assertIn("cannot form one stacked", mapped[arm]["reason"])
        self.assertEqual(mapped["standard_only"]["status"], "measured")

    def test_bins_and_count_summary_must_close_without_dropping_events(self):
        for failure in ("duplicate", "gap", "bad_count"):
            with self.subTest(failure=failure):
                result = sample_result()
                bins = [row for row in result["series"] if row.get("arm") == "standard_only"]
                metric = next(row for row in result["metrics"] if row.get("arm") == "standard_only")
                if failure == "duplicate":
                    result["series"].append(dict(bins[0]))
                elif failure == "gap":
                    bins[1]["bin_lo"] = 2.
                elif failure == "bad_count":
                    metric["n"] += 1
                mapped = slots(result)["standard_only"]
                self.assertEqual(mapped["status"], "unavailable")
                self.assertEqual(mapped["bins"], [])

    def test_recorded_overflow_preserves_valid_bins_and_is_disclosed(self):
        result = sample_result()
        metric = next(row for row in result["metrics"] if row.get("arm") == "standard_only")
        metric["n"] += 2
        result["selection"]["standard"]["histogram_overflow"]["standard_only"] = 2
        mapped = slots(result)["standard_only"]
        self.assertEqual(mapped["status"], "measured")
        self.assertEqual(mapped["event_count"], 10)
        self.assertEqual(mapped["displayed_event_count"], 8)
        self.assertEqual(mapped["binned_event_count"], 8)
        self.assertEqual(mapped["overflow_event_count"], 2)
        self.assertEqual([b["count"] for b in mapped["bins"]], [0, 1, 7])
        self.assertIn("2 Std events above 500 ms", figure6_metadata(result)["caption"])

    def test_missing_is_not_zero_and_mixed_index_semantics_are_not_aligned(self):
        result = sample_result()
        result["series"] = [row for row in result["series"] if row.get("arm") != "adaptive_standard"]
        mapped = slots(result)
        self.assertEqual(mapped["adaptive_standard"]["status"], "unavailable")
        self.assertIsNone(mapped["adaptive_standard"]["event_count"])
        self.assertEqual(mapped["adaptive_lightweight"]["event_count"], 5)
        for row in result["series"]:
            if row.get("arm") == "skip":
                row["x_unit"] = "mcts_iteration"
        mapped = slots(result)
        self.assertTrue(all(mapped[arm]["status"] == "unavailable"
                            for arm in ("skip", "gc", "none", "warm")))


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
class Figure6PaperLayoutTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.plt = plt

    def tearDown(self):
        self.plt.close("all")

    def test_two_panels_keep_paper_geometry_and_actual_counts(self):
        from ae.repro.paper_figure6 import figure6
        fig = figure6(self.plt, sample_result())
        fig.canvas.draw()
        self.assertEqual(list(fig.get_size_inches()), [3.4, 1.55])
        self.assertEqual(len(fig.axes), 2)
        memory, latency = fig.axes
        self.assertAlmostEqual(memory.get_position().width / latency.get_position().width, 1.3)
        self.assertEqual(memory.get_yscale(), "log")
        self.assertEqual(latency.get_xscale(), "log")
        self.assertEqual(latency.get_xlim(), (.05, 500.))
        for line in memory.lines:
            for value, expected in zip(line.get_ydata(), (104.8576, 209.7152)):
                self.assertAlmostEqual(value, expected)
        self.assertEqual([text.get_text() for text in fig.legends[0].texts],
                         ["LW-skip", "reachability GC", "none", "async-warm"])
        self.assertEqual([text.get_text() for text in latency.get_legend().texts],
                         ["Std (8)", "LW (5)", "std (2)"])
        # The two adaptive populations retain their own counts; only their
        # common-bin display is stacked. Standard-only is a separate outline.
        self.assertEqual([patch.get_height() for patch in latency.containers[0]], [2, 3, 0])
        self.assertEqual([patch.get_height() for patch in latency.containers[1]], [0, 0, 2])
        self.assertEqual([patch.get_y() for patch in latency.containers[1]], [2, 3, 0])
        self.assertEqual(latency.patches[0].get_data().values.tolist(), [0, 1, 7])

    def test_missing_arms_keep_the_legend_without_numeric_bars(self):
        from ae.repro.paper_figure6 import figure6
        fig = figure6(self.plt, dict(metrics=[], series=[]))
        fig.canvas.draw()
        self.assertEqual(len(fig.axes[0].lines), 0)
        self.assertEqual(len(fig.axes[1].patches), 0)
        self.assertEqual(sum("N/A" in text.get_text() for text in fig.axes[0].texts), 4)
        self.assertEqual([text.get_text() for text in fig.axes[1].get_legend().texts],
                         ["Std (N/A)", "LW (N/A)", "std (N/A)"])


if __name__ == "__main__":
    unittest.main()
