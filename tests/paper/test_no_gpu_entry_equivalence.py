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

    def test_same_cpu_selection_options_and_status_only_layout_differs(self):
        for flags in ([], ['--output','/path with spaces/result','--baseline-inputs','44'],
                      ['--resume=/previous result','--limit','2','--max-events=29'], ['--list']):
            results={layout:self.invoke(layout,flags,launcher=LAUNCHER,status=7) for layout in ('numa12','numa03')}
            for result,words in results.values():self.assertEqual(result.returncode,7,result.stderr)
            left,right=results['numa12'][1],results['numa03'][1]
            self.assertEqual(left[:3],['--all','--physcpubind=32-35','--membind=1'])
            self.assertEqual(right[:4],['--all','--physcpubind=4-7','--membind=0','HOSTED'])
            self.assertEqual(left[4:],['--group','cpu','--cpu-parallel',*flags])
            self.assertEqual(right[4:],['--group','cpu','--cpu-parallel','--cpu-layout','numa03',*flags])
            self.assertEqual(left[3],'SELF' if os.geteuid()==0 else 'HOSTED')

    def test_illegal_selection_and_missing_values_fail_before_launch_in_both(self):
        for flags in (['--group','gpu'],['--all'],['--cpu-layout','numa12'],['--numa-node=0'],
                      ['--execute-plan','p'],['--output'],['--limit','--list']):
            for layout in ('numa12','numa03'):
                result,words=self.invoke(layout,flags)
                self.assertEqual(result.returncode,2)
                self.assertEqual(words,[])

    def test_self_managed_reviewer_keeps_overrides_background_requires_fixed_hosted(self):
        flags=['--config','/custom/config','--runtime-repo','/custom/runtime']
        result,words=self.invoke('numa12',flags)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(words[3:],['SELF','--group','cpu','--cpu-parallel',*flags])
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
