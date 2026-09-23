"""Record actual runtime source; optionally freeze and audit archived releases.

The source commit identifies the candidate; later documentation/evidence commits
may have different HEADs. Explicit archive verification requires byte-identical executable sources.
Image and trace hashes are recorded separately by the experiment runners.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATHS = ("agent", "replay", "backends", "common", "upper", "worker", "npd", "pycriu",
         "ae/run_all.sh", "ae/run_test.sh", "ae/run_figure09.sh", "ae/run_table3.sh", "ae/reproduce.py", "ae/repro", "ae/runners", "ae/scripts", "ae/configs",
         "ae/vendor", "release", "run.py", "run_root_mcts.py", "run_root_sandbox.py")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def source_records(root=ROOT):
    paths = git(root, "ls-files", "-z", "--", *PATHS).split("\0")
    records = {}
    for name in sorted(set(paths)):
        if not name or name.endswith((".md", ".jsonl", ".csv")) or name == "release/candidate-lock.json":
            continue
        path = root / name
        if path.is_symlink():
            records[name] = {"symlink": os.readlink(path)}
        else:
            records[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return records


def fingerprint(records):
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create(root=ROOT):
    if git(root, "status", "--porcelain", "--", *PATHS):
        raise ValueError("commit candidate sources before freezing the release lock")
    files = source_records(root)
    return {"schema_version": 1, "status": "candidate-not-final",
            "source_commit": git(root, "rev-parse", "HEAD"),
            "source_tree": git(root, "rev-parse", "HEAD^{tree}"),
            "source_sha256": fingerprint(files), "files": files,
            "default_profile": "runtime-default", "default_prewarm": "off",
            "live_llm_validation": "not-run-user-request", "gpu": "not-run-pending-hardware"}


def verify(path: Path, root=ROOT):
    lock = json.loads(path.read_text())
    expected = lock["files"]
    if lock.get("schema_version") != 1 or lock.get("source_sha256") != fingerprint(expected):
        raise ValueError("invalid release source lock")
    actual = source_records(root)
    if expected != actual:
        changed = sorted(name for name in expected.keys() | actual.keys() if expected.get(name) != actual.get(name))
        raise ValueError("release source mismatch: " + ", ".join(changed[:12]))
    extra = git(root, "ls-files", "--others", "--exclude-standard", "--", *PATHS).splitlines()
    extra = [p for p in extra if not p.endswith(".md") and (root / p).resolve() != path.resolve()]
    if extra:
        raise ValueError("untracked candidate sources: " + ", ".join(extra[:12]))
    return {k: lock[k] for k in ("schema_version", "source_commit", "source_sha256", "status")}


def runtime_identity(root=None):
    """Record the executable working tree without requiring a frozen release."""
    root = ROOT if root is None else Path(root)
    names = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', *PATHS).split('\0')
    records = {}
    for name in sorted(set(names)):
        if not name or name.endswith(('.md', '.jsonl', '.csv')) or name == 'release/candidate-lock.json':
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
    """Legacy producer API: capture actual source, never gate a run on an old lock.

    DELTABOX_RELEASE_LOCK no longer controls runtime admission. Explicit archive
    audits can still use the standalone ``verify`` command.
    """
    return runtime_identity()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--file", type=Path, default=ROOT / "release/candidate-lock.json")
    args = parser.parse_args()
    if args.command == "create":
        lock = create()
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open("x") as out:
            json.dump(lock, out, indent=2)
            out.write("\n")
        print(f"Frozen candidate {lock['source_commit']} ({len(lock['files'])} source paths)")
    else:
        print(json.dumps(verify(args.file.resolve()), indent=2))


if __name__ == "__main__":
    main()
