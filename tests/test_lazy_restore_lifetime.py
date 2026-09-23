"""UFFD readers must retain their images until exit, including ancestor chains."""
from pathlib import Path
import os
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from backends.deltabox.gsd import sandbox_controller as sc
from backends.deltabox.gsd.async_checkpoint import retained_ids

class LazyRestoreLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        c = self.c = sc.SandboxController.__new__(sc.SandboxController)
        c._lazy_page_daemons = []
        c.criu_restore_bin = '/fixture/criu'
        c.registry = {'a': {'mem_path': '/a'}, 'b': {'mem_path': '/b', 'prev_ckpt_id': 'a'},
                      'c': {'mem_path': '/c', 'prev_ckpt_id': 'b'}, 'other': {'mem_path': '/other'}}

    def test_live_reader_keeps_recursive_parent_chain_then_releases(self):
        proc = Mock(deltabox_mem_path='/c')
        proc.poll.return_value = None
        self.c._lazy_page_daemons.append(proc)
        self.assertEqual(retained_ids(self.c.registry, self.c._lazy_reader_ids()), {'a', 'b', 'c'})
        proc.poll.return_value = 0
        self.assertEqual(self.c._lazy_reader_ids(), set())
        self.assertEqual(self.c._lazy_page_daemons, [])

    def test_one_of_two_readers_exiting_cannot_release_shared_image(self):
        first, second = Mock(deltabox_mem_path='/c'), Mock(deltabox_mem_path='/c')
        first.poll.return_value = 0
        second.poll.return_value = None
        self.c._lazy_page_daemons = [first, second]
        self.assertEqual(self.c._lazy_reader_ids(), {'c'})

    def test_gc_holds_live_reader_and_ancestors(self):
        c = self.c
        for key, entry in c.registry.items():
            entry['mem_path'] = str(Path(self.temp.name) / key)
            Path(entry['mem_path']).mkdir()
        proc = Mock(deltabox_mem_path=c.registry['c']['mem_path'])
        proc.poll.return_value = None
        c._lazy_page_daemons = [proc]
        c.agent_pid, c.ns_init_pid = 21, 22
        c.template_pool = None
        c.layers_root = self.temp.name
        c.current_upper, c.current_work = '/upper', '/work'
        c.gc_obsolete_snapshots(keep_ids=[])
        self.assertEqual(set(c.registry), {'a', 'b', 'c'})
        proc.poll.return_value = 0
        c.gc_obsolete_snapshots(keep_ids=[])
        self.assertEqual(c.registry, {})

    def test_start_requires_status_byte_and_registers_lease(self):
        proc = Mock()
        proc.poll.return_value = None
        def start(*args, **kwargs):
            os.write(kwargs['pass_fds'][0], b'\0')
            return proc
        with patch.object(sc.subprocess, 'Popen', side_effect=start):
            self.assertIs(self.c._start_lazy_pages_daemon(self.temp.name), proc)
        self.assertEqual(proc.deltabox_mem_path, self.temp.name)
        self.assertEqual(self.c._lazy_page_daemons, [proc])
        proc.terminate.assert_not_called()

    def test_daemon_exit_before_readiness_releases_lease(self):
        proc = Mock(returncode=1)
        proc.poll.return_value = 1
        with patch.object(sc.subprocess, 'Popen', return_value=proc):
            with self.assertRaisesRegex(RuntimeError, 'died rc=1'):
                self.c._start_lazy_pages_daemon(self.temp.name)
        self.assertEqual(self.c._lazy_page_daemons, [])

    def test_timeout_terminates_and_reaps_owned_daemon(self):
        proc = Mock()
        proc.poll.side_effect = [None, 0]
        with patch.object(sc.subprocess, 'Popen', return_value=proc), \
             patch.object(sc.time, 'monotonic', side_effect=[0, 3]):
            with self.assertRaises(TimeoutError):
                self.c._start_lazy_pages_daemon(self.temp.name)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2)
        self.assertEqual(self.c._lazy_page_daemons, [])

if __name__ == '__main__':
    unittest.main()
