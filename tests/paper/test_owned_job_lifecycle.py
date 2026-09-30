"""Regression checks for the copied pidfd/subreaper ownership helper."""
import importlib.util
from pathlib import Path
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('owned_job_lifecycle', ROOT / 'ae/scripts/owned_job_lifecycle.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)

class OwnedChildrenTests(unittest.TestCase):
    def setUp(self):
        self.pids = set()
        self.flags = [0]
        self.clock = [0.0]
        self.closed = []
        self.signals = []
        self.wait_results = []
        self.start_ticks = 55
        self.signal_exit = self.already_zombie = self.stubborn = False
        self.stack = [patch.object(job, 'direct_children', side_effect=lambda: set(self.pids)),
                      patch.object(job, 'subreaper_flag', side_effect=self.flag),
                      patch.object(job, 'process_identity', side_effect=lambda pid:
                                   dict(pid=pid, start_ticks=self.start_ticks)),
                      patch.object(job.os, 'pidfd_open', side_effect=lambda pid, flags: pid + 1000, create=True),
                      patch.object(job.os, 'P_PIDFD', 3, create=True),
                      patch.object(job.signal, 'pidfd_send_signal', side_effect=self.send, create=True),
                      patch.object(job.os, 'waitid', side_effect=self.waitid, create=True),
                      patch.object(job.os, 'WEXITED', 4, create=True),
                      patch.object(job.os, 'WNOHANG', 1, create=True),
                      patch.object(job.os, 'close', side_effect=self.closed.append),
                      patch.object(job.time, 'monotonic', side_effect=lambda: self.clock[0]),
                      patch.object(job.time, 'sleep', side_effect=lambda seconds:
                                   self.clock.__setitem__(0, self.clock[0] + seconds))]
        for context in self.stack:
            context.start()
            self.addCleanup(context.stop)

    def flag(self, value=None):
        if value is not None:
            self.flags[0] = value
        return self.flags[0]

    def send(self, fd, signum):
        self.signals.append((fd, signum))
        if self.signal_exit:
            self.wait_results.append(SimpleNamespace(si_pid=fd - 1000))
            raise ProcessLookupError('natural exit during signal')
        if not self.stubborn:
            self.wait_results.append(SimpleNamespace(si_pid=fd - 1000))

    def waitid(self, which, fd, flags):
        self.assertEqual(which, job.os.P_PIDFD)
        if self.already_zombie or self.wait_results:
            result = self.wait_results.pop(0) if self.wait_results else SimpleNamespace(si_pid=fd - 1000)
            self.pids.discard(result.si_pid)
            return result
        return None

    def test_adopted_escaped_child_is_signaled_by_pidfd_and_reaped_before_flag_restore(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        children.cleanup(None)
        children.restore()
        self.assertEqual(self.signals, [(1321, signal.SIGTERM)])
        self.assertTrue(children.evidence[0]['reaped'])
        self.assertFalse(self.pids)
        self.assertEqual(self.flags[0], 0)
        self.assertEqual(self.closed, [1321])

    def test_already_zombie_child_is_reaped_without_signal(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        self.already_zombie = True
        children.cleanup(None)
        children.restore()
        self.assertFalse(self.signals)
        self.assertTrue(children.evidence[0]['reaped'])
        self.assertEqual(self.flags[0], 0)

    def test_natural_exit_during_pidfd_signal_is_reaped_without_false_failure(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        self.signal_exit = True
        children.cleanup(None)
        children.restore()
        self.assertTrue(children.evidence[0]['reaped'])
        self.assertEqual(self.closed, [1321])

    def test_changed_pid_starttick_is_refused_before_signal(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        children.register(321)
        self.start_ticks += 1
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            children.cleanup(None)
        self.assertFalse(self.signals)
        with self.assertRaisesRegex(RuntimeError, 'Refuse subreaper restore'):
            children.restore()

    def test_stubborn_descendant_reaches_bounded_kill_timeout_and_prevents_restore(self):
        children = job.OwnedChildren()
        self.pids.add(321)
        self.stubborn = True
        with self.assertRaisesRegex(RuntimeError, 'remain after pidfd cleanup'):
            children.cleanup(None)
        self.assertIn((1321, signal.SIGKILL), self.signals)
        self.assertGreaterEqual(self.clock[0], 25)
        self.assertLess(self.clock[0], 25.2)
        with self.assertRaisesRegex(RuntimeError, 'Refuse subreaper restore'):
            children.restore()

    def test_live_reap_initial_drain_excludes_producer_and_restores_sigchld(self):
        original=signal.getsignal(signal.SIGCHLD)
        children=job.OwnedChildren()
        self.addCleanup(children.stop_live_reaping)
        self.pids.update({111,321})
        children.register(111)
        self.wait_results.append(SimpleNamespace(si_pid=321,si_status=0))
        children.start_live_reaping(111)
        self.assertEqual(self.pids,{111})
        self.assertFalse(self.signals)
        self.assertTrue(children.evidence[1]['reaped_during_producer'])
        self.assertNotIn('reaped',children.evidence[0])
        self.assertEqual(self.closed,[1321])
        children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)

    def test_live_adopted_process_is_not_killed_or_prematurely_reaped(self):
        original=signal.getsignal(signal.SIGCHLD)
        children=job.OwnedChildren()
        self.addCleanup(children.stop_live_reaping)
        self.pids.update({111,321}); children.register(111)
        children.start_live_reaping(111)
        self.assertEqual(self.pids,{111,321})
        self.assertFalse(self.signals)
        self.assertFalse(self.closed)
        children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)

    def test_tail_sigchld_reentry_is_drained_after_busy_clears(self):
        test=self
        class TailRace(job.OwnedChildren):
            def __setattr__(self,name,value):
                if name=='reap_busy' and value is False and getattr(self,'armed',False):
                    self.armed=False
                    test.pids.add(444)
                    test.wait_results.append(SimpleNamespace(si_pid=444,si_status=0))
                    self.reap_terminated(None,None)  # Reenters while still busy.
                super().__setattr__(name,value)
        original=signal.getsignal(signal.SIGCHLD)
        children=TailRace()
        self.addCleanup(children.stop_live_reaping)
        self.pids.add(111);children.register(111)
        children.armed=True
        children.start_live_reaping(111)
        self.assertEqual(self.pids,{111})
        self.assertTrue(children.evidence[-1]['reaped_during_producer'])
        self.assertEqual(self.closed,[1444])
        children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)

    def test_live_reap_error_restores_handler_and_remains_fail_closed(self):
        original=signal.getsignal(signal.SIGCHLD)
        children=job.OwnedChildren()
        self.pids.update({111,321}); children.register(111)
        with patch.object(job.os,'waitid',side_effect=RuntimeError('waitid failed')):
            children.start_live_reaping(111)
        with self.assertRaisesRegex(RuntimeError,'live reaping failed'):
            children.stop_live_reaping()
        self.assertEqual(signal.getsignal(signal.SIGCHLD),original)
        self.assertFalse(self.signals)

    def test_preexisting_unrelated_direct_child_refuses_subreaper_setup(self):
        self.pids.add(999)
        with self.assertRaisesRegex(RuntimeError, 'unrelated asynchronous children'):
            job.OwnedChildren()
        self.assertEqual(self.flags[0], 0)


if __name__ == '__main__': unittest.main()
