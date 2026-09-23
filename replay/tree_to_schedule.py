#!/usr/bin/env python3
"""qwen3_tree_to_schedule.py — Convert moatless-tree-search tree-format
trajectory.json (root + children) into a replay schedule (jsonl), output
compatible with runner_vm.py / firecracker_replay.py / cloudhypervisor_replay.py.

The earlier swesearch_to_schedule.py reads a flat `transitions` list, which
moatless-tree-search **no longer produces** as of the version we ran for Qwen3.
Trajectories now have a `root` node with `children` and per-node `node_id`,
`is_duplicate`, `terminal`, `action_steps`, `completions`, etc.

We walk the tree in MCTS expansion order. The expansion order is the order
in which `node_id` values were assigned (monotonic by the search loop).
Between consecutive expansions, a rollback happens if the new node's parent
is not the previously-expanded node. For each new node we emit:
  - {"type": "restore", ...} if rolling back to an earlier ckpt
  - {"type": "ckpt", ...} for the newly-expanded node

Strategy classification:
  - If any action_step has an action.name in {"StringReplace", "CreateFile",
    "AppendString", "InsertLine", "RewriteFile"}, the node is FS-mutating →
    standard path.
  - All other action names (FindFunction, SemanticSearch, ViewCode, Finish, etc.)
    are non-mutating → lightweight path.

Output schedule format (jsonl, one event per line):
  {"type": "ckpt",    "iter": int, "ckpt_id": str, "strategy": "lightweight"|"standard",
   "dump_size_mb": float}
  {"type": "restore", "iter": int, "restore_to_ckpt_id": str}

Also emits a sibling llm_rtt/<inst>.json with per-call LLM RTT deltas (from
`response.created` timestamps), for replay-time LLM injection.

Usage:
  python3 qwen3_tree_to_schedule.py \
      --traj /path/to/trajectory.json \
      --instance pallets__flask-4992 \
      --out-schedule schedules/pallets__flask-4992.jsonl \
      --out-llm-rtt llm_rtt/pallets__flask-4992.json
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

FS_MUTATING_ACTIONS = {
    # Match `action_args_class` short names (class name only, module stripped).
    # Source: actual frequencies in Qwen3 cohort48 mcts-iter100 trajectories.
    "StringReplaceArgs", "CreateFileArgs", "AppendStringArgs",
    "InsertLineArgs", "RewriteFileArgs",
    # Test execution is not FS-mutating in the edit sense, but it is expensive,
    # spawns subprocesses, and can legitimately time out.  Treat it as a real
    # checkpoint boundary instead of replaying it on every LW restore.
    "RunTestsArgs",
    # Legacy state-name form (older swe-search runs)
    "EditCode",
}


def action_to_worker_ops(action: dict) -> list[dict]:
    """Translate recorded moatless action args into executable worker ops.

    These ops are deliberately handled by the checkpointed guest/agent.py
    process.  The host replay driver only sends the request; it no longer
    mutates /testbed on behalf of the worker.
    """
    if not isinstance(action, dict):
        return []
    cls = (action.get("action_args_class") or "").rsplit(".", 1)[-1]
    base = {"action_class": cls}
    if cls == "ViewCodeArgs":
        ops = []
        for file_row in action.get("files") or []:
            path = file_row.get("file_path")
            if path:
                ops.append({**base, "type": "view_file", "path": path,
                            "start_line": file_row.get("start_line"),
                            "end_line": file_row.get("end_line")})
        return ops or [{**base, "type": "noop", "note": "view_code_no_files"}]
    if cls == "FindClassArgs":
        return [{
            **base,
            "type": "find_symbol",
            "symbol_kind": "class",
            "file_pattern": action.get("file_pattern") or "**/*.py",
            "name": action.get("class_name") or "",
        }]
    if cls == "FindFunctionArgs":
        return [{
            **base,
            "type": "find_symbol",
            "symbol_kind": "function",
            "file_pattern": action.get("file_pattern") or "**/*.py",
            "name": action.get("function_name") or "",
        }]
    if cls == "FindCodeSnippetArgs":
        return [{
            **base,
            "type": "grep",
            "file_pattern": action.get("file_pattern") or "**/*",
            "pattern": action.get("code_snippet") or "",
        }]
    if cls == "StringReplaceArgs":
        return [{
            **base,
            "type": "replace",
            "path": action.get("path"),
            "old_str": action.get("old_str", ""),
            "new_str": action.get("new_str", ""),
        }]
    if cls == "CreateFileArgs":
        return [{
            **base,
            "type": "write_file",
            "path": action.get("path"),
            "content": action.get("file_text", ""),
        }]
    if cls == "AppendStringArgs":
        return [{
            **base,
            "type": "append_file",
            "path": action.get("path"),
            "content": action.get("new_str", ""),
        }]
    if cls == "RunTestsArgs":
        return [{
            **base,
            "type": "run_tests",
            "test_files": action.get("test_files") or [],
        }]
    if cls == "VerifiedFinishArgs":
        return [{**base, "type": "noop", "note": "verified_finish"}]
    return [{**base, "type": "noop", "note": f"unhandled_action:{cls}"}]


def node_worker_ops(node: dict) -> list[dict]:
    ops: list[dict] = []
    for step_idx, step in enumerate(node.get("action_steps", []) or []):
        action = step.get("action") or {}
        for op in action_to_worker_ops(action):
            op.setdefault("action_step_idx", step_idx)
            # A recorded tool failure is part of the workload, not proof of a
            # broken replay environment. Carry only this narrowly specified
            # observation; the guest must execute the action and match it.
            observation = step.get("observation") or {}
            if not isinstance(observation, dict):
                observation = {}
            properties = observation.get("properties") or {}
            files = op.get("test_files")
            if (op.get("type") == "run_tests" and isinstance(files, list) and files
                    and all(isinstance(name, str) and name for name in files)
                    and isinstance(properties, dict) and properties.get("fail_reason") == "no_test_files"):
                for kind, detail in (("missing_test_files", "Files not found: "),
                                     ("test_directories", "Directories provided instead of files: ")):
                    if observation.get("message") == "Unable to run tests: " + detail + ", ".join(files):
                        op["expected_outcome"] = {
                            "kind": kind, "test_files": files,
                            "recorded_message": observation["message"],
                            "recorded_fail_reason": "no_test_files",
                        }
            ops.append(op)
    return ops


def _ckpt_id(node_id: int, instance_id: str) -> str:
    h = hashlib.sha1(f"{instance_id}::{node_id}".encode()).hexdigest()
    return h[:8]


def walk_with_parents(root: dict, parent: dict | None = None):
    """Yield (node, parent) tuples in pre-order DFS."""
    yield (root, parent)
    for child in root.get("children", []) or []:
        yield from walk_with_parents(child, root)


def node_strategy(node: dict) -> str:
    for step in node.get("action_steps", []) or []:
        act = step.get("action") or {}
        if not isinstance(act, dict):
            continue
        # Prefer the explicit class name (current moatless convention)
        cls = act.get("action_args_class")
        if isinstance(cls, str):
            short = cls.rsplit(".", 1)[-1]
            if short in FS_MUTATING_ACTIONS:
                return "standard"
        # Fallback for legacy traces using `name`
        name = act.get("name")
        if name in FS_MUTATING_ACTIONS:
            return "standard"
    return "lightweight"


def collect_llm_calls(root: dict) -> list[dict]:
    """Walk and collect every LLM call: {created, prompt_tokens, completion_tokens}.

    A call lives in:
      - node.completions[<name>].response.created + .usage
      - node.action_steps[*].completion.response.created + .usage
    """
    out = []
    for node, _ in walk_with_parents(root):
        for c in (node.get("completions") or {}).values():
            if not isinstance(c, dict):
                continue
            r = c.get("response") or {}
            u = c.get("usage") or {}
            ts = r.get("created") if isinstance(r, dict) else None
            if ts:
                out.append({
                    "created": ts,
                    "prompt_tokens": (u or {}).get("prompt_tokens", 0),
                    "completion_tokens": (u or {}).get("completion_tokens", 0),
                })
        for step in node.get("action_steps") or []:
            c = step.get("completion") or {}
            if not isinstance(c, dict):
                continue
            r = c.get("response") or {}
            u = c.get("usage") or {}
            ts = r.get("created") if isinstance(r, dict) else None
            if ts:
                out.append({
                    "created": ts,
                    "prompt_tokens": (u or {}).get("prompt_tokens", 0),
                    "completion_tokens": (u or {}).get("completion_tokens", 0),
                })
    # Deduplicate, sort by created time
    seen = set()
    uniq = []
    for c in out:
        key = (c["created"], c["prompt_tokens"], c["completion_tokens"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(key=lambda x: x["created"])
    return uniq


def convert(traj_path: Path, instance_id: str) -> tuple[list[dict], dict]:
    """Return (schedule_events, llm_rtt_record)."""
    t = json.loads(traj_path.read_text())
    root = t.get("root")
    if root is None:
        raise SystemExit(f"trajectory has no `root` node: {traj_path}")

    # Order nodes by node_id (MCTS expansion order, monotonic).
    nodes_with_parents = list(walk_with_parents(root))
    # Build node_id → (node, parent) lookup
    by_id = {n.get("node_id"): (n, p) for n, p in nodes_with_parents if n.get("node_id") is not None}
    if not by_id:
        raise SystemExit("no nodes with node_id found")
    expansion_order = sorted(by_id.keys())

    schedule: list[dict] = []
    node_to_ckpt: dict[int, str] = {}
    iter_i = 1
    previously_expanded_node_id = None

    for nid in expansion_order:
        node, parent = by_id[nid]
        if parent is None:
            # root — emit an initial ckpt so subsequent restores can target it.
            cid = _ckpt_id(nid, instance_id)
            node_to_ckpt[nid] = cid
            schedule.append({
                "type": "ckpt",
                "iter": iter_i,
                "ckpt_id": cid,
                # root must be standard (full CRIU dump) — provides the
                # ancestor anchor that deltabox's LW chain restores rely on.
                "strategy": "standard",
                "dump_size_mb": 7.8,
                "worker_ops": [],
                "worker_ops_required": False,
            })
            iter_i += 1
            previously_expanded_node_id = nid
            continue

        parent_id = parent.get("node_id")

        # Detect rollback: previously-expanded node was somewhere else.
        if previously_expanded_node_id != parent_id:
            # MCTS jumped: emit a restore to parent's ckpt
            target_ckpt = node_to_ckpt.get(parent_id)
            if target_ckpt is not None:
                schedule.append({
                    "type": "restore",
                    "iter": iter_i,
                    "restore_to_ckpt_id": target_ckpt,
                })
                iter_i += 1

        # Skip nodes that didn't actually run (duplicates, terminal-without-action)
        if node.get("is_duplicate"):
            previously_expanded_node_id = nid
            continue

        cid = _ckpt_id(nid, instance_id)
        node_to_ckpt[nid] = cid
        strat = node_strategy(node)
        worker_ops = node_worker_ops(node)
        schedule.append({
            "type": "ckpt",
            "iter": iter_i,
            "ckpt_id": cid,
            "strategy": strat,
            "dump_size_mb": 7.8 if strat == "standard" else 0.0,
            "node_id": nid,
            "worker_ops": worker_ops,
            "worker_ops_required": True,
        })
        iter_i += 1
        previously_expanded_node_id = nid

    # LLM RTT extraction.
    # OpenAI `response.created` is Unix-second precision; same-second pairs
    # show delta=0 but the actual interval is uniformly distributed in [0,1000) ms.
    # Impute the expected midpoint 0.5 s so the LLM-only floor doesn't
    # systematically undercount. Filter unrealistic gaps >=600 s as outliers
    # (timestamp drift across MCTS pauses).
    llm_calls = collect_llm_calls(root)
    deltas = []
    n_imputed = 0
    for i in range(1, len(llm_calls)):
        d = llm_calls[i]["created"] - llm_calls[i-1]["created"]
        if 0 <= d < 600:
            if d == 0:
                d = 0.5
                n_imputed += 1
            deltas.append(float(d))
    llm_rtt = {
        "n_calls": len(llm_calls),
        "n_deltas": len(deltas),
        "n_same_second_imputed": n_imputed,
        "imputed_seconds_per_pair": 0.5,
        "mean_rtt_s": sum(deltas) / max(1, len(deltas)),
        "deltas_s": deltas,
        "first_call_ts": llm_calls[0]["created"] if llm_calls else None,
    }
    return schedule, llm_rtt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--traj", required=True)
    ap.add_argument("--instance", required=True)
    ap.add_argument("--out-schedule", required=True)
    ap.add_argument("--out-llm-rtt", required=True)
    args = ap.parse_args()

    schedule, llm_rtt = convert(Path(args.traj), args.instance)

    Path(args.out_schedule).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_schedule, "w") as f:
        for e in schedule:
            f.write(json.dumps(e) + "\n")

    Path(args.out_llm_rtt).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_llm_rtt).write_text(json.dumps(llm_rtt, indent=2))

    n_ckpt = sum(1 for e in schedule if e["type"] == "ckpt")
    n_rest = sum(1 for e in schedule if e["type"] == "restore")
    n_lw   = sum(1 for e in schedule if e["type"] == "ckpt" and e["strategy"] == "lightweight")
    n_std  = sum(1 for e in schedule if e["type"] == "ckpt" and e["strategy"] == "standard")
    print(f"{args.instance}: {len(schedule)} events "
          f"({n_ckpt} ckpt = {n_lw} LW + {n_std} std, {n_rest} restore). "
          f"LLM: {llm_rtt['n_calls']} calls, mean RTT {llm_rtt['mean_rtt_s']:.2f}s")
    print(f"  saved schedule: {args.out_schedule}")
    print(f"  saved llm_rtt : {args.out_llm_rtt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
