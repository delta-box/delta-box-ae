"""Real flock/exec tests; no services, CPU binding, swap or measurements change."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'ae/scripts/hosted_cpu_priority.py'
spec = importlib.util.spec_from_file_location('reviewer_priority_tests', SOURCE)
priority = importlib.util.module_from_spec(spec)
spec.loader.exec_module(priority)

HOLD = r'''
import importlib.util,os,sys,time
from pathlib import Path
source,lock,ready,release=sys.argv[1:]
spec=importlib.util.spec_from_file_location('priority_child',source)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
# Fixtures retain actual filesystem ownership. Production keeps UID 0.
module.LOCK_OWNER_UID=os.geteuid()
with module.reviewer_lease(Path(lock)):
 Path(ready).write_text('leased')
 deadline=time.monotonic()+10
 while not Path(release).exists():
  if time.monotonic()>deadline:raise SystemExit(9)
  time.sleep(.01)
'''

INHERITED = r'''
import os,sys,time
from pathlib import Path
fd=int(sys.argv[1]);os.fstat(fd)
Path(sys.argv[2]).write_text('inherited')
deadline=time.monotonic()+10
while not Path(sys.argv[3]).exists():
 if time.monotonic()>deadline:raise SystemExit(9)
 time.sleep(.01)
os.close(fd)
'''


class ReviewerPriorityTests(unittest.TestCase):
    def setUp(self):
        # A root lock fixture must not inherit a reviewer-owned TMPDIR parent.
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp' if os.geteuid() == 0 else None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.lock = self.root / 'priority.lock'
        self.owner = patch.object(priority, 'LOCK_OWNER_UID', os.geteuid())
        self.owner.start()
        self.addCleanup(self.owner.stop)

    def wait_ready(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None:
                self.fail('Reviewer exited before leasing: ' + str(process.returncode))
            if time.monotonic() > deadline:
                self.fail('Reviewer did not acquire its shared lease')
            time.sleep(.01)

    def hold(self, name):
        ready, release = self.root / (name + '.ready'), self.root / (name + '.release')
        process = subprocess.Popen([sys.executable, '-c', HOLD, str(SOURCE), str(self.lock), str(ready), str(release)])
        def cleanup():
            release.touch()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        self.addCleanup(cleanup)
        self.wait_ready(ready, process)
        return process, release

    def test_two_reviewers_share_intent_and_background_probe_never_reserves_it(self):
        self.assertFalse(priority.reviewers_active(self.lock))
        first, release_first = self.hold('first')
        second, release_second = self.hold('second')
        self.assertTrue(priority.reviewers_active(self.lock))
        release_first.touch(); self.assertEqual(first.wait(timeout=3), 0)
        self.assertTrue(priority.reviewers_active(self.lock))
        release_second.touch(); self.assertEqual(second.wait(timeout=3), 0)
        self.assertFalse(priority.reviewers_active(self.lock))
        # A completed exclusive probe cannot cause reviewer admission to fail.
        third, release_third = self.hold('third')
        self.assertTrue(priority.reviewers_active(self.lock))
        release_third.touch(); self.assertEqual(third.wait(timeout=3), 0)

    def test_inheritable_lease_survives_exec_and_outer_descriptor_close(self):
        ready, release = self.root / 'exec.ready', self.root / 'exec.release'
        fd = priority.acquire_reviewer(self.lock)
        try:
            self.assertTrue(os.get_inheritable(fd))
            process = subprocess.Popen([sys.executable, '-c', INHERITED, str(fd), str(ready), str(release)], close_fds=False)
            self.addCleanup(lambda: release.touch())
            self.wait_ready(ready, process)
        finally:
            os.close(fd)
        try:
            self.assertTrue(priority.reviewer_waiting(self.lock))
        finally:
            release.touch()
            self.assertEqual(process.wait(timeout=3), 0)
        self.assertFalse(priority.reviewers_active(self.lock))

    def test_context_exception_releases_its_owned_descriptor(self):
        with self.assertRaisesRegex(RuntimeError, 'cancel'):
            with priority.reviewer_lease(self.lock):
                self.assertTrue(priority.reviewers_active(self.lock))
                raise RuntimeError('cancel')
        self.assertFalse(priority.reviewers_active(self.lock))

    def test_wait_is_cancellable_and_does_not_suppress_reviewer_admission(self):
        process, release = self.hold('waiting')
        event = threading.Event(); result = []
        thread = threading.Thread(target=lambda: result.append(priority.wait_for_reviewers(self.lock, stop_event=event, interval=.01)))
        thread.start(); event.set(); thread.join(timeout=2)
        self.assertEqual(result, [False])
        self.assertTrue(priority.reviewers_active(self.lock))
        release.touch(); self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(priority.wait_for_reviewers(self.lock, interval=.01))

    def test_refuses_links_hardlinks_and_unprotected_lock(self):
        target = self.root / 'target'; target.touch(mode=0o600)
        self.lock.symlink_to(target)
        with self.assertRaises(OSError):priority.reviewers_active(self.lock)
        self.lock.unlink(); os.link(target, self.lock)
        with self.assertRaisesRegex(ValueError, 'regular file'):priority.reviewers_active(self.lock)
        self.lock.unlink(); target.unlink(); self.lock.touch(mode=0o600)
        self.lock.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'protected'):priority.reviewers_active(self.lock)
        parent = self.root / 'linked'; parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'linked parents'):priority.reviewers_active(parent / 'new.lock')

    def test_production_owner_is_root_and_relative_paths_are_refused(self):
        self.assertEqual(priority.PRIORITY_PATH, Path('/run/lock/deltabox-ae-reviewer-priority.lock'))
        self.assertEqual(priority.DEFAULT_PRIORITY_LOCK, Path('/run/lock/deltabox-ae-reviewer-priority.lock'))
        with patch.object(priority, 'LOCK_OWNER_UID', 0):
            self.lock.touch(mode=0o600)
            if os.geteuid() != 0:
                with self.assertRaisesRegex(ValueError, 'protected'):priority.reviewers_active(self.lock)
        with self.assertRaisesRegex(ValueError, 'absolute fixed path'):priority.reviewers_active(Path('relative.lock'))


if __name__ == '__main__':
    unittest.main()
