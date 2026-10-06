"""GPU-only entry/parser/remote allowlist contracts; no GPU or SSH execution."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ae.scripts import run_review as review
from ae.scripts import figure08_remote as remote

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('gpu_only_hosted_parser', ROOT/'ae/scripts/hosted_launcher.py')
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)
SOURCE = {'source_commit': 'a'*40, 'source_sha256': 'b'*64}


class GPUOnlyEntryTests(unittest.TestCase):
    def invoke(self, flags=(), *, status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'ae').mkdir()
            shutil.copy2(ROOT/'ae/run_all_gpu.sh', root/'ae/run_all_gpu.sh')
            shutil.copy2(ROOT/'ae/run_all.sh', root/'ae/run_all.sh')
            sudo = root/'sudo'
            sudo.write_text('#!/bin/bash\nprintf "%s\\0" "$@"\nexit '+str(status)+'\n')
            sudo.chmod(0o755)
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                       AE_HOSTED_LAUNCHER='/usr/local/sbin/deltabox-ae-run')
            identity = {}
            if os.geteuid() == 0:
                root.chmod(0o755)
                identity = dict(user=65534, group=65534, extra_groups=[])
            result = subprocess.run(['bash',str(root/'ae/run_all_gpu.sh'),*flags],
                                    env=env,capture_output=True,**identity)
            words = result.stdout.decode().split('\0')[:-1]
            parsed = None
            if words:
                self.assertEqual(words[:3], ['-n','--','/usr/local/sbin/deltabox-ae-run'])
                parsed = hosted.parse_arguments(words[3:])
                policy = dict(python=Path('/usr/bin/python3'), runtime_root=ROOT,
                              config=ROOT/'ae/configs/spr4numa-review.json')
                downstream = hosted.command_line(policy, parsed, parsed.output or parsed.resume)
                runtime = review.parser().parse_args(downstream[3:])
                review.validate_gpu_selection(runtime)
                self.assertEqual(runtime.gpu_devices, parsed.gpu_devices)
                self.assertEqual(runtime.group, ['gpu'])
            return result, parsed

    def test_default_scans_all_devices_for_gpu_only_all_cases_fresh_output(self):
        outputs=[]
        for _ in range(2):
            result,args=self.invoke(status=7)
            self.assertEqual(result.returncode,7,result.stderr)
            self.assertEqual(args.group,['gpu'])
            self.assertEqual(args.gpu_devices,list(range(8)))
            self.assertIsNone(args.gpu_cases)
            self.assertIsNone(args.limit)
            self.assertFalse(args.cpu_parallel)
            self.assertEqual(args.resume_failures,0)
            self.assertRegex(str(args.output),r'/ae/results/selected/gpu-only-\d{8}T\d{6}Z-\d+$')
            outputs.append(str(args.output))
        self.assertNotEqual(*outputs)

    def test_explicit_devices_output_spaces_and_resume_preserve_contract(self):
        for flags in (['--gpu-devices','3,2,1,0','--output','/result with spaces'],
                      ['--gpu-devices=3,2,1,0','--output=/result with spaces'],
                      ['--gpu-devices','3,2,1,0','--resume','/result with spaces']):
            result,args=self.invoke(flags)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(args.gpu_devices,[3,2,1,0])
            self.assertEqual(str(args.resume or args.output),'/result with spaces')

    def test_help_and_invalid_modes_do_not_dispatch(self):
        result,args=self.invoke(['--help'])
        self.assertEqual(result.returncode,0)
        self.assertIsNone(args)
        self.assertIn(b'Four idle GPUs',result.stdout)
        for flags in (['--group','cpu'],['--all'],['--limit','3'],['--config','/x'],
                      ['--output'],['--gpu-devices'],['--output','/x','--resume','/y'],
                      ['--gpu-devices','0','--gpu-devices','1']):
            result,args=self.invoke(flags)
            self.assertEqual(result.returncode,2)
            self.assertIsNone(args)

    def test_both_real_parsers_reject_invalid_device_lists_and_cpu_scope(self):
        for flags in ([ '--group','gpu','--gpu-devices',bad] for bad in
                      ('','0,0','8','-1','GPU-abcd','0,','0, 1')):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): hosted.parse_arguments(['--checkout',str(ROOT),*flags])
                with self.assertRaises(SystemExit): review.parser().parse_args(flags)
        for flags in (['--group','cpu'],['--all'],['--group','gpu','--limit','3']):
            flags=[*flags,'--gpu-devices','0,1,2,3']
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): hosted.parse_arguments(['--checkout',str(ROOT),*flags])
            with self.assertRaises(ValueError): review.validate_gpu_selection(review.parser().parse_args(flags))

    def test_review_records_forwards_and_binds_device_choice_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=review.parser().parse_args(['--group','gpu','--gpu-devices','0,1,2,3'])
            with patch.object(review,'current_source',return_value=SOURCE):
                subject=review.Review(args,{},root)
            result=dict(status='partial',successful_cases=6,reason='two devices busy')
            with patch.object(remote,'run_auto',return_value=result) as run, \
                 patch.object(subject,'save'),contextlib.redirect_stdout(io.StringIO()):
                subject.run_gpu()
            self.assertEqual(run.call_args.kwargs['device_indices'],[0,1,2,3])
            self.assertEqual(subject.record['gpu_requested_devices'],[0,1,2,3])
            self.assertEqual(subject.record['coverage'][-1]['status'],'partial')
            (root/'review.json').write_text(json.dumps(subject.record))
            with patch.object(review,'current_source',return_value=SOURCE):
                resumed=review.parser().parse_args(['--group','gpu','--gpu-devices','0,1,2,3','--resume',str(root)])
                review.Review(resumed,{},root)
                resumed.gpu_devices=[4,5,6,7]
                with self.assertRaisesRegex(ValueError,'device selection differs'):
                    review.Review(resumed,{},root)

    def test_remote_snapshot_uses_override_without_rewriting_shared_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'config.json'
            original=remote.DEFAULT_CONFIG.read_bytes()
            source.write_bytes(original)
            captured=[]
            def capture(output,config):
                captured.append(dict(config))
                raise RuntimeError('stop before upload; no remote run')
            free=dict(writable=True,available_bytes=100*1024**3,available_inodes=100000)
            with patch.object(remote,'ssh',return_value=subprocess.CompletedProcess([],0,json.dumps(free))), \
                 patch.object(remote,'snapshot',side_effect=capture):
                result=remote.run_auto(root/'run',source,device_indices=[0,1,2,3])
            self.assertEqual(captured[0]['devices'],[0,1,2,3])
            self.assertEqual(result['requested_devices'],[0,1,2,3])
            self.assertEqual(source.read_bytes(),original)
            config=captured[0]
            observation=dict(gpus=[dict(index=i,uuid=f'GPU-{i}',memory_mib=0,utilization_pct=0)
                                   for i in range(8)],processes=[dict(pid=123,uuid='GPU-1'),dict(pid=124,uuid='GPU-2')])
            idle=remote.idle_devices([observation]*config['samples'],config)
            self.assertEqual([g['index'] for g in idle],[0,3])
            suites=remote.suites_for([g['uuid'] for g in idle])
            self.assertEqual(sum(len(batches) for _,batches,_ in suites),6)
            self.assertTrue(all(set(devices)<= {'GPU-0','GPU-3'} for _,_,devices in suites))
            suites=remote.suites_for(['GPU-0','GPU-1','GPU-2','GPU-3'])
            self.assertEqual(sum(len(batches) for _,batches,_ in suites),8)
            self.assertEqual(suites[-1],('training',[16,64],['GPU-0','GPU-1','GPU-2','GPU-3']))

    def test_report_names_requested_physical_devices_without_inventing_legacy_scope(self):
        record = dict(status='complete', successful_cases=8, host='allinai2plus',
                      requested_devices=[0, 3, 6, 7])
        text = '\n'.join(remote.report_lines(record, '.'))
        self.assertIn('candidate physical GPUs: 0, 3, 6, 7.', text)
        self.assertNotIn('candidate physical GPUs: 0–7.', text)
        record.pop('requested_devices')
        self.assertIn('candidate physical GPUs: see remote-config.json.',
                      '\n'.join(remote.report_lines(record, '.')))

    def summary_subject(self, root, *, cases=None, status='ok', wanted=None):
        args = review.parser().parse_args(['--group', 'gpu', '--gpu-devices', '0,3,6,7'])
        with patch.object(review, 'current_source', return_value=SOURCE):
            subject = review.Review(args, {}, root)
        wanted = wanted or remote.requested_cases()
        subject.record.update(status=status, gpu_output='gpu/attempt-001', gpu_requested_cases=wanted)
        rows = [dict(case_id=name, status='ok', num_gpus=4 if name in ('training-B16', 'training-B64') else 1,
                     reps=3, timing_s={'mean': 1.25}) for name in (cases if cases is not None else wanted)]
        path = root / 'gpu/attempt-001/results/summary.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(model_label='test model', cases=rows)))
        subject.record['gpu'] = dict(status='complete' if len(rows) == 8 else 'partial', successful_cases=len(rows),
                                    requested_devices=[0,3,6,7], host='allinai2plus', selected=[{'index': i} for i in (0,3,6,7)],
                                    evidence={'results/summary.json': dict(sha256=remote.digest(path), bytes=path.stat().st_size)})
        return subject, path

    def test_gpu_summary_saved_and_printed_with_all_eight_timings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject, raw = self.summary_subject(root)
            original = raw.read_bytes()
            subject.save()
            output = io.StringIO()
            with contextlib.redirect_stdout(output): subject.print_summary()
            summary = (root/'SUMMARY.md').read_text()
            self.assertEqual(summary, (root/'result.md').read_text())
            self.assertIn(summary, output.getvalue())
            self.assertIn('ok: '+str(root/'SUMMARY.md'), output.getvalue())
            self.assertIn('GPU cases passed: **8/8**', summary)
            self.assertIn('requested physical GPUs: 0, 3, 6, 7; selected: 0, 3, 6, 7.', summary)
            for case in remote.requested_cases(): self.assertIn('| '+case+' | ok |', summary)
            self.assertIn('| training-B16 | ok | 4 | 3 | 1.250000 |', summary)
            self.assertNotIn('## Table 2', summary)
            self.assertEqual(raw.read_bytes(), original)

    def test_partial_subset_and_bad_evidence_do_not_invent_success_or_timings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wanted = ['training-B16', 'training-B64']
            subject, raw = self.summary_subject(root, cases=['training-B16'], status='failed', wanted=wanted)
            subject.record['attempt_number'] = 2
            subject.save()
            text = (root/'SUMMARY.md').read_text()
            self.assertIn('GPU cases passed: **1/8**', text)
            self.assertIn('Attempt: 2 (resumed run)', text)
            self.assertIn('| generation-B1 | not selected | - | - | - |', text)
            self.assertIn('| training-B64 | not completed | - | - | - |', text)
            raw.write_text('{}')
            subject.save()
            text = (root/'SUMMARY.md').read_text()
            self.assertIn('Timing table unavailable:', text)
            self.assertNotIn('1.250000', text)
            self.assertNotIn('| training-B16 | ok |', text)

    def test_interruption_prints_failure_summary_and_cpu_output_stays_short(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject, _ = self.summary_subject(root, cases=[], status='running')
            output = io.StringIO()
            with contextlib.redirect_stdout(output): subject.terminal_error(KeyboardInterrupt())
            self.assertEqual(subject.record['status'], 'interrupted')
            self.assertIn('Status: **interrupted**', output.getvalue())
            self.assertIn('GPU cases passed: **0/8**', output.getvalue())
            subject.experiments = ['table-02-slow']
            subject.record.update(status='ok', experiments=subject.experiments)
            subject.save()
            output = io.StringIO()
            with contextlib.redirect_stdout(output): subject.print_summary()
            self.assertEqual(output.getvalue(), 'ok: '+str(root/'SUMMARY.md')+'\n')
            self.assertNotIn('GPU experiment summary', (root/'SUMMARY.md').read_text())

    def test_invalid_remote_override_rejected_before_output_or_ssh(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(remote,'ssh') as ssh:
            output=Path(directory)/'run'
            for invalid in ([],[0,0],[8],[True],'0,1,2,3'):
                with self.assertRaises(ValueError): remote.run_auto(output,device_indices=invalid)
                self.assertFalse(output.exists())
            ssh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
