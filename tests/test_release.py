"""Release boundaries: credentials, source identity, and shared protocol."""
import contextlib
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch, Mock, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.npd import npd
from agent.run import parse_args
from common.runtime_profile import checkpoint_environment
from release import lock


class AgentBoundaryTests(unittest.TestCase):
    def test_step_timeout_aborts_instead_of_sending_another_step(self):
        from agent.host import decoupled_mcts as host
        with tempfile.TemporaryDirectory() as tmp:
            driver = Mock()
            driver.start = AsyncMock()
            driver.shutdown = AsyncMock()
            driver.checkpoint = AsyncMock(return_value=Mock(wall_ms=1, ckpt_id="root"))
            driver.merged_dir = tmp
            client = Mock()
            client.init.return_value = {"ok": True}
            client.step.side_effect = TimeoutError("late LLM response")
            with patch("agent.sandbox_driver.SandboxDriver", return_value=driver), patch("agent.host.worker_client.WorkerClient", return_value=client), patch.object(host, "_launch_npd", return_value=Mock()), patch.object(host.time, "sleep"), patch.object(host.LOG, "exception"):
                result = asyncio.run(host.run_decoupled({"task": "test", "model": "test", "base_lower": tmp, "workdir": str(Path(tmp) / "work"), "max_iter": 8}))
            self.assertFalse(result["ok"])
            self.assertEqual(client.step.call_count, 1)
            driver.shutdown.assert_awaited_once()

    def test_shutdown_terminates_namespace_owner_after_restore(self):
        from agent.sandbox_driver import SandboxDriver
        driver = object.__new__(SandboxDriver)
        driver._ns_init_host_pid = 123
        driver._ns_init_start_time = "1000"
        driver._agent_host_pid = 456  # stale original active; must not be killed
        driver.sudo_wrap = True
        with patch.object(driver, "_process_start_time", return_value="1000"), patch("agent.sandbox_driver.subprocess.run", return_value=Mock(returncode=0)) as run, patch("backends.deltabox.gsd.process_wait.wait_for_process_exit", return_value=set()) as wait:
            driver._terminate_namespace()
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "kill", "-KILL", "123"])
        self.assertEqual(wait.call_args.args[0], [123])

    def test_failed_dump_still_cleans_namespace_and_fails_run(self):
        from agent.sandbox_driver import SandboxDriver
        driver = object.__new__(SandboxDriver)
        driver._ctrl = Mock(registry={"ck": {"dump_error": "failed"}}, mount_fd=None)
        driver._send_shutdown = AsyncMock()
        driver._terminate_namespace = Mock()
        driver._shell_proc = Mock()
        driver._overlay_mounted = False
        with self.assertRaisesRegex(RuntimeError, "checkpoint drain failed"):
            asyncio.run(driver.shutdown())
        driver._terminate_namespace.assert_called_once()
        driver._shell_proc.wait.assert_called_once()

    def test_no_key_fails_before_starting_runtime(self):
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parse_args(["--task", "test"])
        self.assertEqual(raised.exception.code, 2)

    def test_workdir_cannot_destroy_source_or_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            work = Path(tmp) / "work"
            work.mkdir()
            (work / "keep").write_text("precious")
            for destination in (source, source / "nested", Path(tmp), work):
                with self.subTest(destination=destination), patch.dict(os.environ, {"API_KEY": "test-only"}), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parse_args(["--task", "test", "--base-lower", str(source), "--workdir", str(destination)])
            self.assertEqual((work / "keep").read_text(), "precious")

    def test_authentication_error_not_retried_or_echoed(self):
        error = urllib.error.HTTPError("https://example.invalid", 401, "unauthorized", {}, io.BytesIO(b"secret echo"))
        with patch.object(npd, "API_KEY", "test-only"), patch.object(npd, "API_BASE", "https://example.invalid/v1"), patch.object(npd.urllib.request, "urlopen", side_effect=error) as call, patch.object(npd.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
                npd._chat([], 0)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    def test_transient_retry_is_finite(self):
        with patch.dict(os.environ, {"NPD_MAX_ATTEMPTS": "2"}), patch.object(npd, "API_KEY", "test-only"), patch.object(npd, "API_BASE", "https://example.invalid/v1"), patch.object(npd.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as call, patch.object(npd.time, "sleep") as sleep, patch.object(npd, "_log"):
            with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
                npd._chat([], 0)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_chat_completions_transport_contract(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}).encode())
        with patch.object(npd, "API_KEY", "test-only"), patch.object(npd, "API_BASE", "https://example.invalid/v1/"), patch.object(npd.urllib.request, "urlopen", return_value=response) as call:
            self.assertEqual(npd._chat([], 0)["content"], "done")
        self.assertEqual(call.call_args.args[0].full_url, "https://example.invalid/v1/chat/completions")


class ReleaseIdentityTests(unittest.TestCase):
    def test_shared_default_and_explicit_historical_protocol(self):
        default = checkpoint_environment()
        historical = checkpoint_environment("historical-async-full")
        self.assertEqual(default["DELTABOX_FIXED_ACTIVE_PID"], "100")
        self.assertEqual(default["DELTABOX_ASYNC_TEMPLATE_FULL_DUMP"], "0")
        self.assertEqual(historical["DELTABOX_ASYNC_TEMPLATE_FULL_DUMP"], "1")
        self.assertEqual(checkpoint_environment(mode="slow")["DELTABOX_FORCE_CRIU_RESTORE"], "1")

    def test_source_lock_accepts_docs_but_rejects_modified_or_extra_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "agent").mkdir()
            source = root / "agent/run.py"
            source.write_text("pass\n")
            (root / "ae").mkdir()
            launcher = root / "ae/run_all.sh"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test")
            frozen = lock.create(root)
            path = root / "release/candidate-lock.json"
            path.parent.mkdir()
            path.write_text(json.dumps(frozen))
            lock.verify(path, root)
            (root / "agent/README.md").write_text("Documentation only")
            lock.verify(path, root)
            launcher.write_text("#!/bin/sh\nexit 1\n")
            with self.assertRaisesRegex(ValueError, "source mismatch.*ae/run_all.sh"):
                lock.verify(path, root)
            launcher.write_text("#!/bin/sh\nexit 0\n")
            source.write_text("raise RuntimeError()\n")
            with self.assertRaisesRegex(ValueError, "source mismatch"):
                lock.verify(path, root)
            source.write_text("pass\n")
            (root / "agent/shadow.py").write_text("pass\n")
            with self.assertRaisesRegex(ValueError, "untracked"):
                lock.verify(path, root)


if __name__ == "__main__":
    unittest.main()
