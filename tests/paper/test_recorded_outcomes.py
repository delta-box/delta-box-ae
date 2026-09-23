"""Expected tool failures must match the trace, never blanket-allow pytest rc 4."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "replay"))
from tree_to_schedule import node_worker_ops

spec = importlib.util.spec_from_file_location("recorded_agent", ROOT / "replay/guest/agent.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def recorded_node():
    return {"action_steps": [{"action": {"action_args_class": "RunTestsArgs", "test_files": ["django/tests"]},
                             "observation": {"message": "Unable to run tests: Files not found: django/tests",
                                             "properties": {"fail_reason": "no_test_files"}}}]}


class RecordedOutcomeTests(unittest.TestCase):
    def test_only_explicit_matching_observation_generates_expectation(self):
        self.assertEqual(node_worker_ops(recorded_node())[0]["expected_outcome"]["kind"], "missing_test_files")
        for field, value in (("observation", None), ("observation", "error"), ("observation", {"properties": None}),
                             ("observation", {"message": "tests passed", "properties": {"fail_reason": "no_test_files"}})):
            node = recorded_node()
            node["action_steps"][0][field] = value
            self.assertNotIn("expected_outcome", node_worker_ops(node)[0])

    def test_expected_failure_still_executes_command_and_is_not_test_pass(self):
        op = node_worker_ops(recorded_node())[0]
        result = subprocess.CompletedProcess([], 4, "no tests ran", "ERROR: file or directory not found: django/tests\n")
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker.subprocess, "run", return_value=result) as call:
            row = worker._worker_exec_one(op, tmp)
        call.assert_called_once()
        self.assertTrue(row["ok"])
        self.assertTrue(row["expected_failure_matched"])
        self.assertFalse(row["test_passed"])
        self.assertEqual(row["rc"], 4)
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker.subprocess, "run", return_value=result), patch.object(worker, "_proc_footprint", return_value={}), patch.object(worker, "trace_event"):
            reply = worker._worker_exec({"ops": [op], "root": tmp})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["results"][0]["expected_failure_matched"])
        self.assertFalse(reply["results"][0]["test_passed"])
        self.assertEqual(reply["results"][0]["rc"], 4)

    def test_recorded_directory_rejection_rechecks_tree_without_starting_tests(self):
        node = recorded_node()
        step = node["action_steps"][0]
        step["action"]["test_files"] = ["tests/"]
        step["observation"]["message"] = "Unable to run tests: Directories provided instead of files: tests/"
        op = node_worker_ops(node)[0]
        self.assertEqual(op["expected_outcome"]["kind"], "test_directories")
        with tempfile.TemporaryDirectory() as tmp, patch.object(worker.subprocess, "run") as run:
            directory = Path(tmp) / "tests"
            directory.mkdir()
            row = worker._worker_exec_one(op, tmp)
            self.assertTrue(row["ok"])
            self.assertTrue(row["expected_failure_matched"])
            self.assertFalse(row["test_passed"])
            self.assertFalse(row["test_subprocess_started"])
            self.assertEqual(row["test_outcome"], "invalid-test-selection")
            self.assertIsNone(row["rc"])
            run.assert_not_called()
            directory.rmdir()
            self.assertFalse(worker._worker_exec_one(op, tmp)["ok"])
            directory.write_text("a file is no longer the recorded directory")
            self.assertFalse(worker._worker_exec_one(op, tmp)["ok"])
        for change in ({"command": "true"}, {"test_files": ["other/"]}, {"expected_outcome": {"kind": "test_directories"}}):
            candidate = copy.deepcopy(op)
            candidate.update(change)
            self.assertFalse(worker._matches_recorded_test_directories(candidate, "/tmp"))

    def test_unexpected_errors_existing_files_and_overrides_still_fail(self):
        op = node_worker_ops(recorded_node())[0]
        with tempfile.TemporaryDirectory() as tmp:
            for changed, rc, stderr in (({}, 4, "ERROR: file or directory not found: other/tests"),
                                        ({}, 0, ""), ({}, 1, ""),
                                        ({}, 2, "ERROR: file or directory not found: django/tests"),
                                        ({}, 4, "ImportError: broken environment"),
                                        ({"expected_outcome": {}}, 4, "ERROR: file or directory not found: django/tests"),
                                        ({"command": "bad command"}, 4, "ERROR: file or directory not found: django/tests")):
                candidate = copy.deepcopy(op)
                candidate.update(changed)
                result = subprocess.CompletedProcess([], rc, "", stderr)
                with patch.object(worker.subprocess, "run", return_value=result):
                    self.assertFalse(worker._worker_exec_one(candidate, tmp)["ok"])
            (Path(tmp) / "django/tests").mkdir(parents=True)
            self.assertFalse(worker._matches_recorded_missing_tests(op, tmp, 4, "ERROR: file or directory not found: django/tests"))

    def test_recorded_diff_transport_newline_and_content_validation(self):
        diff = "--- value.py\n+++ value.py\n@@ -1 +1 @@\n-old\n+new"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'value.py'
            target.write_text('old\n')
            row = worker._worker_exec_one({'type':'apply_recorded_diff','diff':diff}, tmp)
            self.assertTrue(row['ok'])
            self.assertTrue(row['patch_transport_newline_added'])
            self.assertEqual(target.read_text(), 'new\n')
            row = worker._worker_exec_one({'type':'apply_recorded_diff','diff':diff}, tmp)
            self.assertFalse(row['ok'])
            self.assertIn('patch does not apply', row['msg'])
            self.assertEqual(target.read_text(), 'new\n')

    def test_transport_lf_does_not_change_missing_target_newline(self):
        diff = "--- value.py\n+++ value.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'value.py'
            target.write_text('old')
            row = worker._worker_exec_one({'type':'apply_recorded_diff','diff':diff}, tmp)
            self.assertTrue(row['ok'])
            self.assertEqual(target.read_bytes(), b'new')

    def test_repaired_diff_checks_recorded_before_and_after_file_hashes(self):
        diff = "--- value.py\n+++ value.py\n@@ -1 +1 @@\n-old\n+new\n"
        validation = dict(path='value.py', before_sha256=hashlib.sha256(b'old\n').hexdigest(),
                          after_sha256=hashlib.sha256(b'new\n').hexdigest())
        op = dict(type='apply_recorded_diff', diff=diff, recorded_file_validation=validation)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'value.py'
            target.write_bytes(b'wrong\n')
            row = worker._worker_exec_one(op, tmp)
            self.assertFalse(row['ok'])
            self.assertIn('before-file hash', row['msg'])
            self.assertEqual(target.read_bytes(), b'wrong\n')
            target.write_bytes(b'old\n')
            row = worker._worker_exec_one(op, tmp)
            self.assertTrue(row['ok'])
            self.assertTrue(row['recorded_file_validation']['matched'])
            target.write_bytes(b'old\n')
            bad = copy.deepcopy(op);bad['recorded_file_validation']['after_sha256']='0'*64
            row = worker._worker_exec_one(bad,tmp)
            self.assertFalse(row['ok'])
            self.assertIn('after-file hash',row['msg'])


if __name__ == "__main__":
    unittest.main()
