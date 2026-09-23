"""Fork/stash must reject unverified or multithreaded agent state."""
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backends.deltabox.gsd import template_fork as tf


class ForkSafetyTests(unittest.TestCase):
    # Each route reaches a different fork/clone implementation after the
    # common guard. Never let a guard regression create an actual child.
    routes = (
        {"op": "fork"},
        {"op": "fork", "fixed_active_pid": 100},
        {"op": "fork", "fresh_pidns": True},
        {"op": "fork", "fresh_pidns": True, "clone_parent_active": True},
        {"op": "stash_template"},
        {"op": "stash_template", "fixed_active_pid": 100},
        {"op": "stash_template", "fresh_pidns": True},
    )

    def assert_rejected_without_fork(self, message, error):
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                tf, "_read_ctrl_line", return_value=json.dumps(message)))
            response = stack.enter_context(patch.object(tf, "_write_response"))
            process_calls = [stack.enter_context(patch.object(
                owner, name, side_effect=AssertionError(f"unsafe {name}")))
                for owner, name in (
                    (tf.os, "fork"), (tf.os, "kill"),
                    (tf, "_clone3_newpid_pidfd"),
                    (tf, "_clone3_same_pidns_settid"),
                    (tf, "_clone_newpid_parent"), (tf, "_unshare_newpid"),
                )]
            self.assertIsNone(tf.handle_template_message(7, "/unused/out"))
            response.assert_called_once()
            payload = response.call_args.args[1]
            self.assertFalse(payload["ok"])
            self.assertIn(error, payload["error"])
            for process_call in process_calls:
                process_call.assert_not_called()

    def test_proc_errors_reject_every_fork_and_stash_route(self):
        for failure in (PermissionError(errno.EACCES, "denied"),
                        FileNotFoundError(errno.ENOENT, "proc not mounted"),
                        OSError(errno.EIO, "proc read failed")):
            for route in self.routes:
                with self.subTest(failure=failure.errno, route=route), \
                        patch.object(tf.os, "listdir", side_effect=failure), \
                        patch.object(tf, "_log"):
                    self.assert_rejected_without_fork(route, "unavailable")

    def test_real_os_directory_failure_is_not_assumed_single_threaded(self):
        real_listdir = os.listdir
        with tempfile.TemporaryDirectory() as tmp:
            not_a_directory = Path(tmp) / "not-proc"
            not_a_directory.write_text("regular file")
            with patch.object(tf.os, "listdir", side_effect=(
                    lambda path: real_listdir(not_a_directory))), \
                    patch.object(tf, "_log"):
                self.assert_rejected_without_fork(
                    {"op": "fork"}, "unavailable")

    def test_multiple_or_empty_task_lists_reject_every_route(self):
        for tasks in ([], ["100", "101"]):
            for route in self.routes:
                with self.subTest(tasks=tasks, route=route), \
                        patch.object(tf.os, "listdir", return_value=tasks), \
                        patch.object(tf, "_log"):
                    self.assert_rejected_without_fork(route, "multithread")

    @unittest.skipUnless(os.path.isdir("/proc/self/task"), "Linux procfs required")
    def test_real_second_thread_rejects_fork_and_stash(self):
        started, stop = threading.Event(), threading.Event()

        def worker():
            started.set()
            stop.wait()

        thread = threading.Thread(target=worker, name="fork-safety-test")
        thread.start()
        try:
            self.assertTrue(started.wait(1.0))
            with patch.object(tf, "_log"):
                for route in self.routes:
                    with self.subTest(route=route):
                        self.assert_rejected_without_fork(route, "multithread")
        finally:
            stop.set()
            thread.join(1.0)
        self.assertFalse(thread.is_alive())

    def test_verified_single_thread_keeps_existing_child_path(self):
        with patch.object(tf, "_read_ctrl_line", return_value='{"op":"fork"}'), \
                patch.object(tf.os, "listdir", return_value=["100"]) as tasks, \
                patch.dict(tf.os.environ, {"DELTABOX_FRESH_PIDNS_ACTIVE": "0"}), \
                patch.object(tf.os, "fork", return_value=0) as fork, \
                patch.object(tf, "_write_response") as response:
            self.assertEqual(tf.handle_template_message(7, "/unused/out"), "child")
            tasks.assert_called_once_with("/proc/self/task")
            fork.assert_called_once_with()
            response.assert_not_called()


if __name__ == "__main__":
    unittest.main()
