"""Run the official driver against a fake SDK without changing its batch policy."""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "official_fork_failure_fixture",
    ROOT / "ae/vendor/finalbench/official_sandbox_fork/bench_official_fork.py",
)
DRIVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DRIVER
SPEC.loader.exec_module(DRIVER)


class FakeSDK:
    def __init__(self, *, failure=None, failed_index=2, fail_source_number=None):
        self.failure = failure
        self.failed_index = failed_index
        self.fail_source_number = fail_source_number
        self.creates, self.verifies, self.kills, self.snapshots = [], [], [], []
        self.sources = 0

    def create(self, *, template, timeout, metadata, **options):
        role, index = metadata["role"], metadata.get("index")
        self.creates.append((role, index, template, timeout))
        if role == "source":
            self.sources += 1
            if self.sources == self.fail_source_number:
                raise RuntimeError("source unavailable")
        if role == "child" and index == str(self.failed_index) and self.failure == "create":
            raise RuntimeError("HTTP 429 capacity unavailable")
        sandbox_id = f"source-{self.sources}" if role == "source" else f"child-{self.sources}-{index}"

        def command(cmd, *, timeout):
            self.verifies.append((sandbox_id, int(index), cmd, timeout))
            if int(index) == self.failed_index:
                if self.failure == "transport":
                    raise TimeoutError("HTTP request timed out before command result")
                if self.failure == "content":
                    return types.SimpleNamespace(exit_code=1, stdout="", stderr="AssertionError: token mismatch")
            token = ast.literal_eval(next(line.split("=", 1)[1].strip()
                                          for line in cmd.splitlines() if line.strip().startswith("expected_token =")))
            return types.SimpleNamespace(exit_code=0,
                stdout=f"OK token={token} bytes=67108864 checksum=2041721 requests=2 pid=14\n", stderr="")

        def snapshot(**options):
            snapshot_id = f"snapshot-{self.sources}"
            self.snapshots.append(snapshot_id)
            return types.SimpleNamespace(snapshot_id=snapshot_id)

        return types.SimpleNamespace(
            sandbox_id=sandbox_id,
            commands=types.SimpleNamespace(run=command),
            create_snapshot=snapshot,
            kill=lambda **options: self.kills.append((role, sandbox_id)),
        )

    def delete_snapshot(self, snapshot_id, **options):
        self.kills.append(("snapshot", snapshot_id))


class FanoutFailureEvidenceTests(unittest.TestCase):
    def invoke(self, sdk, forks="16"):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fanout.json"
            argv = ["bench_official_fork.py", "--backend", "e2b", "--forks", forks,
                    "--mem-mib", "64", "--max-workers", "16", "--e2b-batch-size", "16",
                    "--e2b-api-url", "http://127.0.0.1:3100",
                    "--e2b-sandbox-url", "http://127.0.0.1:3102",
                    "--e2b-template", "fixture", "--out", str(output)]
            stdout = io.StringIO()
            with patch.dict(sys.modules, {"e2b": types.SimpleNamespace(Sandbox=sdk)}), \
                    patch.object(sys, "argv", argv), \
                    patch.object(DRIVER, "e2b_write_state") as prepare, \
                    contextlib.redirect_stdout(stdout):
                rc = DRIVER.main()
            rows = json.loads(output.read_text())
            self.assertEqual(rows, [json.loads(line) for line in stdout.getvalue().splitlines()])
            self.assertTrue(all(call.kwargs["mem_mib"] == 64 for call in prepare.call_args_list))
            self.assertTrue(all(call[-1] == 120.0 for call in sdk.verifies))
            self.assertTrue(all(call[-1] == 900 for call in sdk.creates))
            return rc, rows

    def test_transport_error_retains_all_child_results_and_failure_exit(self):
        sdk = FakeSDK(failure="transport")
        rc, rows = self.invoke(sdk)
        row = rows[0]
        self.assertEqual(rc, 1)
        self.assertFalse(row["success"])
        self.assertEqual(row["error"], "RuntimeError: inherited-memory verification failed")
        self.assertIn("inherited-memory verification failed", row["traceback"])
        self.assertEqual(len(row["child_creates"]), 16)
        self.assertTrue(all(child["create"]["ok"] for child in row["child_creates"]))
        self.assertEqual(len(row["children"]), 16)
        failed = [child for child in row["children"] if not child["verify"]["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["sandbox_id"], "child-1-2")
        self.assertEqual(failed[0]["verify"]["error"], "TimeoutError: HTTP request timed out before command result")
        self.assertEqual(row["success_count"], 15)
        self.assertEqual(row["batches"][0]["success_count"], 15)
        self.assertEqual(len(sdk.verifies), 16)  # Each child attempted once; no retry.
        self.assertEqual(len(sdk.kills), 18)  # Children, snapshot, source.
        self.assertNotIn("ready_e2e_ms", row)  # Failed partial work is not a timing sample.

    def test_command_content_failure_keeps_stderr_distinct_from_transport(self):
        rc, rows = self.invoke(FakeSDK(failure="content"))
        self.assertEqual(rc, 1)
        failed = [child for child in rows[0]["children"] if not child["verify"]["ok"]]
        self.assertEqual(failed[0]["verify"]["error"],
                         "RuntimeError: command exit_code=1 stderr=AssertionError: token mismatch")

    def test_child_create_failure_keeps_create_error_and_does_not_verify(self):
        sdk = FakeSDK(failure="create")
        rc, rows = self.invoke(sdk)
        row = rows[0]
        self.assertEqual(rc, 1)
        self.assertEqual(row["error"], "RuntimeError: child create failure in measured batch")
        self.assertEqual(len(row["child_creates"]), 16)
        self.assertEqual([c["create"]["error"] for c in row["child_creates"] if not c["create"]["ok"]],
                         ["RuntimeError: HTTP 429 capacity unavailable"])
        self.assertEqual(row["children"], [])
        self.assertEqual(row["batches"], [])
        self.assertEqual(sdk.verifies, [])
        self.assertEqual(len(sdk.kills), 17)

    def test_failed_second_batch_keeps_first_batch_and_stops_without_retry(self):
        sdk = FakeSDK(failure="transport", failed_index=18)
        rc, rows = self.invoke(sdk, forks="64")
        row = rows[0]
        self.assertEqual(rc, 1)
        self.assertEqual(len(row["child_creates"]), 32)
        self.assertEqual(len(row["children"]), 32)
        self.assertEqual([(b["offset"], b["count"], b["success_count"]) for b in row["batches"]],
                         [(0, 16, 16), (16, 16, 15)])
        self.assertEqual(row["success_count"], 31)
        self.assertEqual(len(sdk.verifies), 32)
        self.assertEqual(len(sdk.kills), 34)
        self.assertEqual(len([r for r in row["cleanup"] if r["phase"] == "inter-batch"]), 16)

    def test_success_preserves_all_original_fork_points_and_four_batches(self):
        sdk = FakeSDK()
        rc, rows = self.invoke(sdk, forks="1,4,16,64")
        self.assertEqual(rc, 0)
        self.assertEqual([r["forks"] for r in rows], [1, 4, 16, 64])
        self.assertEqual([r["success_count"] for r in rows], [1, 4, 16, 64])
        self.assertTrue(all(r["success"] and r["ready_e2e_ms"] >= r["freeze_ms"] for r in rows))
        self.assertEqual([b["count"] for b in rows[-1]["batches"]], [16] * 4)
        self.assertEqual(len(sdk.verifies), 85)
        self.assertEqual(len(sdk.kills), 93)

    def test_early_failure_does_not_reuse_previous_points_evidence(self):
        sdk = FakeSDK(fail_source_number=2)
        rc, rows = self.invoke(sdk, forks="1,4")
        self.assertEqual(rc, 1)
        self.assertTrue(rows[0]["success"])
        self.assertFalse(rows[1]["success"])
        for key in ("child_creates", "children", "batches", "success_count"):
            self.assertNotIn(key, rows[1])
        self.assertEqual(len(sdk.verifies), 1)


if __name__ == "__main__":
    unittest.main()
