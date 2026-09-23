#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict, deque
from pathlib import Path


EXP = Path(os.environ.get(
    "CUBE_TABLE2_EXP",
    "/mnt/disk2/dyp/d-overlayfs/experiments/cubesandbox_table2_deltabox_canonical_serial_officialish_20260610",
))
RAW = EXP / "raw_results_deltabox_canonical"
CUBELET_LOG = Path(os.environ.get("CUBELET_LOG", "/data/log/Cubelet/Cubelet-req.log"))

PHASE_RE = re.compile(
    r"cube_ck_phase_timing flow=(?P<flow>\S+) phase=(?P<phase>\S+) "
    r"sandboxID=(?P<sandbox>\S+) templateID=(?P<template>\S+) "
    r"start_unix_ns=(?P<start>\d+) end_unix_ns=(?P<end>\d+) duration_ms=(?P<duration>[0-9.]+)"
)

CK_FS = {"rootfs_dump"}
CK_PROC = {"memory_prepare", "memory_dump"}

RS_FS = {
    "current_rootfs_resolve",
    "rootfs_derive_new_gen",
    "delete_old_rootfs",
    "persist_rootfs_after_rollback",
}
RS_PROC = {"shim_update_restore"}
RS_META_CONTROL = {
    "resolve_targets",
    "resolve_snapshot_objects",
    "build_restore_config",
    "sync_metadata",
}


def parse_phase_line(line: str) -> dict | None:
    try:
        item = json.loads(line)
    except Exception:
        item = None
    if isinstance(item, dict) and item.get("snapshotTiming") == "cube_ck_phase":
        required = ["flow", "phase", "sandboxID", "templateID", "startUnixNs", "endUnixNs", "durationMs"]
        if all(k in item for k in required):
            return {
                "flow": item["flow"],
                "phase": item["phase"],
                "sandbox_id": item["sandboxID"],
                "snapshot_id": item["templateID"],
                "start_unix_ns": int(item["startUnixNs"]),
                "end_unix_ns": int(item["endUnixNs"]),
                "duration_ms": float(item["durationMs"]),
            }
    m = PHASE_RE.search(line)
    if not m:
        return None
    return {
        "flow": m.group("flow"),
        "phase": m.group("phase"),
        "sandbox_id": m.group("sandbox"),
        "snapshot_id": m.group("template"),
        "start_unix_ns": int(m.group("start")),
        "end_unix_ns": int(m.group("end")),
        "duration_ms": float(m.group("duration")),
    }


def union_ms(phases: list[dict]) -> float:
    spans = sorted((p["start_unix_ns"], p["end_unix_ns"]) for p in phases)
    if not spans:
        return 0.0
    merged: list[list[int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged) / 1_000_000.0


def stat(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "mean_ms": None, "median_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    median = ys[mid] if n % 2 else (ys[mid - 1] + ys[mid]) / 2.0
    idx = (n - 1) * 0.95
    lo = math.floor(idx)
    hi = math.ceil(idx)
    p95 = ys[lo] if lo == hi else ys[lo] * (hi - idx) + ys[hi] * (idx - lo)
    return {
        "n": n,
        "mean_ms": sum(xs) / n,
        "median_ms": median,
        "p95_ms": p95,
        "min_ms": min(xs),
        "max_ms": max(xs),
    }


def sum_phase(phases: list[dict], names: set[str]) -> float:
    return sum(p["duration_ms"] for p in phases if p["phase"] in names)


def load_logs(snapshot_ids: set[str]) -> list[dict]:
    rows = []
    seen = set()
    if not CUBELET_LOG.exists():
        raise FileNotFoundError(CUBELET_LOG)
    with CUBELET_LOG.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "cube_ck_phase" not in line:
                continue
            if not any(sid in line for sid in snapshot_ids):
                continue
            row = parse_phase_line(line)
            if not row or row["snapshot_id"] not in snapshot_ids:
                continue
            key = (
                row["flow"],
                row["phase"],
                row["sandbox_id"],
                row["snapshot_id"],
                row["start_unix_ns"],
                row["end_unix_ns"],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda r: (r["start_unix_ns"], r["end_unix_ns"], r["flow"], r["phase"]))
    return rows


def group_rollback(rows: list[dict]) -> dict[tuple[str, str], deque[list[dict]]]:
    by_key = defaultdict(list)
    for row in rows:
        if row["flow"] == "rollback_sandbox":
            by_key[(row["sandbox_id"], row["snapshot_id"])].append(row)

    out: dict[tuple[str, str], deque[list[dict]]] = {}
    for key, phases in by_key.items():
        totals = [p for p in phases if p["phase"] == "rollback_total"]
        totals.sort(key=lambda p: p["start_unix_ns"])
        groups = []
        for total in totals:
            start = total["start_unix_ns"]
            end = total["end_unix_ns"]
            members = [
                p
                for p in phases
                if start <= p["start_unix_ns"] <= end and p["end_unix_ns"] <= end
            ]
            if total not in members:
                members.append(total)
            members.sort(key=lambda p: (p["start_unix_ns"], p["end_unix_ns"], p["phase"]))
            groups.append(members)
        out[key] = deque(groups)
    return out


def summarize_rows(rows: list[dict]) -> dict:
    fields = ["wall_ms", "fs_ms", "process_ms", "control_ms", "phase_union_ms"]
    if rows and "rollback_total_ms" in rows[0]:
        fields += ["rollback_total_ms", "metadata_control_ms", "api_outer_control_ms"]
    out = {field: stat([float(r[field]) for r in rows if r.get(field) is not None]) for field in fields}
    n = len(rows)
    if n:
        mean_wall = out["wall_ms"]["mean_ms"]
        for field in ["fs_ms", "process_ms", "control_ms"]:
            out[field.replace("_ms", "_pct_of_wall")] = (
                100.0 * out[field]["mean_ms"] / mean_wall if mean_wall else None
            )
    out["n_events"] = n
    return out


def fmt_ms(value: float | None) -> str:
    return "NA" if value is None else f"{value:.1f} ms"


def fmt_pct(value: float | None) -> str:
    return "NA" if value is None else f"{value:.1f}%"


def write_markdown(summary: dict, path: Path) -> None:
    lines = [
        "# CubeSandbox Table 2 DeltaBox-canonical phase breakdown",
        "",
        "Canonical input: all raw schedule ckpt/restore events from the DeltaBox Table 2 local run.",
        "",
        "Classification:",
        "- ck filesystem/storage = `rootfs_dump`; ck process/VM = `memory_prepare + memory_dump`; ck control = API duration minus logged commit phase union.",
        "- rs filesystem/storage = rootfs resolve/derive/delete/persist; rs process/VM = `shim_update_restore`; rs control = metadata phases plus API duration minus `rollback_total`.",
        "",
        "## Aggregate",
        "",
        "| flow | elapsed time | filesystem/storage | process/VM | control/orchestration | events |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ["ck", "rs"]:
        row = summary["aggregate"][name]
        lines.append(
            f"| {name} | {fmt_ms(row['wall_ms']['mean_ms'])} | "
            f"{fmt_ms(row['fs_ms']['mean_ms'])} ({fmt_pct(row.get('fs_pct_of_wall'))}) | "
            f"{fmt_ms(row['process_ms']['mean_ms'])} ({fmt_pct(row.get('process_pct_of_wall'))}) | "
            f"{fmt_ms(row['control_ms']['mean_ms'])} ({fmt_pct(row.get('control_pct_of_wall'))}) | "
            f"{row['n_events']} |"
        )

    lines += [
        "",
        "## Per Workload",
        "",
        "| Workload | flow | elapsed time | filesystem/storage | process/VM | control/orchestration | events |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for group in summary["per_group"]:
        for name in ["ck", "rs"]:
            row = group[name]
            lines.append(
                f"| {group['group']} | {name} | {fmt_ms(row['wall_ms']['mean_ms'])} | "
                f"{fmt_ms(row['fs_ms']['mean_ms'])} | {fmt_ms(row['process_ms']['mean_ms'])} | "
                f"{fmt_ms(row['control_ms']['mean_ms'])} | {row['n_events']} |"
            )

    lines += [
        "",
        "## Per Instance",
        "",
        "| group | instance | flow | elapsed time | filesystem/storage | process/VM | control | events |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for inst in summary["per_instance"]:
        for name in ["ck", "rs"]:
            row = inst[name]
            lines.append(
                f"| {inst['group']} | {inst['instance']} | {name} | {fmt_ms(row['wall_ms']['mean_ms'])} | "
                f"{fmt_ms(row['fs_ms']['mean_ms'])} | {fmt_ms(row['process_ms']['mean_ms'])} | "
                f"{fmt_ms(row['control_ms']['mean_ms'])} | {row['n_events']} |"
            )
    lines += ["", f"Missing phase matches: `{len(summary['missing_phase_matches'])}`."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    pilots = sorted(RAW.glob("*/pilot_result.json"))
    if not pilots:
        raise FileNotFoundError(f"no copied pilot_result.json under {RAW}; run aggregate_deltabox_canonical_cube.py first")

    snapshot_ids = set()
    for pilot in pilots:
        data = json.loads(pilot.read_text())
        for it in data.get("iterations") or []:
            if it.get("ok") and it.get("snapshot_id"):
                snapshot_ids.add(it["snapshot_id"])

    rows = load_logs(snapshot_ids)
    commit_by_snapshot: dict[str, list[dict]] = defaultdict(list)
    sandbox_by_snapshot: dict[str, str] = {}
    for row in rows:
        if row["flow"] == "commit_sandbox":
            commit_by_snapshot[row["snapshot_id"]].append(row)
            sandbox_by_snapshot[row["snapshot_id"]] = row["sandbox_id"]

    rollback_groups = group_rollback(rows)
    all_ck = []
    all_rs = []
    per_instance = []
    missing = []

    for pilot in pilots:
        data = json.loads(pilot.read_text())
        instance = data["instance"]
        group = data.get("group", "")
        run_id = data["run_id"]
        ck_rows = []
        rs_rows = []
        for idx, it in enumerate(data.get("iterations") or []):
            if not it.get("ok"):
                continue
            sid = it.get("snapshot_id")
            if it.get("kind") == "ckpt" and it.get("checkpoint_wall_ms") is not None:
                phases = commit_by_snapshot.get(sid, [])
                if not phases:
                    missing.append({"run_id": run_id, "event_index": idx, "kind": "ckpt", "snapshot_id": sid})
                    continue
                wall = float(it["checkpoint_wall_ms"])
                fs = sum_phase(phases, CK_FS)
                proc = sum_phase(phases, CK_PROC)
                phase_union = union_ms(phases)
                row = {
                    "group": group,
                    "instance": instance,
                    "run_id": run_id,
                    "event_index": idx,
                    "node_id": it.get("node_id"),
                    "snapshot_id": sid,
                    "wall_ms": wall,
                    "fs_ms": fs,
                    "process_ms": proc,
                    "phase_union_ms": phase_union,
                    "control_ms": wall - phase_union,
                    "phase_names": sorted({p["phase"] for p in phases}),
                }
                ck_rows.append(row)
                all_ck.append(row)
            elif it.get("kind") == "restore" and it.get("restore_wall_ms") is not None:
                sandbox = sandbox_by_snapshot.get(sid)
                group_rows = None
                if sandbox:
                    q = rollback_groups.get((sandbox, sid))
                    if q:
                        group_rows = q.popleft()
                if not group_rows:
                    missing.append({"run_id": run_id, "event_index": idx, "kind": "restore", "snapshot_id": sid})
                    continue
                wall = float(it["restore_wall_ms"])
                rollback_total = sum_phase(group_rows, {"rollback_total"})
                fs = sum_phase(group_rows, RS_FS)
                proc = sum_phase(group_rows, RS_PROC)
                metadata_control = sum_phase(group_rows, RS_META_CONTROL)
                api_outer = wall - rollback_total
                row = {
                    "group": group,
                    "instance": instance,
                    "run_id": run_id,
                    "event_index": idx,
                    "restore_to_ckpt_id": it.get("restore_to_ckpt_id"),
                    "snapshot_id": sid,
                    "wall_ms": wall,
                    "fs_ms": fs,
                    "process_ms": proc,
                    "metadata_control_ms": metadata_control,
                    "api_outer_control_ms": api_outer,
                    "control_ms": metadata_control + api_outer,
                    "rollback_total_ms": rollback_total,
                    "phase_union_ms": union_ms(group_rows),
                    "phase_names": sorted({p["phase"] for p in group_rows}),
                }
                rs_rows.append(row)
                all_rs.append(row)

        per_instance.append(
            {
                "group": group,
                "instance": instance,
                "run_id": run_id,
                "ck": summarize_rows(ck_rows),
                "rs": summarize_rows(rs_rows),
                "n_ck_rows": len(ck_rows),
                "n_rs_rows": len(rs_rows),
            }
        )

    by_group: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {"ck": [], "rs": []})
    for row in all_ck:
        by_group[row["group"]]["ck"].append(row)
    for row in all_rs:
        by_group[row["group"]]["rs"].append(row)
    per_group = []
    for group in ["Django", "SymPy", "Scientific", "Tools/Small"]:
        if group not in by_group:
            continue
        per_group.append(
            {
                "group": group,
                "ck": summarize_rows(by_group[group]["ck"]),
                "rs": summarize_rows(by_group[group]["rs"]),
            }
        )

    summary = {
        "experiment": "cubesandbox_table2_deltabox_canonical_phase_breakdown",
        "source_results": str(RAW),
        "cubelet_log": str(CUBELET_LOG),
        "classification": {
            "ck_filesystem_storage": sorted(CK_FS),
            "ck_process_vm": sorted(CK_PROC),
            "ck_control_orchestration": "checkpoint_wall_ms - union(commit_sandbox phases)",
            "rs_filesystem_storage": sorted(RS_FS),
            "rs_process_vm": sorted(RS_PROC),
            "rs_control_orchestration": "metadata phases plus restore_wall_ms - rollback_total",
        },
        "aggregate": {
            "ck": summarize_rows(all_ck),
            "rs": summarize_rows(all_rs),
        },
        "per_group": per_group,
        "per_instance": per_instance,
        "missing_phase_matches": missing,
        "ck_rows": all_ck,
        "rs_rows": all_rs,
    }

    out_json = EXP / "cube_table2_deltabox_canonical_phase_breakdown.json"
    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_markdown(summary, EXP / "summary_deltabox_canonical_phase_breakdown.md")
    print(
        json.dumps(
            {
                "out_json": str(out_json),
                "ck": summary["aggregate"]["ck"],
                "rs": summary["aggregate"]["rs"],
                "missing": len(missing),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
