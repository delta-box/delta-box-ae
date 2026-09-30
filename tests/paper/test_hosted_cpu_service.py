"""Unit ownership, no-swap admission, and graceful CPU service cancellation.

All service operations are mocked; these tests never launch measurements or
change the host service manager.
"""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cpu_service_launcher_tests', ROOT / 'ae/scripts/hosted_launcher.py')
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)
UNIT = 'deltabox-ae-cpu-' + 'a' * 32 + '.service'


class CPUServiceTests(unittest.TestCase):
    def setUp(self):
        self.policy = dict(runtime_root=Path('/trusted/runtime'), python=Path('/trusted/venv/bin/python'))
        self.caller = SimpleNamespace(pw_uid=7001, pw_name='atc-ae')
        self.command = [str(self.policy['python']), '-I', '/trusted/runtime/ae/scripts/run_review.py',
                        '--config', '/etc/fixed.json', '--group', 'cpu', '--cpu-parallel',
                        '--output', '/trusted/runtime/ae/results/selected/run']
        self.environment = {'PATH': '/usr/bin', 'E2B_API_KEY': 'secret-must-not-enter-argv'}

    def test_service_boundary_uses_fixed_code_placement_and_no_secret_arguments(self):
        command = hosted.cpu_service_command(self.policy, self.caller, self.command, UNIT)
        for prop in ('MemorySwapMax=0', 'KillMode=mixed', 'KillSignal=SIGINT',
                     'TimeoutStopSec=700', 'SendSIGKILL=yes', 'WorkingDirectory=/trusted/runtime'):
            self.assertIn('--property=' + prop, command)
        self.assertIn('--pipe', command)
        self.assertIn('--wait', command)
        self.assertIn('--collect', command)
        self.assertNotIn('/usr/bin/numactl', command)  # Existing lanes own placement.
        self.assertIn('/trusted/runtime/ae/scripts/hosted_cpu_service.py', command)
        self.assertNotIn('secret-must-not-enter-argv', ' '.join(command))
        self.assertNotIn('--config', command)  # Service re-reads root policy.
        with self.assertRaises(ValueError):
            hosted.cpu_service_command(self.policy, self.caller, self.command, 'cubelet.service')

    def test_background_admission_checks_actual_group_controller_cpu_and_membind(self):
        with tempfile.TemporaryDirectory() as temporary:
            group = Path(temporary)
            (group / 'cpuset.cpus.effective').write_text('0-23,72-95\n')
            (group / 'cpuset.mems.effective').write_text('0,3\n')
            with patch.object(hosted.os, 'sched_getaffinity', return_value={4, 5, 6, 7}), \
                 patch.object(hosted.subprocess, 'check_output', return_value='policy: bind\nmembind: 0\n'):
                identity = hosted.background_cpu_binding(group)
                self.assertEqual(identity['controller_membind'], 0)
                self.assertEqual(identity['cpu_layout'], 'numa03')
                (group / 'cpuset.mems.effective').write_text('0-3\n')
                with self.assertRaisesRegex(ValueError, 'actual NUMA0/3'):
                    hosted.background_cpu_binding(group)
                (group / 'cpuset.mems.effective').write_text('0,3\n')
                (group / 'cpuset.cpus.effective').write_text('0-95\n')
                with self.assertRaisesRegex(ValueError, 'actual NUMA0/3'):
                    hosted.background_cpu_binding(group)
            (group / 'cpuset.cpus.effective').write_text('0-23,72-95\n')
            for actual, policy in [({0, 1, 2, 3}, 'policy: bind\nmembind: 0\n'),
                                   ({4, 5, 6, 7}, 'policy: bind\nmembind: 3\n')]:
                with patch.object(hosted.os, 'sched_getaffinity', return_value=actual), \
                     patch.object(hosted.subprocess, 'check_output', return_value=policy), \
                     self.assertRaisesRegex(ValueError, 'actual NUMA0/3'):
                    hosted.background_cpu_binding(group)

    def test_effective_cgroup_limit_and_unit_membership_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / 'system.slice' / UNIT
            group.mkdir(parents=True)
            limit = group / 'memory.swap.max'
            limit.write_text('0\n')
            membership = root / 'membership'
            membership.write_text('0::/system.slice/' + UNIT + '\n')
            record = hosted.cpu_service_identity(UNIT, cgroup_root=root, membership=membership)
            self.assertEqual(record['memory_swap_max'], '0')
            self.assertEqual(record['cgroup'], str(group))
            limit.write_text('max\n')
            with self.assertRaisesRegex(ValueError, 'must be zero'):
                hosted.cpu_service_identity(UNIT, cgroup_root=root, membership=membership)
            membership.write_text('0::/system.slice/unrelated.service\n')
            with self.assertRaisesRegex(ValueError, 'owned cgroup'):
                hosted.cpu_service_identity(UNIT, cgroup_root=root, membership=membership)

    def supervise(self, first_result, *, yielding=False, stop_error=None, client_error=None, backend_error=None, commit_error=None):
        calls, handlers = [], {}
        class Process:
            returncode = None
            def wait(self, timeout=None):
                calls.append(('wait', timeout))
                if timeout is None or timeout == .5:
                    if first_result == 'term':
                        handlers[signal.SIGTERM](signal.SIGTERM, None)
                    if first_result == 'int':
                        handlers[signal.SIGINT](signal.SIGINT, None)
                    if first_result == 'timeout':
                        raise subprocess.TimeoutExpired('launch-client', 1)
                    if yielding:
                        raise subprocess.TimeoutExpired('launch-client', .5)
                    self.returncode = first_result
                else:
                    self.returncode = -signal.SIGTERM
                return self.returncode
            def poll(self): return self.returncode
            def terminate(self):
                calls.append(('terminate-client',))
                if client_error is not None:
                    raise client_error
            def kill(self): calls.append(('kill-client',))
        def handler(signum, value):
            old = handlers.get(signum, signal.SIG_DFL)
            handlers[signum] = value
            return old
        stopped = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'MainPID': '0'}
        def stop(*args):
            calls.append(('stop-owned-unit', args[0]))
            if stop_error is not None:
                raise stop_error
            return stopped
        with patch.object(hosted.Path, 'is_file', return_value=True),\
             patch.object(hosted.subprocess, 'check_output', return_value='systemd 249\n'),\
             patch.object(hosted.uuid, 'uuid4', return_value=SimpleNamespace(hex='a' * 32)),\
             patch.object(hosted.subprocess, 'Popen', return_value=Process()) as start,\
             patch.object(hosted, 'cpu_unit_state', return_value=stopped),\
             patch.object(hosted, 'stop_cpu_service', side_effect=stop),\
             patch.object(hosted, 'verify_background_cleanup', side_effect=backend_error),\
             patch.object(hosted, 'begin_background_transaction', return_value='fixture-transaction'),\
             patch.object(hosted, 'finish_background_transaction', side_effect=commit_error),\
             patch.object(hosted, 'retain_backend_recovery') as retain,\
             patch.object(hosted.signal, 'signal', side_effect=handler),\
             patch.object(hosted, 'audit_launch') as audit:
            error = None
            try:
                result = hosted.run_cpu_service(self.policy, self.caller, self.command, self.environment,
                    yield_requested=(lambda: True) if yielding else None)
            except BaseException as exc:
                result, error = None, exc
            return result, error, calls, audit.call_args_list, start.call_args

    def test_success_and_failed_workload_return_their_status_without_global_stop(self):
        for expected in (0, 7):
            with self.subTest(expected=expected):
                result, error, calls, audits, start = self.supervise(expected)
                self.assertIsNone(error)
                self.assertEqual(result, expected)
                self.assertFalse(any(row[0] == 'stop-owned-unit' for row in calls))
                self.assertEqual(audits[-1].kwargs['event'], 'cpu-service-finished')
                self.assertEqual(audits[-1].kwargs['returncode'], expected)
                self.assertEqual(start.kwargs['env'], self.environment)

    def test_sigterm_stops_only_owned_unit_and_waits_before_returning_143(self):
        result, error, calls, audits, _ = self.supervise('term')
        self.assertIsNone(error)
        self.assertEqual(result, 143)
        self.assertLess(calls.index(('terminate-client',)), calls.index(('stop-owned-unit', UNIT)))
        self.assertEqual(audits[-1].kwargs['interrupted_signals'], [signal.SIGTERM])
        self.assertEqual(audits[-1].kwargs['unit_state']['MainPID'], '0')

    def test_cooperative_yield_stops_only_owned_unit_before_returning_reserved_125(self):
        result, error, calls, audits, _ = self.supervise(None, yielding=True)
        self.assertIsNone(error)
        self.assertEqual(result, hosted.CPU_REVIEWER_YIELD)
        self.assertEqual([call for call in calls if call[0] == 'stop-owned-unit'], [('stop-owned-unit', UNIT)])
        self.assertLess(calls.index(('terminate-client',)), calls.index(('stop-owned-unit', UNIT)))
        self.assertTrue(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertIsNone(audits[-1].kwargs['cleanup_error'])
        self.assertEqual(audits[-1].kwargs['unit_state']['MainPID'], '0')

    def test_failed_yield_cleanup_is_not_marked_as_success_or_retryable(self):
        result, error, calls, audits, _ = self.supervise(None, yielding=True,
            stop_error=RuntimeError('owned cgroup populated'))
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(audits[-1].kwargs['returncode'], 1)
        self.assertFalse(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertIn('owned cgroup populated', audits[-1].kwargs['cleanup_error'])

    def test_external_backend_restore_failure_cannot_be_a_successful_handover(self):
        result, error, calls, audits, _ = self.supervise(None, yielding=True,
            backend_error=RuntimeError('Cube placement restoration failed'))
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn(('stop-owned-unit', UNIT), calls)
        self.assertEqual(audits[-1].kwargs['returncode'], 1)
        self.assertFalse(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertIn('Cube placement restoration failed', audits[-1].kwargs['cleanup_error'])

    def test_transaction_commit_failure_is_audited_as_failure_and_cannot_yield(self):
        result, error, calls, audits, _ = self.supervise(None, yielding=True,
            commit_error=RuntimeError('Background transaction inode changed'))
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(audits[-1].kwargs['returncode'], 1)
        self.assertFalse(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertIn('transaction inode changed', audits[-1].kwargs['cleanup_error'])

    def test_launch_client_termination_error_still_stops_owned_unit_and_cannot_yield(self):
        result, error, calls, audits, _ = self.supervise(None, yielding=True,
            client_error=OSError('launch client disappeared'))
        self.assertIsNone(result)
        self.assertIsInstance(error, OSError)
        self.assertIn(('stop-owned-unit', UNIT), calls)
        self.assertEqual(audits[-1].kwargs['returncode'], 1)
        self.assertFalse(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertIn('launch client disappeared', audits[-1].kwargs['cleanup_error'])

    def test_workload_125_is_failure_and_cannot_masquerade_as_cooperative_yield(self):
        result, error, calls, audits, _ = self.supervise(125)
        self.assertIsNone(error)
        self.assertEqual(result, 1)
        self.assertEqual(audits[-1].kwargs['workload_returncode'], 125)
        self.assertFalse(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertFalse(any(call[0] == 'stop-owned-unit' for call in calls))

    def test_sigint_is_not_retried_and_owned_cleanup_finishes_before_return_130(self):
        result, error, calls, audits, _ = self.supervise('int')
        self.assertIsNone(error)
        self.assertEqual(result, 130)
        self.assertEqual([call for call in calls if call[0] == 'stop-owned-unit'], [('stop-owned-unit', UNIT)])
        self.assertFalse(audits[-1].kwargs['yielded_to_reviewer'])
        self.assertEqual(audits[-1].kwargs['interrupted_signals'], [signal.SIGINT])

    def test_layout03_service_boundary_excludes_reviewer_cpu_and_memory_nodes(self):
        command = hosted.cpu_service_command(self.policy, self.caller,
            self.command + ['--cpu-layout', 'numa03'], UNIT)
        expected = {'AllowedCPUs': '0-23 72-95', 'AllowedMemoryNodes': '0 3',
                    'CPUAffinity': '4-7', 'NUMAPolicy': 'bind', 'NUMAMask': '0'}
        for key, value in expected.items():
            self.assertIn('--property=' + key + '=' + value, command)
        helper_args = command[command.index('--caller-uid') + 2:]
        self.assertEqual(helper_args[helper_args.index('--cpu-layout') + 1], 'numa03')
        self.assertNotIn('/usr/bin/numactl', command)
        self.assertNotIn('secret-must-not-enter-argv', ' '.join(command))

    def test_default_layout_and_unknown_layout_fail_closed(self):
        self.assertEqual(hosted.cpu_command_layout(self.command), 'numa12')
        for layout in ('numa13', '0,3', 'unbound'):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                hosted.cpu_service_command(self.policy, self.caller,
                    self.command + ['--cpu-layout', layout], UNIT)

    def test_client_timeout_still_stops_owned_service_before_propagating_failure(self):
        result, error, calls, audits, _ = self.supervise('timeout')
        self.assertIsNone(result)
        self.assertIsInstance(error, subprocess.TimeoutExpired)
        self.assertIn(('stop-owned-unit', UNIT), calls)
        self.assertEqual(audits[-1].kwargs['returncode'], 1)

    def test_cleanup_failure_after_success_is_audited_as_failure(self):
        with patch.object(hosted, 'verify_cpu_service_empty', side_effect=RuntimeError('owned cgroup populated')):
            result, error, calls, audits, _ = self.supervise(0)
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn(('stop-owned-unit', UNIT), calls)
        self.assertEqual(audits[-1].kwargs['returncode'], 1)
        self.assertIn('owned cgroup populated', audits[-1].kwargs['cleanup_error'])

    def test_stop_requires_inactive_mainpid_zero_after_grace(self):
        with patch.object(hosted.subprocess, 'run', return_value=SimpleNamespace(returncode=0)),\
             patch.object(hosted, 'cpu_unit_state', return_value={'LoadState': 'loaded', 'ActiveState': 'active', 'MainPID': '19'}):
            with self.assertRaisesRegex(RuntimeError, 'did not stop'):
                hosted.stop_cpu_service(UNIT, self.environment)
        with patch.object(hosted.subprocess, 'run') as stop:
            with self.assertRaises(ValueError):
                hosted.stop_cpu_service('cubelet.service', self.environment)
            stop.assert_not_called()

    def test_mainpid_zero_does_not_hide_a_populated_owned_cgroup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / 'system.slice' / UNIT
            group.mkdir(parents=True)
            (group / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
            state = {'LoadState': 'loaded', 'ActiveState': 'failed', 'MainPID': '0',
                     'ControlGroup': '/system.slice/' + UNIT}
            with self.assertRaisesRegex(RuntimeError, 'still contains processes'):
                hosted.verify_cpu_service_empty(UNIT, state, cgroup_root=root)
            (group / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
            self.assertEqual(hosted.verify_cpu_service_empty(UNIT, state, cgroup_root=root)['cgroup_populated'], '0')
            with self.assertRaisesRegex(ValueError, 'unexpected cgroup'):
                hosted.verify_cpu_service_empty(UNIT, dict(state, ControlGroup='/other/service'), cgroup_root=root)

    def test_only_collected_verified_empty_owned_cgroup_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group = root / 'system.slice' / UNIT
            group.mkdir(parents=True)
            (group / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
            state = {'LoadState': 'not-found', 'MainPID': '0'}
            with patch.object(hosted.Path, 'rmdir', autospec=True) as remove:
                record = hosted.verify_cpu_service_empty(UNIT, state, cgroup_root=root)
                remove.assert_called_once_with(group)
                self.assertTrue(record['cgroup_absent'])
                self.assertTrue(record['empty_cgroup_removed'])
            with patch.object(hosted.Path, 'rmdir', side_effect=FileNotFoundError):
                record = hosted.verify_cpu_service_empty(UNIT, state, cgroup_root=root)
                self.assertTrue(record['cgroup_absent'])
                self.assertFalse(record['empty_cgroup_removed'])
            with patch.object(hosted.Path, 'rmdir', side_effect=PermissionError('owned group busy')):
                with self.assertRaises(PermissionError):
                    hosted.verify_cpu_service_empty(UNIT, state, cgroup_root=root)
            with patch.object(hosted.Path, 'rmdir') as remove:
                hosted.verify_cpu_service_empty(UNIT, dict(state, LoadState='loaded'), cgroup_root=root)
                remove.assert_not_called()

    def test_systemd_version_has_no_unguarded_fallback(self):
        with patch.object(hosted.Path, 'is_file', return_value=True),\
             patch.object(hosted.subprocess, 'check_output', return_value='systemd 239\n'),\
             patch.object(hosted.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(ValueError, '240'):
                hosted.run_cpu_service(self.policy, self.caller, self.command, self.environment)
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
