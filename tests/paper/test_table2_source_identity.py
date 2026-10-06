"""Original-paper identity must not be inferred from matching cohort counts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ae.repro import analysis, plot


class Table2SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.selected = []
        locked = []
        for instance, run_id in (("django__django-1", "replay-original-a"),
                                 ("sympy__sympy-2", "replay-original-b")):
            relative = f"table-02/data/records/deltabox-fast/results/{instance}.{run_id}.results.jsonl"
            rows = [dict(kind="ckpt", ckpt_wall_ms=2.0),
                    dict(kind="restore", restore_wall_ms=1.0, restore_critical_ms=1.0),
                    dict(kind="run_summary", error_n=0, worker_exec_bad_n=0)]
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(row)+"\n" for row in rows))
            raw = path.read_bytes()
            self.selected.append(dict(instance=instance, path=relative, rows=rows))
            locked.append(dict(instance=instance, run_id=run_id, raw_sha256=hashlib.sha256(raw).hexdigest(),
                               raw_bytes=len(raw), checkpoint_events=1, restore_events=1))
        self.lock = self.root / "fixed-paper-lock.json"
        self.lock.write_text(json.dumps(dict(schema_version=1, dataset_id="test-original-source",
                                             expected_counts=dict(runs=2), runs=locked)))
        self.guard = patch.object(analysis, "TABLE2_PAPER_SOURCE_LOCK", self.lock)
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def identity(self):
        ev = analysis.Evidence(self.root, "archived")
        for run in self.selected:
            run["rows"] = ev.jsonl(run["path"])
        return ev, analysis.archived_table2_source_identity(ev, self.selected)

    def test_exact_original_records_are_verified(self):
        _, identity = self.identity()
        self.assertTrue(identity["paper_source_match"])
        self.assertEqual((identity["matched_runs"], identity["expected_runs"]), (2, 2))
        self.assertEqual(identity["batch_kind"], "original_paper_historical_batch")
        self.assertEqual(len(identity["records"]), 2)

    def test_same_instances_counts_and_names_do_not_hide_changed_content(self):
        path = self.root / self.selected[0]["path"]
        rows = self.selected[0]["rows"]
        rows[0]["ckpt_wall_ms"] = 3.0
        path.write_text("".join(json.dumps(row)+"\n" for row in rows))
        _, identity = self.identity()
        self.assertFalse(identity["paper_source_match"])
        self.assertEqual(identity["matched_runs"], 1)
        self.assertEqual(identity["status"], "different_archived_batch")
        self.assertEqual(identity["batch_kind"], "released_historical_batch")
        self.assertIn("Matching instances and event counts", identity["notice"])
        self.assertEqual(identity["records"][0]["checkpoint_events"], 1)
        self.assertIn("Original-paper recomputation", identity["notice"])

    def test_missing_lock_is_unverified_not_a_false_match(self):
        self.lock.unlink()
        _, identity = self.identity()
        self.assertIsNone(identity["paper_source_match"])
        self.assertEqual(identity["status"], "unverified")
        self.assertIn("unverified", identity["notice"])

    def test_fresh_evidence_never_reads_the_historical_lock(self):
        ev = analysis.Evidence(self.root, "fresh")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected historic read")):
            self.assertIsNone(analysis.archived_table2_source_identity(ev, self.selected))

    def test_table2_identity_labels_only_deltabox_without_changing_means(self):
        ev, _ = self.identity()
        replay = dict(table_groups={}, overall=dict(mean_ckpt_once_ms=4.0, mean_restore_zero_llm_ms=5.0,
                                                    n_traces=1, n_restore_events=1))
        with patch.object(analysis, "select_delta", return_value=(self.selected, dict(complete_runs=2))), \
             patch.object(ev, "csv", return_value=[]), patch.object(ev, "glob", return_value=[]), \
             patch.object(ev, "json", return_value=replay):
            result = analysis.table2(ev)
        delta = [row for row in result["metrics"] if row["backend"] == "deltabox"]
        other = [row for row in result["metrics"] if row["backend"] != "deltabox"]
        self.assertTrue(all(row["paper_source_match"] for row in delta))
        self.assertTrue(all("paper_source_match" not in row for row in other))
        overall = {row["metric"]: row["value"] for row in delta if row["group"] == "All"}
        self.assertEqual(overall, dict(checkpoint_ms=2.0, restore_ms=1.0))
        self.assertIn("verify_table2_paper_source.py", " ".join(result["limitations"]))

    def test_archive_title_is_deltabox_specific(self):
        identity = dict(paper_source_match=False, matched_runs=0, expected_runs=12)
        result = dict(selection=dict(deltabox=dict(paper_source_identity=identity)))
        label = plot.archived_table2_source_label(result)
        self.assertIn("DeltaBox", label)
        self.assertIn("0/12", label)
        self.assertIn("released historical batch", label)
        self.assertNotIn("E2B", label)


if __name__ == "__main__":
    unittest.main()
