import copy
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from ae.scripts import job_mount_namespace as m

class InheritedCubeMountTests(unittest.TestCase):
    def setUp(self):
        self.repo=Path('/repo');self.ram='/repo/ae/results/selected/run/lanes/numa0/environment/attempt-001/table-02-cube/cube-memory/ram'
        self.root=dict(mount_id=10,target=self.ram,propagation=[],fstype='tmpfs',options=['rw','noswap'])
        self.child=dict(mount_id=11,target=self.ram+'/storage',propagation=[],fstype='xfs',options=['rw'])
        self.rows=[self.root,self.child];self.commands=[]
    def command(self,args,**kwargs):
        self.commands.append(args);self.rows[:]=[r for r in self.rows if r['target']!=args[1]]
    def run_case(self,namespace='new'):
        with patch.object(m,'mount_records',side_effect=lambda:copy.deepcopy(self.rows)),patch.object(m.os,'readlink',side_effect=lambda p:namespace if p=='/proc/self/ns/mnt' else 'host'),patch.object(m.subprocess,'run',side_effect=self.command):
            return m.release_inherited_cube_mounts(self.repo)
    def test_removes_child_then_ram_and_leaves_other_mounts(self):
        other=dict(self.root,target='/other/ram');self.rows.append(other)
        result=self.run_case();self.assertEqual(self.commands,[['umount',self.ram+'/storage'],['umount',self.ram]])
        self.assertEqual(self.rows,[other]);self.assertEqual(len(result['removed']),2)
    def test_no_cube_mounts_are_a_noop(self):
        self.rows=[];self.assertEqual(self.run_case(),{'removed':[]});self.assertEqual(self.commands,[])
    def test_host_or_parent_namespace_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'new private'):self.run_case('host')
        self.assertFalse(self.commands)
    def test_shared_mount_never_unmounted(self):
        self.child['propagation']=['shared:99']
        with self.assertRaisesRegex(RuntimeError,'propagation'):self.run_case()
        self.assertFalse(self.commands)
    def test_unexpected_child_mount_rejected(self):
        self.child['target']=self.ram+'/unexpected'
        with self.assertRaisesRegex(RuntimeError,'mount tree'):self.run_case()
        self.assertFalse(self.commands)
    def test_missing_noswap_rejected(self):
        self.root['options']=['rw']
        with self.assertRaisesRegex(RuntimeError,'workspace'):self.run_case()
    def test_control_plane_layout_supported(self):
        self.root['target']=self.ram.replace('/cube-memory/','/control-plane/cube-memory/')
        self.child['target']=self.root['target']+'/storage'
        self.assertEqual(len(self.run_case()['removed']),2)
    def test_failed_umount_propagates(self):
        self.command=lambda *a,**k:(_ for _ in ()).throw(subprocess.CalledProcessError(1,['umount']))
        with self.assertRaises(subprocess.CalledProcessError):self.run_case()
    def test_changed_identity_rejected(self):
        with patch.object(m,'mount_records',side_effect=[self.rows,[]]),patch.object(m.os,'readlink',side_effect=['new','host','host']):
            with self.assertRaisesRegex(RuntimeError,'identity changed'):m.release_inherited_cube_mounts(self.repo)
