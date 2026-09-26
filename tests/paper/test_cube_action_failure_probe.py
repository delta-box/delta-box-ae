"""Failure evidence remains useful when any Cube diagnostic transport fails."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "ae/vendor/finalbench/cube_cow_peagle_mcts30_2x_numa12_realrtt/scripts/cube_action_failure_probe.py"
spec = importlib.util.spec_from_file_location("cube_action_failure_probe", PROBE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class CubeActionFailureProbe(unittest.TestCase):
    def sandbox(self):
        return SimpleNamespace(
            commands=SimpleNamespace(run=Mock(return_value=SimpleNamespace(
                exit_code=0, stdout='{"processes":[]}', stderr=""))),
            files=SimpleNamespace(read=Mock(return_value='{"seq":55}\n')),
            close=Mock(return_value=None),
        )

    def test_command_failure_does_not_discard_file_evidence(self):
        sb = self.sandbox()
        sb.commands.run.side_effect = TimeoutError("transport timeout")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "probe.json"
            report = probe.collect("sandbox1", out, connect=lambda _: sb)
            saved = json.loads(out.read_text())
        self.assertEqual(saved, report)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["steps"]["processes"]["ok"])
        self.assertEqual(sb.files.read.call_count, len(probe.FILE_PATHS))
        self.assertEqual(report["steps"]["file:" + probe.FILE_PATHS[0]]["result"]["text"], '{"seq":55}\n')
        sb.close.assert_called_once()

    def test_file_failure_does_not_discard_other_files_or_processes(self):
        sb = self.sandbox()
        def read(path):
            if path.endswith("action.resp.json.tmp"):
                raise FileNotFoundError("not yet written")
            return path
        sb.files.read.side_effect = read
        with tempfile.TemporaryDirectory() as directory:
            report = probe.collect("sandbox1", Path(directory) / "probe.json", connect=lambda _: sb)
        self.assertEqual(report["status"], "partial")
        self.assertTrue(report["steps"]["processes"]["ok"])
        self.assertFalse(report["steps"]["file:/tmp/finalbench/action.resp.json.tmp"]["ok"])
        self.assertTrue(report["steps"]["file:/tmp/action.req.json"]["ok"])
        self.assertEqual(report["steps"]["file:/tmp/action.req.json"]["result"]["text"], "/tmp/action.req.json")

    def test_evidence_is_saved_before_later_step_can_hang(self):
        sb = self.sandbox()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "probe.json"
            def read(_):
                saved = json.loads(out.read_text())
                self.assertEqual(saved["status"], "running")
                self.assertTrue(saved["steps"]["processes"]["ok"])
                return ""
            sb.files.read.side_effect = read
            report = probe.collect("sandbox1", out, connect=lambda _: sb)
        self.assertEqual(report["status"], "complete")

    def test_connect_failure_is_saved_without_transport_or_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "probe.json"
            report = probe.collect("sandbox1", out, connect=Mock(
                side_effect=PermissionError("credential=do-not-persist")))
            saved = out.read_text()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(set(report["steps"]), {"connect"})
        self.assertNotIn("do-not-persist", saved)
        self.assertEqual(report["steps"]["connect"]["error"], "PermissionError")

    def test_nonzero_process_exit_preserves_its_output_and_marks_partial(self):
        sb = self.sandbox()
        sb.commands.run.return_value = SimpleNamespace(exit_code=1, stdout="partial proc data", stderr="denied")
        with tempfile.TemporaryDirectory() as directory:
            report = probe.collect("sandbox1", Path(directory) / "probe.json", connect=lambda _: sb)
        step = report["steps"]["processes"]
        self.assertFalse(step["ok"])
        self.assertEqual(step["result"]["stdout"]["text"], "partial proc data")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(sb.commands.run.call_args.kwargs["timeout"], probe.COMMAND_TIMEOUT)

    def test_close_error_does_not_replace_collected_evidence(self):
        sb = self.sandbox()
        sb.close.side_effect = OSError("close failed")
        with tempfile.TemporaryDirectory() as directory:
            report = probe.collect("sandbox1", Path(directory) / "probe.json", connect=lambda _: sb)
        self.assertFalse(report["steps"]["close_client"]["ok"])
        self.assertTrue(report["steps"]["processes"]["ok"])
        self.assertEqual(len([k for k in report["steps"] if k.startswith("file:")]), len(probe.FILE_PATHS))

    def test_readonly_connect_uses_get_and_bounds_private_client(self):
        sb = SimpleNamespace(_build_data_client=Mock(return_value=SimpleNamespace()), close=Mock())
        data = {"sandboxID": "sandbox1", "state": "running", "envdAccessToken": "not-for-output"}
        sdk = ModuleType("cubesandbox")
        sdk.Config = Mock(return_value=SimpleNamespace(api_url="http://local-cube"))
        sdk.Sandbox = Mock(return_value=sb)
        requests = ModuleType("requests")
        requests.get = Mock(return_value=SimpleNamespace(raise_for_status=Mock(), json=lambda: data))
        httpx = ModuleType("httpx")
        httpx.Timeout = Mock(return_value="bounded-timeout")
        with patch.dict(sys.modules, cubesandbox=sdk, requests=requests, httpx=httpx):
            result = probe.connect_readonly("sandbox1")
        self.assertIs(result, sb)
        requests.get.assert_called_once_with("http://local-cube/sandboxes/sandbox1",
                                           timeout=(probe.IO_TIMEOUT, probe.IO_TIMEOUT))
        self.assertEqual(sb._client.timeout, "bounded-timeout")
        sdk.Config.assert_called_once_with(request_timeout=probe.IO_TIMEOUT)
        sdk.Sandbox.assert_called_once_with(data, config=sdk.Config.return_value)

    def test_readonly_connect_rejects_paused_or_wrong_identity(self):
        for data in ({"sandboxID": "other", "state": "running"},
                     {"sandboxID": "sandbox1", "state": "paused"}):
            with self.subTest(data=data):
                sdk = ModuleType("cubesandbox")
                sdk.Config = Mock(return_value=SimpleNamespace(api_url="http://local-cube"))
                sdk.Sandbox = Mock()
                requests = ModuleType("requests")
                requests.get = Mock(return_value=SimpleNamespace(raise_for_status=Mock(), json=lambda: data))
                with patch.dict(sys.modules, cubesandbox=sdk, requests=requests, httpx=ModuleType("httpx")):
                    with self.assertRaises(ValueError):
                        probe.connect_readonly("sandbox1")
                sdk.Sandbox.assert_not_called()

    def test_rejects_unsafe_sandbox_before_creating_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "probe.json"
            connector = Mock()
            with self.assertRaises(ValueError):
                probe.collect("../../other", out, connect=connector)
            connector.assert_not_called()
            self.assertFalse(out.exists())

    def test_remote_probe_is_valid_python_and_never_reads_environment(self):
        compile(probe.PROCESS_PROBE, "<sandbox-process-probe>", "exec")
        self.assertNotIn("/environ", probe.PROCESS_PROBE)
        self.assertNotIn("os.environ", probe.PROCESS_PROBE)


if __name__ == "__main__":
    unittest.main()
