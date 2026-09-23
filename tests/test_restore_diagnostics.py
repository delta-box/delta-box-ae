"""Observation failures must preserve the underlying workload outcome."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class RestoreDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        module_path = Path(__file__).resolve().parents[1] / 'ae/runners/deltabox/guest/restore_diagnostics.py'
        spec = importlib.util.spec_from_file_location('restore_diagnostics_test', module_path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.trace = self.root / 'sys/kernel/tracing'
        self.trace.mkdir(parents=True)
        (self.root / 'tmp').mkdir()
        for name in ('tracing_on', 'current_tracer', 'trace_clock', 'buffer_size_kb', 'trace', 'trace_marker'):
            (self.trace / name).touch()
        self.finished = []

        class Controller:
            agent_pid = 10
            template_pool = None
            failure = None

            def restore_action(self, target):
                if self.failure:
                    raise self.failure
                return target

            def _kill_pids_and_wait(self, pids, label, during_exit=None):
                if during_exit is not None:
                    return during_exit()
                return None

        self.controller = Controller()
        with patch.dict('sys.modules', {'sandbox_controller': SimpleNamespace(SandboxController=Controller)}), \
             patch.object(self.module, 'Path', side_effect=lambda p: self.root / p.lstrip('/')), \
             patch.object(self.module.atexit, 'register', side_effect=self.finished.append):
            self.module.install_controller()

    def finish(self):
        with patch.object(self.module, 'Path', side_effect=lambda p: self.root / p.lstrip('/')):
            self.finished[0]()

    def test_marker_failure_does_not_prevent_restore(self):
        with patch.object(self.module.os, 'write', side_effect=OSError('trace failure')):
            self.assertEqual(self.controller.restore_action('target'), 'target')
        self.finish()
        data = json.loads((self.root/'tmp/restore-diagnostics.json').read_text())
        self.assertTrue(any(e['operation'] == 'marker' for e in data['errors']))

    def test_marker_failure_does_not_replace_original_exception(self):
        self.controller.failure = ValueError('original restore failure')
        with patch.object(self.module.os, 'write', side_effect=OSError('trace failure')):
            with self.assertRaisesRegex(ValueError, 'original restore failure'):
                self.controller.restore_action('target')
        self.finish()

    def test_export_failure_still_closes_marker(self):
        original_close = os.close
        with patch.object(Path, 'read_bytes', side_effect=OSError('trace unreadable')), \
             patch.object(self.module.os, 'close', wraps=original_close) as close:
            self.finish()
        close.assert_called_once()
        with self.assertRaises(OSError):
            os.fstat(close.call_args.args[0])
        data = json.loads((self.root/'tmp/restore-diagnostics.json').read_text())
        self.assertTrue(any(e['operation'] == 'export' for e in data['errors']))

    def test_kill_wrapper_forwards_restore_preparation(self):
        self.assertEqual(self.controller._kill_pids_and_wait(
            [10], 'restore', lambda: 'prepared'), 'prepared')
        self.finish()


if __name__ == '__main__':
    unittest.main()
