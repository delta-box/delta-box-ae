#!/usr/bin/env python3
"""Run moatless replay to a specific expansion count.

This is the real replay component for the copytree+replay baseline. It uses the
same mock LLM server and the same moatless SearchTree path as
`spr_payload/replay_driver.py`, but stops after a target number of completed
MCTS expansions. The stop point is after moatless has:

  1. received the recorded build_action response from the mock LLM,
  2. executed the selected action (Find*/View/Edit/RunTests/etc.),
  3. emitted SearchTree's `tree_iteration` event.

That gives the correct restored in-memory search state for "replay from root to
target". Test execution is explicitly selected by DELTABOX_BASELINE_TEST_RUNTIME.
The historical default is none; local-pytest binds real tests and records their
results. Audit mode retains changed messages without claiming input equivalence.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path


PAYLOAD = Path(os.environ["SPR_PAYLOAD"])
if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))

from replay_driver import (  # noqa: E402
    _http_json,
    parse_manifest_line,
    resolve_trajectory,
    rewrite_model_base_url,
    strip_recorded_tree,
)
from baseline_runtime import build_runtime, check_runtime, runtime_records
from baseline_audit import message_policy, stats_ok  # noqa: E402


log = logging.getLogger("real_replay_driver")


class TargetReached(Exception):
    """Raised from SearchTree event handler after the target expansion."""


def run_to_expansion(
    manifest_line: str,
    traces_root: Path,
    mock_port: int,
    repo_path: Path,
    index_store_dir: Path,
    target_expansions: int,
) -> dict:
    if target_expansions <= 0:
        return {
            "ok": True,
            "status": "ROOT",
            "target_expansions": target_expansions,
            "completed_expansions": 0,
            "wall_s": 0.0,
            "mock_stats": None,
        }

    instance_id, variant = parse_manifest_line(manifest_line)
    traj_path = resolve_trajectory(traces_root, instance_id, variant)
    if not traj_path.exists():
        raise FileNotFoundError(f"trajectory not found: {traj_path}")
    if not repo_path.exists():
        raise FileNotFoundError(f"repo not found: {repo_path}")

    mock_url_base = f"http://127.0.0.1:{mock_port}"
    load_resp = _http_json(
        f"{mock_url_base}/admin/load",
        "POST",
        {"instance_id": instance_id, "variant": variant},
        timeout=30.0,
    )
    log.info("mock loaded: %s", load_resp)

    with open(traj_path) as f:
        data = json.load(f)
    data = strip_recorded_tree(data)
    rewrite_model_base_url(data, f"{mock_url_base}/v1")

    from moatless.index import CodeIndex
    from moatless.repository.file import FileRepository
    from moatless.search_tree import SearchTree

    repo = FileRepository(repo_path=str(repo_path))
    index_store_dir.mkdir(parents=True, exist_ok=True)
    code_index = CodeIndex.from_index_name(
        instance_id,
        file_repo=repo,
        index_store_dir=str(index_store_dir),
    )
    runtime = build_runtime(repo, data, code_index=code_index)
    tree = SearchTree.from_dict(data, repository=repo, code_index=code_index, runtime=runtime)

    completed = 0
    last_event = None

    def on_event(event: dict) -> None:
        nonlocal completed, last_event
        if event.get("event_type") != "tree_iteration":
            return
        check_runtime(runtime)
        completed += 1
        last_event = event
        log.info(
            "completed expansion %d/%d: %s",
            completed,
            target_expansions,
            event.get("data"),
        )
        if completed >= target_expansions:
            raise TargetReached()

    tree.add_event_handler(on_event)

    t0 = time.perf_counter()
    status = "UNKNOWN"
    error = None
    try:
        tree.run_search()
        status = "SEARCH_FINISHED_BEFORE_TARGET"
    except TargetReached:
        status = "TARGET_REACHED"
    except Exception as e:  # keep stats for diagnosis
        status = "CRASH"
        error = f"{type(e).__name__}: {e}"
        log.exception("replay crashed")
    wall_s = time.perf_counter() - t0

    try:
        stats = _http_json(f"{mock_url_base}/admin/stats", "GET", timeout=10.0)
    except Exception as e:
        stats = {"ok": False, "error": repr(e)}

    ok = (
        status == "TARGET_REACHED"
        and completed >= target_expansions
        and stats_ok(stats)
    )

    return {
        "ok": ok,
        "status": status,
        "error": error,
        "instance": instance_id,
        "variant": variant,
        "target_expansions": target_expansions,
        "completed_expansions": completed,
        "wall_s": wall_s,
        "mock_stats": stats,
        "message_policy": message_policy(),
        "last_event": last_event,
        "test_runtime_records": runtime_records(runtime),
    }


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get('BASELINE_LOG_LEVEL', 'WARNING').upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-line", required=True)
    ap.add_argument("--traces-root", required=True)
    ap.add_argument("--mock-port", type=int, required=True)
    ap.add_argument("--repo-base", required=True)
    ap.add_argument("--index-store-dir", required=True)
    ap.add_argument("--target-expansions", type=int, required=True)
    ap.add_argument("--summary-json", required=True)
    args = ap.parse_args()

    instance_id, _ = parse_manifest_line(args.manifest_line)
    repo_path = Path(args.repo_base) / f"swe-bench_{instance_id}"

    summary = run_to_expansion(
        manifest_line=args.manifest_line,
        traces_root=Path(args.traces_root),
        mock_port=args.mock_port,
        repo_path=repo_path,
        index_store_dir=Path(args.index_store_dir),
        target_expansions=args.target_expansions,
    )

    out = Path(args.summary_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)

    if not summary.get('ok') or os.environ.get('BASELINE_LOG_LEVEL') == 'DEBUG':
        print(json.dumps(summary, sort_keys=True))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    sys.exit(main())
