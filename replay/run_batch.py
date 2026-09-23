#!/usr/bin/env python3
"""Run the paper's twelve DeltaBox traces sequentially with current sources."""
from __future__ import annotations

import argparse
import json
import shlex
import signal
import sys
from pathlib import Path

from host_execution import Lane, build_instance_command, execute_instance, write_json
from run_instance import REPO_ROOT, TABLE_ROOT, add_run_options, interrupted, prepare_single_run
from summarize import summarize

GROUPS = {"django": "django", "sympy": "sympy", "matplotlib": "sci", "astropy": "sci",
          "pylint-dev": "tools", "psf": "tools"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--instance")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--inputs-root", type=Path,
                        default=REPO_ROOT / "ae/paper/table-02/data/inputs/deltabox")
    parser.add_argument("--images-dir", type=Path)
    parser.add_argument("--data-xfs", type=Path)
    parser.add_argument("--schedule", type=Path, help="Prepared schedule plus .meta.json, single instance only")
    add_run_options(parser, modes=("fast", "slow", "both"))
    parser.set_defaults(mode="both")
    args = parser.parse_args(argv)
    if args.all and (args.schedule or args.data_xfs or args.testbed):
        parser.error("--schedule, --data-xfs and --testbed require --instance")
    if args.images_dir is None and args.data_xfs is None:
        parser.error("--images-dir or --data-xfs is required")
    return args


def run_batch(args) -> int:
    instances = (TABLE_ROOT / "instances.txt").read_text().splitlines() if args.all else [args.instance]
    modes = ("fast", "slow") if args.mode == "both" else (args.mode,)
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "preparing", "instances": instances, "modes": modes,
                "runs": [], "sequential": True, "frequency_changes": False}
    manifest_path = output / "batch.json"
    write_json(manifest_path, manifest)
    try:
        runs = []
        for mode in modes:
            for instance in instances:
                image = args.data_xfs
                if image is None:
                    group = GROUPS.get(instance.split("__", 1)[0])
                    if group is None:
                        raise ValueError(f"unknown image group: {instance}; supply --data-xfs")
                    image = args.images_dir / f"data-{group}.xfs"
                options = argparse.Namespace(**vars(args))
                options.instance, options.mode, options.data_xfs = instance, mode, image
                options.trace_dir = None if args.schedule else args.inputs_root / instance
                options.image_hash_cache = args.image_hash_cache or output / "image-hashes.json"
                spec = prepare_single_run(options, existing_output=True)
                runs.append(spec)
                manifest["runs"].append(str(spec.config_path))
        manifest["status"] = "prepared" if args.dry_run else "running"
        write_json(manifest_path, manifest)
        for spec in runs:
            if args.dry_run:
                print(shlex.join(build_instance_command(spec, Lane(0))), flush=True)
            else:
                execute_instance(spec)
        if not args.dry_run:
            summarize(output)
            manifest["status"] = "ok"
        write_json(manifest_path, manifest)
        return 0
    except BaseException as error:
        manifest.update(status="failed", error=str(error) or type(error).__name__)
        write_json(manifest_path, manifest)
        raise


def main(argv=None) -> int:
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    return run_batch(parse_args(argv))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
