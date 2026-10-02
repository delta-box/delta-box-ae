"""Run both shell entries against inert executables, preserving complete argv."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = '/usr/local/sbin/deltabox-ae-run'


class CPUEntryEquivalenceTests(unittest.TestCase):
    def invoke(self, layout, flags=(), *, launcher=None, status=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ae = root / 'ae'
            (ae / 'scripts').mkdir(parents=True)
            for name in ('run_all_no_gpu.sh', 'run_all_no_gpu_numa03.sh', 'scripts/run_no_gpu_entry.sh'):
                shutil.copy2(ROOT/'ae'/name, ae/name)
            # Print dispatch and argument boundaries without invoking any real service.
            (ae/'run_all.sh').write_text('#!/bin/bash\nprintf "%s\\0" SELF "$@"\nexit '+str(status)+'\n')
            for name, text in {
                'numactl':'#!/bin/bash\nprintf "%s\\0" "$1" "$2" "$3"\nshift 3\nexec "$@"\n',
                'sudo':'#!/bin/bash\nprintf "%s\\0" HOSTED "$@"\nexit '+str(status)+'\n',
            }.items():
                path=root/name; path.write_text(text); path.chmod(0o755)
            env={**os.environ, 'PATH':str(root)+os.pathsep+os.environ['PATH']}
            env.pop('AE_HOSTED_LAUNCHER',None)
            if launcher is not None:env['AE_HOSTED_LAUNCHER']=launcher
            entry='run_all_no_gpu.sh' if layout=='numa12' else 'run_all_no_gpu_numa03.sh'
            result=subprocess.run(['bash',str(ae/entry),*flags],capture_output=True,env=env)
            words=result.stdout.decode().split('\0')[:-1]
            if len(words)>3 and words[3]=='HOSTED':
                self.assertEqual(words[4:8],['-n','--',launcher or LAUNCHER,'--checkout'])
                self.assertEqual(words[8],str(root))
                words=words[:4]+words[9:]
            return result,words

    def test_default_reviewer_uses_three_inputs_three_resumes_and_fresh_output(self):
        outputs = []
        for _ in range(2):
            result, words = self.invoke('numa12', status=7)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(words[:4], ['--all', '--physcpubind=32-35', '--membind=1', 'HOSTED'])
            self.assertEqual(words[4:9], ['--group', 'cpu', '--cpu-parallel', '--limit', '3'])
            self.assertEqual(words[9], '--output')
            self.assertRegex(words[10], r'/ae/results/selected/numa12-\d{8}T\d{6}Z-\d+$')
            self.assertFalse(Path(words[10]).exists())
            self.assertEqual(words[11:], ['--resume-failures', '3'])
            outputs.append(words[10])
        self.assertNotEqual(*outputs)

    def test_explicit_overrides_keep_values_spaces_and_exit_status(self):
        for flags in (['--output', '/path with spaces/result', '--limit', '2', '--resume-failures', '1'],
                      ['--output=/path with spaces/result', '--limit=2', '--resume-failures=1']):
            result, words = self.invoke('numa12', flags, status=7)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(words[3:7], ['HOSTED', '--group', 'cpu', '--cpu-parallel'])
            preserved = flags[:-2] if flags[-2] == '--resume-failures' else flags[:-1]
            self.assertEqual(words[7:], [*preserved, '--resume-failures', '1'])

    def test_explicit_zero_disables_failure_resumes(self):
        result, words = self.invoke('numa12', ['--resume-failures', '0'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(words[3:], ['SELF', '--group', 'cpu', '--cpu-parallel', '--limit', '3'])

    def test_listing_and_manual_resume_do_not_reset_failure_budget(self):
        for flags in (['--list'], ['--resume', '/previous result'], ['--resume=/previous result']):
            result, words = self.invoke('numa12', flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(words[3:], ['SELF', '--group', 'cpu', '--cpu-parallel', *flags, '--limit', '3'])
            self.assertNotIn('--resume-failures', words)
            self.assertNotIn('--output', words)
        for flags in (['--resume', '/old'], ['--list'], ['--config', '/custom']):
            result, words = self.invoke('numa12', [*flags, '--resume-failures', '3'])
            self.assertEqual((result.returncode, words), (2, []))

    def test_background_keeps_priority_dispatch_and_default_input_limit(self):
        result, words = self.invoke('numa03', ['--output', '/background'], status=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(words, ['--all', '--physcpubind=4-7', '--membind=0', 'HOSTED',
                                '--group', 'cpu', '--cpu-parallel', '--cpu-layout', 'numa03',
                                '--output', '/background', '--limit', '3'])
        self.assertNotIn('--resume-failures', words)

    def test_illegal_selection_and_missing_values_fail_before_launch_in_both(self):
        for flags in (['--group','gpu'],['--all'],['--cpu-layout','numa12'],['--numa-node=0'],
                      ['--execute-plan','p'],['--output'],['--limit','--list'],
                      ['--resume-failures','4'],['--resume-failures','3','--resume-failures','0']):
            for layout in ('numa12','numa03'):
                result,words=self.invoke(layout,flags)
                self.assertEqual(result.returncode,2)
                self.assertEqual(words,[])

    def test_self_managed_reviewer_keeps_overrides_background_requires_fixed_hosted(self):
        flags=['--config','/custom/config','--runtime-repo','/custom/runtime']
        result,words=self.invoke('numa12',flags)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(words[3:],['SELF','--group','cpu','--cpu-parallel',*flags,'--limit','3'])
        result,words=self.invoke('numa03',flags)
        self.assertEqual((result.returncode,words),(2,[]))
        result,words=self.invoke('numa03',launcher='/unprotected/launcher')
        self.assertEqual((result.returncode,words),(2,[]))

    def test_help_does_not_launch_either_entry(self):
        for layout in ('numa12','numa03'):
            result,_=self.invoke(layout,['--help'],status=7)
            self.assertEqual(result.returncode,0)
            self.assertIn(b'all 16',result.stdout)
            self.assertIn(b'Figure 8(a)',result.stdout)


if __name__=='__main__':
    unittest.main()
