"""Event-driven lifetime tests; CRIU and privileged syscalls are substituted.

These tests exercise the real executor/admission/filesystem publication logic.
The separate Linux probe provides the actual CRIU image/restore evidence.
"""
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backends.deltabox.gsd import async_checkpoint as ac
from backends.deltabox.gsd import async_resources as ar


class AsyncCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ('images', 'layers', 'layers/upper', 'layers/work', 'base'):
            (root / name).mkdir()
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown, wait=True)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.started = threading.Event()
        self.calls = []
        self.disposed = []
        self.next_pid = 100

        def stash(_active, _checkpoint, **_kw):
            self.next_pid += 1
            return self.next_pid

        self.controller = SimpleNamespace(
            registry={}, root_overlays=None, agent_pid=10, ns_init_pid=1,
            overlay_mount_point='/testbed', snapshot_store=str(root / 'images'),
            layers_root=str(root / 'layers'), base_layer=str(root / 'base'),
            current_upper=str(root / 'layers/upper'), current_work=str(root / 'layers/work'),
            criu_dump_bin='/test/criu', _dump_pool=self.executor,
            template_pool=SimpleNamespace(request_stash_template=Mock(side_effect=stash), discard=Mock()),
            _bootstrap_active_before_dump=Mock(return_value=(None, 0., False)),
            _wait_pid_stopped=Mock(return_value=True), _is_pidns_init=Mock(side_effect=lambda pid: pid != 10),
            _apply_overlay_switch=Mock(), _all_ext_mount_map_args=lambda: [],
            _image_dir_size=lambda path: sum(p.stat().st_size for p in Path(path).iterdir()))
        self.pipeline = ac.AsyncIncrementalCheckpoint.__new__(ac.AsyncIncrementalCheckpoint)
        self.pipeline.controller = self.controller
        self.pipeline.slots = threading.BoundedSemaphore(4)
        self.pipeline.max_pending = 4
        self.pipeline.failed = None
        self.pipeline.timeout = 2
        self.pipeline.binary_identity = {'sha256': 'fixture'}

        def dispose(pid, fd):
            self.disposed.append(pid)
            if fd is not None:
                os.close(fd)

        self.pipeline._dispose = Mock(side_effect=dispose)
        contract = dict(version=1, overlay_mount_point='/testbed', cwd='/', fds=[],
                        protocol_fifos_external=True, mutable_task_file_references=False,
                        immutable_image_required=True, diagnostic_stdio_external=True)
        for mocker in (patch.object(ac, 'collect_page_stats', return_value={'present_pages': 1}),
                       patch.object(ar, 'validate_replay_resources', return_value=contract),
                       patch.object(ac, 'open_pidfd', side_effect=lambda _: os.open('/dev/null', os.O_RDONLY)),
                       patch.object(ac.subprocess, 'check_call', side_effect=self.dump)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def dump(self, command, **kw):
        self.calls.append((command, kw))
        staging = Path(command[command.index('-D') + 1])
        (staging / 'inventory.img').write_bytes(b'complete fixture')
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError('test did not release dump')

    def checkpoint(self, parent=None):
        return self.pipeline.checkpoint(parent, 'test')

    def test_overlay_switch_timer_excludes_directory_preparation(self):
        Path(self.controller.current_upper, 'changed').write_text('data')
        with patch.object(ac.time, 'perf_counter', side_effect=[1., 2., 2.001, 3.]):
            layers, dirty, switch_ms, preparation_ms = self.pipeline._sink('timing', None)
        self.assertTrue(dirty)
        self.assertAlmostEqual(switch_ms, 1.)
        self.assertAlmostEqual(preparation_ms, 1999.)
        self.controller._apply_overlay_switch.assert_called_once()

    def test_clean_overlay_does_not_report_preparation_as_an_ioctl(self):
        with patch.object(ac.time, 'perf_counter', side_effect=[1., 1.001]):
            _, dirty, switch_ms, preparation_ms = self.pipeline._sink('clean', None)
        self.assertFalse(dirty)
        self.assertEqual(switch_ms, 0.)
        self.assertAlmostEqual(preparation_ms, 1.)
        self.controller._apply_overlay_switch.assert_not_called()

    def test_returns_while_dump_is_blocked_and_publishes_only_complete_directory(self):
        entry = self.checkpoint()
        self.assertTrue(self.started.wait(1))
        final = Path(entry['mem_path'])
        self.assertFalse(entry['dump_future'].done())
        self.assertFalse(final.exists())
        self.assertTrue(Path(str(final) + '.pending').exists())
        self.assertEqual(self.disposed, [])
        self.release.set()
        entry['dump_future'].result(2)
        self.assertEqual(entry['state'], 'DURABLE_READY')
        self.assertTrue(entry['dispose_done'])
        self.assertEqual((final / 'inventory.img').read_bytes(), b'complete fixture')
        self.assertFalse(Path(str(final) + '.pending').exists())
        self.assertEqual(self.disposed, [entry['dump_tree_pid']])

    def test_child_returns_before_parent_and_never_dumps_against_uncommitted_parent(self):
        parent = self.checkpoint()
        self.assertTrue(self.started.wait(1))
        child = self.checkpoint(parent['id'])
        self.assertFalse(parent['dump_future'].done())
        self.assertFalse(child['dump_future'].done())
        self.assertEqual(len(self.calls), 1)
        self.release.set()
        child['dump_future'].result(2)
        self.assertEqual(len(self.calls), 2)
        command, kw = self.calls[1]
        self.assertEqual(kw['env']['DELTABOX_CRIU_EXACT_PARENT'], '1')
        prior = command[command.index('--prev-images-dir') + 1]
        staging = Path(command[command.index('-D') + 1])
        self.assertEqual((staging / prior).resolve(), Path(parent['mem_path']).resolve())
        self.assertEqual(parent['state'], 'DURABLE_READY')

    def test_failed_parent_prevents_child_dump_and_releases_both_owned_tasks(self):
        failure = RuntimeError('parent CRIU failure')

        def fail(command, **kw):
            self.started.set()
            self.release.wait(2)
            raise failure

        with patch.object(ac.subprocess, 'check_call', side_effect=fail) as command:
            parent = self.checkpoint()
            self.assertTrue(self.started.wait(1))
            child = self.checkpoint(parent['id'])
            self.release.set()
            for entry in (parent, child):
                with self.assertRaisesRegex(RuntimeError, 'parent CRIU failure'):
                    entry['dump_future'].result(2)
                self.assertEqual(entry['state'], 'DURABLE_FAILED')
                self.assertFalse(Path(entry['mem_path']).exists())
            self.assertEqual(command.call_count, 1)
            self.assertEqual(set(self.disposed), {parent['dump_tree_pid'], child['dump_tree_pid']})

    def test_admission_timeout_does_not_create_another_task_or_poison_pipeline(self):
        self.pipeline.slots = threading.BoundedSemaphore(1)
        self.pipeline.max_pending = 1
        first = self.checkpoint()
        self.assertTrue(self.started.wait(1))
        self.pipeline.timeout = .02
        forks_before = self.controller.template_pool.request_stash_template.call_count
        with self.assertRaisesRegex(TimeoutError, 'admission'):
            self.checkpoint(first['id'])
        self.assertEqual(self.controller.template_pool.request_stash_template.call_count, forks_before)
        self.assertIsNone(self.pipeline.failed)
        self.release.set()
        first['dump_future'].result(2)
        later = self.checkpoint(first['id'])
        later['dump_future'].result(2)

    def test_public_future_is_settled_if_executor_rejects_submission(self):
        captured = []

        def reject(*_args, **_kw):
            captured.extend(self.controller.registry.values())
            raise RuntimeError('executor unavailable')

        self.controller._dump_pool = SimpleNamespace(submit=Mock(side_effect=reject))
        with self.assertRaisesRegex(RuntimeError, 'executor unavailable'):
            self.checkpoint()
        self.assertEqual(self.controller.registry, {})
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]['dump_future'].done(), 'rejected public future must not hang readers')
        with self.assertRaisesRegex(RuntimeError, 'executor unavailable'):
            captured[0]['dump_future'].result(0)
        self.assertTrue(self.pipeline.slots.acquire(blocking=False))
        self.pipeline.slots.release()
        self.assertEqual(len(self.disposed), 1)

    def test_unreadable_or_missing_upper_fails_before_fork(self):
        Path(self.controller.current_upper).rmdir()
        with self.assertRaises((OSError, RuntimeError)):
            self.checkpoint()
        self.controller.template_pool.request_stash_template.assert_not_called()

    def test_abort_disposal_failure_keeps_original_error_and_closes_handle_once(self):
        captured, handles = [], []

        def reject(*_args, **_kw):
            captured.extend(self.controller.registry.values())
            raise RuntimeError('submission failed')

        def bad_disposal(_pid, fd):
            handles.append(fd)
            os.close(fd)
            raise TimeoutError('disposal barrier failed')

        self.controller._dump_pool = SimpleNamespace(submit=Mock(side_effect=reject))
        self.pipeline._dispose = Mock(side_effect=bad_disposal)
        with self.assertRaisesRegex(RuntimeError, 'submission failed'):
            self.checkpoint()
        self.assertTrue(captured[0]['dump_future'].done())
        with self.assertRaisesRegex(RuntimeError, 'submission failed'):
            captured[0]['dump_future'].result(0)
        self.assertEqual(len(handles), 1)
        with self.assertRaises(OSError):
            os.fstat(handles[0])

    def test_background_disposal_failure_is_not_reported_as_durable_success(self):
        def bad_disposal(_pid, fd):
            os.close(fd)
            raise TimeoutError('disposal barrier failed')

        self.pipeline._dispose = Mock(side_effect=bad_disposal)
        entry = self.checkpoint()
        self.release.set()
        with self.assertRaisesRegex(TimeoutError, 'disposal barrier failed'):
            entry['dump_future'].result(2)
        self.assertEqual(entry['state'], 'DURABLE_FAILED')
        self.assertIn('disposal', entry['dump_error'])
        self.assertFalse(entry['dispose_done'])
        self.assertIn('disposal barrier failed', entry['cleanup_error'])
        with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
            self.pipeline.drain_before_namespace_teardown()

    def test_failed_dump_with_confirmed_disposal_does_not_block_unrelated_cold_restore(self):
        with patch.object(ac.subprocess, 'check_call', side_effect=RuntimeError('failed image')):
            entry = self.checkpoint()
            with self.assertRaisesRegex(RuntimeError, 'failed image'):
                entry['dump_future'].result(2)
        self.assertTrue(entry['dispose_done'])
        self.pipeline.drain_before_namespace_teardown()

    def test_pending_writer_timeout_blocks_namespace_teardown(self):
        entry = self.checkpoint()
        self.assertTrue(self.started.wait(1))
        self.pipeline.timeout = .02
        with self.assertRaises(FutureTimeout):
            self.pipeline.drain_before_namespace_teardown()
        self.assertFalse(entry['dispose_done'])
        self.release.set()
        entry['dump_future'].result(2)
        self.pipeline.drain_before_namespace_teardown()

    def test_dump_and_disposal_failures_preserve_both_errors(self):
        def bad_disposal(_pid, fd):
            os.close(fd)
            raise TimeoutError('disposal barrier failed')

        self.pipeline._dispose = Mock(side_effect=bad_disposal)
        with patch.object(ac.subprocess, 'check_call', side_effect=RuntimeError('original image failure')):
            entry = self.checkpoint()
            with self.assertRaisesRegex(RuntimeError, 'original image failure'):
                entry['dump_future'].result(2)
        self.assertIn('original image failure', entry['dump_error'])
        self.assertIn('disposal barrier failed', entry['cleanup_error'])
        with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
            self.pipeline.drain_before_namespace_teardown()

    def test_owned_pool_abort_reaps_real_command_and_settles_parent_and_child_futures(self):
        from replay.guest.lifecycle import OwnedDumpPool

        binary = Path(self.temp.name) / 'criu-fixture'
        binary.write_text(f'#!{sys.executable}\n'
                          'from pathlib import Path\nimport sys, time\n'
                          'Path(sys.argv[sys.argv.index("-D") + 1], "started").touch()\n'
                          'time.sleep(60)\n')
        binary.chmod(0o700)
        self.controller.criu_dump_bin = str(binary)
        pool = OwnedDumpPool(self.executor)
        self.controller._dump_pool = pool
        self.addCleanup(pool.abort)
        parent = self.checkpoint()
        ready = Path(parent['mem_path'] + '.pending') / 'started'
        until = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < until:
            time.sleep(.005)
        self.assertTrue(ready.exists(), 'owned real subprocess did not start')
        child = self.checkpoint(parent['id'])
        with pool.lock:
            processes = tuple(pool.processes)
        self.assertEqual(len(processes), 1)
        pool.abort()
        self.assertTrue(all(proc.poll() is not None for proc in processes))
        for entry in (parent, child):
            self.assertTrue(entry['dump_future'].done())
            with self.assertRaises(subprocess.CalledProcessError):
                entry['dump_future'].result(0)
            self.assertTrue(entry['dispose_done'])
            self.assertEqual(entry['state'], 'DURABLE_FAILED')
        self.pipeline.drain_before_namespace_teardown()


class AsyncLineageRetentionTests(unittest.TestCase):
    def test_physical_logical_effective_and_pending_closures_are_retained(self):
        pending, done = Future(), Future()
        done.set_result(None)
        registry = {
            'tip': {'parent_id': 'logical', 'prev_ckpt_id': 'physical', 'effective_restore_id': 'effective'},
            'logical': {'parent_id': 'root'}, 'physical': {'prev_ckpt_id': 'seed'},
            'effective': {}, 'root': {}, 'seed': {},
            'pending': {'dump_future': pending, 'prev_ckpt_id': 'pending-parent'},
            'pending-parent': {'parent_id': 'root'},
            'obsolete': {'dump_future': done},
        }
        self.assertEqual(ac.retained_ids(registry, {'tip'}), set(registry) - {'obsolete'})
        pending.set_result(None)
        self.assertEqual(ac.retained_ids(registry, {'tip'}),
                         {'tip', 'logical', 'physical', 'effective', 'root', 'seed'})

    def test_cycle_and_dangling_references_do_not_loop_or_invent_entries(self):
        registry = {'a': {'parent_id': 'b'}, 'b': {'prev_ckpt_id': 'a', 'effective_restore_id': 'gone'}}
        self.assertEqual(ac.retained_ids(registry, {'a'}), {'a', 'b'})


if __name__ == '__main__':
    unittest.main()
