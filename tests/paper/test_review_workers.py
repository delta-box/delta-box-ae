"""Check concurrent Replay lifecycle and real subprocess cancellation."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ae.scripts import run_review as review
from ae.repro.process import execute


class ReplayWorkersTests(unittest.TestCase):
    def test_workers_overlap_and_all_results_are_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = [dict(key=str(i), experiment='table-02-replay') for i in range(4)]
            plan = dict(review_output=str(root/'suite'), workers=2, jobs=jobs)
            path = root/'plan.json'; path.write_text(json.dumps(plan))
            gate = threading.Barrier(2)
            active = 0; peak = 0; mutex = threading.Lock()
            def worker(index, job, plan, output, stop_event=None):
                nonlocal active, peak
                with mutex:
                    active += 1; peak = max(peak, active)
                gate.wait(timeout=3)
                with mutex:
                    active -= 1
                return dict(job, status='ok', result=index)
            with patch.object(review, 'execute_review_job', side_effect=worker), \
                    patch.object(review, 'from_environment', return_value={}), \
                    patch.object(review, 'repository_state', return_value={}), \
                    patch.object(review, 'host_state', return_value={}), \
                    patch.object(review, 'make_output_accessible'):
                self.assertEqual(review.execute_plan(path), 0)
            saved = json.loads((root/'suite/suite.json').read_text())
            self.assertEqual(peak, 2)
            self.assertEqual([j['result'] for j in saved['jobs']], [1,2,3,4])
            self.assertEqual(saved['status'], 'ok')

    def test_cancellation_reaps_running_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stop = threading.Event(); result = []
            pid = root/'pid'
            command = [sys.executable, '-c',
                       "import os,time;open("+repr(str(pid))+",'w').write(str(os.getpid()));time.sleep(60)"]
            thread = threading.Thread(target=lambda: result.append(
                execute(command, root/'logs', cwd=root, timeout=20, stop_event=stop)))
            thread.start()
            deadline = time.monotonic()+5
            while not pid.exists() and time.monotonic()<deadline:
                time.sleep(.02)
            try:
                self.assertTrue(pid.exists())
            finally:
                stop.set();thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0]['status'], 'failed')
            self.assertIn('cancelled', result[0]['error'])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid.read_text()), 0)
