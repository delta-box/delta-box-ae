"""Owned transient NUMA policy persistence; no real service or global files."""
from contextlib import ExitStack
import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('numa_persistence_hosted', ROOT/'ae/scripts/hosted_launcher.py')
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)
UNIT = 'deltabox-ae-cpu-' + 'a'*32 + '.service'
COMMAND = ['python', '-I', 'run.py', '--checkout', '/public', '--cpu-layout', 'numa03']
BODY = '[Service]\nNUMAPolicy=bind\nNUMAMask=0\n'


class NumaPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.scope.enter_context(patch.object(hosted, 'CPU_SYSTEMD_RUNTIME', self.root))
        self.scope.enter_context(patch.object(hosted.os, 'geteuid', return_value=0))
        # Kernel/root ownership is exercised by the separate real Linux tiny
        # contract. These local tests exercise exact ownership tokens/lifecycle.
        self.scope.enter_context(patch.object(hosted, 'trusted_path', side_effect=lambda p, **k: Path(p)))
        self.scope.enter_context(patch.object(hosted, 'require_root_owned'))
        self.state = {'LoadState':'not-found', 'ActiveState':'inactive', 'MainPID':'0',
                      'cgroup':'/sys/fs/cgroup/system.slice/'+UNIT, 'cgroup_absent':True}

    def prepare(self):
        record = {}
        hosted.prepare_cpu_numa_dropin(UNIT, COMMAND, record)
        return record

    def test_exact_three_lines_independent_of_caller_environment(self):
        with patch.dict(os.environ, {'NUMAPolicy':'preferred', 'NUMAMask':'5'}):
            row = self.prepare()
        self.assertEqual(Path(row['path']).read_text(), BODY)
        self.assertEqual(row['sha256'], hashlib.sha256(BODY.encode()).hexdigest())
        self.assertEqual(row['written_sha256'], row['sha256'])
        self.assertEqual(Path(row['path']).stat().st_mode & 0o777, 0o644)
        self.assertEqual(row['bytes'], len(BODY))
        hosted.finish_cpu_numa_dropin(row, UNIT, self.state)
        self.assertTrue(row['removed'])
        self.assertFalse(Path(row['directory']).exists())

    def test_existing_directory_and_symlink_never_adopted(self):
        for link in (False, True):
            with self.subTest(link=link):
                directory = self.root/(UNIT+'.d')
                if link: directory.symlink_to(self.root)
                else: directory.mkdir()
                row = {}
                with self.assertRaises(FileExistsError):
                    hosted.prepare_cpu_numa_dropin(UNIT, COMMAND, row)
                hosted.finish_cpu_numa_dropin(row, UNIT, self.state)
                self.assertTrue(directory.exists())
                if link: directory.unlink()
                else: directory.rmdir()

    def test_foreign_body_or_member_retained(self):
        for foreign_member in (False, True):
            with self.subTest(foreign_member=foreign_member):
                row = self.prepare()
                path = Path(row['path'])
                if foreign_member: (path.parent/'foreign.conf').write_text('foreign')
                else: path.write_text(BODY+'# changed\n')
                with self.assertRaisesRegex(RuntimeError, 'changed; retained'):
                    hosted.finish_cpu_numa_dropin(row, UNIT, self.state)
                self.assertFalse(row['removed'])
                self.assertTrue(path.exists())
                for p in path.parent.iterdir(): p.unlink()
                path.parent.rmdir()

    def test_replaced_inode_and_hardlink_retained(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                row = self.prepare(); path = Path(row['path']); spare = self.root/'spare'
                if replacement:
                    path.rename(spare); path.write_text(BODY)
                else: os.link(path, spare)
                with self.assertRaisesRegex(RuntimeError, 'identity or bytes changed'):
                    hosted.finish_cpu_numa_dropin(row, UNIT, self.state)
                self.assertTrue(path.exists())
                path.unlink(); spare.unlink(); path.parent.rmdir()

    def test_active_or_unknown_cgroup_never_removes(self):
        row = self.prepare()
        for change in ({'LoadState':'loaded','ActiveState':'active','MainPID':'12'},
                       {'cgroup_absent':False}, {'cgroup':'/foreign'}, {'MainPID':'12','LoadState':'loaded'}):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'empty service proof'):
                hosted.finish_cpu_numa_dropin(row, UNIT, dict(self.state, **change))
        self.assertTrue(Path(row['path']).exists())

    def test_non_root_bad_unit_and_changed_directory_rejected(self):
        with patch.object(hosted.os, 'geteuid', return_value=1), self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError): hosted.prepare_cpu_numa_dropin('foreign.service', COMMAND, {})
        row = self.prepare(); directory = Path(row['directory']); old = self.root/'old'
        directory.rename(old); directory.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'directory identity changed'):
            hosted.finish_cpu_numa_dropin(row, UNIT, self.state)
        self.assertTrue((old/hosted.CPU_NUMA_DROPIN).exists())

    def run_service(self, *, result=0, background=False, wait=None, fsync_error=False, stop_error=False):
        stack = self.scope
        stack.enter_context(patch.object(Path, 'is_file', return_value=True))
        stack.enter_context(patch.object(hosted.subprocess, 'check_output', return_value='systemd 249'))
        stack.enter_context(patch.object(hosted.uuid, 'uuid4', return_value=SimpleNamespace(hex='a'*32)))
        stack.enter_context(patch.object(hosted, 'cpu_unit_state', return_value=dict(self.state)))
        stack.enter_context(patch.object(hosted, 'verify_cpu_service_empty', return_value=dict(self.state)))
        stop = stack.enter_context(patch.object(hosted, 'stop_cpu_service', side_effect=RuntimeError('stop failed') if stop_error else None, return_value=dict(self.state)))
        stack.enter_context(patch.object(hosted, 'begin_background_transaction', return_value='owned-transaction'))
        finish = stack.enter_context(patch.object(hosted, 'finish_background_transaction'))
        retain = stack.enter_context(patch.object(hosted, 'retain_backend_recovery'))
        stack.enter_context(patch.object(hosted, 'verify_background_cleanup'))
        audit = stack.enter_context(patch.object(hosted, 'audit_launch'))
        handlers = {}
        def install_signal(signum, handler):
            handlers[signum] = handler
            return signal.SIG_DFL
        stack.enter_context(patch.object(hosted.signal, 'signal', side_effect=install_signal))
        process = Mock()
        process.wait.return_value = result
        process.poll.return_value = 0
        if wait:
            process.wait.side_effect = lambda *a, **k: wait(handlers)
        def start(*a, **k):
            self.assertEqual((self.root/(UNIT+'.d')/hosted.CPU_NUMA_DROPIN).read_text(), BODY)
            return process
        popen = stack.enter_context(patch.object(hosted.subprocess, 'Popen', side_effect=start))
        if fsync_error: stack.enter_context(patch.object(hosted.os, 'fsync', side_effect=OSError('fsync failed')))
        policy = {'runtime_root':Path('/public'), 'python':Path('/python')}
        call = lambda: hosted.run_cpu_service(policy, SimpleNamespace(pw_uid=1), COMMAND, {},
                                     yield_requested=(lambda: True) if background else None)
        return call, audit, finish, retain, popen, stop

    def test_success_and_original_nonzero_code_preserved_with_removed_receipt(self):
        for rc in (0, 7):
            with self.subTest(rc=rc):
                # Explicit per-iteration scope so lifecycle mocks do not overlap.
                with ExitStack() as inner:
                    old = self.scope; self.scope = inner
                    try:
                        call, audit, finish, retain, _, _ = self.run_service(result=rc, background=True)
                        self.assertEqual(call(), rc)
                    finally: self.scope = old
                row = audit.call_args.kwargs
                self.assertEqual(row['workload_returncode'], rc)
                self.assertTrue(row['numa_policy_dropin']['removed'])
                self.assertEqual(row['numa_policy_dropin']['sha256'], hashlib.sha256(BODY.encode()).hexdigest())
                self.assertFalse((self.root/(UNIT+'.d')).exists())
                finish.assert_called_once(); retain.assert_not_called()

    def test_yield_cleans_before_transaction_commit(self):
        def wait(_): raise subprocess.TimeoutExpired('systemd-run', .5)
        call, audit, finish, _, _, _ = self.run_service(background=True, wait=wait)
        finish.side_effect = lambda *a: self.assertFalse((self.root/(UNIT+'.d')).exists())
        self.assertEqual(call(), hosted.CPU_REVIEWER_YIELD)
        self.assertTrue(audit.call_args.kwargs['numa_policy_dropin']['removed'])

    def test_signal_preserves_130_and_cleans(self):
        def wait(handlers): handlers[signal.SIGINT](signal.SIGINT, None)
        call, audit, _, _, _, _ = self.run_service(wait=wait)
        self.assertEqual(call(), 130)
        self.assertTrue(audit.call_args.kwargs['numa_policy_dropin']['removed'])

    def test_setup_failure_no_launch_owned_file_cleaned_and_error_preserved(self):
        call, audit, finish, _, popen, _ = self.run_service(background=True, fsync_error=True)
        with self.assertRaisesRegex(OSError, 'fsync failed'): call()
        popen.assert_not_called()
        self.assertTrue(audit.call_args.kwargs['numa_policy_dropin']['removed'])
        self.assertFalse((self.root/(UNIT+'.d')).exists())
        finish.assert_called_once()

    def test_partial_write_failure_preserves_exact_partial_bytes_for_cleanup(self):
        call, audit, finish, _, popen, _ = self.run_service(background=True)
        original = os.write
        writes = []
        def write(fd, data):
            if writes: raise OSError('partial write failed')
            writes.append(True)
            return original(fd, data[:3])
        with patch.object(hosted.os, 'write', side_effect=write):
            with self.assertRaisesRegex(OSError, 'partial write failed'): call()
        popen.assert_not_called()
        row = audit.call_args.kwargs['numa_policy_dropin']
        self.assertEqual(row['written'], BODY[:3])
        self.assertEqual(row['written_sha256'], hashlib.sha256(BODY[:3].encode()).hexdigest())
        self.assertTrue(row['removed'])
        finish.assert_called_once()

    def test_popen_failure_cleans_owned_dropin_and_preserves_error(self):
        call, audit, finish, _, popen, _ = self.run_service(background=True)
        popen.side_effect = OSError('cannot launch')
        with self.assertRaisesRegex(OSError, 'cannot launch'): call()
        self.assertTrue(audit.call_args.kwargs['numa_policy_dropin']['removed'])
        finish.assert_called_once()

    def test_cleanup_change_is_fatal_retains_guard_and_transaction(self):
        def wait(_):
            (self.root/(UNIT+'.d')/hosted.CPU_NUMA_DROPIN).write_text('changed')
            return 0
        call, audit, finish, retain, _, _ = self.run_service(background=True, wait=wait)
        with self.assertRaisesRegex(RuntimeError, 'bytes changed'): call()
        finish.assert_not_called(); retain.assert_called_once()
        row = audit.call_args.kwargs
        self.assertEqual(row['returncode'], 1)
        self.assertEqual(row['workload_returncode'], 0)
        self.assertFalse(row['numa_policy_dropin']['removed'])
        self.assertTrue((self.root/(UNIT+'.d')/hosted.CPU_NUMA_DROPIN).exists())

    def test_stop_failure_retains_dropin_and_does_not_commit(self):
        def wait(_): raise OSError('workload failed')
        call, audit, finish, retain, _, _ = self.run_service(background=True, wait=wait, stop_error=True)
        with self.assertRaisesRegex(RuntimeError, 'stop failed'): call()
        finish.assert_not_called(); retain.assert_called_once()
        self.assertFalse(audit.call_args.kwargs['numa_policy_dropin']['removed'])
        self.assertTrue((self.root/(UNIT+'.d')/hosted.CPU_NUMA_DROPIN).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
