"""Storage and evidence checks for the Figure 9 memory-backed cohort."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'ae'), str(ROOT/'ae/runners')]
from vm_experiment import require_memory_workdir
spec = importlib.util.spec_from_file_location('figure09_cohort', ROOT/'ae/scripts/run_figure09_cohort.py')
cohort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cohort)


class Figure09MemoryTests(unittest.TestCase):
    def test_only_verified_unused_staging_copy_can_be_released(self):
        with tempfile.TemporaryDirectory(prefix='f9-') as directory:
            work=Path(directory); base=work/'base.xfs'; base.write_bytes(b'base image')
            active=work/'war-test/rootfs.xfs'; active.parent.mkdir(); active.write_bytes(base.read_bytes())
            stat=base.stat()
            identity=dict(path=str(base.resolve()),device=stat.st_dev,inode=stat.st_ino,
                          size=stat.st_size,mtime_ns=stat.st_mtime_ns,ctime_ns=stat.st_ctime_ns)
            receipt=work/'staging.json'; evidence=work/'release.json'
            def write_receipt(record):
                receipt.write_text(json.dumps(dict(images=dict(base_xfs=dict(
                    source=dict(path='/original/base.xfs'),staged=record)))))
            with patch.object(cohort,'require_memory_workdir'):
                write_receipt(dict(identity,inode=-1))
                with self.assertRaises(ValueError):
                    cohort.release_staged_base(base,active,work,receipt,evidence)
                self.assertTrue(base.exists())
                write_receipt(identity)
                active.unlink(); os.link(base,active)
                with self.assertRaises(ValueError):
                    cohort.release_staged_base(base,active,work,receipt,evidence)
                self.assertTrue(base.exists())
                active.unlink(); active.write_bytes(base.read_bytes())
                # Hard-link changes ctime, so refresh the receipt before success.
                identity['ctime_ns']=base.stat().st_ctime_ns; write_receipt(identity)
                cohort.release_staged_base(base,active,work,receipt,evidence)
                self.assertFalse(base.exists())
                self.assertEqual(active.read_bytes(),b'base image')
                self.assertTrue(json.loads(evidence.read_text())['before_measured_jobs'])

    def test_disk_and_swappable_tmpfs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for fstype, options in [('ext2/ext3','rw'), ('tmpfs','rw,size=24G')]:
                with self.subTest(fstype=fstype), patch('vm_experiment.subprocess.check_output',
                    side_effect=[fstype+'\n', json.dumps({'filesystems':[dict(target=directory,fstype=fstype,options=options)]})]):
                    with self.assertRaisesRegex(ValueError, 'noswap tmpfs'):
                        require_memory_workdir(directory)

    def test_memory_evidence_records_mount(self):
        with tempfile.TemporaryDirectory() as directory, patch('vm_experiment.subprocess.check_output',
            side_effect=['tmpfs\n', json.dumps({'filesystems':[dict(target='/memory',fstype='tmpfs',options='rw,noswap,size=24G')]})]):
            self.assertEqual(require_memory_workdir(directory)['mount']['target'], '/memory')

    def test_excluded_inputs_are_missing_not_zero_measurements(self):
        row = dict(instance='input', fs_arm='xfs', edit_idx=1, applied_ok=False,
                   measurement_status='excluded-input', exclusion_reason='historical-diff-out-of-bounds',
                   file_size_bytes=452, copyup_bytes=None, phys_bytes=None)
        cohort.validate_rows([row], 1, 'input', 'xfs')
        for invalid in (dict(row, copyup_bytes=0), dict(row, phys_bytes=0),
                        dict(row, applied_ok=True), dict(row, exclusion_reason='unknown failure')):
            with self.subTest(row=invalid), self.assertRaises(ValueError):
                cohort.validate_rows([invalid], 1, 'input', 'xfs')

    def test_partial_duplicate_wrong_arm_and_error_rows_are_rejected(self):
        row = dict(instance='input',fs_arm='xfs',edit_idx=0,file_size_bytes=1234,copyup_bytes=4096,phys_bytes=8192)
        cohort.validate_rows([row],1,'input','xfs')
        for rows, expected in [([],1), ([row,row],2), ([dict(row,fs_arm='ext4')],1),
                               ([dict(row,error='failed')],1), ([dict(row,phys_bytes=-512)],1)]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                cohort.validate_rows(rows,expected,'input','xfs')


if __name__ == '__main__':
    unittest.main()
