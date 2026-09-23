"""The detached protocol must reject unsafe legacy/configuration combinations."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backends.deltabox.gsd.async_checkpoint import AsyncIncrementalCheckpoint
from common.runtime_profile import checkpoint_environment


class AsyncConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.binary = Path(self.temp.name) / 'criu'
        self.binary.write_bytes(b'pinned fixture binary')
        self.controller = SimpleNamespace(enable_warm_template=True, template_pool=object(),
            fixed_active_pid=None, prefork_template_dump=False, async_template_full_dump=False,
            checkpoint_stash_template=True, enable_criu_lazy_restore=False, criu_dump_bin=str(self.binary), criu_restore_bin=str(self.binary))
        env = patch.dict(os.environ, {'DELTABOX_FRESH_PIDNS_ACTIVE': '0',
                                     'DELTABOX_RESTAMP_PARENT_INVENTORY': '0'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        lookup = patch('backends.deltabox.gsd.async_checkpoint.shutil.which', return_value=str(self.binary))
        lookup.start(); self.addCleanup(lookup.stop)
        version = patch('backends.deltabox.gsd.async_checkpoint.subprocess.check_output',
                        return_value='Version: 4.2\nDeltaBox capabilities: exact-parent-v1\n')
        self.version = version.start(); self.addCleanup(version.stop)

    def test_correct_configuration_pins_binary_and_capability(self):
        runtime = AsyncIncrementalCheckpoint(self.controller)
        self.assertEqual(runtime.binary_identity['sha256'], hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual(runtime.max_pending, 4)
        self.assertEqual(self.version.call_args.kwargs['env']['DELTABOX_CRIU_CAPABILITIES'], '1')

    def test_legacy_modes_and_missing_warm_pool_are_rejected_before_probe(self):
        for key, value in [('enable_warm_template', False), ('template_pool', None),
                           ('fixed_active_pid', 100), ('prefork_template_dump', True),
                           ('async_template_full_dump', True), ('checkpoint_stash_template', False)]:
            with self.subTest(key=key):
                original = getattr(self.controller, key)
                setattr(self.controller, key, value)
                try:
                    with self.assertRaises(ValueError):AsyncIncrementalCheckpoint(self.controller)
                finally:setattr(self.controller, key, original)
        self.version.assert_not_called()

    def test_lazy_restore_requires_preserved_parent_page_eligibility(self):
        self.controller.enable_criu_lazy_restore = True
        with self.assertRaisesRegex(RuntimeError, 'exact-parent-lazy-v1'):
            AsyncIncrementalCheckpoint(self.controller)
        self.version.return_value += 'exact-parent-lazy-v1'
        AsyncIncrementalCheckpoint(self.controller)
        self.assertEqual(checkpoint_environment('async-incremental-lazy', 'slow')['DELTABOX_CRIU_LAZY_RESTORE'], '1')

    def test_eager_profile_remains_compatible_with_existing_pinned_builds(self):
        self.assertEqual(checkpoint_environment('async-incremental', 'slow')['DELTABOX_CRIU_LAZY_RESTORE'], '0')
        for mode in ('fast', 'slow'):
            config = checkpoint_environment('async-incremental-lazy', mode)
            self.assertEqual(config['DELTABOX_ASYNC_INCREMENTAL_DUMP'], '1')
            self.assertEqual(config['DELTABOX_ASYNC_TEMPLATE_FULL_DUMP'], '0')
            self.assertEqual(config['DELTABOX_CRIU_LAZY_RESTORE'], '1' if mode == 'slow' else '0')

    def test_lazy_restore_rejects_unpatched_parent_reader(self):
        self.controller.enable_criu_lazy_restore = True
        self.controller.criu_restore_bin = '/stock/criu'
        self.version.side_effect = ['exact-parent-v1 exact-parent-lazy-v1', 'Version: 4.2']
        with patch('backends.deltabox.gsd.async_checkpoint.shutil.which', side_effect=lambda path: path):
            with self.assertRaisesRegex(RuntimeError, 'restore binary'):
                AsyncIncrementalCheckpoint(self.controller)

    def test_fresh_active_pid_namespace_is_rejected(self):
        os.environ['DELTABOX_FRESH_PIDNS_ACTIVE'] = '1'
        with self.assertRaisesRegex(ValueError, 'non-init active'):AsyncIncrementalCheckpoint(self.controller)
        self.version.assert_not_called()

    def test_parent_inventory_restamp_is_rejected(self):
        os.environ['DELTABOX_RESTAMP_PARENT_INVENTORY'] = '1'
        with self.assertRaisesRegex(ValueError, 'restamping'):AsyncIncrementalCheckpoint(self.controller)
        self.version.assert_not_called()

    def test_stock_criu_cannot_silently_ignore_the_enable_environment(self):
        self.version.return_value = 'Version: 4.2\nGitID: v4.2-22-g2cf8f13ca\n'
        with self.assertRaisesRegex(RuntimeError, 'exact-parent-v1'):AsyncIncrementalCheckpoint(self.controller)

    def test_missing_binary_is_rejected(self):
        with patch('backends.deltabox.gsd.async_checkpoint.shutil.which', return_value=None):
            with self.assertRaises(FileNotFoundError):AsyncIncrementalCheckpoint(self.controller)
        self.version.assert_not_called()

    def test_capability_process_failure_is_not_converted_into_success(self):
        self.version.side_effect = subprocess.CalledProcessError(1, ['criu', '--version'])
        with self.assertRaises(subprocess.CalledProcessError):AsyncIncrementalCheckpoint(self.controller)

    def test_queue_capacity_is_positive_and_bounded(self):
        for value in ('0', '65', 'bad'):
            with self.subTest(value=value):
                os.environ['DELTABOX_ASYNC_MAX_PENDING'] = value
                with self.assertRaises(ValueError):AsyncIncrementalCheckpoint(self.controller)

    def test_nonpositive_dump_deadline_is_rejected(self):
        for value in ('0', '-1'):
            with self.subTest(value=value):
                os.environ['DELTABOX_ASYNC_DUMP_TIMEOUT'] = value
                with self.assertRaises(ValueError):AsyncIncrementalCheckpoint(self.controller)

    def test_replay_profile_overrides_a_stale_fresh_active_switch(self):
        os.environ['DELTABOX_FRESH_PIDNS_ACTIVE'] = '1'
        profile = checkpoint_environment('async-incremental')
        self.assertEqual(profile['DELTABOX_FRESH_PIDNS_ACTIVE'], '0')
        self.assertEqual(profile['DELTABOX_CHECKPOINT_STASH_TEMPLATE'], '1')
        self.assertEqual(profile['DELTABOX_CRIU_LAZY_RESTORE'], '0')


if __name__ == '__main__':
    unittest.main()
