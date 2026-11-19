import importlib.util
from pathlib import Path
import tempfile
import unittest

P = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('continuous_timing', P / 'vendor/finalbench/fc_diff_dm/continuous_timing.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class TimingTest(unittest.TestCase):
    def execute(self, mode='diff', cleanup=True, ready_ok=True, chain=True):
        class Clock:
            now = 10_000_000_000  # Earlier cache preparation must not enter result.
            def __call__(self):
                self.now += 1_000_000  # Instrumentation/dispatch gaps are visible.
                return self.now
            def advance(self, ms):
                self.now += ms * 1_000_000
        clock = Clock()
        calls = []
        class VM:
            def kill(self):
                calls.append('kill'); clock.advance(5)
            def load_snapshot(self, **kwargs):
                calls.append(('load', kwargs)); clock.advance(70)
                return {'fc_kill_inside_load_ms': 2, 'fc_spawn_ms': 50, 'fc_load_ms': 18}
        class DM:
            def restore_dev(self, label):
                calls.append(('dm', label)); clock.advance(9)
                return {'dm_restore_ms': 9}
        def merge(base, diffs, out):
            calls.append(('merge', diffs)); clock.advance(80); out.write_bytes(b'pages')
            return {'merge_ms': 60}  # Deliberately omit wrapper time from phase report.
        def ready(**kwargs):
            clock.advance(30)
            return {'ok': ready_ok, 'node_id': 0}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            target = {'seq': 0, 'diff_chain': ['diff.mem'] if chain else [],
                      'mem': str(p/'base.mem'), 'vmstate': str(p/'base.vmstate')}
            result = m.restore_operation(VM(), DM(), target, p/'base.mem', p,
                                         mode, 2, merge, ready, cleanup, clock)
            remains = (p/'merged_pre_seq2_target0.mem').exists()
        return result, calls, remains

    def test_continuous_includes_unreported_merge_and_gaps(self):
        r, calls, remains = self.execute()
        reported = r['vm_kill_ms'] + 60 + 9 + 50 + 18 + r['first_rpc_ms']
        self.assertGreater(r['restore_total_ms'], reported)
        self.assertEqual(r['restore_total_ms'], (r['restore_end_ns']-r['restore_start_ns'])/1e6)
        self.assertGreater(r['unattributed_ms'], 0)
        self.assertLess(r['restore_total_ms'], 1000)
        self.assertFalse(remains)
        self.assertEqual(calls.count('kill'), 1)

    def test_load_internal_kill_not_added_twice(self):
        r, _, _ = self.execute()
        self.assertEqual(r['continuous_intervals']['load']['ms'], 71)
        self.assertEqual(r['load']['fc_kill_inside_load_ms'], 2)
        self.assertEqual(r['restore_total_ms'], (r['restore_end_ns']-r['restore_start_ns'])/1e6)

    def test_ready_and_complete_endpoints_are_preserved(self):
        r, _, _ = self.execute()
        self.assertGreater(r['restore_total_ms'], r['restore_ready_ms'])
        self.assertEqual(r['restore_total_ms']-r['restore_ready_ms'], r['post_ready_ms'])

    def test_empty_diff_chain_skips_merge_and_unlink(self):
        r, calls, _ = self.execute(chain=False)
        loaded = next(c[1] for c in calls if isinstance(c, tuple) and c[0]=='load')
        self.assertEqual(loaded['mem_path'].name, 'base.mem')
        self.assertTrue(loaded['enable_diff'])
        self.assertFalse(any(isinstance(c, tuple) and c[0]=='merge' for c in calls))
        self.assertNotIn('merge', r['continuous_intervals'])

    def test_no_cleanup_preserves_reconstruction(self):
        _, _, remains = self.execute(cleanup=False)
        self.assertTrue(remains)

    def test_full_mode_does_not_merge(self):
        r, calls, _ = self.execute(mode='full')
        self.assertEqual(r['merge']['merge_ms'], 0)
        self.assertFalse(any(isinstance(c, tuple) and c[0]=='merge' for c in calls))

    def test_nonready_response_is_not_a_valid_sample(self):
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            self.execute(ready_ok=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
