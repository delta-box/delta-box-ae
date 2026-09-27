"""Host-only fault injection: no VM, network or benchmark is started."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "replay"))
import host_execution
import run_instance

OOM = "[   76.906357] Out of memory: Killed process 457 (python3) anon-rss:280096kB\n"


class GuestRuntimeMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.serial = self.root / "firecracker.log"
        self.serial.write_bytes(b"boot ready\n")
        self.spec = host_execution.InstanceRun(self.root / "run.json", {
            "instance": "test-instance", "mode": "fast", "timeout": 10,
            "status": "running",
        })
        self.spec.save()
        self.vm = Mock(pid=900001)
        self.vm.poll.return_value = None
        self.machine = SimpleNamespace(process=self.vm, ssh_ready=True)

    def execute(self, script):
        # The local Python child stands in for SSH; its argv suffix is ignored.
        self.machine.ssh = [sys.executable, "-c", script]
        run_instance.execute_replay(self.machine, self.spec)

    def failure(self):
        return json.loads((self.root / "guest_runtime_failure.json").read_text())

    def assert_reaped(self, pid):
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        self.vm.terminate.assert_not_called()
        self.vm.kill.assert_not_called()

    def test_live_ssh_is_terminated_on_real_guest_oom_and_original_evidence_is_saved(self):
        script = ("import pathlib,time; time.sleep(.05); "
                  f"p=pathlib.Path({str(self.serial)!r}); "
                  f"p.open('a').write({OOM!r}); time.sleep(30)")
        started = time.monotonic()
        with self.assertRaisesRegex(run_instance.GuestRuntimeFailure, "Killed process 457"):
            self.execute(script)
        self.assertLess(time.monotonic() - started, 5)
        row = self.failure()
        self.assertEqual(row["kind"], "guest_oom")
        self.assertEqual(row["guest_pid"], 457)
        self.assertEqual(row["guest_kernel_time_s"], 76.906357)
        self.assertEqual(row["serial_byte_offset"], len(b"boot ready\n"))
        self.assertEqual(row["runner_pid"], os.getpid())
        self.assertIn("Killed process 457", row["serial_line"])
        self.assertTrue(row["observed_at"].endswith("+00:00"))
        self.assert_reaped(row["ssh_pid"])
        self.assertIn("Killed process 457", (self.root / "host_errors.json").read_text())

    def test_kernel_panic_is_fatal_even_when_ssh_returns_zero(self):
        self.serial.write_text("[ 22.0] Kernel panic - not syncing: Fatal exception\n")
        with self.assertRaisesRegex(run_instance.GuestRuntimeFailure, "Kernel panic"):
            self.execute("pass")
        self.assertEqual(self.failure()["kind"], "guest_kernel_panic")
        self.assert_reaped(self.failure()["ssh_pid"])

    def test_split_kernel_line_has_exact_offset_and_ordinary_warning_is_not_fatal(self):
        prefix = b"user says Out of memory: Killed process 1\n\nWarn: Pid reuse detected\n"
        self.serial.write_bytes(prefix + b"[ 3.25] Out of mem")
        with self.serial.open("rb") as stream:
            monitor = run_instance.GuestSerialMonitor(stream)
            self.assertFalse(monitor.check())
            offset = stream.tell()
            self.assertFalse(monitor.check())
            self.assertEqual(stream.tell(), offset)
            with self.serial.open("ab") as writer:
                writer.write(b"ory: Killed process 81 (python3)")
            with self.assertRaises(run_instance.GuestRuntimeFailure) as caught:
                monitor.check()
        self.assertEqual(caught.exception.evidence["serial_byte_offset"], len(prefix))
        self.assertEqual(caught.exception.evidence["guest_pid"], 81)

    def test_oom_after_last_poll_is_not_hidden_by_zero_ssh_exit(self):
        process = Mock(pid=900002)
        def exited():
            with self.serial.open("a") as out:
                out.write(OOM)
            return 0
        process.poll.side_effect = exited
        self.machine.ssh = ["unused-ssh"]
        with patch.object(run_instance.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(run_instance.GuestRuntimeFailure, "Killed process 457"):
                run_instance.execute_replay(self.machine, self.spec)
        process.wait.assert_called_once_with(timeout=5)

    def test_final_drain_stops_at_exit_snapshot_even_if_serial_keeps_growing(self):
        class GrowingSerial(io.BytesIO):
            name = "firecracker.log"
            reads = 0
            def read(self, size=-1):
                self.reads += 1
                position = self.tell()
                self.seek(0, 2)
                self.write(b"still logging\n" * 8192)
                self.seek(position)
                return super().read(size)
        initial = b"healthy boot\n" * 10000
        stream = GrowingSerial(initial)
        monitor = run_instance.GuestSerialMonitor(stream)
        while monitor.check(limit=len(initial)):
            pass
        self.assertEqual(stream.tell(), len(initial))
        self.assertGreater(len(stream.getvalue()), len(initial))
        self.assertEqual(stream.reads, 2)

    def test_firecracker_exit_reaps_only_ssh_and_records_vm_status(self):
        self.vm.poll.return_value = 17
        with self.assertRaisesRegex(run_instance.GuestRuntimeFailure, "Firecracker.*rc=17"):
            self.execute("import time; time.sleep(30)")
        self.assertEqual(self.failure()["firecracker_returncode"], 17)
        self.assert_reaped(self.failure()["ssh_pid"])

    def test_silent_healthy_ssh_can_complete_without_failure(self):
        self.execute("import time; time.sleep(.65)")
        self.assertFalse((self.root / "guest_runtime_failure.json").exists())
        self.vm.terminate.assert_not_called()

    def test_nonzero_ssh_exit_and_timeout_remain_failures(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.execute("raise SystemExit(7)")
        self.assertEqual(caught.exception.returncode, 7)
        self.assert_reaped(self.failure()["ssh_pid"])
        self.spec.config["timeout"] = .05
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute("import time; time.sleep(30)")
        self.assertEqual(self.failure()["error_type"], "TimeoutExpired")
        self.assert_reaped(self.failure()["ssh_pid"])

    def test_collection_and_diagnostic_write_errors_cannot_replace_guest_oom(self):
        self.serial.write_text(OOM)
        with patch.object(run_instance, "collect_jsonl", side_effect=RuntimeError("SCP unavailable")), \
             patch.object(run_instance, "collect_dmesg") as dmesg, \
             patch.object(run_instance, "collect_diagnostics") as diagnostics, \
             patch.object(run_instance, "write_json", side_effect=OSError("evidence disk full")):
            with self.assertRaisesRegex(run_instance.GuestRuntimeFailure, "Killed process 457"):
                with run_instance.collect_results_on_exit(self.machine, self.spec):
                    self.execute("import time; time.sleep(30)")
        dmesg.assert_called_once()
        diagnostics.assert_called_once()
        errors = (self.root / "host_errors.json").read_text()
        for message in ("Killed process 457", "evidence disk full", "SCP unavailable"):
            self.assertIn(message, errors)

    def test_term_resistant_ssh_is_killed_and_reaped_without_touching_vm(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 5), 0]
        run_instance.reap_replay_ssh(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_parent_manifest_keeps_bound_fatal_reason_and_hashes(self):
        self.serial.write_text(OOM)
        with self.assertRaises(run_instance.GuestRuntimeFailure):
            self.execute("import time; time.sleep(30)")
        # This is the parent's old spec: it never saw the child's in-memory data.
        process = Mock(pid=os.getpid())
        process.poll.return_value = 1
        job = host_execution.RunningJob(self.spec, host_execution.Lane(0), process, io.StringIO())
        with patch.object(host_execution, "kill_group"):
            with self.assertRaisesRegex(RuntimeError, "Killed process 457"):
                host_execution.finish_job(job)
        manifest = json.loads(self.spec.config_path.read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["returncode"], 1)
        self.assertEqual(manifest["runtime_failure"]["kind"], "guest_oom")
        self.assertIn("Killed process 457", manifest["error"])
        artifacts = {item["path"]: item for item in manifest["artifacts"]}
        for name in ("guest_runtime_failure.json", "firecracker.log", "guest.log", "host_errors.json"):
            raw = (self.root / name).read_bytes()
            self.assertEqual(artifacts[name]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(artifacts[name]["bytes"], len(raw))

    def test_unreadable_optional_diagnostic_keeps_primary_reason_and_fatal_artifact(self):
        self.serial.write_text(OOM)
        with self.assertRaises(run_instance.GuestRuntimeFailure):
            self.execute("import time; time.sleep(30)")
        (self.root / "diagnostics.tar.gz").write_bytes(b"unreadable diagnostic")
        process = Mock(pid=os.getpid())
        process.poll.return_value = 1
        job = host_execution.RunningJob(self.spec, host_execution.Lane(0), process, io.StringIO())
        read_bytes = Path.read_bytes
        def read(path):
            if path.name == "diagnostics.tar.gz":
                raise PermissionError("diagnostic unreadable")
            return read_bytes(path)
        with patch.object(host_execution, "kill_group"), patch.object(Path, "read_bytes", read):
            with self.assertRaisesRegex(RuntimeError, "Killed process 457"):
                host_execution.finish_job(job)
        manifest = json.loads(self.spec.config_path.read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertIn("guest_runtime_failure.json", [r["path"] for r in manifest["artifacts"]])
        self.assertIn("diagnostic unreadable", (self.root / "host_errors.json").read_text())

    def test_stale_or_malformed_failure_cannot_replace_parent_process_error(self):
        for payload in ("not json", json.dumps({"runner_pid": -1, "reason": "stale OOM"}),
                        json.dumps({"runner_pid": 55, "run_config_sha256": "0" * 64,
                                    "status": "failed", "reason": "wrong-config OOM"})):
            with self.subTest(payload=payload):
                self.spec.config = {"instance": "test-instance", "mode": "fast", "status": "running"}
                self.spec.save()
                (self.root / "guest_runtime_failure.json").write_text(payload)
                process = Mock(pid=55)
                process.poll.return_value = 19
                job = host_execution.RunningJob(self.spec, host_execution.Lane(0), process, io.StringIO())
                with patch.object(host_execution, "kill_group"):
                    with self.assertRaisesRegex(RuntimeError, "rc=19"):
                        host_execution.finish_job(job)
                manifest = json.loads(self.spec.config_path.read_text())
                self.assertNotIn("runtime_failure", manifest)
                self.assertNotIn("stale OOM", manifest["error"])
                self.assertEqual(manifest["status"], "failed")


if __name__ == "__main__":
    unittest.main()
