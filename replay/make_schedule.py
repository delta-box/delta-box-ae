#!/usr/bin/env python3
"""Convert one recorded MCTS tree to an all-standard Table 4 schedule."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path

from tree_to_schedule import convert


def validate_schedule(events: list[dict], adaptive: bool = False) -> None:
    checkpoints = set()
    if not events:
        raise ValueError("schedule is empty")
    for event in events:
        if event.get("type") == "restore":
            if event.get("restore_to_ckpt_id") not in checkpoints:
                raise ValueError(f"restore references a missing checkpoint: {event}")
        elif event.get("type") == "ckpt":
            checkpoint = event.get("ckpt_id")
            if not checkpoint or checkpoint in checkpoints:
                raise ValueError(f"missing or duplicate checkpoint ID: {event}")
            checkpoints.add(checkpoint)
            if event.get("strategy") not in (("standard", "lightweight", "predump") if adaptive else ("standard",)):
                raise ValueError("schedule strategy is not allowed by the selected adaptive policy")
            latency = event.get("latency_ms")
            if (isinstance(latency, bool) or not isinstance(latency, (int, float))
                    or not math.isfinite(latency) or latency < 0):
                raise ValueError(f"invalid latency_ms: {latency!r}")
            operations = event.get("worker_ops")
            if not isinstance(operations, list) or (event.get("worker_ops_required") and not operations):
                raise ValueError(f"missing worker operations: {checkpoint}")
            for operation in operations:
                if operation.get("note", "").startswith("unhandled_action:"):
                    raise ValueError(f"unsupported recorded action: {operation['note']}")
        else:
            raise ValueError(f"unknown event type: {event.get('type')!r}")


def make_schedule(trace_dir: Path, instance: str, output: Path, adaptive: bool = False) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+", instance):
        raise ValueError(f"invalid SWE-bench instance ID: {instance!r}")
    trajectory_path = trace_dir / "trajectory.json"
    trajectory = json.loads(trajectory_path.read_text())
    commit = trajectory.get("repository", {}).get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise ValueError(f"trajectory has no full repository commit: {trajectory_path}")
    latencies = []
    for line in (trace_dir / "ms_trace.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        duration = json.loads(line).get("dur_s")
        if duration is None:
            continue
        latency = float(duration) * 1000
        if not math.isfinite(latency) or latency < 0:
            raise ValueError(f"invalid LLM duration: {duration}")
        latencies.append(latency)
    if not latencies:
        raise ValueError("ms_trace.jsonl contains no LLM durations")
    events, _ = convert(trajectory_path, instance)
    checkpoint_index = 0
    for event in events:
        if event["type"] != "ckpt":
            continue
        if not adaptive:
            event["strategy"] = "standard"
        event["dump_size_mb"] = 7.8
        has_measurement = checkpoint_index < len(latencies)
        event["latency_ms"] = latencies[checkpoint_index] if has_measurement else statistics.fmean(latencies)
        event["latency_source"] = "ms_trace" if has_measurement else "ms_trace_mean_fill"
        checkpoint_index += 1
    validate_schedule(events, adaptive=adaptive)
    metadata = {
        "instance": instance,
        "conversion_policy": "recorded-action-adaptive" if adaptive else "all-standard",
        "repository_commit": commit,
        "trace_dir": str(trace_dir.resolve()),
        "n_ckpt": checkpoint_index,
        "n_restore": sum(event["type"] == "restore" for event in events),
        "recorded_expected_failures": sum(bool(op.get("expected_outcome")) for event in events
                                          for op in event.get("worker_ops", [])),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(event) + "\n" for event in events))
    output.with_suffix(".meta.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--instance", help="Defaults to the trace directory name")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--adaptive", action="store_true")
    args = parser.parse_args()
    metadata = make_schedule(args.trace_dir, args.instance or args.trace_dir.name, args.out, adaptive=args.adaptive)
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
