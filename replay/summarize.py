#!/usr/bin/env python3
"""Summarize complete fast/slow JSONL runs as Table 4 latency means."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def read_results(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    summaries = [row for row in rows if row.get("kind") == "run_summary"]
    if len(summaries) != 1 or rows[-1].get("kind") != "run_summary":
        raise ValueError(f"incomplete results (missing final run_summary): {path}")
    if summaries[0].get("error_n", 0) or summaries[0].get("worker_exec_bad_n", 0):
        raise ValueError(f"experiment failed: {path}: {summaries[0].get('first_error')}")
    for row in rows:
        if row.get("ok") is False or row.get("err") or row.get("error"):
            raise ValueError(f"failed event in {path}: {row}")
    return rows


def collect(directory: Path) -> list[dict]:
    metrics = {
        "fast": (("ckpt", "ckpt_wall_ms"), ("ckpt", "checkpoint_api_wall_ms"), ("ckpt", "checkpoint_sync_no_dump_ms"),
                 ("restore", "restore_critical_ms"), ("restore", "restore_api_wall_ms")),
        "slow": (("restore", "restore_critical_ms"), ("restore", "restore_api_wall_ms")),
    }
    result = []
    files_seen = 0
    for mode, fields in metrics.items():
        for conditions in (directory / mode).rglob("run.json"):
            if json.loads(conditions.read_text()).get("status") != "ok":
                raise ValueError(f"run has not completed successfully: {conditions}")
        values = {field: [] for _, field in fields}
        files = sorted((directory / mode).rglob("*.results.jsonl"))
        files_seen += len(files)
        for path in files:
            conditions = path.parent / "run.json"
            if conditions.exists() and json.loads(conditions.read_text()).get("status") != "ok":
                raise ValueError(f"run has not completed successfully: {conditions}")
            for row in read_results(path):
                for kind, field in fields:
                    if row.get("kind") != kind:
                        continue
                    value = row.get(field)
                    if (isinstance(value, bool) or not isinstance(value, (int, float))
                            or not math.isfinite(value) or value < 0):
                        raise ValueError(f"invalid {field} in {path}: {value!r}")
                    values[field].append(value)
        for _, field in fields:
            samples = values[field]
            if samples:
                result.append({"mode": mode, "metric": field, "n": len(samples),
                               "mean_ms": statistics.fmean(samples)})
    if not files_seen:
        raise ValueError(f"no results under {directory}/fast or {directory}/slow")
    if not result:
        raise ValueError(f"no latency measurements in {directory}")
    return result


def summarize(directory: Path) -> list[dict]:
    rows = collect(directory)
    with (directory / "summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mode", "metric", "n", "mean_ms"])
        writer.writeheader()
        writer.writerows(rows)
    (directory / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    print("| mode | metric | n | mean (ms) |")
    print("|---|---|---:|---:|")
    for row in rows:
        print(f"| {row['mode']} | {row['metric']} | {row['n']} | {row['mean_ms']:.2f} |")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    summarize(parser.parse_args().input)


if __name__ == "__main__":
    main()
