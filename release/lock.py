"""Record the source that a run actually executes.

Each result records the checkout's commit and a fingerprint of its executable
sources. Nothing compares the source against a frozen list, so editing the
checkout never blocks or invalidates a run. Image and trace hashes are recorded
separately by the experiment runners.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATHS = ("agent", "replay", "backends", "common", "upper", "worker", "npd", "pycriu",
         "ae/run_all.sh", "ae/run_test.sh", "ae/run_all_no_gpu.sh", "ae/run_all_no_gpu_numa03.sh", "ae/run_all_gpu.sh",
         "ae/run_figure09.sh", "ae/run_table3.sh", "ae/reproduce.py", "ae/repro", "ae/runners", "ae/scripts", "ae/configs",
         "ae/vendor", "release", "run.py", "run_root_mcts.py", "run_root_sandbox.py")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def fingerprint(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def runtime_identity(root=None):
    """Fingerprint the executable working tree, including uncommitted changes."""
    root = ROOT if root is None else Path(root)
    names = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', *PATHS).split('\0')
    records = {}
    for name in sorted(set(names)):
        if not name or name.endswith(('.md', '.jsonl', '.csv')):
            continue
        path = root / name
        if path.is_symlink():
            records[name] = {'symlink': os.readlink(path)}
        elif path.is_file():
            records[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            records[name] = {'missing': True}
    return dict(schema_version=1, status='working-tree', source_commit=git(root, 'rev-parse', 'HEAD'),
                source_sha256=fingerprint(records))


def from_environment():
    """Producer API: the actual source identity of this checkout."""
    return runtime_identity()
