"""Exact child-lstat disappearance contracts, using existing temporary proc/cgroup fixtures."""
import errno
import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('vm_child_existing_fixture', Path(__file__).with_name('test_e2b_vm_proof_esrch.py'))
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
m = fixture.m


class VMProofChildDisappearance(unittest.TestCase):
    # Reuse fixture setup/helpers only, not its already-counted ESRCH test methods.
    setUp = fixture.VMProofESRCH.setUp
    make_proc = fixture.VMProofESRCH.make_proc
    set_start = fixture.VMProofESRCH.set_start

    def during(self, stage, action, pid=43):
        original = self.proof._validate_process
        self.callbacks = []
        def validate(task, *args, **kwargs):
            if not kwargs.get('metadata_only'):
                self.callbacks.append((task['pid'], kwargs.get('stage')))
                if task['pid'] == pid and kwargs.get('stage') == stage:
                    action(task)
            return original(task, *args, **kwargs)
        self.stack.enter_context(patch.object(self.proof, '_validate_process', side_effect=validate))

    def gone(self, task):
        shutil.rmtree(self.child)

    def fatal(self, message=None, kind=RuntimeError):
        with self.assertRaisesRegex(kind, message or '.*'):
            self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_cgroups, [])

    def assert_discard(self, stage, target=43):
        self.proof.sample()
        self.assertEqual(self.proof.rows, {})
        self.assertEqual(self.proof.discarded_processes, [])
        self.assertEqual(len(self.proof.discarded_cgroups), 1)
        row = self.proof.discarded_cgroups[0]
        detail = row['process_observation']
        self.assertEqual(row['operation'], 'process-validation-' + stage)
        self.assertEqual(detail['stage'], stage)
        self.assertEqual((detail['target']['pid'], detail['target']['start_ticks']), (target, 10))
        self.assertEqual((detail['owner_pid'], detail['owner_start_ticks']), (42 if target == 43 else target, 10))
        self.assertEqual(detail['child'], self.original_child)
        self.assertEqual(detail['root'], self.original_root)
        self.assertLessEqual(detail['error_observed_at'], row['observed_at'])
        self.assertEqual(detail['errno'], errno.ENOENT)
        self.assertEqual(detail['operation'], 'child.lstat')
        path = self.base/'fanout.json'
        path.write_text(json.dumps([{'children': [{'sandbox_id': 'sandbox1'}]}]))
        with self.assertRaisesRegex(RuntimeError, 'Missing placement proof'):
            self.proof.verify_ids(path)
        return detail

    def capture_identities(self):
        child, root = self.child.lstat(), self.root.lstat()
        self.original_child = {'path': str(self.child), 'device': child.st_dev, 'inode': child.st_ino}
        self.original_root = {'path': str(self.root), 'device': root.st_dev, 'inode': root.st_ino}

    def test_pre_read_deletion_discards_whole_child_and_no_id_credit(self):
        self.capture_identities()
        self.during('pre-numa_maps', self.gone)
        detail = self.assert_discard('pre-numa_maps')
        self.assertEqual(detail['target']['numa_policies'], {})

    def test_post_read_deletion_records_read_policy_and_discards_whole_child(self):
        self.capture_identities()
        self.during('post-numa_maps', self.gone)
        detail = self.assert_discard('post-numa_maps')
        self.assertEqual(detail['target']['numa_policies'], {'bind:3': 1})

    def test_discard_removes_earlier_completed_task_from_unfinished_child(self):
        self.make_proc(44, 44)
        (self.proc/'44/task').mkdir()
        (self.proc/'44/task/44').symlink_to(self.proc/'44', target_is_directory=True)
        (self.child/'cgroup.procs').write_text('42\n44\n')
        self.capture_identities()
        self.during('pre-numa_maps', self.gone, pid=44)
        self.assert_discard('pre-numa_maps', target=44)
        self.assertIn((42, 'post-numa_maps'), self.callbacks)

    def test_previous_complete_sample_remains_but_discard_adds_no_row(self):
        self.proof.sample()
        prior = dict(self.proof.rows)
        self.during('pre-numa_maps', self.gone)
        self.proof.sample()
        self.assertEqual(self.proof.rows, prior)
        self.assertEqual(len(self.proof.discarded_cgroups), 1)

    def test_root_missing_at_callback_is_fatal_not_per_pid_continue(self):
        self.during('pre-numa_maps', lambda task: shutil.rmtree(self.root))
        self.fatal('root missing during pre-numa_maps')

    def test_root_replaced_at_callback_is_fatal(self):
        def replace(task):
            self.root.rename(self.base/'old-root')
            self.root.mkdir()
        self.during('pre-numa_maps', replace)
        self.fatal('root identity changed')

    def test_root_missing_during_independent_proof_escapes_generic_continue(self):
        self.during('pre-numa_maps', self.gone)
        original = self.proof._discard_deleted_child
        def discard(*args):
            shutil.rmtree(self.root)
            return original(*args)
        self.stack.enter_context(patch.object(self.proof, '_discard_deleted_child', side_effect=discard))
        self.fatal(kind=FileNotFoundError)

    def test_root_bad_pin_cannot_be_hidden_by_child_absence(self):
        def action(task):
            (self.root/'cpuset.cpus.effective').write_text('0-3')
            self.gone(task)
        self.during('pre-numa_maps', action)
        self.fatal('effective cgroup placement')

    def test_root_swap_cannot_be_hidden_by_child_absence(self):
        def action(task):
            (self.root/'memory.swap.current').write_text('1')
            self.gone(task)
        self.during('post-numa_maps', action)
        self.fatal('effective no-swap')

    def child_lstat_error_once(self, code, after=None):
        original = Path.lstat
        armed = {'value': False}
        self.during('pre-numa_maps', lambda task: armed.update(value=True))
        def lstat(path, *args, **kwargs):
            if path == self.child and armed['value']:
                armed['value'] = False
                if after:
                    after()
                raise OSError(code, 'synthetic exact child lstat', str(path))
            return original(path, *args, **kwargs)
        self.stack.enter_context(patch.object(Path, 'lstat', lstat))

    def test_child_still_present_is_fatal(self):
        self.child_lstat_error_once(errno.ENOENT)
        self.fatal('leaf remains present')

    def test_child_replaced_is_fatal(self):
        def replace():
            self.child.rename(self.root/'old-child')
            self.child.mkdir()
        self.child_lstat_error_once(errno.ENOENT, replace)
        self.fatal('was replaced')

    def test_child_reappears_on_second_proof_check_is_fatal(self):
        original = Path.lstat
        old = original(self.child)
        counter = {'n': 0, 'armed': False}
        def action(task):
            self.gone(task)
            counter['armed'] = True
        self.during('pre-numa_maps', action)
        def lstat(path, *args, **kwargs):
            if path == self.child and counter['armed']:
                counter['n'] += 1
                if counter['n'] == 3:
                    return old
            return original(path, *args, **kwargs)
        self.stack.enter_context(patch.object(Path, 'lstat', lstat))
        self.fatal('reappeared during deletion verification')

    def test_child_permission_error_is_not_disappearance(self):
        self.child_lstat_error_once(errno.EACCES)
        self.fatal(kind=PermissionError)

    def test_child_enodev_is_not_this_disappearance_signal(self):
        self.child_lstat_error_once(errno.ENODEV)
        self.fatal(kind=OSError)

    def bad_then_delete(self, field, value, stage='pre-numa_maps'):
        def action(task):
            task[field] = value
            self.gone(task)
        self.during(stage, action)

    def test_bad_already_read_cpu_masks_remain_fatal(self):
        self.bad_then_delete('cpus', '0-3')
        self.fatal('task placement')

    def test_bad_already_read_memory_mask_remains_fatal(self):
        self.bad_then_delete('mems', '0')
        self.fatal('task placement')

    def test_bad_already_read_tgid_remains_fatal(self):
        self.bad_then_delete('tgid', 999)
        self.fatal('different process')

    def test_bad_already_read_cgroup_membership_remains_fatal(self):
        self.bad_then_delete('cgroup', '0::/foreign')
        self.fatal('escaped its observed cgroup')

    def test_bad_post_read_numa_policy_remains_fatal(self):
        (self.proc/'43/numa_maps').write_text('123 bind:0 N0=1\n')
        self.during('post-numa_maps', self.gone)
        self.fatal('actual memory policy')

    def test_owner_pid_reuse_before_child_check_remains_fatal(self):
        def action(task):
            self.set_start(42, 20)
            self.gone(task)
        self.during('pre-numa_maps', action)
        self.fatal('owner PID was reused')

    def test_tid_identity_change_before_child_check_remains_fatal(self):
        def action(task):
            self.set_start(43, 20)
            self.gone(task)
        self.during('pre-numa_maps', action)
        self.fatal('thread identity changed')

    def test_unrelated_proc_file_missing_is_not_child_signal(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.proc/'43/numa_maps':
                raise FileNotFoundError(errno.ENOENT, 'unrelated proc read', str(path))
            return original(path, *args, **kwargs)
        self.stack.enter_context(patch.object(Path, 'read_text', read))
        info, root = self.child.lstat(), self.root.lstat()
        validate = lambda task, **kw: self.proof._validate_process(task, 42, 10, self.child, info,
                                      (root.st_dev, root.st_ino), m.cgroup(self.child), **kw)
        with self.assertRaises(FileNotFoundError):
            m.process(43, before_numa_maps=validate)
        self.assertEqual(self.proof.discarded_cgroups, [])

    def test_actual_observer_supplies_both_explicit_callback_stages(self):
        self.during('unused', lambda task: None)
        self.proof.sample()
        self.assertEqual({stage for _, stage in self.callbacks}, {'pre-numa_maps', 'post-numa_maps'})
        self.assertFalse(issubclass(m.CgroupChildDisappeared, FileNotFoundError))
        self.assertTrue(issubclass(m.CgroupChildDisappeared, RuntimeError))
        self.assertTrue(self.proof.rows)


if __name__ == '__main__':
    unittest.main()
