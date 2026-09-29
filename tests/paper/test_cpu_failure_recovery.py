import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ae.repro.analysis import analyze_fresh, main
from ae.scripts import cube_memory_context as cube
from tests.paper.test_analysis import fanout, write

class ScopedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.runs=self.root/'numa1/runs';self.runs.mkdir(parents=True)
        fanout(self.runs,'cube')
        write(self.root/'numa1/configs/attempt-001/correctness.json',{'kernel':'config, not a result'})
    def selected(self, roots=('numa1/runs',)):
        try:return analyze_fresh(self.root,run_subdirs=roots)
        except TypeError as e:self.fail(str(e))
    def test_combined_analysis_excludes_control_files_but_records_scope(self):
        result=self.selected()
        self.assertEqual(result['selection']['actual_run_count'],1)
        self.assertEqual(result['selection']['run_subdirs'],['numa1/runs'])
    def test_unbound_correctness_inside_selected_runs_still_rejected(self):
        write(self.runs/'orphan/correctness.json',{'ok':True})
        with self.assertRaisesRegex(ValueError,'Orphan'):self.selected()
    def test_unscoped_analysis_keeps_existing_strict_behavior(self):
        with self.assertRaisesRegex(ValueError,'Orphan'):analyze_fresh(self.root)
    def test_reject_outside_duplicate_overlapping_and_symlink_scopes(self):
        (self.root/'alias').symlink_to(self.runs,target_is_directory=True)
        for paths in [('../',),('missing',),('numa1/runs','numa1/runs'),('numa1','numa1/runs'),('alias',)]:
            with self.subTest(paths=paths),self.assertRaises(ValueError):self.selected(paths)
    def test_two_scopes_keep_both_lane_measurements(self):
        fanout(self.root/'numa2/runs','e2b')
        result=self.selected(('numa1/runs','numa2/runs'))
        self.assertEqual(result['selection']['actual_run_count'],2)

class LoopReleaseTests(unittest.TestCase):
    def call(self, **kwargs):
        self.assertTrue(hasattr(cube,'wait_loop_release'),'Cleanup must await actual loop release after losetup -d')
        return cube.wait_loop_release(Path('/private/ram/storage.xfs'),**kwargs)
    def test_returns_only_after_backing_file_is_released(self):
        with patch.object(cube,'output',side_effect=['/dev/loop19','/dev/loop19','']),patch.object(cube.time,'sleep') as sleep:
            self.call();self.assertEqual(sleep.call_count,2)
    def test_timeout_does_not_detach_devices_or_unmount_storage(self):
        with patch.object(cube,'output',return_value='/dev/loop19'),patch.object(cube,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'still attached'):self.call(timeout=0)
            run.assert_not_called()
    def test_already_released_requires_no_sleep(self):
        with patch.object(cube,'output',return_value=''),patch.object(cube.time,'sleep') as sleep:
            self.call();sleep.assert_not_called()
