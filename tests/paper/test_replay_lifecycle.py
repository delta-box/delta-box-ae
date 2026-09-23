"""Bounded replay cleanup, including cold restore and constructor failures."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
import errno
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'replay/guest'))
from control_channel import restore_with_ready
from lifecycle import ReplayResources
import lifecycle
from backends.deltabox.gsd import sandbox_controller as sc

_spec = importlib.util.spec_from_file_location('lifecycle_replay_main', ROOT / 'replay/guest/trace_replay_main.py')
replay = importlib.util.module_from_spec(_spec)
with patch.dict(sys.modules, {'sandbox_controller': sc}):
    _spec.loader.exec_module(replay)


class ReplayLifecycleTests(unittest.TestCase):
    def test_new_namespace_owned_before_ready_failure(self):
        controller = SimpleNamespace(ns_init_pid=101)
        events = []
        def restore(target):
            controller.ns_init_pid = 202
            return {'path': 'criu'}
        controller.restore_action = restore
        resources = Mock()
        resources.own_namespace.side_effect = lambda pid: events.append(('owned', pid))
        channel = Mock()
        def ready(*a, **k):
            events.append(('ready',))
            return {'ok': False}
        channel.control_fresh.side_effect = ready
        with self.assertRaisesRegex(RuntimeError, 'activation failed'):
            restore_with_ready(controller, 'A', channel, resources)
        self.assertEqual(events, [('owned', 202), ('ready',)])

    def test_core_partial_restore_failure_still_owns_published_namespace(self):
        controller = SimpleNamespace(ns_init_pid=101)
        failure = RuntimeError('post-CRIU bookkeeping')
        def restore(target):
            controller.ns_init_pid = 202
            raise failure
        controller.restore_action = restore
        resources = Mock()
        with self.assertRaises(RuntimeError) as caught:
            restore_with_ready(controller, 'A', None, resources)
        self.assertIs(caught.exception, failure)
        resources.own_namespace.assert_called_once_with(202)
        resources.own_namespace.side_effect = ProcessLookupError('restored worker died')
        controller.ns_init_pid = 101
        with self.assertRaises(RuntimeError) as caught:
            restore_with_ready(controller, 'A', None, resources)
        self.assertIs(caught.exception, failure)

    def test_warm_namespace_identity_avoids_ownership_syscall(self):
        controller = SimpleNamespace(ns_init_pid=101, restore_action=lambda target: {'path': 'warm-template'})
        resources, channel = Mock(), Mock()
        restore_with_ready(controller, 'A', channel, resources)
        resources.own_namespace.assert_not_called()
        channel.control_fresh.assert_not_called()

    def test_validation_restore_registers_each_new_namespace_without_ready(self):
        controller = SimpleNamespace(ns_init_pid=101, agent_pid=101,
            registry={'A': {'validation_full_id': 'A-full'}, 'A-full': {}})
        def restore(target):
            controller.ns_init_pid += 1
            controller.agent_pid = controller.ns_init_pid
            return {'path': 'criu'}
        controller.restore_action = restore
        resources = Mock()
        with patch.object(replay, 'process_memory_digest', return_value={'ok': True,
                'mem_sha256': 'equal', 'mem_bytes': 4096}), \
             patch.object(replay, 'replay_file_digest', return_value={'replay_file_sha256': 'same'}), \
             patch.object(replay, 'memory_diff_summary', return_value={}):
            rows = replay.validate_incremental_equivalence(controller, ['A'], resources)
        self.assertEqual([c.args for c in resources.own_namespace.call_args_list], [(102,), (103,)])
        self.assertTrue(rows[0]['ok'])

    def test_constructor_failure_closes_partial_controller(self):
        overlays = Mock()
        class BrokenController:
            def __init__(self):
                self.root_overlays = overlays
                self._dump_pool = ThreadPoolExecutor(max_workers=1)
                self._restamp_pool = ThreadPoolExecutor(max_workers=1)
                raise RuntimeError('initialization failed')
        with self.assertRaisesRegex(RuntimeError, 'initialization failed'):
            with ReplayResources() as resources:
                resources.create_controller(BrokenController)
        overlays.teardown.assert_called_once_with()

    def test_compatibility_failure_happens_before_guest_setup(self):
        # Patch the guard itself, not its hash: importing a cached adapter must
        # still revalidate before filesystem setup or any worker is spawned.
        policy_spec = importlib.util.spec_from_file_location('paper_memory_policy', ROOT / 'replay/guest/paper_memory_policy.py')
        policy = importlib.util.module_from_spec(policy_spec)
        with patch.dict(sys.modules, {'sandbox_controller': sc}):
            policy_spec.loader.exec_module(policy)
        args = SimpleNamespace(require_real_agent=True, active_worker_load=False,
                               worker_exec=True, agent_mode='real')
        with patch.dict(sys.modules, {'paper_memory_policy': policy}), \
             patch.dict(os.environ, {'DELTABOX_PAPER_MEMORY_POLICY': 'none'}), \
             patch.object(policy, 'verify_runtime_compatibility', side_effect=RuntimeError('guard drift')), \
             patch.object(replay, '_start_index_sidecar') as sidecar, \
             patch.object(replay, 'spawn_checkpoint_agent') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'guard drift'):
                replay.run_replay(args)
        sidecar.assert_not_called()
        spawn.assert_not_called()

    def test_exception_reaps_running_dump_and_does_not_start_queued_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'must-not-start'
            controller = SimpleNamespace(_dump_pool=ThreadPoolExecutor(max_workers=1),
                _restamp_pool=ThreadPoolExecutor(max_workers=1), root_overlays=None)
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'result write failed'):
                with ReplayResources() as resources:
                    resources.own_controller(controller)
                    future = controller._dump_pool.submit(subprocess.check_call,
                        [sys.executable, '-c', 'import time; time.sleep(60)'])
                    deadline = time.monotonic() + 2
                    while not controller._dump_pool.processes:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.005)
                    child = next(iter(controller._dump_pool.processes))
                    queued = controller._dump_pool.submit(subprocess.check_call,
                        [sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(marker)])
                    raise RuntimeError('result write failed')
            self.assertIsNotNone(child.poll())
            self.assertTrue(future.done())
            self.assertTrue(queued.done())
            self.assertIsNotNone(queued.exception())
            self.assertFalse(marker.exists())
            self.assertLess(time.monotonic() - started, 4)

    def test_caught_event_error_aborts_before_final_settlement(self):
        # Run the actual schedule/catch/break/finalization path with a live
        # sleeping dump command. Fail fast if code regresses to normal join.
        captured = {}
        class NoBlockingFinish(ThreadPoolExecutor):
            def shutdown(self, wait=True, **kwargs):
                if wait:
                    raise AssertionError("failed replay entered normal blocking join")
                return super().shutdown(wait=wait, **kwargs)
        class Controller:
            def __init__(self, **kwargs):
                self.agent_pid = os.getpid()
                self.ns_init_pid = os.getpid()
                self._dump_pool = NoBlockingFinish(max_workers=1)
                self._restamp_pool = ThreadPoolExecutor(max_workers=1)
                self.root_overlays = None
                captured['controller'] = self
            def checkpoint_action(self, *args, **kwargs):
                captured['calls'] = captured.get('calls', 0) + 1
                future = self._dump_pool.submit(subprocess.check_call,
                    [sys.executable, '-c', 'import time;time.sleep(60)'])
                deadline = time.monotonic() + 2
                while not self._dump_pool.processes:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('test dump did not start')
                    time.sleep(.005)
                captured['child'] = next(iter(self._dump_pool.processes))
                captured['future'] = future
                return {'id': 'A', 'strategy': 'standard', 'dump_future': future}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'results.jsonl'
            args = SimpleNamespace(require_real_agent=False, active_worker_load=False,
                worker_exec=False, agent_mode='dummy', schedule='fixture',
                adaptive=False, warm_template=False, prewarm=False, sync_dump=False,
                agent_probe=False, results=str(output))
            schedule = [{'type': 'ckpt', 'ckpt_id': 'one'},
                        {'type': 'restore', 'target_ckpt_id': 'missing'},
                        {'type': 'ckpt', 'ckpt_id': 'never'}]
            started = time.monotonic()
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(replay, 'SandboxController', Controller), \
                 patch.object(replay, 'SNAPSHOT_STORE', str(Path(tmp) / 'snapshots')), \
                 patch.object(replay.os, 'chdir'), \
                 patch.object(replay.os.path, 'ismount', return_value=True), \
                 patch.object(replay.os.path, 'isdir', return_value=True), \
                 patch.object(replay, 'load_schedule', return_value=schedule), \
                 patch.object(replay, 'init_testbed_overlay', return_value={'layers': tmp, 'upper': tmp, 'work': tmp}), \
                 patch.object(replay, '_start_index_sidecar', return_value=(None, None)), \
                 patch.object(replay, 'spawn_checkpoint_agent', return_value=(os.getpid(), os.getpid(), [])), \
                 patch.object(replay, 'write_dirty'), \
                 patch.object(replay, 'current_overlay_footprint', return_value={}), \
                 patch.object(replay, 'process_footprint', return_value={}):
                with self.assertRaises(SystemExit) as caught:
                    replay.run_replay(args)
            self.assertEqual(caught.exception.code, 1)
            self.assertLess(time.monotonic() - started, 4)
            self.assertIsNotNone(captured['child'].poll())
            self.assertTrue(captured['future'].done())
            self.assertEqual(captured['calls'], 1)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertTrue(any(row.get('err') == 'KeyError' for row in rows))
            self.assertTrue(any(row['kind'] == 'dump_completion' and row['ok'] is False for row in rows))

    def test_partial_real_controller_constructor_closes_directory_fd(self):
        captured = {}
        class BrokenController(sc.SandboxController):
            def _persist_epoch(self):
                captured['controller'] = self
                captured['fd'] = self.mount_fd
                # Verify the descriptor is real and open at the failure point.
                os.fstat(self.mount_fd)
                raise RuntimeError('epoch persist failure')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'epoch persist failure'):
                with ReplayResources() as resources:
                    resources.create_controller(BrokenController, agent_pid=123,
                        snapshot_store=str(Path(tmp) / 'snapshots'), layers_root=tmp,
                        initial_upper=tmp, initial_work=tmp, overlay_mount_point=tmp)
            self.assertIsNone(captured['controller'].mount_fd)
            with self.assertRaises(OSError):
                os.fstat(captured['fd'])
            # Repeated cleanup must not close an unrelated descriptor reusing
            # the old number.
            another = os.open(tmp, os.O_RDONLY)
            try:
                resources._close_controller(captured['controller'])
                os.fstat(another)
            finally:
                os.close(another)

    def test_namespace_and_controller_cleanup_continue_after_first_failure(self):
        resources = ReplayResources()
        resources._namespaces = {101: 11, 102: 12}
        with patch.object(resources, '_kill_namespace', side_effect=[TimeoutError('first'), None]) as kill:
            resources._kill_namespaces()
        self.assertEqual([call.args for call in kill.call_args_list], [(101, 11), (102, 12)])
        resources._namespaces.clear()
        pool = Mock()
        pool.abort.side_effect = RuntimeError('dump abort')
        restamp = Mock()
        restamp._threads = ()
        overlays = Mock()
        controller = SimpleNamespace(_dump_pool=pool, _restamp_pool=restamp, root_overlays=overlays)
        resources._close_controller(controller)
        restamp.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        overlays.teardown.assert_called_once_with()

    def test_normal_shutdown_settles_all_admitted_dumps(self):
        controller = SimpleNamespace(_dump_pool=ThreadPoolExecutor(max_workers=1),
            _restamp_pool=ThreadPoolExecutor(max_workers=1), root_overlays=None)
        with ReplayResources() as resources:
            resources.own_controller(controller)
            futures = [controller._dump_pool.submit(subprocess.check_call,
                [sys.executable, '-c', 'pass']) for _ in range(2)]
            controller._dump_pool.shutdown(wait=True)
            self.assertEqual([f.result() for f in futures], [0, 0])

    def test_cleanup_error_preserves_primary_and_remaining_callbacks_run(self):
        remaining = Mock()
        with self.assertRaisesRegex(ValueError, 'primary'):
            with ReplayResources() as resources:
                resources.callback(remaining)
                resources.callback(lambda: (_ for _ in ()).throw(RuntimeError('cleanup')))
                raise ValueError('primary')
        remaining.assert_called_once_with()
        self.assertTrue(resources.cleanup_errors)

    def test_missing_python_pidfd_apis_use_identity_preserving_adapters(self):
        opener, sender = Mock(return_value=42), Mock()
        with patch.object(os, 'pidfd_open', None, create=True), \
             patch.object(signal, 'pidfd_send_signal', None, create=True), \
             patch.object(lifecycle, '_pidfd_opener', return_value=opener), \
             patch.object(lifecycle, '_libc_pidfd_signaller', return_value=sender), \
             patch.object(os, 'kill', side_effect=AssertionError('numeric PID kill forbidden')):
            fd = lifecycle.open_pidfd(123)
            lifecycle.send_pidfd_signal(fd, signal.SIGKILL)
        opener.assert_called_once_with(123)
        sender.assert_called_once_with(42, signal.SIGKILL)

    def test_native_pidfd_denial_is_not_bypassed(self):
        denied = PermissionError(errno.EPERM, 'blocked')
        with patch.object(signal, 'pidfd_send_signal', side_effect=denied, create=True), \
             patch.object(lifecycle, '_libc_pidfd_signaller') as fallback:
            with self.assertRaises(PermissionError):
                lifecycle.send_pidfd_signal(42, signal.SIGKILL)
        fallback.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux syscalls required')
    def test_real_pidfd_cleanup_without_python_wrappers(self):
        with patch.object(os, 'pidfd_open', None, create=True), \
             patch.object(signal, 'pidfd_send_signal', None, create=True):
            with self.assertRaisesRegex(RuntimeError, 'forced fallback'):
                with ReplayResources() as resources:
                    child = resources.popen([sys.executable, '-c', 'import time;time.sleep(60)'])
                    resources.own_namespace(child.pid)
                    raise RuntimeError('forced fallback')
            self.assertIsNotNone(child.poll())
            self.assertFalse(resources._namespaces)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux pidfds required')
    def test_namespace_launcher_child_owned_before_pidfile_registration(self):
        import select
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'child'
            script = ("import os,pathlib,sys,time; p=os.fork(); "
                      "os.setsid() if p == 0 else None; "
                      "time.sleep(60) if p == 0 else None; "
                      # Publish a complete marker: existence alone must not
                      # race Path.write_text opening its still-empty file.
                      "marker=pathlib.Path(sys.argv[1]); "
                      "pending=marker.with_suffix('.pending'); "
                      "pending.write_text(str(p)); pending.replace(marker); "
                      "os.waitpid(p,0); time.sleep(60)")
            fd = None
            try:
                with self.assertRaisesRegex(RuntimeError, 'before pidfile registration'):
                    with ReplayResources() as resources:
                        sentinel = resources.popen_namespace([sys.executable, '-c', script, str(marker)])
                        deadline = time.monotonic() + 2
                        while not marker.exists():
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.005)
                        fd = lifecycle.open_pidfd(int(marker.read_text()))
                        raise RuntimeError('before pidfile registration')
                self.assertIsNotNone(sentinel.poll())
                self.assertTrue(select.select([fd], [], [], 1)[0])
            finally:
                if fd is not None:
                    os.close(fd)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux pidfds required')
    def test_pidfd_owned_process_is_reaped_on_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'initialization'):
            with ReplayResources() as resources:
                child = resources.popen([sys.executable, '-c', 'import time;time.sleep(60)'])
                resources.own_namespace(child.pid)
                raise RuntimeError('initialization')
        self.assertIsNotNone(child.poll())
        self.assertFalse(resources._namespaces)


if __name__ == '__main__':
    unittest.main()
