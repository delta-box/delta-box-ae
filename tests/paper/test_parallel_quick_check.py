"""Real cross-process admission and resource exclusion for the quick lane."""
import contextlib
import fcntl
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from ae.repro import result_storage as storage
from ae.scripts import hosted_launcher as launcher
from ae.scripts import run_pinned_measurement as pinned
from ae.scripts import run_review as review


def child_probe(work, quick, rotate, queue):
    try:
        with storage.parallel_run_locks(Path(work), quick=quick, rotate=rotate):
            queue.put('ok')
    except ValueError:
        queue.put('blocked')


class ParallelQuickTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.ctx = multiprocessing.get_context('spawn')

    def probe(self, quick, rotate=False):
        queue = self.ctx.Queue()
        process = self.ctx.Process(target=child_probe, args=(str(self.work), quick, rotate, queue))
        process.start()
        result = queue.get(timeout=10)
        process.join(timeout=10)
        self.assertEqual(process.exitcode, 0)
        return result

    def test_quick_and_main_coexist_but_second_main_is_rejected(self):
        with storage.parallel_run_locks(self.work, quick=True, rotate=False):
            self.assertEqual(self.probe(False), 'ok')
            self.assertEqual(self.probe(True), 'blocked')
        with storage.parallel_run_locks(self.work, quick=False, rotate=False):
            self.assertEqual(self.probe(False), 'blocked')
            self.assertEqual(self.probe(True), 'ok')

    def test_rotation_refused_while_quick_is_active(self):
        with storage.parallel_run_locks(self.work, quick=True, rotate=False):
            self.assertEqual(self.probe(False, True), 'blocked')

    def test_rotation_downgrade_admits_quick_without_releasing_main_lane(self):
        with storage.parallel_run_locks(self.work, quick=False, rotate=True) as gate:
            with self.assertRaises(ValueError):
                with storage.run_lock(self.work / '.results.lock', shared=True):
                    pass
            storage.measurement_phase(gate)
            self.assertEqual(self.probe(True), 'ok')
            self.assertEqual(self.probe(False), 'blocked')
            with self.assertRaises(ValueError):
                with storage.run_lock(self.work / '.results.lock'):
                    pass

    def test_main_and_quick_namespaces_cannot_overlap(self):
        root = self.root / 'results'
        storage.parallel_output(root, root, quick=False)
        storage.parallel_output(root, root / 'checks/check-1', quick=True)
        for output, quick in [(root, True), (root / 'runs/check', True),
                              (root / 'checks', True), (root / 'checks/check-1', False)]:
            with self.subTest(output=output, quick=quick), self.assertRaises(ValueError):
                storage.parallel_output(root, output, quick=quick)
        (self.root / 'alias').symlink_to(root)
        with self.assertRaises(ValueError):
            storage.parallel_output(root, self.root / 'alias/checks/evil', quick=True)

    def test_node_locks_isolate_nodes_and_release_after_exit(self):
        with pinned.acquire_node_lock(1, timeout=.1, root=self.root):
            with self.assertRaises(TimeoutError):
                pinned.acquire_node_lock(1, timeout=.01, root=self.root)
            with pinned.acquire_node_lock(3, timeout=.1, root=self.root):
                pass
        with pinned.acquire_node_lock(1, timeout=.1, root=self.root):
            pass

    def test_node_lock_rejects_symlink_and_hardlink(self):
        target = self.root / 'file'
        target.write_text('keep')
        (self.root / 'deltabox-numa-3.lock').symlink_to(target)
        with self.assertRaises(OSError):
            pinned.acquire_node_lock(3, timeout=.1, root=self.root)
        os.link(target, self.root / 'deltabox-numa-2.lock')
        with self.assertRaises(ValueError):
            pinned.acquire_node_lock(2, timeout=.1, root=self.root)
        self.assertEqual(target.read_text(), 'keep')

    def test_launcher_allows_only_paired_quick_placement(self):
        base = ['--checkout', '/repo']
        args = launcher.parse_arguments(base + ['--test', '--numa-node', '3', '--cpus', '88-91'])
        command = launcher.command_line(dict(python=Path('/python'), runtime_root=Path('/repo'),
                                             config=Path('/config')), args, Path('/results/checks/q'))
        self.assertIn('--numa-node', command)
        self.assertIn('88-91', command)
        for flags in [['--numa-node', '3', '--cpus', '88-91'], ['--test', '--numa-node', '3'],
                      ['--test', '--numa-node', '-1', '--cpus', '88-91'],
                      ['--test', '--numa-node', '3', '--cpus', '../x']]:
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                launcher.parse_arguments(base + flags)

    def test_configured_quick_placement_and_cube_conflict(self):
        config = {'review': {'parallel_quick_check': True,
                             'quick_check_measurement': {'numa_node': 3, 'cpus': '88-91'}},
                  'measurement': {'pin': True, 'numa_node': 1, 'cpus': '28-31'},
                  'cube': {'manage_memory_service': True}}
        with patch.object(review, 'REPO', self.root), patch.object(review, 'load_config', return_value=config), \
                patch.object(review, 'run_selected', return_value=0) as run:
            self.assertEqual(review.main(['--test']), 0)
            args = run.call_args.args[0]
            self.assertEqual((args.numa_node, args.cpus), (3, '88-91'))
            run.reset_mock()
            self.assertEqual(review.main(['--test', '--numa-node', '1', '--cpus', '28-31']), 2)
            run.assert_not_called()

    def test_unpinned_parallel_attempt_is_rejected(self):
        with patch.object(review, 'REPO', self.root), \
                patch.object(review, 'load_config', return_value={'review': {'parallel_quick_check': True}}), \
                patch.object(review, 'run_selected') as run:
            self.assertEqual(review.main(['--test', '--no-pin']), 2)
            run.assert_not_called()

    def test_full_publication_does_not_visit_active_quick_results(self):
        root = self.root / 'ae/results'
        quick = root / 'checks/quick'
        quick.mkdir(parents=True)
        payload = quick / 'active'
        payload.write_text('other lane')
        payload.chmod(0o600)
        (root / 'SUMMARY.md').write_text('full run')
        before = payload.stat()
        with patch.object(review, 'REPO', self.root):
            review.make_output_accessible(root)
        after = payload.stat()
        self.assertEqual((before.st_mode, before.st_uid, before.st_gid, before.st_ctime_ns),
                         (after.st_mode, after.st_uid, after.st_gid, after.st_ctime_ns))


if __name__ == '__main__':
    unittest.main()
