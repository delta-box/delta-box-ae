"""FC-diff admission must distinguish per-job quota from physical NUMA memory."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from ae.repro import memory_budget as budget


class CapacityTests(unittest.TestCase):
    def test_old_quota_is_rejected_before_starting(self):
        with self.assertRaisesRegex(ValueError, 'at least 28'):
            budget.job_size_gib('table-02-fc-diff', {'mem_mib': 8192, 'memory_job_size_gib': 16})
        self.assertEqual(budget.job_size_gib('table-02-fc-diff', {}), 64)
        self.assertEqual(budget.job_size_gib('table-02-criu', {}), 16)
        self.assertEqual(budget.job_size_gib('table-02-replay', {'memory_job_size_gib': 8}), 8)

    def test_quota_does_not_count_as_available_physical_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)/'capacity.jsonl'
            with patch.object(budget.shutil, 'disk_usage', return_value=SimpleNamespace(
                    total=64*budget.GIB, used=4*budget.GIB, free=60*budget.GIB)), \
                 patch.object(budget, 'node_available', return_value=7*budget.GIB):
                with self.assertRaisesRegex(RuntimeError, 'NUMA 2'):
                    budget.check_capacity(Path(tmp), 'before-merge', 8*budget.GIB, evidence, node=2)
            row = json.loads(evidence.read_text())
            self.assertEqual(row['tmpfs_free_bytes'], 60*budget.GIB)
            self.assertEqual(row['numa_available_bytes'], 7*budget.GIB)

    def test_quota_failure_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)/'capacity.jsonl'
            with patch.object(budget.shutil, 'disk_usage', return_value=SimpleNamespace(
                    total=16*budget.GIB, used=12*budget.GIB, free=4*budget.GIB)):
                with self.assertRaisesRegex(RuntimeError, 'tmpfs'):
                    budget.check_capacity(Path(tmp), 'before-base', 8*budget.GIB, evidence)
            self.assertEqual(json.loads(evidence.read_text())['phase'], 'before-base')

    def test_node_reclaim_excludes_busy_file_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = Path(tmp)/'node2'; node.mkdir()
            (node/'meminfo').write_text(
                'Node 2 MemFree: 100 kB\nNode 2 Active(file): 200 kB\n'
                'Node 2 Inactive(file): 300 kB\nNode 2 Dirty: 50 kB\n'
                'Node 2 Writeback: 25 kB\nNode 2 Mapped: 125 kB\n')
            self.assertEqual(budget.node_available(2, Path(tmp)), 400*1024)
