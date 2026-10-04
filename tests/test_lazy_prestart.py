"""Prestarted lazy-pages daemons stay off the restore path and never pin idle images."""
from concurrent.futures import Future, ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backends.deltabox.gsd import sandbox_controller as sc


def live_daemon():
    proc = Mock()
    proc.poll.return_value = None
    return proc


class LazyPrestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        c = self.c = sc.SandboxController.__new__(sc.SandboxController)
        c._lazy_page_daemons = []
        c.criu_restore_bin = '/fixture/criu'
        c.lazy_prestart = True
        c.lazy_prestart_max = 2
        c._lazy_prestart_lock = threading.Lock()
        c._lazy_prestarted = {}
        c._lazy_prestart_futures = {}
        c._lazy_prestart_retired = set()
        c._lazy_prestart_closing = False
        c._lazy_prestart_pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(c._lazy_prestart_pool.shutdown, wait=True)
        self.image = self.directory('image')

    def directory(self, name):
        path = Path(self.temp.name) / name
        path.mkdir()
        return str(path)

    def drain(self):
        self.c._lazy_prestart_pool.submit(lambda: None).result(timeout=5)

    def prestart(self, path, proc):
        with patch.object(self.c, '_spawn_lazy_pages_daemon', return_value=proc):
            self.assertIs(self.c._schedule_lazy_prestart(path).result(timeout=5), proc)

    def test_durable_image_arms_an_idle_daemon_that_does_not_pin_it(self):
        proc, future = live_daemon(), Future()
        entry = {'mem_path': self.image, 'dump_future': future, 'state': 'DUMPING', 'dump_stats': {}}
        with patch.object(self.c, '_spawn_lazy_pages_daemon', return_value=proc) as spawn:
            self.c._watch_lazy_prestart(entry)
            self.drain()
            spawn.assert_not_called()
            entry['state'] = 'DURABLE_READY'
            future.set_result(None)
            self.drain()
        spawn.assert_called_once_with(self.image)
        self.assertIs(self.c._lazy_prestarted[self.image], proc)
        self.assertIsInstance(proc.deltabox_prestart_ms, float)
        self.c.registry = {'x': {'mem_path': self.image}}
        self.assertEqual(self.c._lazy_reader_ids(), set())

    def test_failed_dump_never_arms_a_daemon(self):
        for state, error in (('DURABLE_FAILED', RuntimeError('dump failed')), ('DURABLE_FAILED', None)):
            with self.subTest(error=error):
                future = Future()
                entry = {'mem_path': self.image, 'dump_future': future, 'state': 'DUMPING',
                         'dump_stats': {}}
                with patch.object(self.c, '_spawn_lazy_pages_daemon') as spawn:
                    self.c._watch_lazy_prestart(entry)
                    entry['state'] = state
                    future.set_exception(error) if error else future.set_result(None)
                    self.drain()
                spawn.assert_not_called()
                self.assertEqual(self.c._lazy_prestarted, {})

    def test_restore_takes_the_ready_daemon_without_starting_one(self):
        proc = live_daemon()
        self.prestart(self.image, proc)
        with patch.object(self.c, '_spawn_lazy_pages_daemon') as spawn:
            taken, source, background_ms = self.c._take_lazy_pages_daemon(self.image)
        spawn.assert_not_called()
        self.assertEqual((taken, source), (proc, 'prestarted'))
        self.assertIsInstance(background_ms, float)
        self.assertEqual(self.c._lazy_prestarted, {})
        self.assertEqual(self.c._lazy_page_daemons, [proc])

    def test_exited_prestarted_daemon_falls_back_to_a_restore_path_start(self):
        stale, fresh = live_daemon(), live_daemon()
        self.prestart(self.image, stale)
        stale.poll.return_value = 0
        with patch.object(self.c, '_spawn_lazy_pages_daemon', return_value=fresh) as spawn:
            taken, source, background_ms = self.c._take_lazy_pages_daemon(self.image)
        spawn.assert_called_once_with(self.image)
        self.assertEqual((taken, source, background_ms), (fresh, 'synchronous', None))
        self.assertEqual(self.c._lazy_page_daemons, [fresh])

    def test_restore_waits_for_an_in_flight_prestart_of_the_same_image(self):
        proc, started, release = live_daemon(), threading.Event(), threading.Event()
        def slow_spawn(path):
            started.set()
            release.wait(5)
            return proc
        with patch.object(self.c, '_spawn_lazy_pages_daemon', side_effect=slow_spawn) as spawn:
            self.c._schedule_lazy_prestart(self.image)
            self.assertTrue(started.wait(5))
            timer = threading.Timer(0.05, release.set)
            timer.start()
            self.addCleanup(timer.cancel)
            taken, source, _ = self.c._take_lazy_pages_daemon(self.image)
        spawn.assert_called_once_with(self.image)
        self.assertEqual((taken, source), (proc, 'prestart-wait'))

    def test_idle_daemons_are_bounded_oldest_first(self):
        paths = [self.directory(name) for name in 'abc']
        procs = [live_daemon() for _ in paths]
        for path, proc in zip(paths, procs):
            self.prestart(path, proc)
        procs[0].terminate.assert_called_once()
        procs[1].terminate.assert_not_called()
        self.assertEqual(list(self.c._lazy_prestarted), paths[1:])

    def test_one_request_per_image_while_a_daemon_is_armed(self):
        self.prestart(self.image, live_daemon())
        self.assertIsNone(self.c._schedule_lazy_prestart(self.image))

    def test_gc_stops_the_idle_daemon_and_retires_its_image(self):
        c, proc = self.c, live_daemon()
        c.registry = {'old': {'mem_path': self.image}}
        self.prestart(self.image, proc)
        c.agent_pid, c.ns_init_pid, c.template_pool = 21, 22, None
        c.layers_root, c.current_upper, c.current_work = self.temp.name, '/upper', '/work'
        c.gc_obsolete_snapshots(keep_ids=[])
        self.assertEqual(c.registry, {})
        proc.terminate.assert_called_once()
        self.assertFalse(os.path.exists(self.image))
        self.assertIsNone(c._schedule_lazy_prestart(self.image))

    def test_retired_image_stops_a_daemon_that_finished_starting_late(self):
        proc, started, release = live_daemon(), threading.Event(), threading.Event()
        def slow_spawn(path):
            started.set()
            release.wait(5)
            return proc
        with patch.object(self.c, '_spawn_lazy_pages_daemon', side_effect=slow_spawn):
            future = self.c._schedule_lazy_prestart(self.image)
            self.assertTrue(started.wait(5))
            self.c._retire_lazy_prestart(self.image)
            release.set()
            self.assertIsNone(future.result(timeout=5))
        proc.terminate.assert_called_once()
        self.assertEqual(self.c._lazy_prestarted, {})

    def test_close_stops_idle_daemons_and_refuses_new_requests(self):
        proc = live_daemon()
        self.prestart(self.image, proc)
        self.c._close_lazy_prestart()
        proc.terminate.assert_called_once()
        self.assertEqual(self.c._lazy_prestarted, {})
        self.assertIsNone(self.c._schedule_lazy_prestart(self.directory('later')))

    def test_disabled_prestart_keeps_the_restore_path_start(self):
        c = sc.SandboxController.__new__(sc.SandboxController)
        c._lazy_page_daemons = []
        proc, future = live_daemon(), Future()
        c._watch_lazy_prestart({'mem_path': self.image, 'dump_future': future})
        future.set_result(None)
        self.assertIsNone(c._schedule_lazy_prestart(self.image))
        with patch.object(c, '_spawn_lazy_pages_daemon', return_value=proc):
            self.assertEqual(c._take_lazy_pages_daemon(self.image), (proc, 'synchronous', None))
        self.assertEqual(c._lazy_page_daemons, [proc])


class SlowRestorePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(sc.os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        trace = patch.object(sc, '_trace_event')
        trace.start()
        self.addCleanup(trace.stop)
        image = Path(self.tmp.name) / 'image'
        image.mkdir()
        self.image, self.events, self.daemon = str(image), [], live_daemon()
        c = self.c = sc.SandboxController.__new__(sc.SandboxController)
        c.registry = {'target': {'mem_path': self.image, 'layers': ['/saved'],
                                 'parent_id': None, 'external_pidns': False}}
        c.root_overlays = None
        c.template_pool = SimpleNamespace(get=Mock(return_value=None), templates={},
                                          reset_channels=Mock(), reaper_pid=None)
        c._join_active_dump_before_kill = Mock(return_value=0.0)
        c._kill_active_subtree = Mock(side_effect=lambda *, during_exit: during_exit())
        for name in ('_apply_overlay_switch', '_schedule_async_parent_restamp',
                     '_append_external_pidns_restore', '_probe_clear_soft_dirty',
                     '_invalidate_inflight_llm', '_stop_lazy_pages_daemon'):
            setattr(c, name, Mock())
        c._is_external_pidns_dump = Mock(return_value=False)
        c._all_ext_mount_map_args = Mock(return_value=[])
        c._async_incremental = None
        c.layers_root = self.tmp.name
        c.fixed_active_pid, c.restore_fastfork_dump_pid, c.enable_prewarm = None, False, False
        c.enable_criu_lazy_restore, c.parallel_lazy_restore = True, False
        c.criu_restore_bin = '/fixture/criu'
        c.agent_pid, c.ns_init_pid = 41, 40
        c._lazy_page_daemons = []
        c._take_lazy_pages_daemon = Mock(return_value=(self.daemon, 'prestarted', 5.0))
        c._schedule_lazy_prestart = Mock(side_effect=lambda path: self.events.append(('arm', path)))

    def restore(self, error=None):
        def criu(cmd, **kwargs):
            self.events.append(('criu', '--lazy-pages' in cmd))
            if error is not None:
                raise error
            Path(cmd[cmd.index('--pidfile') + 1]).write_text('77\n')
        with patch.object(sc.subprocess, 'check_call', side_effect=criu), \
             patch.object(sc.os, 'kill'), patch.object(sc.os, 'waitpid'):
            return self.c.restore_action('target')

    def test_cold_restore_uses_the_prestarted_daemon_and_rearms_after_criu(self):
        result = self.restore()
        self.c._take_lazy_pages_daemon.assert_called_once_with(self.image)
        self.assertEqual(self.events, [('criu', True), ('arm', self.image)])
        self.assertEqual(result['restore_slow_lazy_daemon_source'], 'prestarted')
        self.assertEqual(result['restore_slow_lazy_daemon_prestart_ms'], 5.0)
        self.assertLessEqual(result['restore_slow_lazy_daemon_ms'], result['restore_slow_pre_criu_ms'])
        self.c._stop_lazy_pages_daemon.assert_not_called()

    def test_failed_criu_stops_the_taken_daemon_without_rearming(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.restore(subprocess.CalledProcessError(1, ['criu']))
        self.c._stop_lazy_pages_daemon.assert_called_once_with(self.daemon)
        self.assertEqual(self.events, [('criu', True)])

    def test_eager_cold_restore_never_takes_or_arms_a_daemon(self):
        self.c.enable_criu_lazy_restore = False
        result = self.restore()
        self.c._take_lazy_pages_daemon.assert_not_called()
        self.assertEqual(self.events, [('criu', False)])
        self.assertIsNone(result['restore_slow_lazy_daemon_source'])


if __name__ == '__main__':
    unittest.main()
