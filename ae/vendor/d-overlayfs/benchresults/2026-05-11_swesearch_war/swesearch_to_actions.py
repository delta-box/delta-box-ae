#!/usr/bin/env python3
"""swesearch_to_actions.py — Convert swe-search/mcts trajectory.json to a
flat per-edit action stream for the WAR replay engine.

Each EditCode transition has a recorded `response.output.diff` (unified diff
format). We extract these in transition order, ignoring SearchCode /
PlanToCode / Pending / Finished / Rejected (no FS effect).

Output JSON per instance:
  {
    "instance_id": "...",
    "base_commit": "...",
    "image": "...",  # docker image name to extract /testbed from
    "edits": [
      {
        "edit_idx": 0,
        "transition_id": 15,
        "file_path": "django/core/files/locks.py",
        "diff": "--- ...\n+++ ...\n@@ -107,9 +107,15 @@\n...",
        "diff_bytes": 638
      },
      ...
    ]
  }
"""
import argparse
import glob
import json
import os
import sys


SWESEARCH_DIR = "/home/dong/d-overlayfs/traces/swe-search/mcts"


# Map swe-search instance_id → local docker image, if any
def docker_image_for(instance_id: str) -> str | None:
    parts = instance_id.split("__", 1)
    if len(parts) != 2:
        return None
    org, repo_inst = parts
    candidate = f"swebench/sweb.eval.x86_64.{org}_1776_{repo_inst}"
    return candidate


def extract_actions(traj_json_path: str) -> dict:
    t = json.load(open(traj_json_path))
    instance_id = t.get("info", {}).get("instance_id") or os.path.basename(
        os.path.dirname(traj_json_path))
    # base_commit recorded in repository.commit at any snapshot
    base_commit = None
    for tr in t.get("transitions", []):
        snap = tr.get("snapshot") or {}
        repo = snap.get("repository") or {}
        if repo.get("commit"):
            base_commit = repo["commit"]
            break

    edits = []
    for tr in t.get("transitions", []):
        if tr.get("name") != "EditCode":
            continue
        snap = tr.get("snapshot") or {}
        fc = snap.get("file_context") or {}
        files = fc.get("files") or []
        target_file = files[0].get("file_path") if files else None
        for a in tr.get("actions", []):
            out = (a.get("response") or {}).get("output") or {}
            diff = out.get("diff") or ""
            if not diff or not target_file:
                continue
            edits.append({
                "edit_idx": len(edits),
                "transition_id": tr.get("id"),
                "previous_state_id": tr.get("previous_state_id"),
                "file_path": target_file,
                "diff": diff,
                "diff_bytes": len(diff),
            })

    return {
        "instance_id": instance_id,
        "base_commit": base_commit,
        "image": docker_image_for(instance_id),
        "n_edits": len(edits),
        "edits": edits,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out_dir", default="/home/dong/d-overlayfs/benchmarks/replay/swesearch_actions")
    ap.add_argument("--instances", nargs="*",
                    help="optional: restrict to these instance_ids")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    n_total = n_with_edits = total_edits = 0
    for d in sorted(glob.glob(f"{SWESEARCH_DIR}/*/")):
        inst = os.path.basename(d.rstrip("/"))
        if args.instances and inst not in args.instances:
            continue
        traj = f"{d}trajectory.json"
        if not os.path.exists(traj):
            continue
        n_total += 1
        info = extract_actions(traj)
        if info["n_edits"] == 0:
            continue
        n_with_edits += 1
        total_edits += info["n_edits"]
        out = f"{args.out_dir}/{inst}.json"
        with open(out, "w") as f:
            json.dump(info, f, indent=2)
    print(f"Processed {n_total} instances, {n_with_edits} have EditCodes, "
          f"{total_edits} total edits")


if __name__ == "__main__":
    main()
