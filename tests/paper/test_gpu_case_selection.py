"""GPU subset entry contracts; no SSH, GPU work, or old result mutation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ae.scripts import run_review as review
from ae.scripts import figure08_remote as remote

SOURCE = {"source_commit": "a" * 40, "source_sha256": "b" * 64}
SELECTED = ["training-B16", "training-B64"]


class GPUCaseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def subject(self, flags):
        with patch.object(review, "current_source", return_value=SOURCE):
            return review.Review(review.parser().parse_args(flags), {}, self.root)

    def result(self, requested, *, successful=None):
        successful = requested if successful is None else successful
        missing = [case for case in requested if case not in successful]
        return dict(mode="auto", status="complete" if len(successful) == 8 else "partial",
                    successful_cases=len(successful), expected_cases=8,
                    requested_cases=requested, requested_case_count=len(requested),
                    successful_selected_cases=len(successful),
                    selected_status="partial" if missing else "complete", missing_selected_cases=missing,
                    reason="" if len(successful) == 8 else "Paper coverage remains partial")

    def dispatch(self, subject, result=None, error=None):
        with patch.object(remote, "run_auto", return_value=result, side_effect=error) as run, \
             patch.object(subject, "save"), contextlib.redirect_stdout(io.StringIO()):
            subject.run_gpu()
        return run, subject.record["coverage"][-1]

    def test_default_dispatch_still_requests_all_eight(self):
        subject = self.subject(["--group", "gpu"])
        expected = list(review.GPU_CASES)
        run, row = self.dispatch(subject, self.result(expected))
        self.assertEqual(subject.record["gpu_requested_cases"], expected)
        self.assertEqual(run.call_args.kwargs["requested_case_ids"], expected)
        self.assertEqual((row["status"], row["planned_jobs"], row["successful_jobs"]), ("ok", 8, 8))
        self.assertEqual((row["selected_status"], row["successful_selected_cases"]), ("complete", 8))

    def test_default_selection_overrides_hidden_remote_config_subset(self):
        config = self.root / "remote.json"
        config.write_text(json.dumps({"requested_cases": SELECTED}))
        subject = self.subject(["--group", "gpu"])
        subject.config["gpu_remote_config"] = str(config)
        expected = list(review.GPU_CASES)
        run, row = self.dispatch(subject, self.result(expected))
        run.assert_called_once_with(self.root / "gpu/attempt-001", config, requested_case_ids=expected)
        self.assertEqual(row["requested_cases"], expected)
        self.assertEqual(json.loads(config.read_text())["requested_cases"], SELECTED)

    def test_selected_two_are_forwarded_without_claiming_eight(self):
        subject = self.subject(["--experiment", "figure-08-gpu", "--gpu-cases", "training-B64,training-B16"])
        run, row = self.dispatch(subject, self.result(SELECTED))
        run.assert_called_once_with(self.root / "gpu/attempt-001", remote.DEFAULT_CONFIG,
                                    requested_case_ids=SELECTED)
        self.assertEqual(subject.record["gpu_requested_cases"], SELECTED)
        self.assertEqual((row["status"], row["planned_jobs"], row["successful_jobs"]), ("partial", 8, 2))
        self.assertEqual((row["selected_status"], row["requested_case_count"], row["successful_selected_cases"]),
                         ("complete", 2, 2))
        self.assertEqual(row["requested_cases"], SELECTED)
        self.assertEqual(row["missing_selected_cases"], [])

    def test_partial_selected_scope_retains_missing_case(self):
        subject = self.subject(["--group", "gpu", "--gpu-cases", ",".join(SELECTED)])
        _, row = self.dispatch(subject, self.result(SELECTED, successful=SELECTED[:1]))
        self.assertEqual((row["status"], row["selected_status"], row["successful_jobs"]), ("partial", "partial", 1))
        self.assertEqual(row["missing_selected_cases"], SELECTED[1:])

    def test_invalid_and_repeated_case_arguments_are_rejected(self):
        for value in ("", "training-B16,", "training-B16,training-B16", "training-B3", "all", " training-B16"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                review.parser().parse_args(["--group", "gpu", "--gpu-cases", value])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            review.parser().parse_args(["--group", "gpu", "--gpu-cases", "training-B16",
                                       "--gpu-cases", "training-B64"])

    def test_explicit_case_selection_rejects_other_execution_modes_before_io(self):
        modes = [[], ["--all"], ["--test"], ["--smoke"], ["--list"], ["--group", "cpu"],
                 ["--group", "figure-08"], ["--experiment", "correctness"],
                 ["--group", "gpu", "--experiment", "correctness"],
                 ["--group", "gpu", "--available"], ["--group", "gpu", "--limit", "1"],
                 ["--group", "gpu", "--max-events", "1"],
                 ["--group", "gpu", "--analyze-existing", "/unread"],
                 ["--group", "gpu", "--execute-plan", "/unread"],
                 ["--group", "gpu", "--probe-plan", "/unread"],
                 ["--group", "gpu", "--publish-output", "/unread"]]
        for flags in modes:
            with self.subTest(flags=flags), patch.object(remote, "run_auto") as run, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                review.main([*flags, "--gpu-cases", ",".join(SELECTED)])
            run.assert_not_called()

    def test_explicit_gpu_group_and_experiment_are_compatible(self):
        for flags in (["--group", "gpu"], ["--experiment", "figure-08-gpu"],
                      ["--group", "gpu", "--experiment", "figure-08-gpu"]):
            with self.subTest(flags=flags):
                subject = self.subject([*flags, "--gpu-cases", ",".join(SELECTED)])
                self.assertEqual(subject.experiments, [review.GPU])

    def test_resume_preserves_case_selection_and_original_manifest(self):
        original = self.subject(["--group", "gpu", "--gpu-cases", ",".join(SELECTED)])
        path = self.root / "review.json"
        raw = json.dumps(original.record)
        path.write_text(raw)
        subject = self.subject(["--group", "gpu", "--gpu-cases", "training-B64,training-B16", "--resume", str(self.root)])
        self.assertEqual(subject.attempt, "attempt-002")
        self.assertEqual(subject.record["gpu_requested_cases"], SELECTED)
        self.assertEqual(path.read_text(), raw)
        for flags in ([], ["--gpu-cases", "training-B16"], ["--gpu-cases", "generation-B1"]):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "GPU case selection differs"):
                self.subject(["--group", "gpu", "--resume", str(self.root), *flags])
        self.assertEqual(path.read_text(), raw)

    def test_legacy_default_resume_is_full_matrix_and_rejects_subset(self):
        prior = copy.deepcopy(self.subject(["--group", "gpu"]).record)
        prior.pop("gpu_requested_cases")
        (self.root / "review.json").write_text(json.dumps(prior))
        resumed = self.subject(["--group", "gpu", "--resume", str(self.root)])
        self.assertEqual(resumed.record["gpu_requested_cases"], list(review.GPU_CASES))
        with self.assertRaisesRegex(ValueError, "GPU case selection differs"):
            self.subject(["--group", "gpu", "--resume", str(self.root), "--gpu-cases", ",".join(SELECTED)])

    def test_admission_failure_retains_requested_scope_and_zero_success(self):
        subject = self.subject(["--group", "gpu", "--gpu-cases", ",".join(SELECTED)])
        _, row = self.dispatch(subject, error=RuntimeError("admission failed"))
        self.assertEqual((row["status"], row["planned_jobs"], row["successful_jobs"]), ("failed", 8, 0))
        self.assertEqual(row["requested_cases"], SELECTED)
        self.assertEqual(row["missing_selected_cases"], SELECTED)
        self.assertEqual(row["selected_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
