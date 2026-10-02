"""Bounded failure resumes must retain failures and reject uncertain cleanup."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('failure_resume_launcher', ROOT / 'ae/scripts/hosted_launcher.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class FailureResumeTests(unittest.TestCase):
    def exercise(self, codes, *, limit=2, clean=True, verify_error=None, bindings=None, service_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / 'ae/results/selected/run'; output.mkdir(parents=True)
            args = h.parse_arguments(['--checkout', str(root), '--cpu-parallel', '--group', 'cpu',
                                      '--limit', '3', '--output', str(output), '--resume-failures', str(limit)])
            policy = dict(runtime_root=root, python=Path('/usr/bin/python3'), config=root/'config.json')
            commands = []
            def service(policy, caller, command, environment, *, trust, receipt):
                commands.append(list(command))
                if service_error: raise service_error
                code = codes[len(commands)-1]
                receipt.update(cleanup_verified=clean, cleanup_error=None, interrupted_signals=[],
                               workload_returncode=code, numa_policy_dropin={'removed': clean})
                (output/'review.json').write_text(json.dumps({'status': 'ok' if code == 0 else 'failed',
                    'attempt_number': len(commands), 'original_error': None if code == 0 else 'real failure'}))
                (output/'cpu-work-queue.json').write_text(json.dumps({'groups': {'example': {'status': 'ok' if not code else 'failed'}}}))
                return code
            with patch.object(h,'cpu_campaign_binding',side_effect=bindings if bindings else None,return_value={'source':'same'}), \
                 patch.object(h,'run_cpu_service',side_effect=service), \
                 patch.object(h,'trusted_path',side_effect=lambda path,**kw:path), \
                 patch.object(h,'result_path',return_value=output), \
                 patch.object(h,'verify_background_cleanup',side_effect=verify_error) as verify, \
                 patch.object(h,'retain_backend_recovery') as retain, patch.object(h,'audit_launch'):
                error = None; code = None
                try: code = h.run_cpu_campaign(policy,types.SimpleNamespace(pw_uid=1,pw_name='test'),args,output,{})
                except BaseException as exc:error=exc
                path=output/'cpu-resume-history.json'
                history=json.loads(path.read_text()) if path.exists() else None
                return code,error,commands,history,verify.call_count,retain.call_count

    def test_success_does_not_retry(self):
        code,error,commands,history,_,_=self.exercise([0])
        self.assertEqual(code,0);self.assertIsNone(error);self.assertEqual(len(commands),1)
        self.assertFalse(history['resumed_after_failure'])

    def test_failure_then_success_preserves_original_records_and_uses_resume(self):
        code,error,commands,history,_,_=self.exercise([1,0])
        self.assertEqual(code,0);self.assertIsNone(error);self.assertEqual(len(commands),2)
        self.assertIn('--output',commands[0]);self.assertNotIn('--output',commands[1]);self.assertIn('--resume',commands[1])
        self.assertEqual([c[c.index('--limit')+1] for c in commands],['3','3'])
        self.assertEqual(history['attempts'][0]['returncode'],1)
        self.assertEqual(history['attempts'][0]['records']['review.json']['value']['original_error'],'real failure')
        self.assertTrue(history['resumed_after_failure'])

    def test_failure_budget_is_finite(self):
        code,error,commands,history,_,_=self.exercise([1,1,1])
        self.assertEqual(code,1);self.assertIsNone(error);self.assertEqual(len(commands),3)
        self.assertEqual(history['attempts'][-1]['decision'],'resume-budget-exhausted')

    def test_three_resumes_stop_after_four_failed_attempts(self):
        code,error,commands,history,_,_=self.exercise([1,1,1,1],limit=3)
        self.assertEqual(code,1);self.assertIsNone(error);self.assertEqual(len(commands),4)
        self.assertEqual(history['max_resumes'],3)
        self.assertEqual([a['returncode'] for a in history['attempts']],[1,1,1,1])
        self.assertEqual(history['attempts'][-1]['decision'],'resume-budget-exhausted')
        self.assertIn('--output',commands[0])
        self.assertTrue(all('--resume' in command and '--output' not in command for command in commands[1:]))

    def test_third_resume_can_succeed_without_erasing_failures(self):
        code,error,commands,history,_,_=self.exercise([1,1,1,0],limit=3)
        self.assertEqual(code,0);self.assertIsNone(error);self.assertEqual(len(commands),4)
        self.assertEqual([a['returncode'] for a in history['attempts']],[1,1,1,0])
        self.assertTrue(history['resumed_after_failure'])
        self.assertTrue(all(a['records']['review.json']['value']['original_error']=='real failure'
                            for a in history['attempts'][:3]))
        self.assertEqual([c[c.index('--limit')+1] for c in commands],['3']*4)

    def test_one_resume_budget(self):
        code,error,commands,history,_,_=self.exercise([1,1],limit=1)
        self.assertEqual(code,1);self.assertEqual(len(commands),2)

    def test_cleanup_receipt_missing_never_retries(self):
        code,error,commands,history,checks,_=self.exercise([1],clean=False)
        self.assertEqual(code,1);self.assertEqual(len(commands),1);self.assertEqual(checks,0)
        self.assertEqual(history['attempts'][0]['decision'],'cleanup-unverified')

    def test_backend_restoration_failure_preserves_failure_and_retains_guard(self):
        code,error,commands,history,checks,retains=self.exercise([1],verify_error=RuntimeError('shared backend still mounted'))
        self.assertIsInstance(error,RuntimeError);self.assertEqual(len(commands),1);self.assertEqual(retains,1)
        self.assertEqual(history['attempts'][0]['returncode'],1)
        self.assertEqual(history['attempts'][0]['decision'],'cleanup-unverified')

    def test_signal_exit_never_retries(self):
        code,error,commands,history,_,_=self.exercise([130])
        self.assertEqual(code,130);self.assertEqual(len(commands),1)
        self.assertEqual(history['attempts'][0]['decision'],'not-a-retryable-workload-failure')

    def test_source_change_stops_before_second_launch(self):
        code,error,commands,history,_,_=self.exercise([1],bindings=[{'source':'a'},{'source':'a'},{'source':'b'}])
        self.assertIsInstance(error,RuntimeError);self.assertEqual(len(commands),1)
        self.assertEqual(history['attempts'][0]['returncode'],1)

    def test_service_exception_is_not_retried(self):
        code,error,commands,history,_,_=self.exercise([],service_error=RuntimeError('unit cleanup failed'))
        self.assertIsInstance(error,RuntimeError);self.assertEqual(len(commands),1)

    def test_only_explicit_fresh_hosted_numa12_is_admitted(self):
        common=['--checkout','/fixed','--cpu-parallel','--group','cpu','--resume-failures','2']
        for suffix in ([],['--resume','/prior'],['--output','/fresh','--cpu-layout','numa03'],['--list']):
            with self.subTest(suffix=suffix),self.assertRaises(SystemExit):h.parse_arguments(common+suffix)
        for value in ('-1','4','999'):
            with self.subTest(value=value),self.assertRaises(SystemExit):
                h.parse_arguments(['--checkout','/fixed','--cpu-parallel','--group','cpu','--output','/fresh','--resume-failures',value])

if __name__=='__main__':unittest.main()
