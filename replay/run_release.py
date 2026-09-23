#!/usr/bin/env python3
"""Run selected paper experiments from one source lock on NUMA 2 / max P-state."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release.lock import verify


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock", type=Path, default=ROOT / "release/candidate-lock.json")
    p.add_argument("--config", type=Path, default=ROOT / "ae/configs/spr4numa.json")
    p.add_argument("--out", type=Path, required=True)
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--experiment", action="append")
    selection.add_argument("--all", action="store_true", help="all CPU experiments, including baselines; missing prerequisites fail")
    p.add_argument("--node", type=int, default=2)
    p.add_argument("--cpus", default="52-55")
    p.add_argument("--timeout", type=float, default=86400)
    p.add_argument("--limit", type=int, help="smoke only")
    p.add_argument("--max-events", type=int, help="smoke only")
    p.add_argument("--plan", action="store_true", help="read-only plan; no pinning or VMs")
    args = p.parse_args(argv)
    verify(args.lock.resolve())
    output = args.out.resolve()
    command = [sys.executable, str(ROOT / "ae/reproduce.py"), "plan" if args.plan else "run",
               "--config", str(args.config.resolve()), "--output", str(output / "suite")]
    if args.all:
        command += ["--all"]
    else:
        for experiment in args.experiment or ["table-02-deltabox"]:
            command += ["--experiment", experiment]
    if args.limit is not None:
        command += ["--limit", str(args.limit)]
    if args.max_events is not None:
        command += ["--max-events", str(args.max_events)]
    if not args.plan:
        command += ["--keep-going"]
        command = [sys.executable, str(ROOT / "ae/scripts/run_pinned_measurement.py"),
                   "--node", str(args.node), "--cpus", args.cpus, "--timeout", str(args.timeout),
                   "--out", str(output / "environment"), "--", *command]
    env = dict(os.environ, DELTABOX_RELEASE_LOCK=str(args.lock.resolve()))
    code = subprocess.call(command, cwd=ROOT, env=env)
    verify(args.lock.resolve())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
