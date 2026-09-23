#!/usr/bin/env python3
"""Entry point: decoupled MCTS over the whole-root (runc-model) sandbox.

The agent_worker pivot_root's into a full-root overlay (ro base image lower +
tmpfs upper); each MCTS node boundary is a DeltaBox checkpoint (CRIU dump of the
worker + overlay hot-switch) and restore. Agent actions anywhere under / (edits,
bash, apt installs) are captured on the overlay and roll back with the node.

Host side (MCTS + NPD + worker) is stdlib-only; runs on the system python. Needs
a real LLM endpoint (DeepSeek is OpenAI-compatible), sudo/root, criu, and the
patched DeltaBox kernel. Re-execs itself inside a private mount namespace so all
mounts / pivot_root / criu are contained.

Usage:
  DEEPSEEK_API_KEY=sk-... python3 run_root_mcts.py \
     --task "..." --base-lower /root/calc_repo --base-dev /dev/vdc \
     --model deepseek-v4-flash --api-base https://api.deepseek.com/v1 \
     --max-iter 6 --max-expansions 2 --verify-cmd "..."
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def reexec_in_mount_ns():
    if os.environ.get("_RMCTS_NS") == "1":
        return
    os.environ["_RMCTS_NS"] = "1"
    argv = " ".join(subprocess.list2cmdline([a]) for a in sys.argv)
    os.execvp("unshare", ["unshare", "-m", "--propagation", "private", "bash", "-c",
                          f"mount --make-rprivate /; exec {sys.executable} {argv}"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--base-lower", required=True, help="task repo (git checkout)")
    ap.add_argument("--base-dev", help="ro base-image block device for overlay lower (e.g. /dev/vdc)")
    ap.add_argument("--base-dir", help="ro base rootfs dir for overlay lower")
    ap.add_argument("--layers-root", default="/dev/shm/dbmcts")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--api-base", default=os.environ.get("API_BASE", "https://api.deepseek.com/v1"))
    ap.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    ap.add_argument("--max-iter", type=int, default=6)
    ap.add_argument("--max-expansions", type=int, default=2)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--verify-cmd", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    reexec_in_mount_ns()

    import shutil
    shutil.rmtree(args.layers_root, ignore_errors=True)
    os.makedirs(args.layers_root, exist_ok=True)
    base_dir = args.base_dir
    if args.base_dev:
        base_dir = os.path.join(args.layers_root, "baseimg")
        os.makedirs(base_dir, exist_ok=True)
        r = subprocess.run(f"mount -t xfs -o ro,nouuid {args.base_dev} {base_dir}",
                           shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(f"mount -o ro {args.base_dev} {base_dir}", shell=True,
                               capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERROR mounting base dev: {r.stderr}"); return 1
    if not base_dir:
        print("ERROR: provide --base-dev or --base-dir"); return 1

    from upper.host.root_mcts import run_root_mcts
    config = {
        "task": args.task, "base_lower": args.base_lower, "base_image": base_dir,
        "layers_root": args.layers_root, "model": args.model, "api_base": args.api_base,
        "api_key": args.api_key, "max_iter": args.max_iter,
        "max_expansions": args.max_expansions, "temperature": args.temperature,
        "verify_cmd": args.verify_cmd, "out": args.out, "log_level": args.log_level,
    }
    result = asyncio.run(run_root_mcts(config))
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
