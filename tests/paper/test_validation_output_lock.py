"""Output leases protect evidence before resume can inspect or move live jobs."""
import contextlib
import io
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ae.repro import result_storage as storage
from ae.scripts import run_review as review


def probe_output(work, root, output, quick, queue):
    try:
        with storage.output_tree_lock(work, root, output, quick=quick):
            queue.put('ok')
    except ValueError:
        queue.put('blocked')


class ValidationOutputLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.root = self.repo / 'ae/results'
        self.work = self.repo / 'ae/work'
        self.ctx = multiprocessing.get_context('spawn')

    def probe(self, output, *, quick=False):
        queue = self.ctx.Queue()
        process = self.ctx.Process(target=probe_output, args=(
            self.work, self.root, output, quick, queue))
        process.start()
        try:
            result = queue.get(timeout=10)
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
            return result
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            queue.close()

    def test_same_output_and_both_ancestor_orders_exclude_other_writers(self):
        parent = self.root / 'selected/pilot'
        child = parent / 'nested'
        for owner, contender in [(parent, parent), (parent, child), (child, parent)]:
            with self.subTest(owner=owner, contender=contender):
                with storage.output_tree_lock(self.work, self.root, owner):
                    self.assertEqual(self.probe(contender), 'blocked')
                self.assertEqual(self.probe(contender), 'ok')

    def test_siblings_can_run_but_full_results_writer_conflicts(self):
        with storage.output_tree_lock(self.work, self.root, self.root / 'selected/a'):
            self.assertEqual(self.probe(self.root / 'selected/b'), 'ok')
            self.assertEqual(self.probe(self.root), 'blocked')
        with storage.output_tree_lock(self.work, self.root, self.root):
            self.assertEqual(self.probe(self.root / 'selected/a'), 'blocked')

    def test_only_reserved_quick_subtree_can_coexist_with_main_root(self):
        quick = self.root / 'checks/quick'
        with storage.output_tree_lock(self.work, self.root, self.root):
            self.assertEqual(self.probe(quick, quick=True), 'ok')
        with storage.output_tree_lock(self.work, self.root, quick, quick=True):
            self.assertEqual(self.probe(self.root), 'ok')
            self.assertEqual(self.probe(quick, quick=True), 'blocked')
        self.assertEqual(self.probe(quick), 'blocked')
        self.assertEqual(self.probe(self.root / 'checks', quick=True), 'blocked')

    def test_rotation_excludes_isolated_output_until_it_finishes(self):
        gate = self.work / '.results.lock'
        with storage.run_lock(gate, shared=True):
            with storage.output_tree_lock(self.work, self.root, self.root / 'selected/a'):
                with self.assertRaises(ValueError):
                    with storage.run_lock(gate):
                        pass
        with storage.run_lock(gate):
            with self.assertRaises(ValueError):
                with storage.run_lock(gate, shared=True):
                    pass

    def test_ordinary_and_isolated_resume_conflict_before_review_or_history(self):
        config = {'review': {'parallel_quick_check': True, 'validation_max_jobs': 10},
                  'measurement': {'pin': True, 'numa_node': 0, 'cpus': '0-3'}}
        base = ['--experiment', 'figure-06-adaptive', '--limit', '1',
                '--numa-node', '0', '--cpus', '0-3']
        for outer_isolated in (False, True):
            with self.subTest(outer_isolated=outer_isolated):
                output = self.root / 'selected' / str(outer_isolated)
                outer = base + ['--output', str(output)]
                inner = base + ['--resume', str(output)]
                (outer if outer_isolated else inner).append('--isolated-validation')
                constructed = []
                test = self

                class FakeReview:
                    def __init__(self, *args):
                        constructed.append(args)
                        test.assertEqual(len(constructed), 1,
                                         'Contending resume loaded live evidence')
                        self.record = {'status': 'ok'}

                    def print_summary(self):
                        pass

                    def run(self):
                        (output / 'review.json').write_text('active evidence')
                        test.assertEqual(review.main(inner), 2)
                        test.assertEqual((output / 'review.json').read_text(), 'active evidence')
                        test.assertFalse((output / 'attempt-history').exists())
                        return 0

                with patch.object(review, 'REPO', self.repo), \
                        patch.object(review, 'load_config', return_value=config), \
                        patch.object(review, 'Review', FakeReview), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(review.main(outer), 0)
                self.assertEqual(len(constructed), 1)

    def test_isolated_cannot_enter_quick_results(self):
        args = review.parser().parse_args([
            '--isolated-validation', '--experiment', 'figure-06-adaptive',
            '--limit', '1', '--numa-node', '0', '--cpus', '0-3',
            '--output', str(self.root / 'checks/quick')])
        with patch.object(review, 'REPO', self.repo), self.assertRaises(ValueError):
            review.isolated_validation_output(args, {'review': {'validation_max_jobs': 10}})


if __name__ == '__main__':
    unittest.main()
