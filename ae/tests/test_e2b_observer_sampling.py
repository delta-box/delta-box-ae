"""Sampling diagnostics retain every original per-thread placement check."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "observer_sampling_fixture", Path(__file__).with_name("test_e2b_vm_proof_esrch.py"))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
m = FIXTURE.m


class ObserverSamplingTests(unittest.TestCase):
    def fixture(self, interval_s):
        case = FIXTURE.VMProofESRCH()
        case.setUp()
        case.root.chmod(0o755)
        case.child.chmod(0o755)
        self.addCleanup(case.doCleanups)
        proof = m.VMProof(3, "72-75", observer=Mock(receipt={}), interval_s=interval_s)
        return case, proof

    def one_scan(self, proof):
        with patch.object(proof.stop, "wait", side_effect=lambda seconds: proof.stop.set()) as wait:
            proof.watch()
        wait.assert_called_once_with(proof.interval_s)

    def test_default_and_diagnostic_intervals_keep_all_tid_reads_and_policies(self):
        for interval in (.025, .25, 1):
            with self.subTest(interval=interval):
                case, proof = self.fixture(interval)
                self.one_scan(proof)
                self.assertEqual(proof.errors, [])
                sample = next(iter(proof.rows.values()))
                self.assertEqual([t["pid"] for t in sample["tasks"]], [42])
                self.assertEqual({t["pid"] for t in sample["threads"]}, {42, 43})
                self.assertTrue(all(t["numa_policies"] == {"bind:3": 1} for t in sample["threads"]))
                metrics = proof.evidence()["observer_sampling"]["metrics"]
                # Retain the original leader read plus reads for both TIDs.
                self.assertEqual(metrics["numa_maps_reads"], 3)
                self.assertEqual(metrics["numa_maps_read_failures"], 0)
                self.assertEqual(metrics["numa_maps_characters"], 3 * len("123 bind:3 N3=1\n"))
                self.assertEqual(metrics["samples_started"], 1)
                self.assertEqual(metrics["samples_completed"], 1)
                self.assertEqual(metrics["samples_failed"], 0)
                self.assertGreater(metrics["sample_wall_ns"], 0)
                self.assertGreater(metrics["observer_thread_cpu_ns"], 0)
                histogram = proof.evidence()["observer_sampling"]["sample_duration_histogram"]
                self.assertEqual(sum(histogram["counts"]), 1)
                self.assertIsNone(getattr(m._OBSERVER_METRICS, "current", None))
                case.doCleanups()

    def test_changed_thread_policy_fails_on_later_scan_even_for_previously_seen_process(self):
        for interval in (.025, .25):
            with self.subTest(interval=interval):
                case, proof = self.fixture(interval)
                def next_scan(seconds):
                    (case.proc / "43/numa_maps").write_text("123 bind:2 N2=1\n")
                with patch.object(proof.stop, "wait", side_effect=next_scan):
                    proof.watch()
                self.assertEqual(len(proof.errors), 1)
                self.assertIn("actual memory policy", proof.errors[0])
                self.assertEqual(proof.metrics["samples_started"], 2)
                self.assertEqual(proof.metrics["samples_completed"], 1)
                self.assertEqual(proof.metrics["samples_failed"], 1)
                self.assertEqual(proof.metrics["numa_maps_reads"], 6)
                case.doCleanups()

    def test_read_error_is_counted_and_not_hidden_by_metrics(self):
        case, proof = self.fixture(.25)
        case.fail_read()
        proof.watch()
        self.assertEqual(len(proof.errors), 1)
        self.assertIn("remains present", proof.errors[0])
        self.assertEqual(proof.metrics["numa_maps_reads"], 3)
        self.assertEqual(proof.metrics["numa_maps_read_failures"], 1)
        self.assertEqual(proof.metrics["samples_failed"], 1)
        self.assertEqual(proof.rows, {})

    def test_missing_source_or_child_coverage_still_fails(self):
        for interval in (.025, .25):
            case, proof = self.fixture(interval)
            self.one_scan(proof)
            path = case.base / "fanout.json"
            path.write_text(json.dumps([{"children": [{"sandbox_id": "sandbox1"}],
                                         "cleanup": [{"resource": "source", "id": "unobserved"}]}]))
            with self.assertRaisesRegex(RuntimeError, "Missing placement proof"):
                proof.verify_ids(path)
            case.doCleanups()

    def test_invalid_interval_is_rejected_before_service_admission(self):
        bad_values = (None, True, False, "0.25", 0, -.25, .02499, 1.00001,
                      float("nan"), float("inf"), float("-inf"))
        for bad in bad_values:
            with self.subTest(bad=bad), patch.object(m, "require_admission") as admission:
                with self.assertRaisesRegex(ValueError, "observer_interval_s"):
                    with m.service_placement({"e2b": {"observer_interval_s": bad}},
                                             Path("/unused"), fanout_path=Path("/unused/fanout.json")):
                        self.fail("Invalid interval reached service body")
                admission.assert_not_called()

    def test_unobserved_reads_are_not_added_to_another_observer(self):
        case, proof = self.fixture(.25)
        self.one_scan(proof)
        before = dict(proof.metrics)
        self.assertEqual(m.read_observed_numa_maps(case.proc / "42/numa_maps"), "123 bind:3 N3=1\n")
        self.assertEqual(proof.metrics, before)

    def test_interval_defaults_to_existing_25_ms(self):
        proof = m.VMProof(3, "72-75")
        self.assertEqual(proof.interval_s, .025)
        self.assertEqual(proof.evidence()["observer_sampling"]["interval_s"], .025)


if __name__ == "__main__":
    unittest.main()
