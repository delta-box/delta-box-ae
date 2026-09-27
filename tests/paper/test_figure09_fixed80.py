"""Fixed Figure 9 coverage and rejection of source/selection drift."""
import collections
import copy
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ae"))
from repro import catalog, figure09_cohort as fixed

def value(command, flag):
    return command[command.index(flag) + 1]

class Figure09Fixed80Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ae = ROOT / "ae"
        with (cls.ae / fixed.SOURCE_PATH).open() as stream:
            cls.rows = list(csv.DictReader(stream))
        cls.manifest = json.loads((cls.ae / fixed.MANIFEST_PATH).read_text())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config = dict(timeout=14400, baseline_inputs="44")

    def choose(self, manifest=None, rows=None, config=None):
        path = self.path / "manifest.json"
        path.write_text(json.dumps(self.manifest if manifest is None else manifest, indent=2) + "\n")
        return fixed.figure09_rows(self.rows if rows is None else rows,
                                   self.config if config is None else config, manifest_path=path)

    def plan(self, config=None, **kwargs):
        return catalog.build_jobs(["figure-09"], self.config if config is None else config,
                                  self.path / "config.json", self.path / "out", **kwargs)

    def test_default_240_jobs_pair_full_actions_and_bind_selection(self):
        before = copy.deepcopy(self.config)
        jobs = self.plan()
        self.assertEqual(self.config, before)
        self.assertEqual(len(jobs), 240)
        entries = {entry["key"]: entry for entry in self.manifest["inputs"]}
        grouped = collections.defaultdict(list)
        for job in jobs:
            key = value(job["command"], "--input-key")
            grouped[key].append(value(job["command"], "--arm"))
            self.assertEqual(job["expected_edits"], entries[key]["n_edits"])
            self.assertEqual(job["inputs"], [entries[key]["actions"]["path"]])
            self.assertEqual(value(job["command"], "--actions"),
                             str(self.ae / entries[key]["actions"]["path"]))
            self.assertNotIn("--limit", job["command"])
            self.assertNotIn("--max-events", job["command"])
            self.assertEqual(job["run_purpose"], "full-trace")
            self.assertEqual(job["input_selection"]["inputs"], 80)
            self.assertEqual(job["input_selection"]["sha256"],
                             hashlib.sha256((self.ae / fixed.MANIFEST_PATH).read_bytes()).hexdigest())
        self.assertEqual(list(grouped), [entry["key"] for entry in self.manifest["inputs"]])
        self.assertTrue(all(arms == ["ext4", "xfs", "xfs_reflink"] for arms in grouped.values()))
        self.assertEqual(sum(job["expected_edits"] for job in jobs), 462 * 3)

    def test_baseline_all_cannot_expand_figure09(self):
        for mode in ("44", "all"):
            with self.subTest(mode=mode):
                self.assertEqual(len(self.plan(dict(self.config, baseline_inputs=mode))), 240)
        for mode in (80, "80"):
            self.assertEqual(len(self.plan(dict(self.config, figure09_inputs=mode))), 240)
        for mode in ("all", 185, "185", 44, True, None):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "only 80 inputs"):
                    self.plan(dict(self.config, figure09_inputs=mode))

    def test_complete_coverage_preserves_work_and_long_tails(self):
        rows, selection = self.choose()
        keys = {fixed._key(row) for row in rows}
        self.assertEqual(dict(collections.Counter(row["pool"] for row in rows)), fixed.POOL_QUOTAS)
        self.assertEqual(len({row["instance"].split("__")[0] for row in rows}), 10)
        self.assertEqual(len({(row["pool"], row["instance"].split("__")[0]) for row in rows}), 27)
        self.assertTrue({fixed._key(row) for row in self.rows[:26]} <= keys)
        long_rows = [row for row in self.rows if int(row["n_edits"]) > 10]
        self.assertEqual(len(long_rows), 19)
        self.assertTrue({fixed._key(row) for row in long_rows} <= keys)
        self.assertIn("claude_mcts__django__django-12276", keys)
        self.assertEqual(selection["edit_actions_per_filesystem"], 462)

    def test_quick_limit_selects_from_fixed80_without_cutting_actions(self):
        jobs = self.plan(limit=2)
        self.assertEqual(len(jobs), 6)
        self.assertEqual({value(job["command"], "--input-key") for job in jobs},
                         {entry["key"] for entry in self.manifest["inputs"][:2]})
        self.assertTrue(all(job["run_purpose"] == "quick-check" for job in jobs))
        self.assertTrue(all(job["input_selection"]["inputs"] == 80 for job in jobs))
        self.assertTrue(all("--limit" not in job["command"] for job in jobs))
        self.assertEqual(len(self.plan(limit=185)), 240)

    def test_duplicate_missing_unknown_and_reordered_keys_fail(self):
        variants = []
        item = copy.deepcopy(self.manifest)
        item["inputs"][-1] = copy.deepcopy(item["inputs"][0])
        variants.append(item)
        item = copy.deepcopy(self.manifest)
        item["inputs"].pop()
        variants.append(item)
        item = copy.deepcopy(self.manifest)
        item["inputs"][0]["key"] = "unknown"
        variants.append(item)
        item = copy.deepcopy(self.manifest)
        item["inputs"][0], item["inputs"][1] = item["inputs"][1], item["inputs"][0]
        variants.append(item)
        for item in variants:
            with self.subTest(keys=[x["key"] for x in item["inputs"][:2]]):
                with self.assertRaises(ValueError):
                    self.choose(item)

    def test_manifest_source_and_scope_fields_fail_closed(self):
        variants = [
            ("input_count", 185), ("pool_quotas", dict(fixed.POOL_QUOTAS, **{"claude/linear": 31})),
            ("input_keys_sha256", "0" * 64),
            ("source", dict(self.manifest["source"], sha256="0" * 64)),
            ("source", dict(self.manifest["source"], path="paper/figure-09/other.csv")),
        ]
        for field, bad in variants:
            item = copy.deepcopy(self.manifest)
            item[field] = bad
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.choose(item)

    def test_source_csv_and_supplied_row_identity_must_match(self):
        real_digest = fixed.digest
        with mock.patch.object(fixed, "digest",
                               side_effect=lambda p: "0" * 64 if Path(p) == self.ae / fixed.SOURCE_PATH else real_digest(p)):
            with self.assertRaisesRegex(ValueError, "source CSV SHA256 differs"):
                self.choose()
        with self.assertRaisesRegex(ValueError, "row set/order differs"):
            self.choose(rows=self.rows[::-1])

    def test_action_and_trajectory_hash_mismatch_rejected(self):
        for field in ("actions", "trajectory"):
            item = copy.deepcopy(self.manifest)
            item["inputs"][0][field]["sha256"] = "0" * 64
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "input SHA256/size differs"):
                    self.choose(item)
        item = copy.deepcopy(self.manifest)
        item["inputs"][0]["actions"]["path"] = "../unexpected"
        with self.assertRaisesRegex(ValueError, "input path differs"):
            self.choose(item)

    def test_actual_modified_action_bytes_rejected_without_touching_source(self):
        target = self.ae / self.manifest["inputs"][0]["actions"]["path"]
        original = Path.read_bytes
        with mock.patch.object(Path, "read_bytes",
                               lambda path: original(path) + b"\n" if path == target else original(path)):
            with self.assertRaisesRegex(ValueError, "input SHA256/size differs"):
                self.choose()

    def test_truncated_action_metadata_and_coverage_rejected(self):
        item = copy.deepcopy(self.manifest)
        item["inputs"][0]["n_edits"] -= 1
        with self.assertRaisesRegex(ValueError, "complete action plan differs"):
            self.choose(item)
        item = copy.deepcopy(self.manifest)
        item["coverage"]["edit_count"] -= 1
        with self.assertRaisesRegex(ValueError, "coverage metadata differs"):
            self.choose(item)

    def test_manifest_metadata_drift_also_changes_source_identity(self):
        item = copy.deepcopy(self.manifest)
        item["selection_policy"]["limits"] += " changed"
        with self.assertRaisesRegex(ValueError, "fixed manifest SHA256 differs"):
            self.choose(item)

    def test_standalone_cohort_uses_same_catalog_binding(self):
        import importlib.util
        sys.path.insert(0, str(ROOT / "ae/runners"))
        self.addCleanup(sys.path.remove, str(ROOT / "ae/runners"))
        spec = importlib.util.spec_from_file_location(
            "figure09_standalone_test", ROOT / "ae/scripts/run_figure09_cohort.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.build_jobs, catalog.build_jobs)
        jobs = module.build_jobs(["figure-09"], dict(self.config, baseline_inputs="all"),
                                 self.path / "config.json", self.path / "out")
        self.assertEqual(len(jobs), 240)
        self.assertEqual({job["input_selection"]["sha256"] for job in jobs},
                         {fixed.MANIFEST_SHA256})

    def test_no_measurement_based_selection_or_lost_prior_input_claim(self):
        item = copy.deepcopy(self.manifest)
        item["selection_policy"]["uses_measured_performance"] = True
        with self.assertRaisesRegex(ValueError, "must not depend on measured performance"):
            self.choose(item)
        item = copy.deepcopy(self.manifest)
        item["preserved_prior_run"]["input_keys"].pop()
        with self.assertRaisesRegex(ValueError, "prior completed input coverage differs"):
            self.choose(item)

if __name__ == "__main__":
    unittest.main()
