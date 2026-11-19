"""trajectory_index.py — load + flatten + sort + hash a moatless trajectory.json.

Each trajectory.json is the root of a MCTS tree. Every node may carry multiple
LLM `completions` keyed by purpose (`build_action`, `value`, `discriminator`,
...). For replay we flatten the tree to a timestamp-ordered sequence:

    [(timestamp, completion_dict), ...]   sorted by response.created ascending

and build the assertion hash table

    {canonical_messages_hash(input): expected_sequence_index, ...}

so we can verify each replay request lands at the cursor we expect (catches
silent drift).

Run as module to dump stats on a trajectory file:
    python -m benchmarks.finalbench.trajectory_index <trajectory.json>
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from protocol import canonical_messages_hash


@dataclass(frozen=True)
class Completion:
    """One LLM call from the recording."""
    seq: int                # position in the timestamp order sequence
    node_id: int            # MCTS node where this call originated
    purpose: str            # completion key: build_action / value / exec.<Action> / ...
    created: int            # response.created (unix ts) — sort key
    input_hash: str         # sha256 of canonical(input messages) — assertion key
    input: list[dict]       # wire-sent messages (after stripping any trailing
                            # assistant-response that moatless's completion.py
                            # appended post-response)
    response: dict          # raw response (chat.completion object)
    model: str
    usage: dict             # token usage from response
    dur_s: float            # recorded RTT from ms_trace.jsonl (0.0 if not found)


def _walk_nodes(node: dict):
    """Yield (node_id, purpose, completion_dict) for every completion in the tree.

    Two storage locations honored:
      1. `node.completions[<purpose>]` — top-level (build_action, value, etc.)
         These go through moatless's react.py path which does NOT append the
         assistant response to `messages`, so `input` == wire-sent messages.
      2. `node.action_steps[i].observation.execution_completion` — completions
         from action-execution-time sub-LLM calls (e.g. IdentifyMixin in
         FindClass/FindFunction/ViewCode when search results exceed
         max_search_tokens). These go through completion.py base
         `create_completion` which DOES append the assistant response to
         `messages` before recording, so `input` has one EXTRA trailing
         message vs wire-sent.

    The `purpose` field encodes both:
      - "<key>" for node.completions[key]
      - "exec.<action_name>" for execution_completion (action_name extracted
        from the corresponding action_step.action_args_class)
    """
    if not isinstance(node, dict):
        return
    nid = node.get("node_id")
    comps = node.get("completions") or {}
    if isinstance(comps, dict):
        for purpose, c in comps.items():
            if isinstance(c, dict) and isinstance(c.get("response"), dict):
                yield nid, purpose, c
            elif isinstance(c, list):
                # some nodes have list-valued completions (re-expansion produces
                # multiple build_action calls). preserve list order.
                for c_i in c:
                    if isinstance(c_i, dict) and isinstance(c_i.get("response"), dict):
                        yield nid, purpose, c_i
    for step in (node.get("action_steps") or []):
        if not isinstance(step, dict):
            continue
        obs = step.get("observation") or {}
        ec = obs.get("execution_completion")
        if isinstance(ec, dict) and isinstance(ec.get("response"), dict):
            # Identify which action triggered this exec call
            action = step.get("action") or {}
            ac = action.get("action_args_class", "") or ""
            action_name = ac.split(".")[-1].replace("Args", "") if ac else "unknown"
            yield nid, f"exec.{action_name}", ec
    for child in node.get("children", []) or []:
        yield from _walk_nodes(child)


_RETRY_USER_PREFIXES = (
    "The response was invalid",                  # react.py format-parse retry
    "The identified code sections are too large",  # identify_mixin / search_base size retry
)


def _strip_trailing_response(input_msgs: list[dict], response: dict) -> list[dict]:
    """Canonicalise recorded `input` back to FIRST-WIRE-CALL state.

    Three independent strips applied iteratively:

    1. Trailing assistant message that duplicates the LLM response — this is
       the post-call `messages.append({"role":"assistant","content":...})`
       moatless does at completion.py:215 (NOT in react.py). Only the very
       last message can be this.

    2. Trailing [assistant, user("The response was invalid...")] pair — this
       is moatless's react.py retry-padding (line ~145): when ReAct format
       parse fails, the failed assistant response + an error user message
       get appended to `messages` before retrying. trajectory.json stores
       only the FINAL successful call's Completion, but its `input` reflects
       the ACCUMULATED messages including all prior retry-pair padding.
       The first wire call (before any retries) sent only the head — that's
       what replay-moatless will send.

    Net effect: replay-moatless sends an n=2 wire on its first build_action
    call (just [system, user_task]) and mock's strip yields the same n=2,
    hash matches, mock returns the recorded (already-valid) response,
    moatless parses successfully → no retry → trajectory replayed faithfully.
    """
    if not input_msgs:
        return input_msgs

    # Compute the recorded response.choices[0].message.content once
    try:
        resp_content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        resp_content = None
    if isinstance(resp_content, dict):
        resp_content = json.dumps(resp_content, sort_keys=True)

    msgs = list(input_msgs)

    # Iteratively strip ALL of:
    #   A) trailing [assistant, user("retry-error...")] pair (a failed retry)
    #   B) bare trailing assistant message (residue of completion.py's post-call
    #      `messages.append(assistant_msg)` for ANY call in a retry chain —
    #      including the final successful call whose assistant equals
    #      response.choices[0].message.content)
    # Repeat until neither pattern matches.
    while True:
        changed = False
        # A: retry-error pair
        if len(msgs) >= 2:
            last, prev = msgs[-1], msgs[-2]
            if (isinstance(last, dict) and last.get("role") == "user"
                and isinstance(last.get("content"), str)
                and any(last["content"].startswith(p) for p in _RETRY_USER_PREFIXES)
                and isinstance(prev, dict) and prev.get("role") == "assistant"):
                msgs.pop(); msgs.pop()
                changed = True
                continue
        # B: bare trailing assistant (only safe to strip if length >= 2 — we
        # never want to remove everything; and only if it's not a build_action
        # path where input naturally ends with user → assistant won't be last)
        if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "assistant":
            msgs.pop()
            changed = True
            continue
        if not changed:
            break

    # Strip leading duplicate system prompts (moatless's create_completion
    # mutates `messages` in place and prepends system on each retry → recording's
    # `input` has N copies of system_prompt for an N-retry call).
    while len(msgs) >= 2:
        if (isinstance(msgs[0], dict) and msgs[0].get("role") == "system"
            and isinstance(msgs[1], dict) and msgs[1].get("role") == "system"
            and msgs[0].get("content") == msgs[1].get("content")):
            msgs.pop(0)
            continue
        break

    return msgs


def load_trajectory(path: str | Path) -> list[Completion]:
    """Load + flatten + sort a single trajectory.json into a sequence.

    Returns a list of Completion ordered by `response.created` ascending.

    Per-replay design: mock serves `sequence[cursor]` and advances cursor on
    each call. The `input_hash` field is for assertion at serve time — we
    check `hash(request.messages) == sequence[cursor].input_hash` to catch
    drift. Duplicate hashes within a sequence are EXPECTED — MCTS sibling
    expansions naturally use the same parent-prompt with `temperature>0` to
    get diverse responses, so two sibling completions have identical inputs
    but different recorded outputs.
    """
    path = Path(path)
    with open(path) as f:
        traj = json.load(f)
    raw: list[tuple[int, int, str, dict]] = []
    for nid, purpose, c in _walk_nodes(traj.get("root", {})):
        created = c["response"].get("created")
        if not isinstance(created, (int, float)):
            continue
        raw.append((int(created), nid, purpose, c))
    raw.sort(key=lambda x: (x[0], x[1], x[2]))  # ts -> node_id -> purpose for ties

    # Optional: load co-located ms_trace.jsonl for recorded dur_s per call.
    # Match by H2 hypothesis (response.created ≈ t_wall_start_s + dur_s)
    # with a 3-second tolerance window.
    ms_path = path.parent / "ms_trace.jsonl"
    ms_rows: list[dict] = []
    if ms_path.exists():
        try:
            ms_rows = [json.loads(l) for l in open(ms_path) if l.strip()]
        except Exception:
            ms_rows = []
    ms_unused = list(range(len(ms_rows)))

    def best_dur_s(created: int) -> float:
        if not ms_unused:
            return 0.0
        # H2: ms.t_wall_start_s + ms.dur_s ≈ created (LLM call end time)
        best_idx = None
        best_diff = float("inf")
        for idx in ms_unused:
            ms = ms_rows[idx]
            d = abs(ms["t_wall_start_s"] + ms["dur_s"] - created)
            if d < best_diff:
                best_diff = d
                best_idx = idx
        if best_idx is None or best_diff > 3.0:
            return 0.0
        ms_unused.remove(best_idx)
        return float(ms_rows[best_idx]["dur_s"])

    sequence: list[Completion] = []
    for seq, (created, nid, purpose, c) in enumerate(raw):
        raw_input = c.get("input") or []
        # Strip moatless's post-call assistant append (only present on the
        # completion.py base path, not the react.py build_action path).
        wire_input = _strip_trailing_response(raw_input, c["response"])
        h = canonical_messages_hash(wire_input)
        usage = c["response"].get("usage") or c.get("usage") or {}
        model = c["response"].get("model") or c.get("model") or ""
        sequence.append(Completion(
            seq=seq, node_id=nid, purpose=purpose, created=created,
            input_hash=h, input=wire_input, response=c["response"],
            model=model, usage=usage, dur_s=best_dur_s(created),
        ))
    return sequence


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m benchmarks.finalbench.trajectory_index <trajectory.json>",
              file=sys.stderr)
        return 2
    p = sys.argv[1]
    try:
        seq = load_trajectory(p)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"loaded {p}")
    print(f"  total completions: {len(seq)}")
    unique = len({s.input_hash for s in seq})
    dups = len(seq) - unique
    print(f"  unique input hashes: {unique}  ({dups} duplicates — MCTS sibling expansions)")
    matched = sum(1 for s in seq if s.dur_s > 0)
    print(f"  ms_trace dur_s matched: {matched}/{len(seq)}")
    if matched:
        durs = [s.dur_s for s in seq if s.dur_s > 0]
        import statistics
        print(f"  dur_s: median={statistics.median(durs):.2f}s "
              f"min={min(durs):.2f}s max={max(durs):.2f}s "
              f"sum={sum(durs):.1f}s")
    if seq:
        print(f"  ts range: {seq[0].created} → {seq[-1].created}  "
              f"({seq[-1].created - seq[0].created}s span)")
        # purpose distribution
        from collections import Counter
        purpose_counts = Counter(s.purpose for s in seq)
        print("  purpose mix:")
        for p_, n in purpose_counts.most_common():
            print(f"    {p_:30s} {n:5d}")
        # node depth distribution
        node_counts = Counter(s.node_id for s in seq)
        print(f"  spans {len(node_counts)} distinct MCTS nodes; "
              f"max calls per node = {max(node_counts.values())}")
        # first + last
        print(f"  first: seq=0 node={seq[0].node_id} purpose={seq[0].purpose} "
              f"hash={seq[0].input_hash[:16]}")
        print(f"  last:  seq={len(seq)-1} node={seq[-1].node_id} "
              f"purpose={seq[-1].purpose} hash={seq[-1].input_hash[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
