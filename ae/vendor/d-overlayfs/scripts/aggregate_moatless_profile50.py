#!/usr/bin/env python3
"""Aggregate native moatless-swe-search per-step profiling JSONL.

Input is a run root produced by run_swe_search_profile50_no_testbed.sh.  The
script reads step_metrics/<instance>.jsonl, measures the corresponding isolated
repo checkouts with `du -sb --exclude=.git`, and writes motiv_totals.json with
the fields consumed by the motivation plot.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics as st
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


def mean(xs: list[float]) -> float | None:
    return st.mean(xs) if xs else None


def percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    vals = sorted(xs)
    idx = min(len(vals) - 1, max(0, round((len(vals) - 1) * q)))
    return vals[idx]


def du_source_bytes(path: Path) -> int | None:
    if not path.is_dir():
        return None
    result = subprocess.run(
        ["du", "-sb", "--exclude=.git", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def load_worklist(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                out[parts[0]] = {
                    "split": parts[1],
                    "repo": parts[2] if len(parts) >= 3 else "",
                }
    return out


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows.append(json.loads(line))
    return rows


def by_step_mean(values: list[tuple[int, float]]) -> list[float]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for step_idx, value in values:
        buckets[step_idx].append(value)
    return [mean(buckets[i]) or 0.0 for i in sorted(buckets)]


def summarize_instance(inst: str, rows: list[dict[str, Any]], repo_bytes: int | None) -> dict[str, Any]:
    rss_mb: list[float] = []
    private_dirty_mb: list[float] = []
    soft_dirty_mb: list[float] = []
    private_dirty_delta_mb: list[float] = []
    rss_delta_mb: list[float] = []
    action_write_kb: list[float] = []
    patch_delta_kb: list[float] = []

    if rows:
        first_before = rows[0].get("before_proc_snapshot") or {}
        if isinstance(first_before.get("vmrss_kb"), int):
            rss_mb.append(first_before["vmrss_kb"] / 1024.0)
        if isinstance(first_before.get("smaps_private_dirty_kb"), int):
            private_dirty_mb.append(first_before["smaps_private_dirty_kb"] / 1024.0)

    for row in rows:
        after = row.get("after_proc_snapshot") or {}
        if isinstance(after.get("vmrss_kb"), int):
            rss_mb.append(after["vmrss_kb"] / 1024.0)
        if isinstance(after.get("smaps_private_dirty_kb"), int):
            private_dirty_mb.append(after["smaps_private_dirty_kb"] / 1024.0)

        if isinstance(row.get("soft_dirty_mb"), (int, float)):
            soft_dirty_mb.append(float(row["soft_dirty_mb"]))
        delta = row.get("smaps_private_dirty_kb_since_prev_kb")
        if isinstance(delta, (int, float)):
            private_dirty_delta_mb.append(float(delta) / 1024.0)
        rss_delta = row.get("vmrss_kb_since_prev_kb")
        if isinstance(rss_delta, (int, float)):
            rss_delta_mb.append(float(rss_delta) / 1024.0)

        action_write_kb.append(float(row.get("action_write_bytes") or 0) / 1024.0)
        patch_delta_kb.append(float(row.get("repo_patch_bytes_delta") or 0) / 1024.0)

    return {
        "instance_id": inst,
        "steps": len(rows),
        "repo_source_bytes": repo_bytes,
        "repo_source_mb": repo_bytes / 1e6 if repo_bytes is not None else None,
        "rss_mean_mb": mean(rss_mb),
        "rss_peak_mb": max(rss_mb) if rss_mb else None,
        "private_dirty_mean_mb": mean(private_dirty_mb),
        "soft_dirty_mean_mb": mean(soft_dirty_mb),
        "private_dirty_delta_mean_mb": mean(private_dirty_delta_mb),
        "rss_delta_mean_mb": mean(rss_delta_mb),
        "action_write_mean_kb": mean(action_write_kb),
        "patch_delta_mean_kb": mean(patch_delta_kb),
        "soft_dirty_samples": len(soft_dirty_mb),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--repos-dir", default=None)
    parser.add_argument("--worklist", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-copy", action="store_true")
    parser.add_argument(
        "--copy-to",
        action="append",
        default=[
            "/mnt/disk2/dyp/d-overlayfs/finalcode/finaltest/motiv_totals.json",
            "/mnt/disk2/dyp/d-overlayfs/rollbackable-sandbox-paper/figs/motiv_totals.json",
        ],
    )
    args = parser.parse_args()

    run_root = Path(args.run_root)
    repos_dir = Path(args.repos_dir) if args.repos_dir else run_root / "repos"
    metrics_dir = run_root / "step_metrics"
    worklist = load_worklist(Path(args.worklist) if args.worklist else run_root / "worklist.tsv")
    requested = list(worklist) if worklist else sorted(p.stem for p in metrics_dir.glob("*.jsonl"))

    per_instance: dict[str, Any] = {}
    fs_bytes: dict[str, int] = {}
    rss_instance_means: list[float] = []
    soft_dirty_instance_means: list[float] = []
    private_dirty_delta_instance_means: list[float] = []
    mem_by_step: list[tuple[int, float]] = []
    private_dirty_by_step: list[tuple[int, float]] = []
    rss_delta_by_step: list[tuple[int, float]] = []
    fs_by_step: list[tuple[int, float]] = []
    patch_by_step: list[tuple[int, float]] = []
    all_mem_mb: list[float] = []
    all_rss_delta_mb: list[float] = []
    all_fs_kb: list[float] = []

    for inst in requested:
        rows = read_jsonl(metrics_dir / f"{inst}.jsonl")
        repo_bytes = du_source_bytes(repos_dir / f"swe-bench_{inst}")
        if repo_bytes is not None:
            fs_bytes[inst] = repo_bytes
        summary = summarize_instance(inst, rows, repo_bytes)
        if summary["rss_mean_mb"] is not None:
            rss_instance_means.append(summary["rss_mean_mb"])
        if summary["soft_dirty_mean_mb"] is not None:
            soft_dirty_instance_means.append(summary["soft_dirty_mean_mb"])
        if summary["private_dirty_delta_mean_mb"] is not None:
            private_dirty_delta_instance_means.append(summary["private_dirty_delta_mean_mb"])

        for idx, row in enumerate(rows):
            if isinstance(row.get("soft_dirty_mb"), (int, float)):
                value = float(row["soft_dirty_mb"])
                mem_by_step.append((idx, value))
                all_mem_mb.append(value)
            delta = row.get("smaps_private_dirty_kb_since_prev_kb")
            if isinstance(delta, (int, float)):
                private_dirty_by_step.append((idx, float(delta) / 1024.0))
            rss_delta = row.get("vmrss_kb_since_prev_kb")
            if isinstance(rss_delta, (int, float)):
                value = float(rss_delta) / 1024.0
                rss_delta_by_step.append((idx, value))
                all_rss_delta_mb.append(value)
            write_kb = float(row.get("action_write_bytes") or 0) / 1024.0
            fs_by_step.append((idx, write_kb))
            all_fs_kb.append(write_kb)
            patch_by_step.append((idx, float(row.get("repo_patch_bytes_delta") or 0) / 1024.0))

        summary.update(worklist.get(inst, {}))
        per_instance[inst] = summary

    fs_vals = list(fs_bytes.values())
    mem_source = "soft_dirty" if all_mem_mb else "private_dirty_delta"
    mem_series = by_step_mean(mem_by_step if all_mem_mb else private_dirty_by_step)
    mem_all = all_mem_mb if all_mem_mb else [v for _, v in private_dirty_by_step]

    out: dict[str, Any] = {
        "run_root": str(run_root),
        "metric_source": "native moatless-swe-search no-testbed",
        "memory_delta_source": mem_source,
        "n_instances_requested": len(requested),
        "n_instances_with_metrics": sum(1 for inst in requested if (metrics_dir / f"{inst}.jsonl").exists()),
        "n_instances_with_fs": len(fs_vals),
        "instances": requested,
        "fs_bytes": fs_bytes,
        "per_instance": per_instance,
        "fs_mean_mb": mean([x / 1e6 for x in fs_vals]),
        "fs_median_mb": percentile([x / 1e6 for x in fs_vals], 0.5),
        "rss_mean_mb": mean(rss_instance_means),
        "rss_median_mb": percentile(rss_instance_means, 0.5),
        "rss_perstep_delta_mb": mean(all_rss_delta_mb),
        "rss_perstep_delta_series_mb": by_step_mean(rss_delta_by_step),
        "rss_perstep_delta_all_mb": all_rss_delta_mb,
        "mem_perstep_delta_mb": mean(mem_all),
        "mem_perstep_series_mb": mem_series,
        "mem_perstep_all_mb": mem_all,
        "mem_perstep_p50_mb": percentile(mem_all, 0.5),
        "mem_perstep_p95_mb": percentile(mem_all, 0.95),
        "private_dirty_perstep_series_mb": by_step_mean(private_dirty_by_step),
        "fs_perstep_delta_kb": mean(all_fs_kb),
        "fs_perstep_series_kb": by_step_mean(fs_by_step),
        "fs_perstep_all_kb": all_fs_kb,
        "patch_perstep_delta_kb": mean([v for _, v in patch_by_step]),
        "patch_perstep_series_kb": by_step_mean(patch_by_step),
        "patch_perstep_all_kb": [v for _, v in patch_by_step],
        "units": {
            "fs_mean_mb": "du bytes / 1e6, .git excluded",
            "rss_mean_mb": "VmRSS KiB / 1024",
            "mem_perstep_delta_mb": "soft-dirty present rw-p pages * page_size / 1024^2",
            "fs_perstep_series_kb": "action write bytes / 1024",
        },
    }

    out_path = Path(args.out) if args.out else run_root / "motiv_totals.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    copied: list[str] = []
    if not args.no_copy:
        for target_s in args.copy_to or []:
            if not target_s:
                continue
            target = Path(target_s)
            if target.parent.is_dir():
                shutil.copy2(out_path, target)
                copied.append(str(target))

    print(f"wrote {out_path}")
    if copied:
        print("copied to:")
        for target in copied:
            print(f"  {target}")
    print(
        "summary: "
        f"fs_mean_mb={out.get('fs_mean_mb')}, "
        f"rss_mean_mb={out.get('rss_mean_mb')}, "
        f"mem_perstep_delta_mb={out.get('mem_perstep_delta_mb')}, "
        f"fs_perstep_delta_kb={out.get('fs_perstep_delta_kb')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
