"""Only the lazy Table3 plan probes its required capability; no VM is started."""
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ae.scripts import run_review as review

class LazyPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.binary=Path(self.tmp.name)/'criu';self.binary.write_bytes(b'fixture')
        self.command=['python','fixture.py','--mode','slow','--checkpoint-profile','async-incremental-lazy','--criu-dump-binary',str(self.binary)]
        self.job={'experiment':'table-03-slow','command':self.command}
        self.config={'checkpoint_profile':'async-incremental-lazy'}
    def test_valid_capability_probe_is_version_only(self):
        with patch.object(review.subprocess,'check_output',return_value='Version:4.2 DeltaBox capabilities: exact-parent-v1 exact-parent-lazy-v1') as probe:
            self.assertEqual(review.job_unavailable(self.job,self.config),[])
        self.assertEqual(probe.call_args.args[0],[str(self.binary),'--version'])
        self.assertEqual(probe.call_args.kwargs['env']['DELTABOX_CRIU_CAPABILITIES'],'1')
    def test_old_exact_parent_without_lazy_is_rejected_before_vm(self):
        with patch.object(review.subprocess,'check_output',return_value='DeltaBox capabilities: exact-parent-v1'):
            reasons=review.job_unavailable(self.job,self.config)
        self.assertTrue(any('exact-parent-lazy-v1' in r for r in reasons))
    def test_missing_pinned_binary_does_not_probe_or_fall_back(self):
        self.binary.unlink()
        with patch.object(review.subprocess,'check_output') as probe:
            reasons=review.job_unavailable(self.job,self.config)
        self.assertTrue(reasons);probe.assert_not_called()
    def test_probe_error_is_not_treated_as_supported(self):
        with patch.object(review.subprocess,'check_output',side_effect=subprocess.TimeoutExpired('criu',10)):
            reasons=review.job_unavailable(self.job,self.config)
        self.assertTrue(any('capability probe failed' in r for r in reasons))
    def test_eager_compatibility_profile_does_not_require_lazy(self):
        self.command[self.command.index('--checkpoint-profile')+1]='async-incremental'
        self.config['checkpoint_profile']='async-incremental'
        with patch.object(review.subprocess,'check_output') as probe:
            self.assertEqual(review.job_unavailable(self.job,self.config),[])
        probe.assert_not_called()
    def test_lazy_config_cannot_silently_plan_eager(self):
        self.command[self.command.index('--checkpoint-profile')+1]='async-incremental'
        with patch.object(review.subprocess,'check_output') as probe:
            reasons=review.job_unavailable(self.job,self.config)
        self.assertTrue(any('does not match' in r for r in reasons));probe.assert_not_called()

if __name__=='__main__':unittest.main()
