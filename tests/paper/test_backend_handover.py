"""Shared backend admission and handover receipts; no services or VM launches."""
import ast
from contextlib import contextmanager, nullcontext
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def source(name, candidate=None):
    path = ROOT / 'ae/scripts' / name
    if not path.is_file() and candidate is not None:
        path = ROOT.parent / candidate / 'ae/scripts' / name
    return path


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


hosted = module(source('hosted_launcher.py'), 'handover_launcher_tests')
parallel_source = source('run_cpu_parallel.py', 'placement-candidate')
review_source = source('run_review.py', 'placement-candidate')
parallel = module(parallel_source, 'handover_parallel_tests')


class BackendHandoverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'runtime'
        self.work = self.root / 'ae/work'
        self.work.mkdir(parents=True)
        self.output = self.root / 'ae/results/selected/campaign'
        self.output.mkdir(parents=True)
        self.policy = {'runtime_root': self.root}
        self.command = ['/trusted/python', '-I', '/trusted/run_review.py', '--config',
                        '/trusted/review.json', '--group', 'cpu', '--cpu-parallel',
                        '--cpu-layout', 'numa03', '--output', str(self.output)]
        self.unit = 'deltabox-ae-cpu-' + 'a' * 32 + '.service'

    def write(self, relative, value):
        path = self.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_yield_before_any_inner_admission_needs_no_fabricated_receipt(self):
        self.output.rmdir()
        hosted.verify_background_cleanup(self.policy, self.command)

    def test_cube_context_guard_blocks_shared_handover(self):
        self.write('lanes/numa0/environment/current/cube/RECOVERY_REQUIRED.json', {})
        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
            hosted.verify_background_cleanup(self.policy, self.command)

    def test_global_e2b_guard_blocks_even_when_owned_unit_has_exited(self):
        (self.work / 'E2B_SERVICE_RECOVERY_REQUIRED.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'E2B'):
            hosted.verify_background_cleanup(self.policy, self.command)

    def test_cleanup_error_receipt_blocks_handover(self):
        self.write('lanes/numa3/environment/current/cube-cleanup-error.json', {'error': 'busy mount'})
        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
            hosted.verify_background_cleanup(self.policy, self.command)

    def test_lane_grace_timeout_and_failed_cleanup_steps_block_handover(self):
        for receipt in ({'cpu_lanes': {'0': {'cleanup_timeout': True}}},
                        {'steps': [{'name': 'cube-service-cleanup', 'status': 'failed'}]}):
            with self.subTest(receipt=receipt):
                path = self.write('review.json', receipt)
                with self.assertRaises(RuntimeError):
                    hosted.verify_background_cleanup(self.policy, self.command)
                path.unlink()

    def test_staging_failure_blocks_handover(self):
        self.write('lanes/numa0/runs/table-02-criu/input/staging-cleanup.json', {'status': 'failed'})
        with self.assertRaisesRegex(RuntimeError, 'staging'):
            hosted.verify_background_cleanup(self.policy, self.command)

    def test_actual_failed_queue_prevents_retry_but_does_not_invent_shared_recovery(self):
        self.write('cpu-work-queue.json', {'groups': {'table-02-deltabox': {'status': 'failed'}}})
        with self.assertRaisesRegex(RuntimeError, 'failed experiment'):
            hosted.verify_background_cleanup(self.policy, self.command)
        # Actual workload failure is distinct from unverified backend cleanup.
        hosted.verify_background_cleanup(self.policy, self.command, check_experiment_failure=False)
        self.assertFalse((self.work / 'CPU_SERVICE_RECOVERY_REQUIRED.json').exists())

    def test_interrupted_coverage_and_running_queue_claim_are_not_ordinary_failures(self):
        self.write('review.json', {'status': 'failed', 'steps': [{'name': 'experiment-run', 'status': 'interrupted'}]})
        self.write('cpu-work-queue.json', {'groups': {'table-02-deltabox': {'status': 'running'}}})
        hosted.verify_background_cleanup(self.policy, self.command)

    def test_persistent_recovery_marker_is_created_once_without_replacing_prior_evidence(self):
        hosted.retain_backend_recovery(self.policy, self.command, RuntimeError('owned cleanup failed'))
        path = self.work / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
        original = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn('owned cleanup failed', original.decode())
        hosted.retain_backend_recovery(self.policy, self.command, RuntimeError('later failure'))
        self.assertEqual(path.read_bytes(), original)

    @contextmanager
    def protected_proc_fixture(self, *, membership=None, dead=False):
        # Fixture files keep their real modes/inodes/links; simulate root UID
        # and Linux proc fields on a developer workstation without /proc.
        real_fstat, real_read = os.fstat, Path.read_text
        stat_line = str(os.getpid()) + ' (outer) S 1 ' + '0 ' * 17 + '222\n'
        def root_stat(fd):
            values = list(real_fstat(fd))
            values[4:6] = [0, 0]
            return os.stat_result(values)
        def read(path, *args, **kwargs):
            if str(path) == '/proc/self/cgroup':
                return '0::/system.slice/' + (membership or self.unit) + '\n'
            if str(path) in ('/proc/self/stat', '/proc/' + str(os.getpid()) + '/stat'):
                if dead:
                    raise FileNotFoundError(str(path))
                return stat_line
            return real_read(path, *args, **kwargs)
        with patch.object(os, 'fstat', side_effect=root_stat), patch.object(Path, 'read_text', read):
            yield

    def test_transaction_creation_refuses_recovery_guards_and_existing_transaction(self):
        for name in ('CPU_SERVICE_RECOVERY_REQUIRED.json', 'E2B_SERVICE_RECOVERY_REQUIRED.json'):
            with self.subTest(name=name), self.protected_proc_fixture():
                guard = self.work / name
                guard.write_text('{}')
                with self.assertRaisesRegex(RuntimeError, 'recovery'):
                    hosted.begin_background_transaction(self.policy, self.unit)
                self.assertFalse((self.work / 'CPU_BACKGROUND_TRANSACTION.json').exists())
                guard.unlink()
        with self.protected_proc_fixture():
            first = hosted.begin_background_transaction(self.policy, self.unit)
            path = first[0]
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                hosted.begin_background_transaction(self.policy, self.unit)
            self.assertEqual(path.read_bytes(), original)
            hosted.finish_background_transaction(first, self.unit)

    def test_transaction_finish_requires_full_original_record_and_inode(self):
        with self.protected_proc_fixture():
            transaction = hosted.begin_background_transaction(self.policy, self.unit)
            path, _, original = transaction
            path.write_text(json.dumps(dict(original, start_ticks='wrong')))
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                hosted.finish_background_transaction(transaction, self.unit)
            self.assertTrue(path.exists())
            replacement = path.with_suffix('.replacement')
            replacement.write_text(json.dumps(original))
            replacement.chmod(0o600)
            os.replace(replacement, path)
            with self.assertRaisesRegex(RuntimeError, 'file identity changed'):
                hosted.finish_background_transaction(transaction, self.unit)
            self.assertTrue(path.exists())

    def test_live_background_unit_bypasses_its_transaction_even_with_owned_active_e2b_guard(self):
        review, modules, *_ = self.review_fixture()
        with self.protected_proc_fixture(), patch.dict(sys.modules, modules):
            transaction = hosted.begin_background_transaction(self.policy, self.unit)
            (self.work / 'E2B_SERVICE_RECOVERY_REQUIRED.json').write_text('{}')
            review.assert_backend_recovery(background=True)
            self.assertTrue(transaction[0].exists())

    def test_dead_transaction_is_refused_before_own_unit_bypass(self):
        review, modules, *_ = self.review_fixture()
        with self.protected_proc_fixture():
            hosted.begin_background_transaction(self.policy, self.unit)
        with self.protected_proc_fixture(dead=True), patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'Unfinished background transaction'):
                review.assert_backend_recovery(background=True)

    def test_foreign_unit_prefix_and_default_reviewer_wait_for_transaction_commit(self):
        for membership, background in ((self.unit + '-foreign', True), (self.unit, False)):
            with self.subTest(membership=membership, background=background):
                review, modules, *_ = self.review_fixture()
                with self.protected_proc_fixture(membership=membership), patch.dict(sys.modules, modules):
                    transaction = hosted.begin_background_transaction(self.policy, self.unit)
                    def commit(interval):
                        self.assertEqual(interval, .2)
                        hosted.finish_background_transaction(transaction, self.unit)
                    with patch.object(time, 'sleep', side_effect=commit) as sleep:
                        review.assert_backend_recovery(background=background)
                        sleep.assert_called_once_with(.2)

    def test_transaction_disappearing_between_exists_and_open_is_clean_commit(self):
        review, modules, *_ = self.review_fixture()
        with self.protected_proc_fixture(), patch.dict(sys.modules, modules):
            transaction = hosted.begin_background_transaction(self.policy, self.unit)
            real_open = os.open
            def clearing_open(path, flags, *args, **kwargs):
                if Path(path) == transaction[0] and flags & os.O_ACCMODE == os.O_RDONLY:
                    transaction[0].unlink()
                    raise FileNotFoundError(str(path))
                return real_open(path, flags, *args, **kwargs)
            with patch.object(os, 'open', side_effect=clearing_open):
                review.assert_backend_recovery()

    def test_new_transaction_json_in_progress_retries_then_checks_complete_identity(self):
        review, modules, *_ = self.review_fixture()
        with self.protected_proc_fixture(), patch.dict(sys.modules, modules):
            transaction = hosted.begin_background_transaction(self.policy, self.unit)
            transaction[0].write_text('')
            def publish(*args):
                transaction[0].write_text(json.dumps(transaction[2]))
            with patch.object(time, 'sleep', side_effect=publish) as sleep:
                review.assert_backend_recovery(background=True)
                self.assertGreaterEqual(sleep.call_count, 1)

    def test_persistently_incomplete_transaction_is_rejected_after_bounded_creation_window(self):
        review, modules, *_ = self.review_fixture()
        with self.protected_proc_fixture(), patch.dict(sys.modules, modules):
            transaction = hosted.begin_background_transaction(self.policy, self.unit)
            transaction[0].write_text('')
            with patch.object(time, 'monotonic', side_effect=[0, 0, 3]), \
                 patch.object(time, 'sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'Incomplete background transaction'):
                    review.assert_backend_recovery(background=True)
                sleep.assert_not_called()
            self.assertTrue(transaction[0].exists())

    def test_live_but_expired_transaction_is_rejected_before_own_unit_bypass(self):
        review, modules, *_ = self.review_fixture()
        with self.protected_proc_fixture(), patch.dict(sys.modules, modules):
            transaction = hosted.begin_background_transaction(self.policy, self.unit)
            with patch.object(time, 'monotonic', side_effect=[0, 731]):
                with self.assertRaisesRegex(RuntimeError, 'Unfinished background transaction'):
                    review.assert_backend_recovery(background=True)
            self.assertTrue(transaction[0].exists())

    def test_transaction_gate_refuses_symlinks_hardlinks_and_writable_records(self):
        for kind in ('symlink', 'hardlink', 'writable'):
            with self.subTest(kind=kind), self.protected_proc_fixture():
                review, modules, *_ = self.review_fixture()
                transaction = hosted.begin_background_transaction(self.policy, self.unit)
                path = transaction[0]
                extra = path.with_suffix('.extra')
                if kind == 'symlink':
                    path.rename(extra)
                    path.symlink_to(extra)
                elif kind == 'hardlink':
                    os.link(path, extra)
                else:
                    path.chmod(0o666)
                with patch.dict(sys.modules, modules):
                    with self.assertRaises(OSError if kind == 'symlink' else RuntimeError):
                        review.assert_backend_recovery(background=True)
                path.unlink()
                if extra.exists():
                    extra.unlink()

    def test_transaction_commit_failure_is_audited_as_failure_instead_of_yield_125(self):
        class Client:
            returncode = None
            def wait(self, timeout=None):
                if self.returncode is None:
                    raise subprocess.TimeoutExpired('mock launch client', .5)
                return self.returncode
            def poll(self):
                return self.returncode
            def terminate(self):
                self.returncode = -15
            def kill(self):
                self.returncode = -9
        real_is_file = Path.is_file
        def is_file(path):
            return str(path) == '/sys/fs/cgroup/cgroup.controllers' or real_is_file(path)
        policy = dict(self.policy, python=Path('/trusted/python'))
        stopped = {'LoadState': 'not-found', 'MainPID': '0'}
        with self.protected_proc_fixture(), \
             patch.object(Path, 'is_file', is_file), \
             patch.object(hosted.subprocess, 'check_output', return_value='systemd 249\n'), \
             patch.object(hosted.subprocess, 'Popen', return_value=Client()), \
             patch.object(hosted, 'stop_cpu_service', return_value=stopped), \
             patch.object(hosted, 'finish_background_transaction', side_effect=RuntimeError('transaction identity changed')), \
             patch.object(hosted, 'audit_launch') as audit:
            with self.assertRaisesRegex(RuntimeError, 'transaction identity changed'):
                hosted.run_cpu_service(policy, SimpleNamespace(pw_uid=7001, pw_name='atc-ae'),
                    self.command, {}, yield_requested=lambda: True)
        final = audit.call_args_list[-1].kwargs
        self.assertEqual(final['event'], 'cpu-service-finished')
        self.assertEqual(final['returncode'], 1)
        self.assertFalse(final['yielded_to_reviewer'])
        self.assertIn('transaction identity changed', final['cleanup_error'])
        self.assertTrue((self.work / 'CPU_BACKGROUND_TRANSACTION.json').exists())

    def review_fixture(self, *, cpu_parallel=True):
        # Compile the actual entry/gate bodies without importing optional AE
        # runtime libraries. No implementation text or call order is recreated.
        tree = ast.parse(review_source.read_text())
        definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        review = ModuleType('isolated_handover_review')
        review.__dict__.update(Path=Path, json=json, os=os, stat=stat, re=re, time=time,
            subprocess=subprocess, REPO=self.root,
            sys=SimpleNamespace(platform='linux', modules=sys.modules, stderr=io.StringIO()))
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(review_source), 'exec'), review.__dict__)
        args = hosted.parse_arguments(['--checkout', str(self.root), '--group', 'cpu',
                                      *(['--cpu-parallel'] if cpu_parallel else []), '--output', str(self.output)])
        for key, value in dict(config=self.work / 'review.json', no_pin=False, available=False,
            analyze_existing=None, execute_plan=None, probe_plan=None, publish_output=None,
            experiment_config=[]).items():
            setattr(args, key, value)
        review.parser = Mock(return_value=SimpleNamespace(parse_args=Mock(return_value=args), error=Mock(side_effect=AssertionError)))
        review.validate_gpu_selection = Mock()
        review.load_config = Mock(return_value={})
        review.apply_validation_defaults = Mock()
        review.check_timeout = Mock()
        review.validation_job_limit = Mock(return_value=10)
        review.no_symlink_parents = lambda p: p
        review.output_tree_lock = lambda *a, **k: nullcontext()
        review.Review = Mock(side_effect=RuntimeError('measurement-construction-sentinel'))
        ready = Mock()
        def check_e2b():
            if (self.work / 'E2B_SERVICE_RECOVERY_REQUIRED.json').exists():
                raise RuntimeError('E2B recovery required')
            ready()
        modules = {review.__name__: review,
            'ae.scripts.run_cpu_parallel': parallel,
            'ae.scripts.cube_paper_profile': SimpleNamespace(validate=Mock()),
            'ae.scripts.e2b_paper_profile': SimpleNamespace(validate=Mock()),
            'ae.scripts.e2b_service_context': SimpleNamespace(assert_backend_ready=check_e2b)}
        attempting, entered = threading.Event(), threading.Event()
        lock_path = self.work / '.results.lock'
        @contextmanager
        def run_lock(path, *, wait=False, shared=False, **kwargs):
            self.assertEqual(path, lock_path)
            self.assertFalse(shared)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                attempting.set()
                fcntl.flock(fd, fcntl.LOCK_EX)
                entered.set()
                yield fd
            finally:
                os.close(fd)
        review.run_lock = run_lock
        return review, modules, attempting, entered, lock_path

    def admission(self, *, clear_guard, cpu_parallel=True):
        review, modules, attempting, entered, path = self.review_fixture(cpu_parallel=cpu_parallel)
        guard = self.work / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
        guard.write_text('{}')
        producer = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(producer, fcntl.LOCK_EX)
        result = []
        def call():
            try:
                result.append(review.main([]))
            except BaseException as error:
                result.append(error)
        with patch.object(parallel, 'REPO', self.root), patch.dict(sys.modules, modules):
            thread = threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(attempting.wait(timeout=2))
                self.assertFalse(entered.is_set())
                review.Review.assert_not_called()
                if clear_guard:
                    guard.unlink()  # Producer commits cleanup before releasing EX.
            finally:
                os.close(producer)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertTrue(entered.is_set())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], RuntimeError)
        return review, str(result[0])

    def test_reviewer_waits_for_ex_then_accepts_a_guard_cleared_during_cleanup(self):
        review, error = self.admission(clear_guard=True)
        self.assertEqual(error, 'measurement-construction-sentinel')
        review.Review.assert_called_once()

    def test_cpu_reviewer_checks_retained_guard_after_ex_before_any_measurement(self):
        review, error = self.admission(clear_guard=False)
        self.assertIn('recovery is required', error)
        review.Review.assert_not_called()

    def test_serial_reviewer_checks_retained_guard_after_ex_before_any_measurement(self):
        review, error = self.admission(clear_guard=False, cpu_parallel=False)
        self.assertIn('recovery is required', error)
        review.Review.assert_not_called()


if __name__ == '__main__':
    unittest.main()
