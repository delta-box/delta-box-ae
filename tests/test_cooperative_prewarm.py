"""Safe CoW warm policy: content, cancellation, fork lifecycle, failure evidence."""
import ctypes
import errno
import importlib.util
import json
import mmap
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cooperative_prewarm", ROOT / "replay/guest/cooperative_prewarm.py")
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)
WARM_ENV = {"DELTABOX_PAPER_MEMORY_POLICY": "warm", "DELTABOX_FORK_ONLY_MEMCURVE": "1",
            "DELTABOX_ASYNC_INCREMENTAL_DUMP": "0"}


class MappingTests(unittest.TestCase):
    def test_only_private_anonymous_rw_and_tier_order(self):
        lines = [
            "1000-3000 rw-p 00000000 00:00 0 [heap]",
            "4000-5000 rw-p 00000000 00:00 0",
            "6000-7000 rw-p 00000000 00:00 0 [anon:arena]",
            "8000-9000 rw-s 00000000 00:00 0",
            "a000-b000 rw-p 00000000 00:00 42 /tmp/data",
            "c000-d000 rw-p 00000000 00:00 0 /dev/zero (deleted)",
            "e000-f000 rw-p 00000000 00:00 0 [anon_shmem:shared]",
            "10000-11000 r--p 00000000 00:00 0",
        ]
        self.assertEqual(pw.anonymous_private_ranges(lines),
                         [(1, [(0x1000, 0x3000)]), (2, [(0x4000, 0x5000), (0x6000, 0x7000)])])

    def test_other_profiles_rejected_before_loading_libc(self):
        for env in ({}, {**WARM_ENV, "DELTABOX_PAPER_MEMORY_POLICY": "none"},
                    {**WARM_ENV, "DELTABOX_ASYNC_INCREMENTAL_DUMP": "1"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(pw.ctypes, "CDLL") as library:
                with self.assertRaisesRegex(pw.PrewarmError, "fork-only"):
                    pw.CooperativePrewarm()
                library.assert_not_called()

    def test_contiguous_vma_splits_do_not_hide_real_oom(self):
        self.assertTrue(pw._covers(4096, 12288, [(4096, 8192), (8192, 12288)]))
        self.assertFalse(pw._covers(4096, 16384, [(4096, 8192), (12288, 16384)]))


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux madvise/pthread/procfs contract")
class LinuxWarmTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, WARM_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = str(Path(self.tmp.name) / "prewarm.jsonl")

    def helper(self):
        helper = pw.CooperativePrewarm(log_path=self.log)
        self.addCleanup(helper._journal.close)
        return helper

    def test_unsupported_advice_is_a_startup_error(self):
        with patch.object(pw.CooperativePrewarm, "_populate", side_effect=OSError(errno.EINVAL, "unsupported")):
            with self.assertRaisesRegex(pw.PrewarmError, "unavailable"):
                self.helper()

    def test_background_permission_failure_propagates_and_thread_is_joined(self):
        helper = self.helper()
        with patch.object(pw, "_ranges", return_value=[(1, [(4096, 8192)])]), \
                patch.object(helper, "_populate", side_effect=OSError(errno.EPERM, "denied")):
            helper.start_epoch(1)
            helper._thread.join()
            with self.assertRaisesRegex(pw.PrewarmError, "denied"):
                helper.quiesce()
        self.assertIsNone(helper._thread)
        self.assertFalse(pw.journal_records(self.log)[0]["ok"])

    def test_cancel_join_limits_work_and_repeated_epoch_does_not_spawn(self):
        helper = self.helper()
        entered, finish = threading.Event(), threading.Event()
        def blocked(address, size):
            entered.set()
            finish.wait(2)
        with patch.object(pw, "_ranges", return_value=[(1, [(4096, 4096 + pw.CHUNK_BYTES * 4)])]), \
                patch.object(helper, "_populate", side_effect=blocked) as populate:
            helper.start_epoch(4)
            self.assertTrue(entered.wait(2))
            helper.start_epoch(4)
            helper._cancel.set()
            finish.set()
            helper.quiesce()
        self.assertEqual(populate.call_count, 1)
        self.assertIsNone(helper._thread)
        self.assertTrue(helper.last_result["cancelled"])
        self.assertFalse(helper.last_result["complete"])
        self.assertEqual(len(pw.journal_records(self.log)), 1)

    def test_enomem_is_only_skipped_when_mapping_changed(self):
        for live, should_fail in (([(1, [(4096, 8192)])], True), ([], False)):
            helper = self.helper()
            with patch.object(pw, "_ranges", side_effect=[[(1, [(4096, 8192)])], live]), \
                    patch.object(helper, "_populate", side_effect=OSError(errno.ENOMEM, "unmapped or OOM")):
                helper.start_epoch(1)
                helper._thread.join()
                if should_fail:
                    with self.assertRaises(pw.PrewarmError):
                        helper.quiesce()
                else:
                    helper.quiesce()
                    self.assertEqual(helper.last_result["skipped_bytes"], 4096)
                    self.assertTrue(helper.last_result["ok"])

    def test_worker_journal_uses_no_file_io_and_is_fixed_size(self):
        helper = self.helper()
        with patch.object(pw, "_ranges", return_value=[]), \
                patch.object(pw.os, "open", side_effect=AssertionError("hot-path open")), \
                patch.object(pw.os, "write", side_effect=AssertionError("hot-path write")):
            helper.start_epoch(1)
            helper._thread.join()
            helper.quiesce()
        self.assertEqual(Path(self.log).stat().st_size, pw.JOURNAL_BYTES)
        self.assertEqual(pw.journal_records(self.log)[0]["state"], "completed")

    def test_journal_failure_is_fatal_instead_of_losing_error_evidence(self):
        helper = self.helper()
        with patch.object(pw, "_ranges", return_value=[]), \
                patch.object(helper, "_record", side_effect=OSError(errno.EIO, "journal failed")):
            helper.start_epoch(1)
            helper._thread.join()
            with self.assertRaisesRegex(pw.PrewarmError, "journal failed"):
                helper.quiesce()
        self.assertIsNone(helper._thread)

    def test_thread_creation_failure_is_not_retried_as_success(self):
        helper = self.helper()
        with patch.object(pw, "_ranges", return_value=[]), \
                patch.object(pw.threading.Thread, "start", side_effect=RuntimeError("cannot start")):
            with self.assertRaisesRegex(pw.PrewarmError, "Cannot start"):
                helper.start_epoch(1)
            with self.assertRaisesRegex(pw.PrewarmError, "cannot start"):
                helper.start_epoch(1)
        self.assertIsNone(helper._thread)

    def test_fork_concurrent_writes_next_fork_and_epoch_have_no_live_threads(self):
        region = mmap.mmap(-1, 32 << 20, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                           prot=mmap.PROT_READ | mmap.PROT_WRITE)
        region[:] = b"Z" * len(region)
        view = ctypes.c_char.from_buffer(region)
        address = ctypes.addressof(view)
        helper = self.helper()
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.setsid()
            os.close(read_fd)
            try:
                with patch.object(pw, "_ranges", return_value=[(1, [(address, address + len(region))])]):
                    helper.start_epoch(1)
                    writes = 0
                    while helper._thread.is_alive():
                        writes += 1
                        ctypes.c_uint64.from_address(address).value = writes
                    helper.quiesce()
                    assert writes > 0
                    assert ctypes.c_uint64.from_address(address).value == writes
                    assert region[8:] == b"Z" * (len(region) - 8)
                    assert len(os.listdir("/proc/self/task")) == 1
                    sys.path.insert(0, str(ROOT))
                    from backends.deltabox.gsd.template_fork import _assert_single_threaded
                    assert _assert_single_threaded() == 1
                    successor = os.fork()
                    if successor == 0:
                        try:
                            assert len(os.listdir("/proc/self/task")) == 1
                            assert ctypes.c_uint64.from_address(address).value == writes
                            helper.start_epoch(2)
                            helper._thread.join()
                            helper.quiesce()
                            assert helper.last_result["complete"]
                            assert len(os.listdir("/proc/self/task")) == 1
                            assert ctypes.c_uint64.from_address(address).value == writes
                            os._exit(0)
                        except BaseException:
                            os._exit(3)
                    assert os.waitpid(successor, 0)[1] == 0
                    os.write(write_fd, json.dumps({"ok": True, "concurrent_writes": writes}).encode())
                os._exit(0)
            except BaseException as exc:
                os.write(write_fd, json.dumps({"ok": False, "error": repr(exc)}).encode())
                os._exit(2)
        os.close(write_fd)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                waited, status = os.waitpid(child, os.WNOHANG)
                if waited:
                    break
                time.sleep(0.01)
            else:
                os.killpg(child, signal.SIGKILL)
                os.waitpid(child, 0)
                self.fail("fork lifecycle timed out")
            result = json.loads(os.read(read_fd, 65536))
            self.assertEqual(status, 0, result)
            self.assertTrue(result["ok"], result)
            self.assertEqual(region[:], b"Z" * len(region), "parent template must be unchanged")
            rows = pw.journal_records(self.log)
            self.assertEqual([row["epoch"] for row in rows], [1, 2])
            self.assertTrue(all(row["ok"] for row in rows))
        finally:
            os.close(read_fd)
            del view
            region.close()


if __name__ == "__main__":
    unittest.main()
