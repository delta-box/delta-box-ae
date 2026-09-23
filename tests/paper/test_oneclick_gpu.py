"""CPU-only integration checks for one-click GPU admission, resume and Figure 8."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ae.scripts import run_review as review
from repro import gpu_protocol as protocol, review_gpu as gpu
from ae.scripts.build_review_comparison import figure08_supplement, supplemental_markdown
from tests.paper.test_gpu_timing import measurement, runner

SOURCE = {"source_commit": "a" * 40, "source_sha256": "b" * 64}


class OneClickGPUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}")
        (self.model / "model.safetensors").write_bytes(b"test fixture; never loaded")
        self.config = protocol.load_config(model_path=str(self.model), devices=["0", "1", "2", "3"])
        self.config_path = self.root / "gpu.json"
        self.config_path.write_text(json.dumps(self.config))
        self.ready = dict(ok=True, selected_gpus=[dict(uuid=f"GPU-fixture-{i}", index=str(i)) for i in range(4)],
                          initial_gpu_processes=[])
        self.calls = []
        self.busy = False
        self.fail_run = False
        self.subject = SimpleNamespace(config={"gpu": {"config": str(self.config_path)}}, limits=[],
            output=self.root / "review", attempt="attempt-001", previous_record={}, python=sys.executable,
            save=lambda: None, step=self.step,
            record=dict(release=SOURCE, experiments=[*gpu.FANOUT, gpu.GPU], outputs={}, steps=[],
                        coverage=[dict(experiment=name, status="ok", successful_jobs=1) for name in gpu.FANOUT]))
        self.subject.output.mkdir()

    def executor(self, argv, output, **kwargs):
        output.mkdir(parents=True)
        config_path = Path(argv[argv.index("--config") + 1])
        config = json.loads(config_path.read_text())
        case = protocol.case_by_id(config, argv[argv.index("--case") + 1])
        raw = measurement(case, runner.digest(config_path), config["devices"][:case["num_gpus"]])
        raw.update(worker_source_sha256=runner.digest(runner.WORKER),
                   protocol_source_sha256=runner.digest(Path(protocol.__file__)))
        protocol.publish_json(Path(argv[argv.index("--output") + 1]), raw)
        return dict(status="ok", returncode=0)

    def step(self, name, command, timeout=0, **kwargs):
        self.calls.append((name, list(command)))
        ok = True
        if name == gpu.GPU + "-check":
            path = Path(command[command.index("--output") + 1])
            path.write_text(json.dumps(dict(self.ready, ok=not self.busy)))
            ok = not self.busy
        elif name == gpu.GPU + "-run":
            if self.fail_run:
                ok = False
            else:
                config = protocol.load_config(command[command.index("--config") + 1])
                output = Path(command[command.index("--output") + 1])
                with patch.object(runner, "from_environment", return_value=SOURCE):
                    runner.run_suite(config, output, preflight=self.ready, executor=self.executor, inventory=lambda: ([], []))
        else:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.subject.record["steps"].append(dict(name=name, status="ok" if ok else "failed"))
        return ok

    def run_gpu(self):
        with contextlib.redirect_stdout(io.StringIO()):
            gpu.run_gpu(self.subject)
        return self.subject.record["coverage"][-1]

    def analysis(self):
        directory = self.root / "analysis"
        directory.mkdir()
        series = [dict(panel="a", backend=backend, x=n, y=100, unit="ms", estimated=False,
                       plot_group="this-run", source_identity="release-sha256:" + SOURCE["source_sha256"])
                  for backend in ("deltabox", "cube", "e2b") for n in (1, 4, 16, 64)]
        (directory / "summary.json").write_text(json.dumps(
            dict(schema_version=1, source="fresh", experiments={"figure-08": dict(status="analyzed", series=series)})))
        return directory

    def test_full_defaults_and_figure8_include_gpu_but_smoke_and_cpu_do_not(self):
        for flags, expected in (([], True), (["--all"], True), (["--group", "figure-08"], True),
                                (["--group", "gpu"], True), (["--smoke"], False), (["--group", "cpu"], False),
                                (["--group", "figure-08-cpu"], False)):
            with self.subTest(flags=flags), patch.object(review, "current_source", return_value=SOURCE):
                instance = review.Review(review.parser().parse_args(flags), {}, self.root)
                self.assertEqual(gpu.GPU in instance.experiments, expected)

    def test_busy_gpu_does_not_launch_workers_or_become_a_success(self):
        self.busy = True
        row = self.run_gpu()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["successful_jobs"], 0)
        self.assertEqual([name for name, _ in self.calls], [gpu.GPU + "-check"])
        self.assertIn("联系作者", row["reasons"][0])
        metadata = gpu.finish_gpu(self.subject, self.analysis(), True)
        panels = figure08_supplement(metadata)["panels"]
        self.assertEqual([panel["status"] for panel in panels], ["failed", "failed"])
        self.assertTrue(all(not panel["artifacts"] for panel in panels))

    def test_complete_matrix_drives_plots_and_theory_from_this_run(self):
        row = self.run_gpu()
        self.assertEqual((row["status"], row["successful_jobs"]), ("ok", 8))
        summary = json.loads(Path(row["summary"]["path"]).read_text())
        self.assertEqual([case["num_gpus"] for case in summary["cases"]], [1, 1, 1, 1, 1, 1, 4, 4])
        analysis = self.analysis()
        metadata = gpu.finish_gpu(self.subject, analysis, True)
        panels = figure08_supplement(metadata)["panels"]
        self.assertEqual([panel["status"] for panel in panels], ["ok", "ok"])
        command = next(cmd for name, cmd in self.calls if name == gpu.THEORY)
        self.assertEqual(command[command.index("--gpu-results") + 1], row["summary"]["path"])
        self.assertEqual(command[command.index("--fanout-summary") + 1], str(analysis / "summary.json"))
        for language in ("en", "zh"):
            text = "\n".join(supplemental_markdown(panels, language=language))
            self.assertIn("### Figure 8(b)", text)
            self.assertIn("### Figure 8(c)", text)

    def test_verified_successful_gpu_matrix_is_reused_on_resume(self):
        self.run_gpu()
        self.subject.previous_record = copy.deepcopy(self.subject.record)
        self.subject.record["coverage"] = [row for row in self.subject.record["coverage"] if row["experiment"] != gpu.GPU]
        self.subject.attempt = "attempt-002"
        self.calls.clear()
        row = self.run_gpu()
        self.assertTrue(row["reused_verified"])
        self.assertEqual(self.calls, [])

    def test_changed_worker_result_cannot_be_reused(self):
        row = self.run_gpu()
        self.subject.previous_record = copy.deepcopy(self.subject.record)
        self.subject.record["coverage"] = []
        suite_path = Path(row["summary"]["path"])
        suite = json.loads(suite_path.read_text())
        result = suite_path.parent / suite["cases"][0]["result"]["path"]
        result.write_text(result.read_text() + " ")
        self.subject.attempt = "attempt-002"
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "worker result changed"):
            self.run_gpu()
        self.assertEqual(self.calls, [])

    def test_partial_or_busy_allowed_config_cannot_pass_as_full(self):
        for update in ({"batches": [1]}, {"allow_busy": True}):
            with self.subTest(update=update):
                self.config_path.write_text(json.dumps(dict(self.config, **update)))
                with self.assertRaises(ValueError):
                    self.run_gpu()
        self.assertEqual(self.calls, [])

    def test_failed_matrix_is_not_used_for_theory(self):
        self.fail_run = True
        row = self.run_gpu()
        self.assertEqual(row["status"], "failed")
        metadata = gpu.finish_gpu(self.subject, self.analysis(), True)
        self.assertNotIn(gpu.THEORY, [name for name, _ in self.calls])
        self.assertEqual(figure08_supplement(metadata)["panels"][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
