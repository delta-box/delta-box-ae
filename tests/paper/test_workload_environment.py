"""Workload setup must use verified source/env and retain failures as failures."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "replay/guest"))
import workload_environment as workload

spec = importlib.util.spec_from_file_location("workload_test_agent", ROOT / "replay/guest/agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class WorkloadEnvironmentTests(unittest.TestCase):
    def test_metadata_requires_exact_commit(self):
        profile = workload.WORKLOADS["astropy__astropy-14182"]
        self.assertEqual(workload.workload_for("astropy__astropy-14182", profile["base_commit"])["version"], "5.1")
        with self.assertRaisesRegex(ValueError, "commit mismatch"):
            workload.workload_for("astropy__astropy-14182", "0" * 40)
        self.assertIsNone(workload.workload_for("unknown", "0" * 40))

    def test_missing_environment_cannot_fall_back_to_generic_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "missing workload interpreter"):
                workload.prepare_workload(workload.WORKLOADS["django__django-14672"], environments=tmp)

    def test_build_failure_is_evidence_and_does_not_select_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "astropy__5.1/bin/python"
            binary.parent.mkdir(parents=True)
            binary.touch()
            evidence = root / "evidence.json"
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(workload.subprocess, "check_output", return_value='{"python":"3.9","packages":[]}'), \
                 patch.object(workload.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["build"])):
                with self.assertRaises(subprocess.CalledProcessError):
                    workload.prepare_workload(workload.WORKLOADS["astropy__astropy-14182"], root=tmp,
                                               environments=tmp, evidence=str(evidence))
                self.assertNotIn("AGENT_WORKER_PYTHON", os.environ)
            self.assertEqual(json.loads(evidence.read_text())["status"], "failed")

    def test_only_test_subprocess_uses_explicit_interpreter_and_safe_arguments(self):
        paths = ["tests/a b.py", "tests/$(touch sentinel).py"]
        with patch.object(agent, "WORKER_PYTHON", "/opt/workload env/bin/python"):
            command = agent._worker_shell_command_for_tests({"test_files": paths})
        self.assertEqual(shlex.split(command), ["/opt/workload env/bin/python", "-m", "pytest", "-q", *paths])

    def test_django_uses_native_settings_and_module_labels(self):
        with patch.object(agent, "WORKER_TEST_RUNNER", "django"), patch.object(agent, "WORKER_PYTHON", "/env/bin/python"):
            command = agent._worker_shell_command_for_tests({"test_files": ["tests/test_many_to_many_proxy.py", "tests/many_to_many/tests.py"]})
            self.assertEqual(shlex.split(command), ["/env/bin/python", "tests/runtests.py", "--settings=test_sqlite", "--parallel=1", "--noinput", "test_many_to_many_proxy", "many_to_many.tests"])
            missing = agent._worker_shell_command_for_tests({"test_files": ["django/tests"], "expected_outcome": {"kind": "missing_test_files"}})
            self.assertIn("-m pytest", missing)

    def test_django_workload_errors_require_completed_test_run(self):
        with patch.object(agent, "WORKER_TEST_RUNNER", "django"):
            self.assertEqual(agent._test_command_outcome({}, 1, "", "RuntimeError: invalid generated model\nRan 1 test in 0.001s\n\nFAILED (errors=1)\n"), "completed")
            self.assertEqual(agent._test_command_outcome({}, 0, "", "Ran 2 tests in 0.002s\n\nOK\n"), "completed")
            for rc, output in [(1, "ModuleNotFoundError: dependency"), (0, "Ran 0 tests in 0s\nOK"), (2, "Ran 1 test in 0s\nFAILED (errors=1)")]:
                self.assertEqual(agent._test_command_outcome({}, rc, "", output), "infrastructure-error")

    def test_generated_django_model_error_is_preserved_and_not_a_test_pass(self):
        op = {"type": "run_tests", "test_files": ["tests/test_generated.py"]}
        stderr = "RuntimeError: Model class test_generated.Parent doesn't declare an explicit app_label and isn't in an application in INSTALLED_APPS.\n"
        result = subprocess.CompletedProcess([], 1, "", stderr)
        with patch.object(agent, "WORKER_TEST_RUNNER", "django"), patch.object(agent.subprocess, "run", return_value=result):
            row = agent._worker_exec_one(op, "/tmp")
            self.assertTrue(row["ok"])
            self.assertFalse(row["test_passed"])
            self.assertEqual(row["test_outcome"], "workload-collection-error")
            self.assertEqual(row["stderr"], stderr)
            self.assertEqual(row["rc"], 1)
            self.assertEqual(agent._clip_worker_result_for_control(row)["test_outcome"], "workload-collection-error")
            self.assertEqual(agent._test_command_outcome({"test_files": ["tests/other.py"]}, 1, "", stderr), "infrastructure-error")

    def test_failed_traceback_keeps_the_cause_in_bounded_control_reply(self):
        output = "Traceback start\n" + "frame\n" * 900 + "ModuleNotFoundError: dependency"
        reply = agent._clip_worker_result_for_control({"ok": False, "stdout": output})
        self.assertTrue(reply["stdout"].startswith("Traceback start"))
        self.assertTrue(reply["stdout"].endswith("ModuleNotFoundError: dependency"))
        self.assertLess(len(reply["stdout"]), 2100)


if __name__ == "__main__":
    unittest.main()
