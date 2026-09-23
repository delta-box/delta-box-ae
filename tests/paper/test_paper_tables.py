"""Paper cells must preserve missing data, populations and timer semantics."""
import importlib.util
import json
import unittest

from ae.repro.paper_tables import table2_manifest, table3_manifest


def measured(metric, value, *, backend="deltabox", mode="fast", n=1, **labels):
    return dict(metric=metric, value=value, backend=backend, mode=mode,
                n=n, unit="ms", statistic="mean", **labels)


def cell(manifest, row, column, backend=None):
    return next(item for item in manifest["cells"]
                if item["row"] == row and item["column"] == column
                and (backend is None or item.get("backend") == backend))


class PaperTableDataTests(unittest.TestCase):
    def test_empty_results_keep_all_paper_cells_without_old_values(self):
        second = table2_manifest(dict(metrics=[]))
        third = table3_manifest(dict(metrics=[]))
        self.assertEqual(len(second["cells"]), 60)
        self.assertEqual(len(third["cells"]), 15)
        self.assertEqual([s["backend"] for s in second["systems"]],
                         ["replay", "fc-diff", "criu", "cube", "e2b", "deltabox"])
        self.assertEqual([r["label"] for r in second["rows"]],
                         ["Django", "SymPy", "Scientific", "Tools/Small repos", "Event Avg"])
        self.assertTrue(all(c["display"] == "—" for c in second["cells"] + third["cells"]))
        json.dumps([second, third], allow_nan=False)

    def test_table2_uses_fast_experiment_and_records_slow_exclusion(self):
        rows = [
            measured("checkpoint_ms", 12.51, group="Django", experiment="table-02-deltabox"),
            measured("checkpoint_ms", 98.76, group="Django", mode="slow", experiment="table-03-slow"),
            measured("restore_ms", 1.23, group="Django", experiment="table-02-deltabox"),
        ]
        manifest = table2_manifest(dict(metrics=rows, source="fresh"))
        self.assertEqual(cell(manifest, "Django", "ck", "deltabox")["display"], "12.51")
        self.assertEqual(cell(manifest, "Django", "rs", "deltabox")["display"], "1.23")
        self.assertEqual(cell(manifest, "All", "ck", "deltabox")["display"], "—")
        self.assertEqual(manifest["excluded_metrics"][0]["metric_index"], 1)

    def test_different_sources_cannot_silently_fill_disjoint_rows_of_one_column(self):
        rows = [measured("restore_ms", 1, group="Django", source_identity="A"),
                measured("restore_ms", 9, group="SymPy", source_identity="B")]
        manifest = table2_manifest(dict(metrics=rows, source="fresh"))
        self.assertEqual(cell(manifest, "Django", "rs", "deltabox")["display"], "—")
        self.assertIn("Multiple populations", cell(manifest, "Django", "rs", "deltabox")["reason"])

    def test_duplicate_table2_aggregates_are_not_event_weighted_twice(self):
        row = measured("restore_ms", 1, group="Django")
        manifest = table2_manifest(dict(metrics=[row, row]))
        self.assertEqual(cell(manifest, "Django", "rs", "deltabox")["display"], "—")

    def test_legacy_replay_is_not_relabeled_as_actual_zero_latency(self):
        for backend in ("replay", "replay-including-llm", "replay-sleep-subtracted-estimate"):
            row = measured("restore_ms", 42, backend=backend, group="Django")
            manifest = table2_manifest(dict(metrics=[row], source="fresh"))
            self.assertEqual(cell(manifest, "Django", "rs", "replay")["display"], "—")
        row = measured("restore_ms", 42, backend="replay-zero-llm", group="Django")
        manifest = table2_manifest(dict(metrics=[row], source="fresh"))
        self.assertEqual(cell(manifest, "Django", "rs", "replay")["display"], "42.0")

    def test_recorded_replay_is_displayed_only_with_explicit_timing_identity(self):
        row = measured("restore_ms", 42, backend="replay-sleep-subtracted-estimate", group="Django",
                       mock_latency_policy="recorded", replay_timing_method="recorded-sleep-subtracted")
        manifest = table2_manifest(dict(metrics=[row], source="fresh"))
        self.assertEqual(cell(manifest, "Django", "rs", "replay")["display"], "42.0")
        zero = dict(row, backend="replay-zero-llm", replay_timing_method="zero-latency-wall", mock_latency_policy="zero")
        mixed = table2_manifest(dict(metrics=[row, zero], source="fresh"))
        self.assertEqual(cell(mixed, "Django", "rs", "replay")["display"], "—")
        self.assertIn("Multiple populations", cell(mixed, "Django", "rs", "replay")["reason"])

    def test_table3_direct_timer_mapping_never_derives_coordination_or_zero_blocking(self):
        rows = [measured(name, value, checkpoint_profile="async-incremental") for name, value in (
            ("checkpoint_overlay_ms", .23), ("checkpoint_fork_ms", 3.45),
            ("checkpoint_sync_no_dump_ms", 18), ("ckpt_wall_ms", 25),
            ("restore_fast_ioctl_ms", .12), ("restore_fast_fork_total_ms", 1.23),
            ("restore_fast_dispatch_ms", .34), ("restore_fast_fork_wait_ms", 1.11),
            ("restore_wall_ms", 12.34),
        )]
        rows.append(measured("restore_wall_ms", 98.76, mode="slow"))
        manifest = table3_manifest(dict(metrics=rows))
        self.assertEqual(cell(manifest, 0, "ck")["display"], "0.23")
        self.assertEqual(cell(manifest, 1, "rs_fast")["display"], "1.23")
        self.assertEqual(cell(manifest, 2, "ck")["display"], "async")
        self.assertEqual(cell(manifest, 2, "ck")["status"], "configuration")
        self.assertEqual(cell(manifest, 3, "rs_fast")["display"], "—")
        self.assertEqual(cell(manifest, 4, "ck")["display"], "—")
        self.assertEqual(cell(manifest, 2, "rs_slow")["display"], "—")
        self.assertEqual(cell(manifest, 4, "rs_fast")["display"], "12.34")
        self.assertEqual(cell(manifest, 4, "rs_slow")["display"], "98.76")
        self.assertEqual(cell(manifest, 4, "rs_slow")["annotation"], "†")
        self.assertIn("Full controller API", cell(manifest, 4, "rs_slow")["boundary"])
        self.assertIn("overlaps ioctl", cell(manifest, 1, "rs_fast")["boundary"])

    def test_new_component_window_and_overlap_model_keep_api_measurements_separate(self):
        rows = [measured(name, value, checkpoint_profile="historical-async-full") for name, value in (
            ("restore_table3_total_ms", 2.3), ("restore_wall_ms", 100),
            ("restore_fast_coordination_ms", .4), ("checkpoint_masked_model_ms", 0),
        )]
        rows += [measured(name, value, mode="slow", checkpoint_profile="historical-async-full") for name, value in (
            ("restore_table3_total_ms", 11), ("restore_wall_ms", 200),
            ("restore_slow_ioctl_ms", .2), ("restore_slow_criu_ms", 8.7),
            ("restore_slow_coordination_ms", 2.1),
        )]
        manifest = table3_manifest(dict(metrics=rows))
        self.assertEqual(cell(manifest, 4, "rs_fast")['value'], 2.3)
        self.assertEqual(cell(manifest, 4, "rs_slow")['value'], 11)
        self.assertEqual(cell(manifest, 2, "rs_slow")['value'], 8.7)
        self.assertEqual(cell(manifest, 3, "rs_slow")['value'], 2.1)
        self.assertEqual(cell(manifest, 2, "ck")['display'], 'async')
        self.assertEqual(cell(manifest, 4, "ck")['status'], 'derived-model')
        self.assertEqual(cell(manifest, 4, "ck")['annotation'], '‖')
        self.assertEqual(cell(manifest, 1, "rs_slow")['status'], 'not-applicable')

    def test_table3_pools_instance_means_with_recorded_event_counts_only(self):
        rows = [measured("restore_wall_ms", 10., n=1, instance="a"),
                measured("restore_wall_ms", 30., n=3, instance="b"),
                measured("restore_wall_ms", 90., n=2, mode="slow", instance="a")]
        manifest = table3_manifest(dict(metrics=rows))
        fast = cell(manifest, 4, "rs_fast")
        self.assertEqual((fast["value"], fast["n"]), (25., 4))
        self.assertEqual([s["metric_index"] for s in fast["sources"]], [0, 1])
        self.assertEqual(cell(manifest, 4, "rs_slow")["value"], 90.)
        rows[1]["cohort"] = "different"
        self.assertEqual(cell(table3_manifest(dict(metrics=rows)), 4, "rs_fast")["display"], "—")

    def test_table3_duplicate_instances_are_not_counted_twice(self):
        rows = [measured("restore_wall_ms", 10., instance="same"),
                measured("restore_wall_ms", 30., instance="same")]
        self.assertEqual(cell(table3_manifest(dict(metrics=rows)), 4, "rs_fast")["display"], "—")

    def test_slow_checkpoint_cannot_fill_fast_checkpoint_cells(self):
        rows = [measured("checkpoint_overlay_ms", 99, mode="slow"),
                measured("checkpoint_fork_ms", 100, mode="slow")]
        manifest = table3_manifest(dict(metrics=rows))
        self.assertEqual(cell(manifest, 0, "ck")["display"], "—")
        self.assertEqual(cell(manifest, 1, "ck")["display"], "—")
        self.assertEqual(len(manifest["excluded_metrics"]), 2)


@unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Plotting extra is not installed")
class PaperTableRenderTests(unittest.TestCase):
    def test_fixed_layout_has_white_background_no_vertical_rules_and_original_aspect(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ae.repro.paper_tables import table2, table3

        for function, ratio, body_label in ((table2, 3.65, "Event Avg"),
                                            (table3, 1.32, "Agent-perceived\nblocking")):
            figure = function(plt, dict(metrics=[]))
            try:
                figure.canvas.draw()
                self.assertAlmostEqual(figure.get_figwidth() / figure.get_figheight(), ratio, delta=.02)
                axes = figure.axes[0]
                self.assertIn(body_label, [text.get_text() for text in axes.texts])
                self.assertTrue(all(line.get_ydata()[0] == line.get_ydata()[1] for line in axes.lines))
                self.assertEqual(figure.get_facecolor(), (1., 1., 1., 1.))
                self.assertFalse(axes.tables)
                self.assertFalse(any("checkpoint_sync_no_dump_ms" in text.get_text() for text in axes.texts))
                renderer = figure.canvas.get_renderer()
                # Captions and notes must fit inside the saved image width.
                for text in axes.texts:
                    box = text.get_window_extent(renderer)
                    self.assertGreaterEqual(box.x0, -1, text.get_text())
                    self.assertLessEqual(box.x1, figure.bbox.width + 1, text.get_text())
            finally:
                plt.close(figure)


if __name__ == "__main__":
    unittest.main()
