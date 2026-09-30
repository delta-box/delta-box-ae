"""RAM lifecycle regressions; simulated mount layers, no host mounts/processes."""
import gc
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024 ** 3
spec = importlib.util.spec_from_file_location('memory_job', ROOT / 'ae/scripts/run_memory_job.py')
job = importlib.util.module_from_spec(spec)
if (ROOT / 'ae/repro/common.py').exists():
    spec.loader.exec_module(job)
else:
    common = ModuleType('repro.common')
    common.configured_path = lambda config, key: Path(config[key])
    common.load_config = lambda path: json.loads(Path(path).read_text())
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    common.write_json = write_json
    cleanup = ModuleType('repro.staging_cleanup')
    cleanup.cleanup_reconstructable_staging = lambda path: None
    capacity = ModuleType('vendor.finalbench.fc_diff_dm.fc_capacity')
    capacity.GIB = GIB
    capacity.check_capacity = lambda *args, **kwargs: None
    capacity.job_size_gib = lambda *args, **kwargs: None
    with patch.dict(sys.modules, {'repro.common': common, 'repro.staging_cleanup': cleanup,
             'vendor.finalbench.fc_diff_dm.fc_capacity': capacity}):
        spec.loader.exec_module(job)


class MemoryJobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'ae/work').mkdir(parents=True)
        self.suite = self.root / 'suite'; self.suite.mkdir()
        (self.suite / 'original-marker').write_text('older immutable disk evidence')
        self.original_inode = self.suite.stat().st_ino
        self.config = self.root / 'config.json'; self.config.write_text('{"mem_mib":8192}')
        self.key = 'django__django-14182'
        self.events = []
        self.active = {}
        self.aliases = {}
        self.ram = None
        self.original = None
        self.unmount_failure = None
        self.foreign = None
        self.child_error = self.staging_error = self.copy_error = self.wait_error = None
        self.status = 0
        self.environments = []
        self.children_cleaned = False
        self.post_copy_child = False
        self.post_copy_child_error = None
        self.term_during_copy = False
        self.signal_before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(lambda: self.assertEqual(signal.getsignal(signal.SIGTERM), self.signal_before))
        patches = [patch.object(job, 'ROOT', self.root),
            patch.object(job.os, 'geteuid', return_value=0),
            patch.object(job.os, 'sched_getaffinity', return_value={72,73,74,75}, create=True),
            patch.dict(os.environ, {'AE_MEASUREMENT_IDENTITY': json.dumps({'frequency_policy':'locked-2101000-khz'})}, clear=True),
            patch.object(job, 'bind_private', side_effect=self.bind),
            patch.object(job, 'mount_private', side_effect=self.ram_mount),
            patch.object(job, 'verify_owned_mount', side_effect=self.verify),
            patch.object(job, 'unmount_owned', side_effect=self.unmount),
            patch.object(job, 'all_mount_records', side_effect=lambda: list(self.active.values())),
            patch.object(job, 'mount_info', return_value={'fstype':'tmpfs','options':'noswap,mpol=bind:3,size=30G'}),
            patch.object(job, 'OwnedChildren', side_effect=self.children),
            patch.object(job.subprocess, 'check_output', return_value='policy: bind\nmembind: 3'),
            patch.object(job.subprocess, 'Popen', side_effect=self.producer),
            patch.object(job.subprocess, 'run', side_effect=self.command),
            patch.object(job, 'cleanup_reconstructable_staging', side_effect=self.staging),
            patch.object(job, 'job_size_gib'),
            patch.object(job, 'check_capacity', side_effect=self.capacity),
            patch.object(job.shutil, 'disk_usage', return_value=SimpleNamespace(total=30*GIB,used=1234,free=30*GIB-1234))]
        for context in patches:
            context.start(); self.addCleanup(context.stop)

    def record(self, target, records):
        row = dict(mount_id=len(self.active)+100, target=str(target), owned=True,
                   device=1, inode=1, previous={'mount':None,'device':1,'inode':1})
        self.active[str(target)] = row.copy()
        records.append(row)
        return row

    def bind(self, source, target, records):
        self.events.append('bind:' + str(target))
        self.record(target, records)
        if target.name.startswith('memory-archive-'):
            held = target.with_name(target.name+'-held')
            target.rename(held); target.symlink_to(source, target_is_directory=True)
            self.aliases[str(target)] = held

    def ram_mount(self, command, target, records, **kwargs):
        self.events.append('ram-mount')
        self.assertEqual(command, ['mount','-t','tmpfs','-o',
            'size=30G,noswap,mpol=bind:3,mode=0755','deltabox-ae-memory',str(self.suite)])
        self.record(target, records)
        self.original = self.root/'original-suite'
        self.suite.rename(self.original); self.suite.mkdir()
        for alias in self.aliases:
            Path(alias).unlink(); Path(alias).symlink_to(self.original,target_is_directory=True)
        self.ram = self.suite

    def verify(self, record):
        if self.foreign == record['target'] or record['target'] not in self.active:
            raise RuntimeError('Owned mount identity changed')

    def unmount(self, record):
        self.verify(record)
        self.events.append('umount:' + record['target'])
        if self.unmount_failure == record['target'] or (
                self.unmount_failure == 'alias' and record['target'] in self.aliases):
            raise RuntimeError('owned unmount failed')
        target = Path(record['target'])
        if target == self.suite:
            held = self.root/'retired-ram'
            self.suite.rename(held); self.original.rename(self.suite)
            self.ram = held
            for alias in self.aliases:
                Path(alias).unlink(); Path(alias).symlink_to(self.suite,target_is_directory=True)
        elif str(target) in self.aliases:
            target.unlink(); self.aliases[str(target)].rename(target)
        self.active.pop(str(target)); record['unmounted'] = True

    def children(self):
        evidence = []
        def register(pid):
            evidence.append(dict(pid=pid,start_ticks=10))
        def cleanup(child):
            self.events.append('children-cleanup')
            if self.child_error:
                raise self.child_error
            self.children_cleaned = True
            if self.post_copy_child:
                self.events.append('archive-child-cleanup')
                if self.post_copy_child_error:
                    raise self.post_copy_child_error
                self.post_copy_child = False
            if child is not None:
                child.returncode = self.status
            for row in evidence:
                row['reaped'] = True
            # Simulated adopted setsid child must be reaped before staging/cp.
            evidence.append(dict(pid=4322,start_ticks=11,reaped=True,term_sent=True))
        def restore():
            self.events.append('subreaper-restore')
        return SimpleNamespace(register=register,cleanup=cleanup,restore=restore,evidence=evidence,
                               start_live_reaping=lambda pid: self.events.append('live-reap-start'),
                               stop_live_reaping=lambda: self.events.append('live-reap-stop'))

    def producer(self, command, *, env, start_new_session):
        self.assertTrue(start_new_session)
        self.assertEqual(command, self.args().command)
        self.environments.append(env)
        output = self.suite/self.key; output.mkdir()
        (output/'known').write_text('new producer evidence')
        self.events.append('producer')
        child = SimpleNamespace(pid=4321,returncode=None)
        def wait(timeout=None):
            if self.wait_error and timeout is None:
                raise self.wait_error
            child.returncode = self.status
            return self.status
        child.wait = wait
        child.poll = lambda: child.returncode
        return child

    def command(self, command, **kwargs):
        self.assertEqual(command[0],'cp')
        self.assertTrue(self.children_cleaned,'archive must follow adopted-child reap')
        self.events.append('copy')
        if self.term_during_copy:
            self.post_copy_child = True
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        if self.copy_error:
            raise self.copy_error
        shutil.copytree(command[-2],command[-1])
        return subprocess.CompletedProcess(command,0)

    def staging(self, output):
        self.assertTrue(self.children_cleaned,'staging cleanup must follow child reap')
        self.events.append('staging')
        if self.staging_error:
            raise self.staging_error

    def capacity(self, path, phase, needed, receipt, *, node, filesystem_required):
        self.assertEqual((path,phase,needed,node,filesystem_required),
                         (self.suite,'before-staging',34*GIB,3,28*GIB))
        return {'node_required_bytes':36*GIB,'filesystem_required_bytes':28*GIB}

    def args(self):
        return SimpleNamespace(suite=self.suite,key=self.key,size_gib=30,node=3,
            experiment='table-02-fc-diff',config=self.config,
            command=['producer','--out',str(self.suite/self.key)])

    def receipts(self):
        return [json.loads(p.read_text()) for p in (self.root/'ae/work').glob('memory-job-cleanup-error-*.json')]

    def original_marker(self):
        parent = self.original if self.original is not None and self.original.exists() else self.suite
        self.assertEqual((parent/'original-marker').read_text(),'older immutable disk evidence')

    def test_success_preserves_budget_identity_and_archives_after_reap(self):
        self.assertEqual(job.run(self.args()),0)
        self.assertEqual(self.suite.stat().st_ino,self.original_inode)
        self.assertEqual((self.suite/self.key/'known').read_text(),'new producer evidence')
        meta=json.loads((self.suite/self.key/'memory-job.json').read_text())
        self.assertEqual(meta['cleanup_status'],'ok')
        self.assertTrue(all(row['unmounted'] for row in meta['mounts']))
        self.assertTrue(all(row['reaped'] for row in meta['owned_children']))
        identity=json.loads(self.environments[0]['AE_MEASUREMENT_IDENTITY'])
        self.assertEqual(identity,dict(frequency_policy='locked-2101000-khz',storage_mode='tmpfs-noswap',node=3,cpus=[72,73,74,75]))
        self.assertEqual(self.environments[0]['TMPDIR'],'/tmp')
        measured_backing=json.loads(self.environments[0]['AE_MEMORY_JOB'])
        self.assertFalse({'suite','suite_identity','archive_path','archive_identity'} & measured_backing.keys())
        self.assertLess(self.events.index('children-cleanup'),self.events.index('staging'))
        self.assertLess(self.events.index('children-cleanup'),self.events.index('copy'))
        self.assertGreater(self.events.index('subreaper-restore'),self.events.index('copy'))
        unmounts=[e for e in self.events if e.startswith('umount:')]
        self.assertEqual(unmounts[0],'umount:/tmp')
        self.assertEqual(unmounts[1],'umount:'+str(self.suite))
        self.assertFalse(self.active)
        self.assertFalse(list((self.root/'ae/work').glob('memory-archive-*')))
        self.original_marker()

    def test_alias_unmount_failure_retains_real_disk_data_after_gc(self):
        self.unmount_failure='alias'
        with patch.dict(os.environ,{'AE_HOSTED_CALLER_UID':'1012'}):
            with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
                job.run(self.args())
        gc.collect()
        self.original_marker()
        self.assertEqual((self.suite/self.key/'known').read_text(),'new producer evidence')
        self.assertEqual(self.suite.stat().st_ino,self.original_inode)
        alias=Path(self.receipts()[0]['archive_path'])
        self.assertTrue(alias.is_symlink())
        self.assertEqual((alias/'original-marker').read_text(),'older immutable disk evidence')
        guard=json.loads((self.root/'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json').read_text())
        self.assertEqual(guard['receipt'],str(next((self.root/'ae/work').glob('memory-job-cleanup-error-*.json'))))
        self.assertIn('owned unmount failed',str(self.receipts()[0]['cleanup_errors']))

    def test_input_unmount_failure_keeps_ram_and_alias(self):
        self.unmount_failure='/tmp'
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertEqual([e for e in self.events if e.startswith('umount:')],['umount:/tmp'])
        self.assertIn(str(self.suite),self.active)
        self.assertTrue(Path(self.receipts()[0]['archive_path']).is_symlink())
        self.assertTrue((self.suite/self.key/'known').exists())
        self.original_marker()

    def test_ram_unmount_failure_keeps_alias_and_ram(self):
        self.unmount_failure=str(self.suite)
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertIn(str(self.suite),self.active)
        self.assertFalse(any(e.endswith(next(iter(self.aliases))) for e in self.events if e.startswith('umount:')))
        self.original_marker()

    def test_foreign_mount_identity_blocks_archive_and_teardown(self):
        self.foreign=str(self.suite)
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertNotIn('copy',self.events)
        self.assertFalse(any(e.startswith('umount:') for e in self.events))
        self.original_marker()

    def test_descendant_cleanup_failure_blocks_archive_and_unmount(self):
        self.child_error=RuntimeError('owned descendants remain')
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertNotIn('copy',self.events)
        self.assertNotIn('staging',self.events)
        self.assertNotIn('subreaper-restore',self.events)
        self.assertFalse(any(e.startswith('umount:') for e in self.events))
        self.assertEqual(self.receipts()[0]['producer_returncode'],0)

    def test_copy_failure_keeps_ram_evidence(self):
        self.copy_error=RuntimeError('archive storage full')
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertFalse(any(e.startswith('umount:') for e in self.events))
        self.assertTrue((self.suite/self.key/'known').is_file())
        self.original_marker()

    def test_staging_failure_archives_but_records_failed_cleanup(self):
        self.staging_error=RuntimeError('staging cleanup failed')
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertIn('copy',self.events)
        self.assertFalse(self.active)
        final=json.loads((self.suite/self.key/'memory-job.json').read_text())
        self.assertEqual(final['cleanup_status'],'failed')
        self.assertEqual(final['producer_returncode'],0)
        self.assertEqual(final['returncode'],1)

    def test_producer_failure_keeps_actual_status_and_archives(self):
        self.status=7
        self.assertEqual(job.run(self.args()),7)
        self.assertNotIn('staging',self.events)
        self.assertIn('copy',self.events)
        self.assertEqual(json.loads((self.suite/self.key/'memory-job.json').read_text())['producer_returncode'],7)
        self.assertFalse(self.receipts())

    def test_interrupt_reraises_after_reap_archive_and_signal_restore(self):
        self.wait_error=KeyboardInterrupt('actual interruption')
        self.status=-15
        with self.assertRaisesRegex(KeyboardInterrupt,'actual interruption'):
            job.run(self.args())
        self.assertFalse(self.active)
        self.assertIn('copy',self.events)
        final=json.loads((self.suite/self.key/'memory-job.json').read_text())
        self.assertEqual(final['producer_returncode'],-15)
        self.assertIn('KeyboardInterrupt',final['original_error'])
        self.assertEqual(signal.getsignal(signal.SIGTERM),self.signal_before)

    def test_sigterm_during_cp_reaps_archive_children_and_retains_ram(self):
        self.term_during_copy=True
        with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
            job.run(self.args())
        self.assertIn('archive-child-cleanup',self.events)
        self.assertFalse(self.post_copy_child)
        self.assertFalse(any(e.startswith('umount:') for e in self.events))
        receipt=self.receipts()[0]
        self.assertIn('KeyboardInterrupt',receipt['archive_error'])
        self.assertEqual(receipt['producer_returncode'],0)
        self.assertEqual(signal.getsignal(signal.SIGTERM),self.signal_before)
        self.original_marker()

    def test_archive_child_cleanup_failure_blocks_parent_mount_teardown(self):
        original_command=self.command
        def cp_with_escape(command,**kwargs):
            result=original_command(command,**kwargs)
            self.post_copy_child=True
            return result
        self.post_copy_child_error=RuntimeError('archive escaped child still alive')
        with patch.object(job.subprocess,'run',side_effect=cp_with_escape):
            with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
                job.run(self.args())
        self.assertIn('copy',self.events)
        self.assertFalse(any(e.startswith('umount:') for e in self.events))
        self.assertNotIn('subreaper-restore',self.events)
        self.assertIn('archive child cleanup',str(self.receipts()[0]['cleanup_errors']))

    def test_repeated_interrupt_in_cleanup_fails_closed_with_hosted_guard(self):
        self.wait_error=KeyboardInterrupt('first interrupt')
        self.child_error=KeyboardInterrupt('second interrupt')
        with patch.dict(os.environ,{'AE_HOSTED_CALLER_UID':'1012'}):
            with self.assertRaisesRegex(RuntimeError,'Memory cleanup failed'):
                job.run(self.args())
        receipt=self.receipts()[0]
        self.assertIn('first interrupt',receipt['original_error'])
        self.assertIn('second interrupt',str(receipt['cleanup_errors']))
        self.assertTrue((self.root/'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json').exists())
        self.assertFalse(any(e.startswith('umount:') for e in self.events))
        self.assertNotIn('copy',self.events)

    def test_existing_guard_is_not_overwritten(self):
        guard=self.root/'ae/work/CPU_SERVICE_RECOVERY_REQUIRED.json'
        guard.write_text('original recovery evidence')
        self.unmount_failure='/tmp'
        with patch.dict(os.environ,{'AE_HOSTED_CALLER_UID':'1012'}):
            with self.assertRaises(RuntimeError): job.run(self.args())
        self.assertEqual(guard.read_text(),'original recovery evidence')
        self.assertEqual(len(self.receipts()),1)

    def test_existing_canonical_output_is_rejected_without_resource_changes(self):
        (self.suite/self.key).mkdir()
        with self.assertRaises(FileExistsError): job.run(self.args())
        self.assertFalse(self.events)
        self.assertFalse(list((self.root/'ae/work').iterdir()))


class MountOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.source=Path('/owned-source'); self.target=Path('/owned-target')
        self.previous={'mount':{'mount_id':10,'target':str(self.target)},'device':1,'inode':2}
        self.record={'mount_id':20,'target':str(self.target),'owned':True,'device':3,'inode':4,'previous':self.previous}

    def test_stacked_mount_restores_previous_mount_and_inode(self):
        with patch.object(job,'mount_record',side_effect=[{'mount_id':20,'target':str(self.target)},self.previous['mount']]), \
             patch.object(job,'path_identity',side_effect=[{'device':3,'inode':4},{'device':1,'inode':2}]), \
             patch.object(job.subprocess,'run') as command:
            job.unmount_owned(self.record)
        command.assert_called_once_with(['umount',str(self.target)],check=True)
        self.assertTrue(self.record['unmounted'])

    def test_changed_mount_id_refuses_umount(self):
        with patch.object(job,'mount_record',return_value={'mount_id':21,'target':str(self.target)}), \
             patch.object(job.subprocess,'run') as command:
            with self.assertRaisesRegex(RuntimeError,'identity changed'): job.unmount_owned(self.record)
        command.assert_not_called()

    def test_changed_inode_refuses_umount(self):
        with patch.object(job,'mount_record',return_value={'mount_id':20,'target':str(self.target)}), \
             patch.object(job,'path_identity',return_value={'device':3,'inode':5}), \
             patch.object(job.subprocess,'run') as command:
            with self.assertRaisesRegex(RuntimeError,'identity changed'): job.unmount_owned(self.record)
        command.assert_not_called()

    def test_unmount_success_but_wrong_underlying_layer_is_failure(self):
        with patch.object(job,'mount_record',side_effect=[{'mount_id':20,'target':str(self.target)},None]), \
             patch.object(job,'path_identity',return_value={'device':3,'inode':4}), \
             patch.object(job.subprocess,'run'):
            with self.assertRaisesRegex(RuntimeError,'not restored'): job.unmount_owned(self.record)
        self.assertNotIn('unmounted',self.record)

    def test_partially_failed_bind_is_recorded_with_exact_identity(self):
        records=[]
        with patch.object(job,'mount_record',side_effect=[self.previous['mount'],{'mount_id':20,'target':str(self.target)}]), \
             patch.object(job,'path_identity',side_effect=[{'device':1,'inode':2},{'device':3,'inode':4},{'device':3,'inode':4}]), \
             patch.object(job.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['mount'])):
            with self.assertRaises(subprocess.CalledProcessError): job.bind_private(self.source,self.target,records)
        self.assertEqual(records,[self.record])

    def test_foreign_bind_is_retained_and_never_classified_owned(self):
        records=[]
        with patch.object(job,'mount_record',side_effect=[None,{'mount_id':20,'target':str(self.target)}]), \
             patch.object(job,'path_identity',side_effect=[{'device':1,'inode':2},{'device':3,'inode':4},{'device':9,'inode':9}]), \
             patch.object(job.subprocess,'run'):
            with self.assertRaisesRegex(RuntimeError,'source identity changed'): job.bind_private(self.source,self.target,records)
        self.assertFalse(records[0]['owned'])

    def test_visible_mount_id_selected_instead_of_guessing_stack_order(self):
        rows=[{'mount_id':20,'target':str(self.target)},{'mount_id':10,'target':str(self.target)}]
        with patch.object(job,'all_mount_records',return_value=rows), \
             patch.object(job.subprocess,'check_output',return_value='20\n'):
            self.assertEqual(job.mount_record(self.target),rows[0])


if __name__ == '__main__': unittest.main()
