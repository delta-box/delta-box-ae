"""A detached stash hint must name the right process across PID namespaces."""
import json
import unittest
from unittest.mock import Mock, call, patch

from backends.deltabox.gsd import template_fork as tf


class StashPidHintTests(unittest.TestCase):
    def request(self, hint, nspids=(900, 12), namespaces=('pid:[2]', 'pid:[2]'), reaper=None, child=None):
        pool = tf.TemplatePool.__new__(tf.TemplatePool)
        pool.templates = {}
        pool.reaper_pid = reaper
        pool.ctrl_in_path = '/unused'
        pool._drain_pending = Mock()
        pool._drain_pending_commands = Mock()
        pool._read_response_line = Mock(return_value=json.dumps(dict(
            ok=True, mode='stash_template', parent=2, child=12,
            child_host_pid=hint)).encode())
        with patch.object(tf.os, 'open', return_value=99), \
             patch.object(tf.os, 'write', side_effect=lambda fd, data: len(data)), patch.object(tf.os, 'close'), \
             patch.object(tf.os.path, 'isdir', return_value=True), \
             patch.object(tf, '_nspids_for_pid', return_value=list(nspids)), \
             patch.object(tf.os, 'readlink', side_effect=namespaces), \
             patch.object(tf, '_translate_ns_to_host', return_value=901) as fallback, \
             patch.object(tf, '_find_child_host_pid', return_value=child) as find_child, \
             patch.object(tf, '_log'):
            result = pool.request_stash_template(800, 'checkpoint')
            if reaper:
                find_child.assert_called_once_with(reaper, 12, exclude_pids={800})
        return result, pool.templates, fallback

    def test_valid_detached_hint_avoids_global_scan(self):
        result, templates, fallback = self.request(900)
        self.assertEqual((result, templates), (900, {'checkpoint': 900}))
        fallback.assert_not_called()

    def test_inner_pid_collision_uses_translation(self):
        result, _, fallback = self.request(12, nspids=(12, 99))
        self.assertEqual(result, 901)
        fallback.assert_called_once_with(12, 800)

    def test_matching_inner_pid_in_wrong_namespace_uses_translation(self):
        result, _, fallback = self.request(900, namespaces=('pid:[3]', 'pid:[2]'))
        self.assertEqual(result, 901)
        fallback.assert_called_once_with(12, 800)

    def test_absent_gone_or_source_hint_uses_translation(self):
        for hint, nspids in ((None, ()), (900, ()), (800, (800, 12))):
            with self.subTest(hint=hint, nspids=nspids):
                result, _, fallback = self.request(hint, nspids=nspids)
                self.assertEqual(result, 901)
                fallback.assert_called_once_with(12, 800)

    def test_vanishing_namespace_rejects_hint(self):
        with patch.object(tf, '_nspids_for_pid', return_value=[900, 12]), \
             patch.object(tf.os, 'readlink', side_effect=FileNotFoundError):
            self.assertFalse(tf._valid_stash_host_pid(900, 12, 800))

    def test_reparented_child_found_without_global_scan(self):
        result, _, fallback = self.request(None, reaper=700, child=900)
        self.assertEqual(result, 900)
        fallback.assert_not_called()

    def test_helper_not_exited_or_reaper_missing_falls_back(self):
        result, _, fallback = self.request(None, reaper=700)
        self.assertEqual(result, 901)
        fallback.assert_called_once_with(12, 800)

    def test_reaper_child_in_nested_namespace_is_rejected(self):
        result, _, fallback = self.request(None, reaper=700, child=900,
                                           namespaces=('pid:[3]', 'pid:[2]'))
        self.assertEqual(result, 901)
        fallback.assert_called_once_with(12, 800)

    def helper_request(self, children, identities):
        pool = tf.TemplatePool.__new__(tf.TemplatePool)
        pool.templates = {}
        pool.reaper_pid = 700
        pool.ctrl_in_path = '/unused'
        pool._drain_pending = Mock()
        pool._drain_pending_commands = Mock()
        pool._read_response_line = Mock(return_value=json.dumps(dict(
            ok=True, mode='stash_template', parent=2, helper=11, child=12)).encode())
        with patch.object(tf.os, 'open', return_value=99), \
             patch.object(tf.os, 'write', side_effect=lambda fd, data: len(data)), patch.object(tf.os, 'close'), \
             patch.object(tf, '_valid_stash_host_pid', side_effect=identities), \
             patch.object(tf, '_translate_ns_to_host', return_value=901) as fallback, \
             patch.object(tf, '_find_child_host_pid', side_effect=children) as find, \
             patch.object(tf, '_log'):
            result = pool.request_stash_template(800, 'checkpoint')
        return result, find, fallback

    def test_live_helper_child_avoids_global_scan(self):
        result, find, fallback = self.helper_request([850, 900], [True, True])
        self.assertEqual(result, 900)
        self.assertEqual(find.call_args_list, [call(800, 11), call(850, 12)])
        fallback.assert_not_called()

    def test_helper_exits_during_lookup_child_found_under_reaper(self):
        result, find, fallback = self.helper_request([850, None, 900], [True, True])
        self.assertEqual(result, 900)
        self.assertEqual(find.call_args_list[-1], call(700, 12, exclude_pids={800}))
        fallback.assert_not_called()

    def test_helper_exited_before_lookup_child_found_under_reaper(self):
        result, _, fallback = self.helper_request([None, 900], [True])
        self.assertEqual(result, 900)
        fallback.assert_not_called()

    def test_wrong_helper_namespace_uses_reaper_then_global_fallback(self):
        result, find, fallback = self.helper_request([850, None], [False])
        self.assertEqual(result, 901)
        self.assertEqual(find.call_args_list, [call(800, 11), call(700, 12, exclude_pids={800})])
        fallback.assert_called_once_with(12, 800)

    def test_wrong_child_namespace_uses_reaper_then_global_fallback(self):
        result, _, fallback = self.helper_request([850, 900, None], [True, False])
        self.assertEqual(result, 901)
        fallback.assert_called_once_with(12, 800)



class FreshStashResolutionTests(unittest.TestCase):
    def resolve(self, *, children='7 901', source_ids=(800, 100),
                ids=None, ppids=None, namespaces=None, parent_visible=7):
        from unittest.mock import mock_open
        ids = ids or {800: list(source_ids), 7: [7, 77, 1], 901: [901, 7, 1]}
        ppids = ppids or {7: 800, 901: 800}
        namespaces = namespaces or {800: 'pid:[10]', 7: 'pid:[20]', 901: 'pid:[30]'}
        with patch('builtins.open', mock_open(read_data=children)), \
             patch.object(tf, '_nspids_for_pid', side_effect=lambda pid: ids.get(pid, [])), \
             patch.object(tf, '_ppid_for_pid', side_effect=lambda pid: ppids.get(pid)), \
             patch.object(tf.os, 'readlink', side_effect=lambda path: namespaces[int(path.split('/')[2])]):
            return tf._find_fresh_stash_pid(800, parent_visible)

    def test_nested_source_uses_source_depth_not_first_or_last_pid(self):
        self.assertEqual(self.resolve(), 901)

    def test_outer_numeric_collision_cannot_win(self):
        self.assertIsNone(self.resolve(children='7'))

    def test_host_namespace_source_uses_outer_slot(self):
        self.assertEqual(self.resolve(children='901', source_ids=(800,),
                         ids={800: [800], 901: [901, 1]}, parent_visible=901), 901)

    def test_extra_namespace_depth_is_rejected(self):
        self.assertIsNone(self.resolve(children='901', ids={800: [800, 100], 901: [901, 7, 8, 1]}))

    def test_non_init_inner_pid_is_rejected(self):
        self.assertIsNone(self.resolve(children='901', ids={800: [800, 100], 901: [901, 7, 2]}))

    def test_wrong_direct_parent_is_rejected(self):
        self.assertIsNone(self.resolve(children='901', ppids={901: 700}))

    def test_same_pid_namespace_is_rejected(self):
        self.assertIsNone(self.resolve(children='901', namespaces={800: 'pid:[10]', 901: 'pid:[10]'}))

    def test_missing_identity_and_init_parent_hint_are_rejected(self):
        self.assertIsNone(self.resolve(ids={800: []}))
        self.assertIsNone(self.resolve(parent_visible=1))

    def test_child_disappearing_during_proc_read_is_rejected(self):
        with patch.object(tf, '_nspids_for_pid', return_value=[800, 100]), \
             patch('builtins.open', side_effect=FileNotFoundError):
            self.assertIsNone(tf._find_fresh_stash_pid(800, 7))


class TemplateWriteAllTests(unittest.TestCase):
    def test_partial_writes_preserve_every_byte_in_order(self):
        with patch.object(tf.os, 'write', side_effect=[2, 1, 3]) as write:
            tf._write_all(99, b'abcdef', 1)
        self.assertEqual([bytes(c.args[1]) for c in write.call_args_list],
                         [b'abcdef', b'cdef', b'def'])

    def test_eagain_waits_for_writable_then_retries_unsent_bytes(self):
        with patch.object(tf.os, 'write', side_effect=[2, BlockingIOError(), 4]) as write, \
             patch.object(tf.select, 'select', return_value=([], [99], [])) as select:
            tf._write_all(99, b'abcdef', 1)
        self.assertEqual([bytes(c.args[1]) for c in write.call_args_list],
                         [b'abcdef', b'cdef', b'cdef'])
        self.assertEqual(select.call_args.args[:3], ([], [99], []))
        self.assertGreater(select.call_args.args[3], 0)

    def test_unwritable_fd_times_out(self):
        with patch.object(tf.os, 'write', side_effect=BlockingIOError), \
             patch.object(tf.select, 'select', return_value=([], [], [])):
            with self.assertRaisesRegex(TimeoutError, 'write timed out'):
                tf._write_all(99, b'payload', 1)

    def test_expired_deadline_does_not_wait_again(self):
        with patch.object(tf.os, 'write', side_effect=BlockingIOError), \
             patch.object(tf.time, 'monotonic', side_effect=[10, 12]), \
             patch.object(tf.select, 'select') as select:
            with self.assertRaises(TimeoutError):
                tf._write_all(99, b'payload', 1)
        select.assert_not_called()

    def test_interruption_does_not_drop_prefix(self):
        with patch.object(tf.os, 'write', side_effect=[InterruptedError(), 7]) as write:
            tf._write_all(99, b'payload', 1)
        self.assertEqual([bytes(c.args[1]) for c in write.call_args_list], [b'payload', b'payload'])

    def test_zero_progress_fails_instead_of_spinning(self):
        with patch.object(tf.os, 'write', return_value=0):
            with self.assertRaises(BrokenPipeError):
                tf._write_all(99, b'payload', 1)


if __name__ == '__main__':
    unittest.main()
