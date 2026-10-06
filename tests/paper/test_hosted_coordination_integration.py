"""Exercise real admission/cleanup functions against separate checkout fixtures."""
import fcntl
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ae.repro.coordination import coordination_root
from ae.scripts import run_review, run_cpu_parallel, run_memory_job, run_nvme_job
from ae.runners import vm

ROOT = Path(__file__).resolve().parents[2]


class SharedAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'a12-repo'
        self.local = self.repo / 'ae/work'
        self.local.mkdir(parents=True)
        self.canonical = self.root / 'existing-repo/ae/work'
        self.canonical.mkdir(parents=True)
        env = patch.dict(os.environ, {'AE_HOSTED_COORDINATION_ROOT': str(self.canonical),
                                     'AE_HOSTED_CALLER_UID': str(os.getuid())})
        env.start()
        self.addCleanup(env.stop)

    def test_memory_and_nvme_cleanup_failures_publish_shared_gate_keep_local_receipt(self):
        cases = [(run_memory_job, {'suite': 'owned-suite', 'mounts': []}),
                 (run_nvme_job, {'work_root': 'owned-work', 'work_identity': {'inode': 12}})]
        guard = self.canonical / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
        for module, meta in cases:
            with self.subTest(module=module.__name__), patch.object(module, 'ROOT', self.repo):
                receipt = Path(module.retain_recovery(meta, ['simulated cleanup failure']))
                self.assertEqual(receipt.parent, self.local)
                self.assertEqual(json.loads(guard.read_text())['receipt'], str(receipt))
                self.assertFalse((self.local / guard.name).exists())
                with patch.object(run_review, 'REPO', self.repo):
                    with self.assertRaisesRegex(RuntimeError, 'Shared backend recovery'):
                        run_review.assert_backend_recovery()
                previous = guard.read_bytes(), guard.stat().st_ino
                module.retain_recovery(meta, ['second failure'])
                self.assertEqual((guard.read_bytes(), guard.stat().st_ino), previous)
                guard.unlink()

    def test_vm_rootfs_failure_publishes_same_shared_gate(self):
        args = SimpleNamespace(log=self.local / 'vm.log')
        evidence = {'loop': '/dev/fixture', 'image_device': 4, 'image_inode': 8}
        with patch.object(vm, 'REPO_ROOT', self.repo):
            vm._retain_rootfs_cleanup(args, evidence)
        guard = self.canonical / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
        self.assertEqual(json.loads(guard.read_text())['image_inode'], 8)
        self.assertTrue(Path(evidence['receipt']).is_relative_to(self.local))
        self.assertFalse((self.local / guard.name).exists())
        with patch.object(run_review, 'REPO', self.repo):
            with self.assertRaisesRegex(RuntimeError, 'Shared backend recovery'):
                run_review.assert_backend_recovery()

    def e2b_module(self):
        spec = importlib.util.spec_from_file_location('shared_e2b_fixture', ROOT / 'ae/scripts/e2b_service_context.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_e2b_admission_reads_shared_recovery_guard(self):
        module = self.e2b_module()
        guard = self.canonical / 'E2B_SERVICE_RECOVERY_REQUIRED.json'
        guard.write_text('{"reason": "failed restoration"}')
        self.assertEqual(module.GUARD, guard)
        with self.assertRaisesRegex(RuntimeError, 'Earlier E2B service recovery'):
            module.assert_backend_ready()
        guard.unlink()
        module.assert_backend_ready()

    def test_parallel_quick_main_gate_uses_shared_coordination_directory(self):
        results = self.repo / 'ae/results'
        results.mkdir()
        observed = []
        @contextmanager
        def gate(path, **kwargs):
            observed.append((path, kwargs))
            yield 17
        with patch.object(run_review, 'REPO', self.repo), \
             patch.object(run_review.sys, 'platform', 'linux'), \
             patch.object(run_review, 'load_config', return_value={'review': {'parallel_quick_check': True}}), \
             patch.object(run_review, 'pin_requested', return_value=True), \
             patch.object(run_review, 'parallel_run_locks', side_effect=gate), \
             patch.object(run_review, 'run_selected', return_value=0):
            self.assertEqual(run_review.main(['--experiment', 'correctness', '--output', str(results / 'selected/check')]), 0)
        self.assertEqual(observed, [(self.canonical, {'quick': False, 'rotate': False})])

    def test_parallel_lease_keeps_canonical_inode_and_rejects_local_inode(self):
        canonical = self.canonical / '.results.lock'
        local = self.local / '.results.lock'
        canonical.touch()
        local.touch()
        with canonical.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            run_cpu_parallel.inherited_lease(stream.fileno(), coordination_root(self.repo) / '.results.lock')
            with self.assertRaisesRegex(ValueError, 'did not inherit'):
                run_cpu_parallel.inherited_lease(stream.fileno(), local)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux /proc/locks')
    def test_e2b_ancestor_proof_matches_real_shared_write_flock(self):
        module = self.e2b_module()
        canonical = self.canonical / '.results.lock'
        canonical.touch()
        with canonical.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.assertEqual(module.require_results_lease(), os.getpid())
        with self.assertRaisesRegex(ValueError, 'ancestor results EX lease'):
            module.require_results_lease()


if __name__ == '__main__':
    unittest.main()
