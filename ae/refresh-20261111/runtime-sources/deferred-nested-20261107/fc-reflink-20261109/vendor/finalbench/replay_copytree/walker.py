"""walker.py — parse a SWE-bench trajectory.json into the ordered sequence of
checkpoint/restore events a replay+copytree baseline would issue.

Output schema for the event stream:

  [
    {"event": "expand", "iter": 0, "new_node": 30, "parent": 0,
     "action_args_class": "...", "action": {...},
     "cursor_for_replay": 0,           # cursor index in the LLM sequence
     "dur_s_of_this_call": 0.86 },     # recorded LLM RTT for this expansion's build_action call
    {"event": "restore", "iter": 1, "target_node": 0,
     "target_cursor_inclusive": -1,    # how far the replay needs to drive moatless (cursor 0..N inclusive)
     "actions_to_replay": [],          # FS-mutating actions on the path root → target, in order
     "llm_replay_cost_s": 0.0 },
    {"event": "expand", "iter": 1, "new_node": 31, "parent": 0, ...},
    ...
  ]

The walker also returns metadata: total expansions, total rollbacks, list of FS-mutating
actions in expansion order, mapping node_id → action_step.
"""
from __future__ import annotations
import json
from pathlib import Path

# Action classes that mutate the working tree. Read-only actions (Find*, ViewCode,
# RunTests, VerifiedFinish) are skipped during restore action replay.
FS_MUTATING = {"StringReplaceArgs", "CreateFileArgs", "AppendStringArgs"}


def _build_parent_map(root):
    pm = {}
    def w(n, pid=None):
        if not isinstance(n, dict): return
        nid = n.get('node_id')
        if nid is not None: pm[nid] = pid
        for c in (n.get('children') or []):
            w(c, nid)
    w(root, None)
    return pm


def _node_to_action_step(root):
    """Return {node_id: action_step_dict_or_None}."""
    out = {}
    def w(n):
        if not isinstance(n, dict): return
        nid = n.get('node_id')
        steps = n.get('action_steps') or []
        # Most nodes have 0 or 1 action_step (the chosen action for that expansion).
        # Root and finished nodes may have 0. Multiple is rare but possible.
        out[nid] = steps[0] if steps else None
        for c in (n.get('children') or []): w(c)
    w(root)
    return out


def _build_action_completions(root):
    """Yield (created_ts, node_id) for every build_action completion."""
    out = []
    def w(n):
        if not isinstance(n, dict): return
        nid = n.get('node_id')
        comps = n.get('completions') or {}
        for purpose, c in comps.items():
            if purpose != 'build_action': continue
            items = c if isinstance(c, list) else [c]
            for ci in items:
                if isinstance(ci, dict) and isinstance(ci.get('response'), dict):
                    out.append((ci['response'].get('created'), nid))
        for c in (n.get('children') or []): w(c)
    w(root)
    # Stable sort: (created, node_id)
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def _load_ms_trace(traj_path: Path) -> list[dict]:
    """Load per-LLM-call timing from co-located ms_trace.jsonl. Each row has
    keys: seq, t_wall_start_s, dur_s, model, n_messages. Indexed by `seq`."""
    p = traj_path.parent / 'ms_trace.jsonl'
    if not p.exists(): return []
    rows = []
    for line in open(p):
        line = line.strip()
        if not line: continue
        rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get('seq', 0))
    return rows


def parse_trajectory(traj_path: str | Path) -> dict:
    """Parse trajectory.json into the ordered event stream + metadata.

    Returns:
      {
        "instance": "django__django-11211",
        "events": [...],
        "n_expansions": 29,
        "n_rollbacks": 28,
        "n_fs_mutating_actions": <int>,
        "node_to_path": {nid: [root, ..., nid]},  # root-to-node path for each node
        "node_to_action": {nid: action_step or None},
        "trajectory_total_dur_s": <sum of all LLM dur_s>,
      }
    """
    traj_path = Path(traj_path)
    with open(traj_path) as f:
        traj = json.load(f)
    root = traj['root']
    inst = (traj.get('metadata') or {}).get('instance_id', traj_path.parent.name)

    parent_of = _build_parent_map(root)
    node_action = _node_to_action_step(root)
    build_actions = _build_action_completions(root)
    ms_trace = _load_ms_trace(traj_path)

    # Build root→node path for each node
    def path_to(nid):
        out = []
        x = nid
        while x is not None:
            out.append(x)
            x = parent_of.get(x)
        return list(reversed(out))
    node_path = {nid: path_to(nid) for nid in parent_of.keys()}

    # Map node → cursor index in the LLM sequence (using build_actions order)
    # cursor_of_node[N] = the index in the flattened LLM-call sequence that produces N
    cursor_of_node = {}
    for cursor, (_, nid) in enumerate(build_actions):
        # The build_action that *creates* nid is the cursor that emits it.
        # Note: multiple build_actions may exist for one node (re-expansion); we pick
        # the first occurrence for cursor_of_node, but the full sequence is still
        # by `created` order.
        if nid not in cursor_of_node:
            cursor_of_node[nid] = cursor

    # Sum dur_s per cursor index from ms_trace
    def dur_at(cursor):
        if 0 <= cursor < len(ms_trace):
            return float(ms_trace[cursor].get('dur_s', 0.0))
        return 0.0

    events = []
    prev_node = None
    n_rollbacks = 0
    n_fs_mut_actions = 0
    for iter_idx, (created, new_nid) in enumerate(build_actions):
        parent_nid = parent_of.get(new_nid)
        cursor = iter_idx
        # Restore event if parent != prev_node (and not first iter)
        if iter_idx > 0 and parent_nid != prev_node:
            n_rollbacks += 1
            target = parent_nid
            # cursor_for_replay: how far moatless needs to drive the LLM sequence
            # to land at target. In our baseline, we re-run moatless from scratch
            # until it has reached target. That means it has consumed up to and
            # including the build_action that *creates target*, which is cursor_of_node[target]
            # — but ONLY if target is non-root. For target=root (0), no LLM calls
            # need replay (we're at the pristine state already).
            if target == 0 or target is None:
                target_cursor_inclusive = -1
                llm_replay_cost = 0.0
            else:
                target_cursor_inclusive = cursor_of_node.get(target, -1)
                llm_replay_cost = sum(dur_at(c) for c in range(target_cursor_inclusive + 1))
            # Actions to replay: walk path root→target, collect FS-mutating action_steps
            actions_path = []
            for nid_on_path in node_path.get(target, []):
                step = node_action.get(nid_on_path)
                if not step: continue
                a = step.get('action') or {}
                cls = a.get('action_args_class', '').split('.')[-1]
                if cls in FS_MUTATING:
                    actions_path.append({
                        "node_id": nid_on_path,
                        "action_args_class": cls,
                        "args": {k: v for k, v in a.items() if k != 'action_args_class'},
                    })
            events.append({
                "event": "restore",
                "iter": iter_idx,
                "target_node": target,
                "target_cursor_inclusive": target_cursor_inclusive,
                "actions_to_replay": actions_path,
                "llm_replay_cost_s": llm_replay_cost,
            })

        # Expand event
        action_step = node_action.get(new_nid)
        action_args = {}
        action_cls = None
        if action_step:
            a = action_step.get('action') or {}
            action_cls = a.get('action_args_class', '').split('.')[-1]
            action_args = {k: v for k, v in a.items() if k != 'action_args_class'}
            if action_cls in FS_MUTATING:
                n_fs_mut_actions += 1
        events.append({
            "event": "expand",
            "iter": iter_idx,
            "new_node": new_nid,
            "parent": parent_nid,
            "action_args_class": action_cls,
            "action_args": action_args,
            "cursor": cursor,
            "dur_s_of_this_call": dur_at(cursor),
        })

        prev_node = new_nid

    trajectory_total_dur_s = sum(float(r.get('dur_s', 0.0)) for r in ms_trace)

    return {
        "instance": inst,
        "events": events,
        "n_expansions": len(build_actions),
        "n_rollbacks": n_rollbacks,
        "n_fs_mutating_actions": n_fs_mut_actions,
        "node_to_path": node_path,
        "node_to_action": node_action,
        "trajectory_total_dur_s": trajectory_total_dur_s,
        "max_iterations": traj.get('max_iterations'),
        "ms_trace_rows": ms_trace,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: walker.py <trajectory.json>", file=sys.stderr)
        sys.exit(2)
    parsed = parse_trajectory(sys.argv[1])
    print(f"instance={parsed['instance']}")
    print(f"n_expansions={parsed['n_expansions']}  n_rollbacks={parsed['n_rollbacks']}")
    print(f"n_fs_mutating_actions={parsed['n_fs_mutating_actions']}")
    print(f"trajectory_total_dur_s={parsed['trajectory_total_dur_s']:.1f}")
    print(f"\nfirst 10 events:")
    for ev in parsed['events'][:10]:
        if ev['event'] == 'expand':
            print(f"  expand  iter={ev['iter']}  node={ev['new_node']}  parent={ev['parent']}  action={ev['action_args_class']}  dur={ev['dur_s_of_this_call']:.2f}s")
        else:
            print(f"  RESTORE iter={ev['iter']}  target=node {ev['target_node']}  cursor=0..{ev['target_cursor_inclusive']}  llm_replay={ev['llm_replay_cost_s']:.1f}s  fs_actions={len(ev['actions_to_replay'])}")
