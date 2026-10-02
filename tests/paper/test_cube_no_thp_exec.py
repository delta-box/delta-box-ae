import ctypes
import os
from pathlib import Path
import subprocess
import sys
import unittest
from ae.runners.cube_fanout_audit import P as ROOT
class CubeTHPExecTests(unittest.TestCase):
 def test_disable_is_inherited_through_exec_and_parent_unchanged(self):
  helper=Path(os.environ.get('CUBE_THP_TEST_HELPER',str(ROOT/'ae/scripts/cube_no_thp_exec.py')))
  self.assertTrue(helper.is_file(),'Private Cube process tree needs its own THP control')
  libc=ctypes.CDLL(None)
  before=libc.prctl(42,0,0,0,0)
  child='import ctypes; print(ctypes.CDLL(None).prctl(42,0,0,0,0))'
  r=subprocess.run([sys.executable,str(helper),sys.executable,'-c',child],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr)
  self.assertEqual(r.stdout.strip(),'1')
  self.assertEqual(libc.prctl(42,0,0,0,0),before)
