"""Restricted async replay resources reject unsupported state before cloning."""
import os
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backends.deltabox.gsd import async_resources as ar


class ReplayResourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / 'proc/123'
        for directory in ('fd', 'fdinfo', 'task/123'):
            (self.proc / directory).mkdir(parents=True)
        (self.proc / 'stat').write_text('123 (worker name) ' + ' '.join(['S'] + ['0'] * 18 + ['456']))
        (self.proc / 'task/123/children').write_text('')
        (self.proc / 'cwd').symlink_to('/')
        (self.proc / 'exe').symlink_to('/usr/bin/python3')
        (self.proc / 'maps').write_text('1000-2000 rw-p 00000000 00:00 0 [heap]\n')
        self.fifo = self.root / 'commands'
        os.mkfifo(self.fifo)
        self.log = self.root / 'diagnostics.log'
        self.log.write_text('')
        self.fd(0, '/dev/null', os.O_RDONLY)
        self.fd(1, self.log, os.O_WRONLY | os.O_APPEND)
        self.fd(3, self.fifo, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)

    def fd(self, number, target, flags):
        (self.proc / 'fd' / str(number)).symlink_to(target)
        (self.proc / 'fdinfo' / str(number)).write_text(f'pos:\t0\nflags:\t{flags:o}\nmnt_id:\t17\n')

    def inspect(self, **kw):
        return ar.validate_replay_resources(123, overlay_mount_point='/testbed',
            allowed_fifo_paths=[str(self.fifo)], allowed_stdio_paths=[str(self.log)],
            proc_root=self.root / 'proc', **kw)

    def test_idle_replay_contract_records_external_transports(self):
        result = self.inspect()
        self.assertEqual(result['starttime'], 456)
        self.assertEqual([f['kind'] for f in result['fds']],
                         ['null_stdio', 'diagnostic_stdio', 'protocol_fifo'])
        self.assertTrue(result['fds'][2]['cloexec'])
        self.assertTrue(result['protocol_fifos_external'])
        self.assertTrue(result['immutable_image_required'])

    def test_persistent_regular_fd_is_rejected_even_readonly(self):
        data = self.root / 'application.data'; data.write_text('state')
        self.fd(4, data, os.O_RDONLY)
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'persistent fd 4'):
            self.inspect()

    def test_append_log_exception_does_not_apply_to_arbitrary_fds(self):
        self.fd(4, self.log, os.O_WRONLY | os.O_APPEND)
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'persistent fd 4'):
            self.inspect()

    def test_shared_or_mutable_file_mappings_rejected(self):
        for mapping, reason in (
            ('1000-2000 rw-s 00000000 00:00 0 /dev/shm/state', 'Shared mapping'),
            ('1000-2000 r--s 00000000 00:00 0 /dev/shm/state', 'Shared mapping'),
            ('1000-2000 rw-s 00000000 08:00 7 /usr/lib/gconv/cache', 'Shared mapping'),
            ('1000-2000 r--s 00000000 08:00 7 /testbed/cache', 'Shared mapping'),
            ('1000-2000 r--s 00000000 08:00 7 /usr/lib/gconv/cache (deleted)', 'Deleted file'),
            ('1000-2000 rw-p 00000000 08:00 7 /testbed/state', 'mutable task'),
            ('1000-2000 r--p 00000000 08:00 7 /tmp/application', 'immutable image'),
            ('1000-2000 rw-p 00000000 08:00 7 /usr/lib/old (deleted)', 'Deleted file')):
            with self.subTest(mapping=mapping):
                (self.proc / 'maps').write_text(mapping + '\n')
                with self.assertRaisesRegex(ar.UnsupportedAsyncResources, reason):
                    self.inspect()

    def test_readonly_shared_immutable_library_cache_is_allowed(self):
        path = '/usr/lib/x86_64-linux-gnu/gconv/gconv-modules.cache'
        (self.proc / 'maps').write_text(f'1000-2000 r--s 00000000 08:00 7 {path}\n')
        result = self.inspect()
        self.assertEqual(result['immutable_image_mappings'][0],
                         dict(address='1000-2000', permissions='r--s', path=path))
        self.assertTrue(result['immutable_image_required'])

    def test_private_library_mapping_has_explicit_immutable_precondition(self):
        (self.proc / 'maps').write_text('1000-2000 rw-p 00000000 08:00 7 /usr/lib/library.so\n')
        result = self.inspect()
        self.assertEqual(result['immutable_image_mappings'][0]['path'], '/usr/lib/library.so')

    def test_readonly_data_image_conda_executable_and_library_are_allowed(self):
        executable = '/mnt/data/opt/miniconda3/envs/testbed/bin/python3.9'
        library = '/mnt/data/opt/miniconda3/envs/testbed/lib/libpython3.9.so.1.0'
        (self.proc / 'exe').unlink()
        (self.proc / 'exe').symlink_to(executable)
        (self.proc / 'maps').write_text(f'1000-2000 r-xp 00000000 08:00 7 {library}\n')
        result = self.inspect()
        self.assertEqual(result['executable'], executable)
        self.assertEqual(result['immutable_image_mappings'][0]['path'], library)
        self.assertTrue(result['immutable_image_required'])

    def test_data_image_allowlist_does_not_cover_task_payload_or_similar_prefix(self):
        for path in ('/mnt/data/testbeds/app/library.so', '/mnt/data/opt-extra/library.so'):
            with self.subTest(path=path):
                (self.proc / 'maps').write_text(f'1000-2000 r-xp 00000000 08:00 7 {path}\n')
                with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'immutable image'):
                    self.inspect()

    def test_unowned_child_rejected_owned_dump_allowed(self):
        (self.proc / 'task/123/children').write_text('124')
        (self.root / 'proc/124').mkdir()
        (self.root / 'proc/124/stat').write_text('124 (worker) ' + ' '.join(['S'] + ['0'] * 18 + ['789']))
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'unowned'):
            self.inspect()
        self.inspect(allowed_child_pids=[124])

    def test_exited_children_are_not_live_resource_owners(self):
        (self.proc / 'task/123/children').write_text('124 125')
        (self.root / 'proc/124').mkdir()
        (self.root / 'proc/124/stat').write_text('124 (worker) ' + ' '.join(['Z'] + ['0'] * 18 + ['789']))
        self.assertEqual(self.inspect()['exited_children'], [124, 125])

    def test_cwd_and_executable_may_not_reference_task_overlay(self):
        for link, target in [('cwd', '/testbed'), ('exe', '/testbed/python')]:
            with self.subTest(link=link):
                original = os.readlink(self.proc / link)
                (self.proc / link).unlink(); (self.proc / link).symlink_to(target)
                with self.assertRaises(ar.UnsupportedAsyncResources):
                    self.inspect()
                (self.proc / link).unlink(); (self.proc / link).symlink_to(original)

    def test_multithreading_and_unavailable_proc_rejected(self):
        (self.proc / 'task/124').mkdir()
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'single-threaded'):
            self.inspect()
        (self.proc / 'stat').unlink()
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'Cannot verify'):
            self.inspect()

    def test_blocking_fifo_or_duplicate_descriptor_rejected(self):
        (self.proc / 'fdinfo/3').write_text(f'flags:\t{os.O_RDWR:o}\nmnt_id:\t17\n')
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'NONBLOCK'):
            self.inspect()
        (self.proc / 'fdinfo/3').write_text(f'flags:\t{os.O_RDWR | os.O_NONBLOCK:o}\nmnt_id:\t17\n')
        self.fd(4, self.fifo, os.O_RDWR | os.O_NONBLOCK)
        with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'Duplicated'):
            self.inspect()


class DumpResourcePreparationTests(unittest.TestCase):
    def contract(self):
        return dict(version=1, protocol_fifos_external=True,
                    mutable_task_file_references=False,
                    overlay_mount_point='/testbed', cwd='/',
                    fds=[dict(fd=3, path='/tmp/agent.in', flags=os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC,
                              kind='protocol_fifo', cloexec=True)])

    def test_child_contract_omits_large_evidence_and_copies_fds(self):
        contract = self.contract()
        contract.update(pid=123, starttime=456, executable='/usr/bin/python3',
                        immutable_image_mappings=[{'path': '/usr/lib/lib.so'}] * 10000,
                        exited_children=[789])
        compact = ar.dump_child_contract(contract)
        self.assertLess(len(json.dumps(compact)), 4096)
        self.assertNotIn('immutable_image_mappings', compact)
        self.assertNotIn('pid', compact)
        self.assertTrue(compact['immutable_image_required'])
        compact['fds'][0]['fd'] = 123
        self.assertEqual(contract['fds'][0]['fd'], 3)

    def test_unverified_contract_cannot_be_compacted(self):
        contract = self.contract()
        contract['mutable_task_file_references'] = True
        with self.assertRaises(ar.UnsupportedAsyncResources):
            ar.dump_child_contract(contract)

    def test_guard_only_child_reopens_fds_without_claiming_mount_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ar.os, 'chdir') as chdir, patch.object(ar.os, 'open', return_value=99) as opened, \
             patch.object(ar.os, 'dup2') as dup, patch.object(ar.os, 'close') as close:
            result = ar.freeze_dump_view(dict(contract=self.contract(), lower_layers=['/frozen/a', '/base'], workspace=tmp))
        chdir.assert_not_called()
        opened.assert_called_once_with('/tmp/agent.in', os.O_RDWR | os.O_NONBLOCK)
        dup.assert_called_once_with(99, 3, inheritable=False)
        close.assert_called_once_with(99)
        self.assertFalse(result['isolated_overlay'])
        self.assertFalse(result['mutable_task_file_references'])

    def test_missing_contract_or_overlay_cwd_fails_before_fd_mutation(self):
        for changes in ({'version': 2}, {'mutable_task_file_references': True}, {'cwd': '/testbed/dir'}):
            contract = self.contract(); contract.update(changes)
            with self.subTest(changes=changes), patch.object(ar.os, 'open') as opened:
                with self.assertRaises(ar.UnsupportedAsyncResources):
                    ar.freeze_dump_view(contract)
                opened.assert_not_called()

    def test_reopened_fifo_has_independent_open_file_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = os.path.join(tmp, 'transport')
            os.mkfifo(fifo)
            active = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            child = os.dup(active)
            try:
                contract = self.contract()
                contract['fds'][0].update(fd=child, path=fifo)
                ar.freeze_dump_view(contract)
                os.set_blocking(child, True)
                self.assertFalse(os.get_blocking(active), 'child FD flags must not affect active OFD')
                self.assertTrue(os.get_blocking(child))
            finally:
                os.close(child)
                os.close(active)

    def test_inherited_transport_uses_root_relative_key_and_closes_fd(self):
        with patch.object(ar.os, 'open', return_value=50), \
             patch.object(ar.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)), \
             patch.object(ar.os, 'close') as close:
            with ar.protocol_restore_fds(self.contract()) as (args, fds):
                self.assertEqual(args, ['--inherit-fd', 'fd[50]:tmp/agent.in'])
                self.assertEqual(fds, (50,))
            close.assert_called_once_with(50)

    def test_replaced_fifo_fails_and_does_not_leak_descriptor(self):
        with patch.object(ar.os, 'open', return_value=50), \
             patch.object(ar.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)), \
             patch.object(ar.os, 'close') as close:
            with self.assertRaisesRegex(ar.UnsupportedAsyncResources, 'replaced'):
                with ar.protocol_restore_fds(self.contract()):
                    self.fail('must not yield')
            close.assert_called_once_with(50)

    def test_partial_external_open_failure_closes_earlier_descriptors(self):
        contract = self.contract()
        contract['fds'].append(dict(contract['fds'][0], path='/tmp/agent.out', fd=4))
        with patch.object(ar.os, 'open', side_effect=[50, FileNotFoundError('missing transport')]), \
             patch.object(ar.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)), \
             patch.object(ar.os, 'close') as close:
            with self.assertRaises(FileNotFoundError):
                with ar.protocol_restore_fds(contract):
                    self.fail('partial setup must not yield')
            close.assert_called_once_with(50)

    def test_diagnostic_stdio_inherited_once_per_path(self):
        contract = self.contract()
        item = dict(path='/tmp/replay-agent.log', kind='diagnostic_stdio', flags=os.O_WRONLY | os.O_APPEND,
                    cloexec=False)
        contract['fds'] = [dict(item, fd=1), dict(item, fd=2)]
        with patch.object(ar.os, 'open', return_value=50) as opened, \
             patch.object(ar.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600)), \
             patch.object(ar.os, 'close') as close:
            with ar.protocol_restore_fds(contract) as (args, fds):
                self.assertEqual(args, ['--inherit-fd', 'fd[50]:tmp/replay-agent.log'])
                self.assertEqual(fds, (50,))
            opened.assert_called_once()
            close.assert_called_once_with(50)


if __name__ == '__main__':
    unittest.main()
