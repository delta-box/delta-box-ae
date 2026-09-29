"""Real process tests for lane concurrency, inherited leases and peer cleanup."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from ae.scripts import run_review as review
from ae.scripts import run_cpu_parallel as parallel


class TwoLaneTests(unittest.TestCase):
    def test_partition_is_exhaustive_disjoint_and_services_share_one_lane(self):
        groups = parallel.partition(review.CPU_EXPERIMENTS)
        self.assertEqual(set(groups), {0, 1})
        self.assertFalse(set(groups[0]) & set(groups[1]))
        self.assertEqual(set(groups[0] + groups[1]), set(review.CPU_EXPERIMENTS))
        for name in parallel.BASELINES:
            self.assertIn(name, groups[1])
        self.assertNotIn(review.GPU, groups[0] + groups[1])

    def test_parallel_refuses_placement_gpu_and_internal_overrides(self):
        parallel.validate(review.parser().parse_args(['--group', 'cpu']))
        for flags in (['--numa-node', '2', '--cpus', '48-51'], ['--group', 'gpu'],
                      ['--experiment', 'figure-08-gpu'], ['--all'], ['--test'],
                      ['--no-pin'], ['--available'], ['--execute-plan', 'x'],
                      ['--experiment-config', 'table-02-replay=x']):
            with self.subTest(flags=flags):
                args = review.parser().parse_args(['--group', 'cpu', *flags])
                with self.assertRaises(ValueError):
                    parallel.validate(args)

    def test_lane_arguments_preserve_limits_and_fix_placement(self):
        args = review.parser().parse_args(['--group', 'cpu', '--limit', '2', '--max-events', '4'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for node, names in parallel.partition(review.CPU_EXPERIMENTS).items():
                flags = parallel.lane_arguments(args, node, root, names)
                lane = review.parser().parse_args(flags)
                self.assertEqual((lane.numa_node, lane.cpus), (node, parallel.PLACEMENT[node]))
                self.assertEqual((lane.limit, lane.max_events), (2, 4))
                self.assertEqual(lane.experiment, names)
                self.assertEqual(lane.output, root / 'lanes' / ('numa' + str(node)))

    def test_inherited_lease_rejects_wrong_file_and_stays_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lease'
            with path.open('w') as handle, (Path(directory) / 'wrong').open('w') as wrong:
                fcntl.flock(handle, fcntl.LOCK_EX)
                parallel.inherited_lease(handle.fileno(), path)
                with self.assertRaises(ValueError):
                    parallel.inherited_lease(wrong.fileno(), path)
                code = 'import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
                proc = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
                self.assertNotEqual(proc.returncode, 0)

    def run_processes(self, programs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / 'lease').open('w') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                commands = {n: [sys.executable, '-c', source, str(root), str(n), str(handle.fileno())]
                            for n, source in programs.items()}
                updates = []
                rows = parallel.launch_lanes(commands, root / 'control', handle.fileno(),
                                            lambda x: updates.append(json.loads(json.dumps(x))), poll_interval=0.01)
            return rows, updates, {p.name: p.read_text() for p in root.glob('*.txt')}

    def test_two_children_are_live_together_and_inherit_the_lease(self):
        source = """import os,sys,time
from pathlib import Path
root=Path(sys.argv[1]);n=int(sys.argv[2]);os.fstat(int(sys.argv[3]))
(root/(str(n)+'.txt')).write_text('started')
deadline=time.monotonic()+5
while not (root/(str(1-n)+'.txt')).exists():
 if time.monotonic()>deadline: raise SystemExit(5)
 time.sleep(.01)
"""
        rows, updates, files = self.run_processes({0: source, 1: source})
        self.assertTrue(all(r['status'] == 'ok' for r in rows.values()))
        self.assertEqual(set(files), {'0.txt', '1.txt'})
        self.assertTrue(any(all(r['status'] == 'running' for r in snapshot.values()) for snapshot in updates))

    def test_failed_lane_cancels_owned_peer_and_waits_for_cleanup(self):
        failure = """import sys,time
from pathlib import Path
r=Path(sys.argv[1]);end=time.monotonic()+5
while not (r/'ready.txt').exists():
 if time.monotonic()>end:raise SystemExit(8)
 time.sleep(.01)
raise SystemExit(3)
"""
        peer = """import sys,time,signal
from pathlib import Path
r=Path(sys.argv[1])
def stop(*a):
 (r/'cleaned.txt').write_text('cleanup completed');raise SystemExit(130)
signal.signal(signal.SIGTERM,stop)
(r/'ready.txt').write_text('ready')
while True:time.sleep(.01)
"""
        rows, _, files = self.run_processes({0: failure, 1: peer})
        self.assertEqual(rows[0]['returncode'], 3)
        self.assertEqual(rows[1]['status'], 'cancelled')
        self.assertEqual(files['cleaned.txt'], 'cleanup completed')
        self.assertFalse(any(r.get('cleanup_timeout') for r in rows.values()))

    def test_nested_lane_inputs_remain_discoverable_for_combined_analysis(self):
        from ae.repro.analysis import Evidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for n in [0, 1]:
                p = root / ('numa' + str(n)) / 'runs' / 'example' / 'run.json'
                p.parent.mkdir(parents=True);p.write_text('{}')
            self.assertEqual(len(list(Evidence(root, 'fresh').glob('**/run.json'))), 2)


if __name__ == '__main__':
    unittest.main()
