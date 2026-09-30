"""Private NVMe namespace regression checks; no real mounts or children."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('nvme_job', ROOT / 'ae/scripts/run_nvme_job.py')
job = importlib.util.module_from_spec(spec)
if (ROOT / 'ae/repro/common.py').exists():
    spec.loader.exec_module(job)
else:
    # Isolated local candidate uses the same explicit metadata contract.
    common = ModuleType('repro.common')
    common.write_json = lambda path, value: path.write_text(json.dumps(value))
    cleanup = ModuleType('repro.staging_cleanup')
    cleanup.cleanup_reconstructable_staging = lambda path: dict(status='ok')
    with patch.dict(sys.modules, {'repro.common': common, 'repro.staging_cleanup': cleanup}):
        spec.loader.exec_module(job)


class NVMeJobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'ae/work').mkdir(parents=True)
        self.work_root = self.root / 'configured-work'
        self.work_root.mkdir()
        self.suite = self.root / 'suite'
        self.suite.mkdir()
        self.key = 'django__django-14182'
        self.old = self.work_root / self.key
        self.old.mkdir()
        (self.old / 'untouched').write_text('original failure evidence')
        self.active = {}
        self.archive_originals = {}
        self.namespaces = []
        self.identities = []
        self.commands = []
        self.child_cleanup = []
        self.returncode = 0
        self.wait_error = self.cleanup_error = self.copy_error = self.child_error = None
        self.unmount_error = self.foreign_mount = False
        self.large = False
        self.extra_mount = False
        self.pid = 10000
        real_rmtree = shutil.rmtree
        self.stack = [patch.object(job, 'ROOT', self.root), patch.object(job, 'NVME', self.root),
                      patch.object(job.os, 'geteuid', return_value=0), patch.dict(os.environ, {}, clear=True),
                      patch.object(job.signal, 'signal', return_value=signal.SIG_DFL),
                      patch.object(job, 'bind_private', side_effect=self.bind),
                      patch.object(job, 'verify_owned_mount', side_effect=self.verify),
                      patch.object(job, 'unmount_owned', side_effect=self.unmount),
                      patch.object(job, 'all_mount_records', side_effect=self.mounts),
                      patch.object(job, 'mount_info', return_value=dict(fstype='ext4', source='fixture-nvme')),
                      patch.object(job, 'OwnedChildren', side_effect=self.children),
                      patch.object(job.subprocess, 'Popen', side_effect=self.producer),
                      patch.object(job.subprocess, 'run', side_effect=self.run_command),
                      patch.object(job, 'cleanup_reconstructable_staging', side_effect=self.cleanup),
                      patch.object(job.shutil, 'rmtree', wraps=real_rmtree)]
        for context in self.stack:
            context.start()
            self.addCleanup(context.stop)

    def args(self, suite=None):
        suite = self.suite if suite is None else suite
        return SimpleNamespace(suite=suite, work_root=self.work_root, key=self.key,
                               command=['--', 'producer', '--out', str(suite / self.key)])

    def mounts(self):
        records = list(self.active.values())
        if self.extra_mount:
            records.append(dict(mount_id=999, target=str(self.namespaces[-1] / 'nested')))
        return records

    def bind(self, source, target, records):
        observed = source.stat()
        record = dict(mount_id=len(records) + 1, target=str(target),
                      device=observed.st_dev, inode=observed.st_ino)
        self.active[str(target)] = dict(record)
        records.append(record)
        if target.name.startswith('nvme-archive-'):
            held = target.with_name(target.name + '-held')
            target.rename(held)
            target.symlink_to(source, target_is_directory=True)
            self.archive_originals[str(target)] = held

    def verify(self, record):
        observed = self.active.get(record['target'])
        if self.foreign_mount or observed is None or observed['mount_id'] != record['mount_id']:
            raise RuntimeError('Owned mount identity changed')

    def unmount(self, record):
        self.verify(record)
        if self.unmount_error and record['target'] == str(self.suite):
            raise RuntimeError('owned suite unmount failed')
        target = Path(record['target'])
        if str(target) in self.archive_originals:
            target.unlink()
            self.archive_originals[str(target)].rename(target)
        self.active.pop(str(target))
        record['unmounted'] = True

    def children(self):
        evidence = []
        def register(pid):
            evidence.append(dict(pid=pid, start_ticks=100))
        def cleanup(child):
            self.child_cleanup.append(child)
            if self.child_error:
                raise self.child_error
            for item in evidence:
                item['reaped'] = True
        return SimpleNamespace(register=register, cleanup=cleanup, restore=lambda: None,
                               start_live_reaping=lambda pid: None, stop_live_reaping=lambda: None,
                               evidence=evidence)

    def producer(self, command, *, env, start_new_session):
        self.assertTrue(start_new_session)
        self.assertEqual(command[-2:], ['--out', str(self.current_suite / self.key)])
        self.identities.append(json.loads(env['AE_MEASUREMENT_IDENTITY']))
        namespace = Path(env['AE_NVME_WORK_NAMESPACE'])
        self.namespaces.append(namespace)
        output = namespace / self.key
        output.mkdir()
        (output / 'known.txt').write_text('only new job bytes')
        if self.large:
            with (output / 'large.bin').open('wb') as stream:
                stream.truncate(2 * 1024**3 + 1)
        self.pid += 1
        child = SimpleNamespace(pid=self.pid, returncode=None)
        def wait(timeout=None):
            if self.wait_error and timeout is None:
                raise self.wait_error
            child.returncode = self.returncode
            return self.returncode
        child.wait = wait
        child.poll = lambda: child.returncode
        return child

    def cleanup(self, output):
        if self.cleanup_error:
            raise self.cleanup_error
        return dict(status='ok', actual_path=str(output))

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(command[0], 'cp')
        if self.copy_error:
            raise self.copy_error
        shutil.copytree(command[-2], command[-1], symlinks=True)
        return subprocess.CompletedProcess(command, 0)

    def invoke(self, suite=None):
        self.current_suite = self.suite if suite is None else suite
        return job.run(self.args(self.current_suite))

    def receipts(self):
        return [json.loads(path.read_text()) for path in (self.root / 'ae/work').glob('nvme-job-cleanup-error-*.json')]

    def assert_old_untouched(self):
        self.assertEqual((self.old / 'untouched').read_text(), 'original failure evidence')
        self.assertNotIn(self.work_root, [Path(call.args[0]) for call in job.shutil.rmtree.call_args_list])
        self.assertNotIn(self.old, [Path(call.args[0]) for call in job.shutil.rmtree.call_args_list])

    def test_repeat_same_key_uses_unique_namespaces_and_preserves_old_key(self):
        first_suite = self.suite
        second_suite = self.root / 'suite2'
        second_suite.mkdir()
        self.assertEqual(self.invoke(first_suite), 0)
        self.assertEqual(self.invoke(second_suite), 0)
        self.assertEqual(len(set(self.namespaces)), 2)
        self.assertEqual(self.identities[0], self.identities[1])
        self.assertEqual(self.identities[0]['nvme_work_root'], str(self.work_root))
        self.assertTrue(all(not path.exists() for path in self.namespaces))
        for suite in (first_suite, second_suite):
            self.assertEqual((suite / self.key / 'known.txt').read_text(), 'only new job bytes')
            record = json.loads((suite / self.key / 'nvme-job.json').read_text())
            self.assertEqual(record['configured_work_root'], str(self.work_root))
            self.assertNotEqual(record['work_root'], str(self.work_root))
            self.assertEqual(record['returncode'], 0)
            self.assertEqual(record['cleanup_status'], 'ok')
            self.assertTrue(all(item['unmounted'] for item in record['mounts']))
            self.assertFalse((suite / self.key / 'untouched').exists())
        self.assert_old_untouched()
        self.assertFalse(self.active)

    def test_existing_canonical_output_is_still_rejected(self):
        (self.suite / self.key).mkdir()
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertFalse(self.namespaces)
        self.assert_old_untouched()

    def test_large_failed_job_pointer_uses_new_actual_path_without_copy_or_global_gate(self):
        self.returncode = 7
        self.large = True
        self.assertEqual(self.invoke(), 7)
        pointer = json.loads((self.suite / self.key / 'nvme-retained.json').read_text())
        self.assertEqual(pointer['path'], str(self.namespaces[0] / self.key))
        self.assertTrue(Path(pointer['path']).is_dir())
        self.assertNotEqual(pointer['path'], str(self.old))
        self.assertFalse(self.commands)
        self.assertFalse((self.root / 'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json').exists())
        self.assert_old_untouched()

    def test_archived_small_failed_job_preserves_code_and_removes_only_duplicate_work(self):
        self.returncode = 7
        self.assertEqual(self.invoke(), 7)
        self.assertFalse(self.namespaces[0].exists())
        self.assertEqual(json.loads((self.suite / self.key / 'nvme-job.json').read_text())['returncode'], 7)
        self.assert_old_untouched()

    def test_staging_cleanup_failure_is_archived_retained_and_raised(self):
        self.cleanup_error = ValueError('artifact conflict')
        with self.assertRaisesRegex(RuntimeError, 'NVMe cleanup failed'):
            self.invoke()
        self.assertTrue((self.suite / self.key / 'known.txt').exists())
        self.assertTrue(self.namespaces[0].exists())
        receipt = self.receipts()[0]
        self.assertEqual(receipt['returncode'], 1)
        self.assertIn('artifact conflict', str(receipt['cleanup_errors']))
        self.assert_old_untouched()

    def test_copy_failure_keeps_new_work_and_reports_actual_identity(self):
        self.copy_error = RuntimeError('copy failed')
        with self.assertRaisesRegex(RuntimeError, 'NVMe cleanup failed'):
            self.invoke()
        receipt = self.receipts()[0]
        self.assertEqual(receipt['work_root'], str(self.namespaces[0]))
        self.assertEqual(receipt['work_identity']['inode'], self.namespaces[0].stat().st_ino)
        self.assertTrue((self.namespaces[0] / self.key / 'known.txt').exists())
        self.assert_old_untouched()

    def test_child_cleanup_failure_does_not_archive_unmount_or_delete_active_work(self):
        self.child_error = RuntimeError('escaped child still maps work')
        with patch.dict(os.environ, {'AE_HOSTED_CALLER_UID': '1012'}):
            with self.assertRaisesRegex(RuntimeError, 'NVMe cleanup failed'):
                self.invoke()
        self.assertFalse(self.commands)
        self.assertTrue(self.active)
        self.assertTrue(self.namespaces[0].exists())
        guard = json.loads((self.root / 'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json').read_text())
        self.assertEqual(guard['work_root'], str(self.namespaces[0]))
        self.assert_old_untouched()

    def test_unmount_failure_preserves_archive_alias_and_work_without_recursive_delete(self):
        self.unmount_error = True
        with self.assertRaisesRegex(RuntimeError, 'NVMe cleanup failed'):
            self.invoke()
        self.assertTrue(self.namespaces[0].exists())
        self.assertIn(str(self.suite), self.active)
        self.assertIn('unmount failed', str(self.receipts()[0]['cleanup_errors']))
        self.assert_old_untouched()

    def test_foreign_mount_identity_is_not_unmounted_or_deleted(self):
        self.foreign_mount = True
        with self.assertRaisesRegex(RuntimeError, 'NVMe cleanup failed'):
            self.invoke()
        self.assertTrue(self.active)
        self.assertFalse(self.commands)
        self.assertTrue(self.namespaces[0].exists())

    def test_interruption_is_reraised_after_owned_cleanup_and_evidence_archive(self):
        self.wait_error = KeyboardInterrupt('interrupted producer')
        with self.assertRaisesRegex(KeyboardInterrupt, 'interrupted producer'):
            self.invoke()
        self.assertEqual(len(self.child_cleanup), 1)
        self.assertFalse(self.active)
        self.assertFalse(self.namespaces[0].exists())
        self.assertEqual(json.loads((self.suite / self.key / 'nvme-job.json').read_text())['returncode'], 1)
        self.assertIn('interrupted producer', json.loads((self.suite / self.key / 'nvme-job.json').read_text())['original_error'])

    def test_nested_mount_blocks_owned_namespace_deletion(self):
        self.extra_mount = True
        with self.assertRaisesRegex(RuntimeError, 'NVMe cleanup failed'):
            self.invoke()
        self.assertTrue(self.namespaces[0].exists())
        self.assertIn('still contains a mount', str(self.receipts()[0]['cleanup_errors']))

    def test_existing_global_recovery_gate_is_never_overwritten(self):
        guard = self.root / 'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json'
        guard.write_text('original gate')
        original = guard.stat().st_ino, guard.read_bytes()
        self.child_error = RuntimeError('active child')
        with patch.dict(os.environ, {'AE_HOSTED_CALLER_UID': '1012'}):
            with self.assertRaises(RuntimeError):
                self.invoke()
        self.assertEqual((guard.stat().st_ino, guard.read_bytes()), original)


class OwnedChildrenTests(unittest.TestCase):
    def setUp(self):
        self.pids = set()
        self.flags = [0]
        self.clock = [0.0]
        self.closed = []
        self.signals = []
        self.wait_results = []
        self.start_ticks = 55
        self.signal_exit = self.already_zombie = self.stubborn = False
        self.stack = [patch.object(job, 'direct_children', side_effect=lambda: set(self.pids)),
                      patch.object(job, 'subreaper_flag', side_effect=self.flag),
                      patch.object(job, 'process_identity', side_effect=lambda pid:
                                   dict(pid=pid, start_ticks=self.start_ticks)),
                      patch.object(job.os, 'pidfd_open', side_effect=lambda pid, flags: pid + 1000, create=True),
                      patch.object(job.os, 'P_PIDFD', 3, create=True),
                      patch.object(job.signal, 'pidfd_send_signal', side_effect=self.send, create=True),
                      patch.object(job.os, 'waitid', side_effect=self.waitid, create=True),
                      patch.object(job.os, 'WEXITED', 4, create=True),
                      patch.object(job.os, 'WNOHANG', 1, create=True),
                      patch.object(job.os, 'close', side_effect=self.closed.append),
                      patch.object(job.time, 'monotonic', side_effect=lambda: self.clock[0]),
                      patch.object(job.time, 'sleep', side_effect=lambda seconds:
                                   self.clock.__setitem__(0, self.clock[0] + seconds))]
        for context in self.stack:
            context.start()
            self.addCleanup(context.stop)

    def flag(self, value=None):
        if value is not None:
            self.flags[0] = value
        return self.flags[0]

    def send(self, fd, signum):
        self.signals.append((fd, signum))
        if self.signal_exit:
            self.wait_results.append(SimpleNamespace(si_pid=fd - 1000))
            raise ProcessLookupError('natural exit during signal')
        if not self.stubborn:
            self.wait_results.append(SimpleNamespace(si_pid=fd - 1000))

    def waitid(self, which, fd, flags):
        self.assertEqual(which, job.os.P_PIDFD)
        if self.already_zombie or self.wait_results:
            result = self.wait_results.pop(0) if self.wait_results else SimpleNamespace(si_pid=fd - 1000)
            self.pids.discard(result.si_pid)
            return result
        return None

    def test_adopted_escaped_child_is_signaled_by_pidfd_and_reaped_before_flag_restore(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        children.cleanup(None)
        children.restore()
        self.assertEqual(self.signals, [(1321, signal.SIGTERM)])
        self.assertTrue(children.evidence[0]['reaped'])
        self.assertFalse(self.pids)
        self.assertEqual(self.flags[0], 0)
        self.assertEqual(self.closed, [1321])

    def test_already_zombie_child_is_reaped_without_signal(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        self.already_zombie = True
        children.cleanup(None)
        children.restore()
        self.assertFalse(self.signals)
        self.assertTrue(children.evidence[0]['reaped'])
        self.assertEqual(self.flags[0], 0)

    def test_natural_exit_during_pidfd_signal_is_reaped_without_false_failure(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        self.signal_exit = True
        children.cleanup(None)
        children.restore()
        self.assertTrue(children.evidence[0]['reaped'])
        self.assertEqual(self.closed, [1321])

    def test_changed_pid_starttick_is_refused_before_signal(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        children.register(321)
        self.start_ticks += 1
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            children.cleanup(None)
        self.assertFalse(self.signals)
        with self.assertRaisesRegex(RuntimeError, 'Refuse subreaper restore'):
            children.restore()

    def test_stubborn_descendant_reaches_bounded_kill_timeout_and_prevents_restore(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        self.stubborn = True
        with self.assertRaisesRegex(RuntimeError, 'remain after pidfd cleanup'):
            children.cleanup(None)
        self.assertIn((1321, signal.SIGKILL), self.signals)
        self.assertGreaterEqual(self.clock[0], 25)
        self.assertLess(self.clock[0], 25.2)
        with self.assertRaisesRegex(RuntimeError, 'Refuse subreaper restore'):
            children.restore()

    def test_live_reap_initial_drain_excludes_producer_and_restores_sigchld(self):
        original=signal.getsignal(signal.SIGCHLD)
        children=job.OwnedChildren()
        self.addCleanup(children.stop_live_reaping)
        self.pids.update({111,321})
        children.register(111)
        self.wait_results.append(SimpleNamespace(si_pid=321,si_status=0))
        children.start_live_reaping(111)
        self.assertEqual(self.pids,{111})
        self.assertFalse(self.signals)
        self.assertTrue(children.evidence[1]['reaped_during_producer'])
        self.assertNotIn('reaped',children.evidence[0])
        self.assertEqual(self.closed,[1321])
        children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)

    def test_live_adopted_process_is_not_killed_or_prematurely_reaped(self):
        original=signal.getsignal(signal.SIGCHLD)
        children=job.OwnedChildren()
        self.addCleanup(children.stop_live_reaping)
        self.pids.update({111,321}); children.register(111)
        children.start_live_reaping(111)
        self.assertEqual(self.pids,{111,321})
        self.assertFalse(self.signals)
        self.assertFalse(self.closed)
        children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)

    def test_tail_sigchld_reentry_is_drained_after_busy_clears(self):
        test=self
        class TailRace(job.OwnedChildren):
            def __setattr__(self,name,value):
                if name=='reap_busy' and value is False and getattr(self,'armed',False):
                    self.armed=False
                    test.pids.add(444)
                    test.wait_results.append(SimpleNamespace(si_pid=444,si_status=0))
                    self.reap_terminated(None,None)  # Reenters while still busy.
                super().__setattr__(name,value)
        original=signal.getsignal(signal.SIGCHLD)
        children=TailRace()
        self.addCleanup(children.stop_live_reaping)
        self.pids.add(111);children.register(111)
        children.armed=True
        children.start_live_reaping(111)
        self.assertEqual(self.pids,{111})
        self.assertTrue(children.evidence[-1]['reaped_during_producer'])
        self.assertEqual(self.closed,[1444])
        children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)

    def test_live_reap_error_restores_handler_and_remains_fail_closed(self):
        original=signal.getsignal(signal.SIGCHLD)
        children=job.OwnedChildren()
        self.pids.update({111,321}); children.register(111)
        with patch.object(job.os,'waitid',side_effect=RuntimeError('waitid failed')):
            children.start_live_reaping(111)
        with self.assertRaisesRegex(RuntimeError,'live reaping failed'):
            children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)
        self.assertFalse(self.signals)

    def test_preexisting_unrelated_direct_child_refuses_subreaper_setup(self):
        self.pids.add(999)
        with self.assertRaisesRegex(RuntimeError, 'unrelated asynchronous children'):
            job.OwnedChildren()
        self.assertEqual(self.flags[0], 0)


class MountOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.source.mkdir()
        self.target.mkdir()
        observed = self.target.stat()
        self.record = dict(mount_id=44, target=str(self.target),
                           device=observed.st_dev, inode=observed.st_ino)

    def test_matching_mount_id_and_inode_is_unmounted_and_verified_absent(self):
        with patch.object(job, 'mount_record', side_effect=[dict(mount_id=44, target=str(self.target)), None]),\
             patch.object(job.subprocess, 'run') as command:
            job.unmount_owned(self.record)
        command.assert_called_once_with(['umount', str(self.target)], check=True)
        self.assertTrue(self.record['unmounted'])

    def test_replaced_mount_id_is_refused_before_umount(self):
        with patch.object(job, 'mount_record', return_value=dict(mount_id=45, target=str(self.target))),\
             patch.object(job.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                job.unmount_owned(self.record)
        command.assert_not_called()

    def test_replaced_source_inode_is_refused_before_umount(self):
        self.record['inode'] += 1
        with patch.object(job, 'mount_record', return_value=dict(mount_id=44, target=str(self.target))),\
             patch.object(job.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                job.unmount_owned(self.record)
        command.assert_not_called()

    def test_partial_failed_bind_is_recorded_for_only_owned_cleanup(self):
        observed = self.source.stat()
        original_stat = Path.stat
        def fake_stat(path, *args, **kwargs):
            return observed if path == self.target else original_stat(path, *args, **kwargs)
        records = []
        with patch.object(job, 'mount_record', side_effect=[None, dict(mount_id=44, target=str(self.target))]),\
             patch.object(Path, 'stat', fake_stat),\
             patch.object(job.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'mount')):
            with self.assertRaises(subprocess.CalledProcessError):
                job.bind_private(self.source, self.target, records)
        self.assertEqual(records, [dict(mount_id=44, target=str(self.target),
                                       device=observed.st_dev, inode=observed.st_ino)])


if __name__ == '__main__':
    unittest.main()
