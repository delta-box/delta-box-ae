#!/usr/bin/env python3
"""Entry point for the DeltaBox runtime: run the live MCTS coding agent.

Paper-form (default, --mode decoupled): the MCTS strategy runs on the HOST and
drives a single-threaded, fork-safe agent worker INSIDE the sandbox. The worker
runs the ReAct step (action decided by a live LLM reached through the Network
Proxy Daemon, then executed against the overlay-mounted repo) and holds its
reasoning context in its own memory; each node boundary is a real DeltaBox
checkpoint/restore (warm-template fork + async incremental CRIU dump + OverlayFS
layer switch). A warm-fork restore reconstitutes a node's worker memory in
milliseconds — the value a checkpoint of this process actually buys.

    sudo -E /path/to/venv/bin/python run.py \
        --task "fix the bug in ..." --base-lower /path/to/repo \
        --workdir /tmp/agent_run --model deepseek-v4-flash \
        --api-base https://api.deepseek.com/v1 --api-key "$DEEPSEEK_API_KEY" \
        --max-iter 6 --max-expansions 2

REQUIRES sudo (mounts overlayfs, runs CRIU) and the patched DeltaBox OverlayFS
kernel for the filesystem half of C/R. Endpoint/model/key fall back to
API_BASE / MODEL_NAME / API_KEY (or DEEPSEEK_API_KEY).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# make the runtime root importable as the package root (namespace packages).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="DeltaBox runtime — live MCTS coding agent")
    ap.add_argument("--task", required=True, help="user task / problem statement")
    ap.add_argument("--instance-id", dest="instance_id", default="",
                    help="SWE-bench instance id (recorded in the trajectory)")
    ap.add_argument("--base-lower", dest="base_lower",
                    default=os.environ.get("DELTABOX_AGENT_BASE_LOWER"),
                    help="overlayfs lowerdir = the repository checkout to work on")
    ap.add_argument("--workdir",
                    default=os.environ.get("DELTABOX_AGENT_WORKDIR",
                                           "/tmp/deltabox_agent_run"),
                    help="all runtime state (overlay upper/work, trajectory) goes here")
    ap.add_argument("--out", default=None, help="patch output file (decoupled mode)")
    ap.add_argument("--api-base", dest="api_base",
                    default=os.environ.get("API_BASE", "https://api.deepseek.com/v1"))
    ap.add_argument("--model", default=os.environ.get("MODEL_NAME", "deepseek-v4-flash"))
    ap.add_argument("--api-key", dest="api_key",
                    default=os.environ.get("API_KEY")
                    or os.environ.get("DEEPSEEK_API_KEY"))
    ap.add_argument("--api-key-env", default=None, help="read AK from this environment variable; do not put AK in command history")
    ap.add_argument("--checkpoint-profile", choices=("runtime-default", "historical-async-full"), default="runtime-default")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-iter", dest="max_iter", type=int, default=8)
    ap.add_argument("--max-expansions", dest="max_expansions", type=int, default=1,
                    help="1 = linear chain; >1 = MCTS branching")
    ap.add_argument("--apply-test-patch", dest="apply_test_patch", action="store_true")
    ap.add_argument("--verify-candidates", dest="verify_candidates", action="store_true")
    ap.add_argument("--host-verify-feedback", dest="host_verify_feedback",
                    action="store_true")
    ap.add_argument("--no-warm-template", dest="warm_template", action="store_false",
                    help="disable the warm-template fork + incremental dump flow "
                         "(fall back to a full CRIU dump/restore each checkpoint)")
    ap.add_argument("--no-clean", dest="clean", action="store_false",
                    help="do not rm -rf the workdir first")
    ap.add_argument("--mode", choices=["decoupled", "moatless"], default="decoupled",
                    help="decoupled = paper form (host MCTS + in-sandbox worker + "
                         "NPD); moatless = legacy monolithic host agent")
    ap.add_argument("--verify-cmd", dest="verify_cmd", default=None,
                    help="shell command run in the repo to score finish nodes "
                         "(rc 0 = solved); decoupled mode only")
    ap.add_argument("--log-level", dest="log_level", default="INFO")
    ap.set_defaults(clean=True, warm_template=True)
    args = ap.parse_args(argv)
    if args.api_key_env:
        args.api_key = os.environ.get(args.api_key_env)
    if not args.api_key or args.api_key == "EMPTY":
        ap.error("a real API key is required; set --api-key-env or API_KEY (replay/ needs no key)")
    if not args.base_lower:
        ap.error("--base-lower is required")
    if min(args.max_iter, args.max_expansions) < 1:
        ap.error("iteration and expansion counts must be positive")
    from pathlib import Path
    base, work = Path(args.base_lower).resolve(), Path(args.workdir).resolve()
    repo = Path(__file__).resolve().parents[1]
    if not base.is_dir():
        ap.error("--base-lower must be an existing repository directory")
    if (work in (Path('/'), Path.home()) or work.is_relative_to(base)
            or base.is_relative_to(work) or repo.is_relative_to(work) or work.is_relative_to(repo)):
        ap.error("--workdir must be separate from the source checkout, runtime repository and home directory")
    if work.exists() and any(work.iterdir()) and args.clean:
        ap.error("--workdir is nonempty; use a fresh directory (no automatic deletion of existing data)")
    return args


def main():
    args = parse_args()
    if args.mode == "decoupled":
        import asyncio
        from common.runtime_profile import checkpoint_environment
        os.environ.update(checkpoint_environment(args.checkpoint_profile))
        from agent.host.decoupled_mcts import run_decoupled
        result = asyncio.run(run_decoupled(vars(args)))
    else:
        # Legacy monolithic path (moatless agent on the host).
        from upper.agent.run_eval import run_from_config
        result = run_from_config(vars(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
