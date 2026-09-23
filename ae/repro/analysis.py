#!/usr/bin/env python3
"""Strict, standard-library analysis of saved paper evidence or fresh run outputs.

Archived analysis is not remeasurement. Fresh analysis never imports archive values.
The JSON schema is shared by the CSV export and the optional matplotlib renderer.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ae.repro.common import AE_ROOT, number, write_json
from ae.repro.replay_audit import summarize as summarize_replay_audit, validate_stats

GROUPS = ("Django", "SymPy", "Scientific", "Tools/Small")
BINS = ((1, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 256))
RAW = "archived_raw_events"
PER_TRACE = "archived_per_trace_summary"
AGG = "archived_aggregate"


def group_for(instance):
    family = instance.split("__")[0]
    if family == "django":
        return "Django"
    if family == "sympy":
        return "SymPy"
    if family in ("astropy", "matplotlib", "pydata", "scikit-learn"):
        return "Scientific"
    if family in ("mwaskom", "pallets", "psf", "pylint-dev", "pytest-dev", "sphinx-doc"):
        return "Tools/Small"
    raise ValueError(f"Unknown workload family: {instance}")


def mean(xs):
    return statistics.fmean(number(x, "measurement") for x in xs) if xs else None


def metric(metric_name, value, unit, n, evidence_kind, **labels):
    if value is not None:
        value = number(value, metric_name)
    return dict(metric=metric_name, value=value, unit=unit, n=n,
                statistic=labels.pop("statistic", "mean"), evidence_kind=evidence_kind, **labels)


class Evidence:
    """Read once, hash the exact consumed bytes, verify published manifest hashes."""
    def __init__(self, root, source):
        self.root = Path(root).absolute()
        self.source = source
        self.sources = {}
        self.expected = {}
        self.used = set()
        if not self.root.is_dir():
            raise ValueError(f"Input directory does not exist: {root}")
        if source == "archived":
            for path in sorted(self.root.glob("*/files.jsonl")):
                for row in self.jsonl(path, allow_empty=True):
                    target = Path(row["target"])
                    if target.parts[0] != "paper" or ".." in target.parts:
                        raise ValueError(f"Invalid archive manifest target: {target}")
                    self.expected[str(Path(*target.parts[1:]))] = row["sha256"]

    def path(self, path):
        path = Path(path)
        path = path if path.is_absolute() else self.root / path
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Input is outside selected source directory: {path}") from exc
        if ".." in path.parts:
            raise ValueError(f"Unsafe input path: {path}")
        return path

    def read(self, path):
        path = self.path(path)
        rel = str(path.relative_to(self.root))
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if rel in self.expected and sha != self.expected[rel]:
            raise ValueError(f"Archive SHA-256 mismatch: {rel}")
        self.sources[rel] = dict(path=rel, sha256=sha, bytes=len(data),
                                 manifest_verified=rel in self.expected)
        self.used.add(rel)
        return data.decode("utf-8")

    def json(self, path):
        return json.loads(self.read(path))

    def jsonl(self, path, allow_empty=False):
        rows = [json.loads(line) for line in self.read(path).splitlines() if line.strip()]
        if (not rows and not allow_empty) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"Expected nonempty JSONL object records: {path}")
        return rows

    def csv(self, path):
        return list(csv.DictReader(self.read(path).splitlines()))

    def glob(self, pattern):
        return sorted(self.root.glob(pattern))


def experiment(evidence, fn):
    evidence.used = set()
    result = fn()
    result.setdefault("status", "analyzed")
    for key, default in (("metrics", []), ("series", []), ("selection", {}), ("limitations", [])):
        result.setdefault(key, default)
    result["sources"] = sorted(evidence.used)
    return result


def completed(rows):
    summaries = [r for r in rows if r.get("kind") == "run_summary"]
    return (len(summaries) == 1 and rows[-1].get("kind") == "run_summary"
            and summaries[0].get("error_n") == 0
            and not summaries[0].get("worker_exec_bad_n", 0)
            and not any(r.get("ok") is False or r.get("err") or r.get("error") for r in rows))


def select_delta(ev, table="table-02", mode="fast", allow_missing=False):
    cohort = {r["instance"]: r for r in ev.csv(f"{table}/cohort-deltabox.csv")}
    candidates = defaultdict(list)
    rejected = []
    paths = ev.glob(f"{table}/data/records/deltabox-{mode}/results/**/*.results.jsonl")
    for path in paths:
        instance = path.name.split(".replay")[0]
        rows = ev.jsonl(path)
        counts = Counter(r.get("kind") for r in rows)
        reason = None
        if instance not in cohort:
            reason = "not in declared cohort"
        elif not completed(rows):
            reason = "failed or missing final run_summary"
        elif any(counts[k] != int(cohort[instance]["n_" + k]) for k in ("ckpt", "restore")):
            reason = "event counts differ from complete cohort schedule (smoke or incomplete)"
        rel = str(path.relative_to(ev.root))
        if reason:
            rejected.append(dict(path=rel, instance=instance, reason=reason))
        else:
            candidates[instance].append(dict(instance=instance, path=rel, rows=rows))
    selected = []
    missing = []
    for instance in cohort:
        matches = candidates[instance]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous complete {mode} runs for {instance}")
        if not matches:
            missing.append(instance)
        else:
            selected.extend(matches)
    if missing and not allow_missing:
        raise ValueError(f"Missing complete {mode} runs: {missing}")
    return selected, dict(input_count=len(cohort), attempted_files=len(paths),
                          complete_runs=len(selected), missing_complete=missing,
                          selected_files=[r["path"] for r in selected], excluded=rejected)


def trace_record(instance, ck, rs, **labels):
    return dict(instance=instance, group=group_for(instance),
                checkpoint=(len(ck), sum(number(x, "checkpoint") for x in ck)),
                restore=(len(rs), sum(number(x, "restore") for x in rs)), **labels)


def latency_metrics(records, backend, evidence_kind):
    out = []
    for group in (*GROUPS, "All"):
        selected = [r for r in records if group == "All" or r["group"] == group]
        if not selected:
            continue
        for op in ("checkpoint", "restore"):
            n = sum(r[op][0] for r in selected)
            total = sum(r[op][1] for r in selected)
            out.append(metric(op + "_ms", total / n if n else None, "ms", n,
                              evidence_kind, backend=backend, group=group, n_traces=len(selected)))
    return out


def parse_pilot(data):
    """Normalize only actual raw pilot fields; no summary or reference fallback."""
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("pilot_result is not successful")
    instance = data["instance"]
    iterations = data.get("iterations")
    if iterations is not None:
        if not iterations:
            raise ValueError("empty pilot iterations")
        if any("e2b_steps" in r for r in iterations):
            rows = [r for iteration in iterations for r in iteration["e2b_steps"]]
            if not rows or any(r.get("ok") is not True for r in rows):
                raise ValueError("failed/empty E2B steps")
            ck = [number(r["checkpoint_persist_ms"], "checkpoint_persist_ms") for r in rows]
            rs = [number(r["resume_ms"], "resume_ms") for r in rows]
            if "n_e2b_steps" in data and len(rows) != data["n_e2b_steps"]:
                raise ValueError("E2B step count mismatch")
            return "e2b", trace_record(instance, ck, rs)
        if any(r.get("ok") is not True for r in iterations):
            raise ValueError("failed Cube iteration")
        ck = [number(r["checkpoint_wall_ms"], "checkpoint_wall_ms") for r in iterations if r["kind"] == "ckpt"]
        rs = [number(r["restore_wall_ms"], "restore_wall_ms") for r in iterations if r["kind"] == "restore"]
        for key, values in (("n_ckpt_events", ck), ("n_restore_events", rs)):
            if key in data and data[key] != len(values):
                raise ValueError(f"Cube {key} mismatch")
        if not ck:
            raise ValueError("missing Cube checkpoint events")
        return "cube", trace_record(instance, ck, rs)
    ckpts = data.get("ckpts", data.get("checkpoints"))
    if not ckpts:
        raise ValueError("Unsupported raw pilot schema (no checkpoint events)")
    restores = data.get("restore_events", data.get("restores"))
    if restores is None:
        raise ValueError("Missing restore_events; mechanical single-restore pilots are not controller replay")
    if "fc_total_ms" in ckpts[0]:
        ck = [number(r["fc_total_ms"], "fc_total_ms") + number(r["dm_snapshot_ms"], "dm_snapshot_ms") for r in ckpts]
        rs = [number(r["load"]["fc_load_ms"], "fc_load_ms")
              + number(r["dm_restore"]["dm_restore_ms"], "dm_restore_ms")
              + number(r["merge"]["merge_ms"], "merge_ms") for r in restores]
        return "fc-diff", trace_record(instance, ck, rs)
    ck = []
    for r in ckpts:
        if "checkpoint_total_ms" in r:
            ck.append(number(r["checkpoint_total_ms"], "checkpoint_total_ms"))
        else:
            if r["fs"].get("rc", 0) or r["criu_dump"].get("rc", 0):
                raise ValueError("Failed CRIU checkpoint component")
            ck.append(number(r["fs"]["rsync_ms"], "rsync_ms") + number(r["criu_dump"]["criu_dump_ms"], "criu_dump_ms"))
    rs = [number(r["restore_total_ms"], "restore_total_ms") for r in restores]
    for key, values in (("n_ckpts", ck), ("n_restores", rs)):
        if key in data and data[key] != len(values):
            raise ValueError(f"CRIU {key} mismatch")
    return "criu", trace_record(instance, ck, rs)


def table2(ev):
    metrics, selections = [], {}
    selected, selections["deltabox"] = select_delta(ev)
    records = [trace_record(r["instance"], [x["ckpt_wall_ms"] for x in r["rows"] if x["kind"] == "ckpt"],
                            [x["restore_wall_ms"] for x in r["rows"] if x["kind"] == "restore"]) for r in selected]
    metrics += latency_metrics(records, "deltabox", RAW)
    for backend, pattern, cohort_name in (
        ("cube", "table-02/data/records/cube-canonical/raw_results_deltabox_canonical/*/pilot_result.json", "cube"),
        ("e2b", "table-02/data/inputs/e2b/*/pilot_result.json", "e2b")):
        cohort = {r["instance"] for r in ev.csv(f"table-02/cohort-{cohort_name}.csv")}
        records, paths = [], []
        for path in ev.glob(pattern):
            found_backend, rec = parse_pilot(ev.json(path))
            if found_backend != backend or rec["instance"] not in cohort:
                raise ValueError(f"Unexpected pilot cohort: {path}")
            records.append(rec)
            paths.append(str(path.relative_to(ev.root)))
        if Counter(r["instance"] for r in records) != Counter(cohort):
            raise ValueError(f"{backend}: cohort missing or duplicated")
        metrics += latency_metrics(records, backend, RAW)
        selections[backend] = dict(input_count=len(cohort), complete_runs=len(records), selected_files=paths)
    for backend, path in (
        ("fc-diff", "table-02/data/records/fc_diff_dm/results/controller_raw_per_trace_summary.csv"),
        ("criu", "table-02/data/records/criu_copytree/results/_aggregate_full3_20260529_104122/per_trace_summary.csv")):
        rows = ev.csv(path)
        records, excluded = [], []
        for row in rows:
            if row["ok"] != "True":
                excluded.append(dict(instance=row["instance"], reason=row["status"]))
                continue
            if backend == "fc-diff":
                ck = sum(number(float(row[k]), k) for k in ("mean_ckpt_fc_ms", "mean_ckpt_dm_ms"))
                rs = sum(number(float(row[k]), k) for k in ("mean_restore_fc_load_ms", "mean_restore_dm_ms", "mean_restore_merge_ms"))
            else:
                ck, rs = number(float(row["ckpt_total_mean_ms"]), "ckpt_total_mean_ms"), number(float(row["restore_total_mean_ms"]), "restore_total_mean_ms")
            nc, nr = int(row["n_ckpts"]), int(row["n_restores"])
            if min(nc, nr) < 0:
                raise ValueError("Negative event count")
            records.append(dict(instance=row["instance"], group=group_for(row["instance"]), checkpoint=(nc, nc * ck), restore=(nr, nr * rs)))
        metrics += latency_metrics(records, backend, PER_TRACE)
        selections[backend] = dict(planned_inputs=244, recorded_traces=len(rows), complete_runs=len(records),
                                    excluded=excluded, missing_planned=244-len(rows), selected_files=[path])
    path = "table-02/data/records/replay_copytree/results/table2_family_aggregate_zero_llm.json"
    data = ev.json(path)
    for group, row in list(data["table_groups"].items()) + [("All", data["overall"])]:
        group = group.replace(" repos", "")
        for name, key, count in (("checkpoint_ms", "mean_ckpt_once_ms", "n_traces"),
                                  ("restore_ms", "mean_restore_zero_llm_ms", "n_restore_events")):
            metrics.append(metric(name, row[key], "ms", row[count], AGG, backend="replay", group=group,
                                  n_traces=row["n_traces"]))
    selections["replay"] = dict(input_count=data["overall"]["n_traces"], selected_files=[path],
                                 checkpoint_denominator="one pristine copy per trace")
    return dict(metrics=metrics, selection=selections, limitations=[
        "Backends use separate cohorts and event counts; group means are event weighted per operation.",
        "DeltaBox complete archive means 8.3246/1.3990 ms differ from published 10.83/1.86 ms.",
        "FC and CRIU raw events are absent; their means are recomputed from per-trace summaries.",
        "Replay zero-LLM correction is aggregate-only; per-restore served completion prefixes are absent.",
        "Replay checkpoint is once per trace; FC checkpoint includes its initial full snapshot.",
        "Fast run_config.json was overwritten by a later smoke run; historical full-run settings remain unresolved."])


FAST_COMPONENTS = ("checkpoint_overlay_ms", "checkpoint_fork_ms", "checkpoint_sync_no_dump_ms", "ckpt_wall_ms",
                   "restore_fast_ioctl_ms", "restore_fast_fork_total_ms", "restore_fast_dispatch_ms",
                   "restore_fast_fork_wait_ms", "restore_fast_fork_agent_ms", "restore_wall_ms")
FRESH_COMPONENTS = FAST_COMPONENTS + (
    "checkpoint_api_wall_ms", "restore_api_wall_ms", "restore_critical_ms",
    "restore_table3_total_ms", "restore_fast_coordination_ms", "restore_slow_coordination_ms",
    "restore_slow_ioctl_ms", "restore_slow_criu_ms", "restore_slow_total_ms",
    "restore_slow_pre_criu_ms", "restore_slow_lazy_daemon_ms", "restore_slow_post_criu_ms")


def table3(ev):
    metrics, selection = [], {}
    for mode in ("fast", "slow"):
        selected, selection[mode] = select_delta(ev, "table-03", mode, allow_missing=mode == "slow")
        rows = [r for run in selected for r in run["rows"]]
        fields = FAST_COMPONENTS if mode == "fast" else ("ckpt_wall_ms", "restore_wall_ms")
        for field in fields:
            values = [r[field] for r in rows if r.get(field) is not None]
            metrics.append(metric(field, mean(values), "ms", len(values), RAW, backend="deltabox", mode=mode))
    for field in ("slow_overlay_ms", "slow_criu_ms", "slow_coordination_ms", "agent_perceived_checkpoint_ms"):
        metrics.append(metric(field, None, "ms", 0, "unavailable", backend="deltabox",
                              reason="No direct component timer; perceived checkpoint masking is a scheduling model."))
    return dict(metrics=metrics, selection=selection, limitations=[
        "Slow path accepts only 8 complete runs (224 restores); four incomplete attempts are excluded.",
        "Published slow total 9.29 ms matches that complete subset; slow component timers are not archived.",
        "Phase overlap prevents summing ioctl and fork-window time as serialized work.",
        "Fast archive total differs from paper; do not use paper constants to repair it.",
        "Fast and slow are different runs, with unmatched/partially overwritten configuration evidence."])


def figure1(ev):
    data = ev.json("figure-01/data/records/cube-phases.json")
    e2b = ev.json("table-02/data/records/e2b-original/e2b_sample8_table2_aggregate.json")
    metrics = []
    for flow, rows_key, total_key in (("checkpoint", "ck_rows", "weighted_ck_mean_ms"),
                                      ("restore", "rs_rows", "weighted_rs_mean_ms")):
        rows = data[rows_key]
        for name, field in (("filesystem", "fs_ms"), ("process", "process_ms"), ("control_plane", "control_ms"), ("total", "wall_ms")):
            metrics.append(metric(name, mean([r[field] for r in rows]), "ms", len(rows), RAW, backend="cube", operation=flow))
        split = (135.35, 175.04, .96) if flow == "checkpoint" else (.15, 47.59, 409.09)
        total = number(e2b[total_key], total_key)
        for name, value in zip(("filesystem", "process", "guest_readiness"), split):
            metrics.append(metric(name, total * value / sum(split), "ms", None, "published_adjustment",
                                  backend="e2b", operation=flow, formula="archived Table 2 total * frozen phase ratio"))
        metrics.append(metric("total", total, "ms", 185, AGG, backend="e2b", operation=flow))
        values = {"filesystem": 347., "replay": 0., "total": 347.} if flow == "checkpoint" else {"filesystem": 440., "replay": 27254., "total": 27694.}
        for name, value in values.items():
            metrics.append(metric(name, value, "ms", None, "published_adjustment", backend="replay", operation=flow,
                                  formula="frozen values in historical plot_fig_e2b_cube_compare.py"))
    return dict(metrics=metrics, selection=dict(cube_checkpoint_events=317, cube_restore_events=334), limitations=[
        "E2B phases are frozen ratios rescaled to the original 8-trace Table 2 aggregate, not 8-trace phase measurements.",
        "Replay decomposition uses explicitly tagged historical plotting constants (347/440/27254 ms).",
        "Cube uses actual phase rows; historical fallback phase values are not used."])


def figure2(ev):
    fs = ev.json("figure-02/data/records/motiv_totals.json")
    memory = ev.json("figure-02/data/records/memory-profile/tree_rss_summary.json")
    positive = [number(x, "filesystem delta") for x in fs["fs_perstep_all_kb"] if number(x, "filesystem delta") > 0]
    metrics = [metric("total", fs["fs_mean_mb"], "MB", 30, AGG, domain="filesystem", panel="a"),
               metric("step_delta", mean(positive), "KB_historical", len(positive), AGG, domain="filesystem", panel="a"),
               metric("total", memory["mean_of_instance_mean_rss_mb"], "MB_archived", 5, AGG, domain="memory", panel="a"),
               metric("step_delta", memory["pooled_per_step_soft_dirty_mean_mb"], "MiB", memory["pooled_per_step_count"], RAW, domain="memory", panel="a")]
    series = [dict(panel="b", domain="filesystem", metric="step_delta", x=i+1, x_unit="step", y=number(v, "fs series"),
                   unit="KB_historical", evidence_kind=AGG) for i, v in enumerate(fs["fs_perstep_series_kb"])]
    by_index = defaultdict(list)
    pooled = []
    for instance in memory["instances"]:
        rows = ev.json(f"figure-02/data/records/memory-profile/{instance['instance_id']}/per_step_memory_dirty.json")
        for i, row in enumerate(rows):
            by_index[i].append(number(row["soft_dirty_mb"], "soft_dirty_mb"))
            pooled.append(row["soft_dirty_mb"])
    if len(pooled) != memory["pooled_per_step_count"] or not math.isclose(mean(pooled), memory["pooled_per_step_soft_dirty_mean_mb"], rel_tol=1e-12):
        raise ValueError("Memory raw events do not match archived pooled summary")
    for i, values in sorted(by_index.items()):
        if len(values) >= 2:
            series.append(dict(panel="b", domain="memory", metric="step_delta", x=i+1, x_unit="step", y=mean(values),
                               unit="MiB", n=len(values), evidence_kind=RAW))
    return dict(metrics=metrics, series=series,
                selection=dict(filesystem_inputs=30, filesystem_samples=len(fs["fs_perstep_all_kb"]),
                               filesystem_positive_samples=len(positive), memory_inputs=5, memory_samples=len(pooled),
                               excluded_memory_line_positions=[i+1 for i, v in by_index.items() if len(v) < 2]),
                limitations=["Filesystem panel-a uses positive writes only; panel-b includes read-only steps.",
                             "Historical filesystem KB is multiplied by 1000 for Figure 2a; do not silently reinterpret as KiB.",
                             "Memory dirty values are MiB despite historical MB labels; total is process-tree RSS, not VM allocation.",
                             "Memory delta bar includes the final single-contributor step; the line excludes it."])


def histogram(values, edges):
    counts = [0] * (len(edges)-1)
    for value in values:
        value = number(value, "histogram value")
        for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
            if lo <= value < hi or i == len(counts)-1 and value == hi:
                counts[i] += 1
                break
    return counts


def figure6(ev):
    rows = ev.csv("figure-06/data/records/memory-policies/write_forkonly_memcurve_curve.csv")
    series, metrics = [], []
    for arm in ("skip", "gc", "none", "warm"):
        cks = sorted((r for r in rows if r["arm"] == arm and r["after"] == "ckpt"), key=lambda r: int(r["ev_i"]))
        if not cks:
            raise ValueError(f"Missing memory policy: {arm}")
        for i, row in enumerate(cks):
            series.append(dict(panel="a", arm=arm, metric="memory", x=i+1, x_unit="checkpoint", y=number(float(row["combined_footprint_mb"]), "memory"),
                               unit="MB_archived", evidence_kind=RAW))
        metrics.append(metric("final_memory", float(cks[-1]["combined_footprint_mb"]), "MB_archived", len(cks), RAW, arm=arm, panel="a"))
    populations = defaultdict(list)
    excluded, files = [], defaultdict(list)
    for path in ev.glob("figure-06/data/records/adaptive/**/*.results.jsonl"):
        rel = str(path.relative_to(ev.root))
        if not any(name in path.name.lower() for name in ("xarray", "django", "sympy")):
            excluded.append(dict(path=rel, reason="Astropy probe outside paper cohort"))
            continue
        rows = [r for r in ev.jsonl(path) if r.get("kind") == "ckpt" and r.get("ckpt_wall_ms") is not None]
        lw = sum(r.get("strategy") == "lightweight" for r in rows)
        std = sum(r.get("strategy") == "standard" for r in rows)
        if lw + std != len(rows) or not rows:
            raise ValueError(f"Unknown/empty adaptive arm: {path}")
        arm = "standard_only" if lw == 0 and std > 0 else "adaptive"
        files[arm].append(rel)
        for row in rows:
            key = "standard_only" if arm == "standard_only" else "adaptive_" + row["strategy"]
            populations[key].append(number(row["ckpt_wall_ms"], "ckpt_wall_ms"))
    edges = [10 ** (math.log10(.05) + (math.log10(500)-math.log10(.05))*i/35) for i in range(36)]
    edges[0], edges[-1] = .05, 500.
    for arm, values in sorted(populations.items()):
        metrics.append(metric("checkpoint_ms", mean(values), "ms", len(values), RAW, arm=arm, panel="b"))
        counts = histogram([max(x, .05) for x in values], edges)
        for i, count in enumerate(counts):
            series.append(dict(panel="b", arm=arm, metric="checkpoint_histogram", x=edges[i], x_unit="ms", y=count, unit="events",
                               bin_lo=edges[i], bin_hi=edges[i+1], evidence_kind=RAW))
    return dict(metrics=metrics, series=series, selection=dict(adaptive_files=dict(files), excluded=excluded,
                population_counts={k: len(v) for k, v in populations.items()}), limitations=[
        "Memory policy observation is one fork-only SymPy trace, CRIU disabled; footprint includes tmpfs and PSS.",
        "Adaptive histogram classifies whole runs before splitting strategies; standard-only is not all standard events.",
        "Historical histogram clamps below 0.05 ms and spans 0.05–500 ms; raw means remain unclamped."])


def figure7(ev):
    data = ev.json("figure-07/data/records/end2end_2sys.json")
    metrics = []
    for group in data["groups"]:
        for backend, row in data["data"][group].items():
            kind = "derived_model" if row.get("modeled") else AGG
            for field in ("floor_s", "wall_s", "ratio"):
                metrics.append(metric(field, row[field], "ratio" if field == "ratio" else "s", None, kind,
                                      backend=backend, group=group, modeled=bool(row.get("modeled"))))
    return dict(metrics=metrics, selection=dict(deltabox_inputs=12, e2b_inputs=8), limitations=[
        "Saved aggregate rendering, not a new end-to-end execution.", data["_note"],
        "Use saved ratios; saved baseline and total durations have independent rounding.",
        "Current selected raw DeltaBox component sums do not exactly reproduce the archived floors."])


def figure8(ev):
    root = "figure-08/data/records/substrate/"
    names = {"deltabox": "deltabox_official_fork_readtouch_20260605_223523.json",
             "cube": "cube_official_fork_20260605_210626.json", "e2b": "e2b_official_fork_20260605_211829.json"}
    series, excluded, e2b16 = [], [], None
    for backend, name in names.items():
        data = ev.json(root + name)
        rows = data["rows"] if isinstance(data, dict) else data
        for row in rows:
            if row.get("success") is not True:
                excluded.append(dict(path=root+name, forks=row["forks"], reason=row.get("error", "failed")))
                continue
            if row.get("success_count") != row["forks"]:
                raise ValueError("Fan-out did not verify every requested child")
            value = number(row["ready_e2e_ms"], "ready_e2e_ms")
            series.append(dict(panel="a", backend=backend, metric="ready_e2e_ms", x=row["forks"], x_unit="children", y=value,
                               unit="ms", n=1, evidence_kind=RAW, estimated=False))
            if backend == "e2b" and row["forks"] == 16:
                e2b16 = value
    more = ev.json(root + "e2b_official_fork16_c16_x4_20260605_215731.json")[0]
    if more.get("success") is not True or more.get("success_count") != 16 or e2b16 is None:
        raise ValueError("Missing successful E2B N16 evidence for explicit estimate")
    series.append(dict(panel="a", backend="e2b", metric="ready_e2e_ms", x=64, x_unit="children",
                       y=4*mean([e2b16, more["ready_e2e_ms"]]), unit="ms", n=2, evidence_kind="derived_model", estimated=True,
                       formula="4 * mean(two independently measured N16 ready_e2e_ms)", measured_forks=16))
    for row in ev.json(root + "e2b_official_fork64_20260605_212120.json"):
        excluded.append(dict(path=root+"e2b_official_fork64_20260605_212120.json", forks=row["forks"], reason=row.get("error", "failed")))
    primitive = defaultdict(list)
    templates = ev.glob("figure-08/data/records/fork-primitive/fanout_modeA_*.json")
    for path in templates:
        data = ev.json(path)
        for n, reps in data["raw_per_rep"].items():
            # Archive already excludes warmups. Keep every saved repetition.
            values = [number(r["wall_ns"], "wall_ns")/1e6 for rep in reps for r in rep["per_fork"]]
            if len(reps) != data["reps"] or len(values) != len(reps)*int(n):
                raise ValueError("Primitive repetition/child count mismatch")
            primitive[int(n)].append((statistics.median(values), len(values)))
    for n, values in sorted(primitive.items()):
        series.append(dict(panel="primitive", backend="deltabox", metric="per_child_ms", x=n, x_unit="children",
                           y=statistics.median(v[0] for v in values), unit="ms", n=sum(v[1] for v in values),
                           n_templates=len(values), evidence_kind=RAW, statistic="median of template medians"))
    return dict(series=series, selection=dict(native_failed=excluded, primitive_templates=len(templates), gpu="not measured in CPU fanout analysis; panel c is a separate CPU model"),
                limitations=["E2B N64 is 4 times the mean of two measured N16 batches; no successful N64 run is claimed.",
                             "Substrate latency includes inherited-state verification; primitive latency is a separate measurement.",
                             "Figure 8(b) is measured by the separate GPU runner; expected occupation/staleness is available through the CPU model."])


def order_percentile(values, percentile):
    if not values:
        return None
    xs = sorted(number(v, "order-statistic sample") for v in values)
    return xs[round(percentile/100*(len(xs)-1))]


def bin_for(size_bytes):
    size = number(size_bytes, "file_size_bytes")/1024
    return next((i for i, (lo, hi) in enumerate(BINS) if lo <= size < hi), None)


def aggregate_war(rows_by_arm):
    """Pool-aware two-stage order statistics, exactly matching historical plotting."""
    series, selection = [], {}
    for arm, rows in rows_by_arm.items():
        grouped = defaultdict(list)
        for row in rows:
            if row.get("applied_ok") is True:
                grouped[(row["instance"], row["file_path"])].append(row)
        bins = defaultdict(list)
        for (instance, file_path), edits in grouped.items():
            def upper(field):
                return sorted(number(e[field], field) for e in edits)[len(edits)//2]
            index = bin_for(upper("file_size_bytes"))
            if index is not None:
                bins[index].append((upper("copyup_bytes"), upper("phys_bytes"), len(edits)))
        selection[arm] = dict(records=len(rows), applied_ok=sum(len(v) for v in grouped.values()),
                              grouped_units=len(grouped), plotted_units=sum(len(v) for v in bins.values()),
                              plotted_edits=sum(r[2] for values in bins.values() for r in values))
        for i, (lo, hi) in enumerate(BINS):
            values = bins[i]
            for j, name in enumerate(("copyup_bytes", "phys_bytes")):
                series.append(dict(panel="a" if j == 0 else "b", arm=arm, metric=name, x=(lo+hi)/2*1024, x_unit="bytes",
                                   y=order_percentile([v[j] for v in values], 50), unit="bytes", bin_lo=lo*1024, bin_hi=hi*1024,
                                   p25=order_percentile([v[j] for v in values], 25), p75=order_percentile([v[j] for v in values], 75),
                                   n_units=len(values), n_edits=sum(v[2] for v in values), statistic="two-stage order median", evidence_kind=RAW))
    return series, selection


def figure9(ev):
    rows_by_arm = {arm: [] for arm in ("ext4", "xfs", "xfs_reflink")}
    files = Counter()
    for path in ev.glob("figure-09/data/records/*.jsonl"):
        arm = next((arm for arm in ("xfs_reflink", "xfs", "ext4") if path.stem.endswith("_"+arm)), None)
        if arm is None:
            continue
        instance = path.stem[:-(len(arm)+1)]
        for row in ev.jsonl(path):
            if row.get("instance", instance) != instance or row.get("fs_arm", arm) != arm:
                raise ValueError(f"WAR identity differs from filename: {path}")
            rows_by_arm[arm].append(dict(row, instance=instance))
        files[arm] += 1
    if not all(rows_by_arm.values()):
        raise ValueError("Missing WAR arm")
    series, selection = aggregate_war(rows_by_arm)
    for arm in selection:
        selection[arm]["files"] = files[arm]
    return dict(series=series, selection=selection, limitations=[
        "Groups are (pool-prefixed instance,file_path), not distinct SWE-bench instances.",
        "First-stage upper median and second-stage rounded-rank percentile differ from conventional medians.",
        "Bins use original file size; all six bins are retained, including the historically unlabeled 32–64 KiB bin.",
        "The historical 12.1–66.4 kB shaded band is a frozen annotation, not a statistic recomputed here.",
        "Raw reflink copyup grows across bins; no flatness is imposed to match paper prose."])


ARCHIVED = {"table-02": table2, "table-03": table3, "figure-01": figure1, "figure-02": figure2,
            "figure-06": figure6, "figure-07": figure7, "figure-08": figure8, "figure-09": figure9}


def export_summary(summary, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "summary.json", summary)
    for name in ("metrics", "series"):
        rows = [dict(row, experiment=key, **({"run_experiment": row["experiment"]} if "experiment" in row else {}))
                for key, result in summary["experiments"].items() for row in result[name]]
        fields = sorted(set().union(*(row.keys() for row in rows))) if rows else ["experiment"]
        with (output / f"{name}.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return summary


def envelope(ev, experiments):
    producer = Path(__file__)
    return dict(schema_version=1, source=ev.source,
                analysis_mode="archived-data-analysis" if ev.source == "archived" else "fresh-run-analysis",
                input_root=str(ev.root), producer=dict(path=str(producer.resolve()), sha256=hashlib.sha256(producer.read_bytes()).hexdigest()),
                sources=[ev.sources[k] for k in sorted(ev.sources)], experiments=experiments,
                skipped=[dict(experiment="figure-08b", reason="Separate opt-in GPU measurement; panel c has a CPU theory entrypoint"),
                         dict(experiment="figure-03/04/05", reason="qualitative design diagrams")])


def analyze(archive_root=None, output=None):
    """Analyze verified archive bytes and write summary.json, metrics.csv, series.csv."""
    ev = Evidence(archive_root or AE_ROOT / "paper", "archived")
    experiments = {key: experiment(ev, lambda fn=fn: fn(ev)) for key, fn in ARCHIVED.items()}
    summary = envelope(ev, experiments)
    return export_summary(summary, output) if output is not None else summary


FRESH = "fresh_raw_events"
CONFIG_DIMENSIONS = ("experiment", "mode", "checkpoint_profile", "adaptive", "memory_policy", "prewarm_requested", "prewarm_mode", "run_purpose", "message_policy", "baseline_test_runtime", "mock_latency_policy", "replay_timing_method", "legacy_timing_policy", "e2b_worker_mode", "recorded_search_order", "source_identity", "measurement_identity")


def validate_latency_audits(config, reports):
    latency_policy = config.get('mock_latency_policy')
    if latency_policy is not None:
        if latency_policy not in ('zero', 'recorded'):
            raise ValueError('Unknown manifest mock latency policy')
        for report in reports:
            if (report.get('latency_policy') != latency_policy
                    or report['stats'].get('latency_policy') != latency_policy):
                raise ValueError('Mock latency policy differs from manifest-bound audit')
            if latency_policy == 'zero' and report['stats'].get('sleep_wall_s') != 0.0:
                raise ValueError('Zero-latency mock audit contains injected sleep')


def source_identity(config):
    """Use source identity recorded by the producer, never this checkout or a path."""
    owners = [config] + [config.get(key) or {} for key in
                        ('runtime_fingerprint', 'source_provenance', 'runtime')]
    def digest(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
    locked = {(owner.get('release') or {}).get('source_sha256') for owner in owners}
    locked.discard(None)
    if locked:
        if len(locked) != 1 or not all(digest(value) for value in locked):
            raise ValueError('Manifest contains invalid or conflicting release source hashes')
        return 'release-sha256:' + next(iter(locked))
    for owner in owners:
        if digest(owner.get('source_sha256')):
            return 'source-sha256:' + owner['source_sha256']
        files = owner.get('files')
        if isinstance(files, dict) and files:
            hashes = {name: (record.get('sha256') if isinstance(record, dict) else record)
                      for name, record in files.items()}
            if all(not Path(name).is_absolute() and digest(value) for name, value in hashes.items()):
                return 'source-files-sha256:' + hashlib.sha256(
                    json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    for owner in owners:
        commit = owner.get('git_commit') or owner.get('commit')
        if not isinstance(commit, str) or not commit:
            continue
        diff = owner.get('tracked_diff_sha256')
        if digest(diff):
            return 'git:' + commit + '+diff-sha256:' + diff
        if owner.get('tracked_worktree_dirty') is False or owner.get('status') == '':
            return 'git-clean:' + commit
        # Commit alone cannot identify a dirty or incompletely described tree.
    return 'unknown'


def fresh_labels(config):
    labels = {key: config.get(key) for key in CONFIG_DIMENSIONS}
    labels["source_identity"] = source_identity(config)
    identity = config.get("measurement_identity")
    labels["measurement_identity"] = json.dumps(identity, sort_keys=True, separators=(",", ":")) if identity else "legacy-unspecified"
    if labels["legacy_timing_policy"] is None and (
            config.get("conversion_policy") == "legacy-recorded-diff-and-file-context"
            or config.get("trajectory_timing") is True):
        labels["legacy_timing_policy"] = "recorded-wall"
    if config.get("backend") == "replay":
        labels["mock_latency_policy"] = config.get("mock_latency_policy", "recorded-legacy")
        labels["replay_timing_method"] = config.get("replay_timing_method", "legacy-replay")
    # Runtime execution changes observations and workload cost. Hash the full
    # declared identity after measurement instead of repeating the package list
    # in every row/plot label. Historical absent binding was explicitly none.
    test_runtime = config.get("baseline_test_runtime", {"backend": "none"})
    labels["baseline_test_runtime"] = ("none" if test_runtime.get("backend") == "none" else
        test_runtime.get("backend", "unknown") + ":" + hashlib.sha256(
            json.dumps(test_runtime, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    if labels["prewarm_requested"] is None and "guest_flags" in config:
        labels["prewarm_requested"] = "--prewarm" in config["guest_flags"]
    if labels["prewarm_mode"] is None:
        labels["prewarm_mode"] = (config.get("guest_env", {}).get("DELTABOX_PREWARM_MODE", "write")
                                  if labels["prewarm_requested"] else "off")
    if config.get("guest_env", {}).get("DELTABOX_DISABLE_PREWARM") == "1":
        labels["prewarm_mode"] = "off"
    labels["run_purpose"] = config.get("run_purpose", "unspecified")
    if config.get('storage_mode') is not None:
        labels['storage_mode'] = config['storage_mode']
    labels["cohort"] = ";".join(f"{k}={json.dumps(v, sort_keys=True)}" for k, v in labels.items())
    # Comparison arms remain separate measurements, but may share a plot.
    plot_dimensions = ("experiment", "mode", "checkpoint_profile", "run_purpose", "message_policy", "baseline_test_runtime", "mock_latency_policy", "replay_timing_method", "legacy_timing_policy", "e2b_worker_mode", "recorded_search_order", "source_identity", "measurement_identity")
    # Figure 6(a)'s named arms intentionally compare prewarm policies together.
    if config.get("experiment") != "figure-06-memory":
        plot_dimensions += ("prewarm_requested", "prewarm_mode")
    if 'storage_mode' in labels:
        plot_dimensions += ('storage_mode',)
    labels["plot_group"] = ";".join(f"{k}={json.dumps(labels[k])}" for k in plot_dimensions)
    return labels


class FreshRun:
    """A successful producer manifest and its exclusively owned measured artifacts."""
    def __init__(self, ev, path, claimed):
        self.ev, self.path = ev, path
        self.config = ev.json(path)
        config = self.config
        if any(config.get('guest_env', {}).get(key) == '1' for key in
               ('DELTABOX_API_PROFILE', 'DELTABOX_DUMP_DIAGNOSTICS', 'DELTABOX_RESTORE_DIAGNOSTICS')):
            raise ValueError(f"Diagnostic instrumentation is not a performance sample: {path}")
        if config.get("analysis_mode") != "fresh-measurement" or config.get("status") != "ok":
            raise ValueError(f"Fresh manifest must be a successful fresh-measurement: {path}")
        if not isinstance(config.get("experiment"), str) or not config["experiment"]:
            raise ValueError(f"Fresh manifest lacks experiment identity: {path}")
        artifacts = config.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError(f"Fresh manifest lacks measured artifacts: {path}")
        self.artifacts = {}
        self.sources = {str(path.relative_to(ev.root))}
        for record in artifacts:
            relative = Path(record["path"])
            if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
                raise ValueError(f"Artifact path must be relative to its run manifest: {relative}")
            target = path.parent / relative
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(path.parent.resolve()) or not resolved.is_relative_to(ev.root.resolve()):
                raise ValueError(f"Artifact escapes its run directory: {relative}")
            if resolved in claimed:
                raise ValueError(f"Artifact claimed more than once: {target}")
            if not target.is_file() or target.name == "run.json":
                raise ValueError(f"Invalid measured artifact: {relative}")
            size = record["bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"Invalid artifact size: {relative}")
            raw = target.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if len(raw) != size or digest != record["sha256"]:
                raise ValueError(f"Fresh artifact bytes/SHA-256 mismatch: {target}")
            rel = str(target.relative_to(ev.root))
            ev.expected[rel] = digest
            ev.sources[rel] = dict(path=rel, sha256=digest, bytes=size, manifest_verified=True)
            ev.used.add(rel)
            self.sources.add(rel)
            claimed.add(resolved)
            self.artifacts[str(relative)] = target

    def artifact(self, name):
        try:
            return self.artifacts[str(name)]
        except KeyError as exc:
            raise ValueError(f"Measurement is not bound by run.json artifacts: {self.path.parent / name}") from exc

    def json(self, name):
        return self.ev.json(self.artifact(name))

    def jsonl(self, name):
        return self.ev.jsonl(self.artifact(name))

    def result(self):
        record = self.config["result"]
        supplied = Path(record["path"])
        target = supplied if supplied.is_absolute() else self.path.parent / supplied
        resolved = target.resolve(strict=True)
        matches = [p for p in self.artifacts.values() if p.resolve() == resolved]
        if len(matches) != 1:
            raise ValueError(f"Result is not a manifest-bound artifact: {supplied}")
        path = matches[0]
        evidence = self.ev.sources[str(path.relative_to(self.ev.root))]
        if any(record.get(key) != evidence[key] for key in ("sha256", "bytes")):
            raise ValueError(f"Result descriptor differs from measured artifact: {supplied}")
        return path


def fresh_delta(run):
    ev, config = run.ev, run.config
    instance = config["instance"]
    rows = run.jsonl(f"{instance}.results.jsonl")
    if not completed(rows):
        raise ValueError(f"Fresh run has failed/incomplete events: {run.path}")
    events = [r for r in rows if r.get("kind") in ("ckpt", "restore")]
    if not events:
        raise ValueError("Fresh DeltaBox has no measured events")
    counts = Counter(r["kind"] for r in events)
    for kind in ("ckpt", "restore"):
        if counts[kind] != config["n_"+kind]:
            raise ValueError(f"Fresh {kind} count differs from run.json: {run.path}")
    if config.get("schedule_artifact"):
        # The bound local copy remains valid when the result bundle moves hosts.
        schedule = run.artifact(config["schedule_artifact"])
    else:
        # Legacy bundles may refer to an absolute schedule, but only inside the
        # selected source tree; never reach back into another machine's output.
        schedule = Path(config["schedule"])
        if not schedule.is_absolute():
            schedule = run.path.parent / schedule
        schedule = ev.path(schedule)
        if not schedule.resolve(strict=True).is_relative_to(ev.root.resolve()):
            raise ValueError("Fresh schedule escapes selected input directory")
    scheduled = ev.jsonl(schedule)
    rel = str(schedule.relative_to(ev.root))
    if ev.sources[rel]["sha256"] != config["schedule_sha256"]:
        raise ValueError("Fresh schedule SHA-256 mismatch")
    ev.sources[rel]["manifest_verified"] = True
    run.sources.add(rel)
    if len(scheduled) != len(events):
        raise ValueError("Fresh schedule and event length mismatch")
    for index, (actual, expected) in enumerate(zip(events, scheduled)):
        if actual["ev_i"] != index or actual["kind"] != expected["type"]:
            raise ValueError("Fresh event order differs from schedule")
        key, target = ("schedule_ckpt_id", "ckpt_id") if actual["kind"] == "ckpt" else ("schedule_target_id", "restore_to_ckpt_id")
        if actual[key] != expected[target] or bool(actual.get("bootstrap")) != bool(expected.get("bootstrap")):
            raise ValueError("Fresh event target/bootstrap differs from schedule")
        if actual.get("agent_mode") != "real" or actual.get("require_real_agent") is not True:
            raise ValueError("Fresh result lacks real-worker evidence")
        if actual["kind"] == "ckpt":
            if not (actual.get("worker_exec") or {}).get("ok") and not actual.get("worker_exec_test_timeout"):
                raise ValueError("Fresh checkpoint worker action failed")
            if not (actual.get("worker_index_status") or {}).get("ok"):
                raise ValueError("Fresh checkpoint worker index failed")
        elif not (actual.get("worker_index_status_after_restore") or {}).get("matches_target_ckpt"):
            raise ValueError("Fresh restore worker index does not match checkpoint")
    if not rows[-1].get("worker_index_loaded") or not rows[-1].get("worker_exec_required"):
        raise ValueError("Fresh run lacks loaded/executed real index evidence")
    ck = [number(r["checkpoint_api_wall_ms"], "checkpoint_api_wall_ms") for r in events if r["kind"] == "ckpt"]
    rs = [number(r["restore_api_wall_ms"], "restore_api_wall_ms") for r in events if r["kind"] == "restore"]
    return trace_record(instance, ck, rs), rows


def unavailable(reason, **extra):
    return dict(status="unavailable", metrics=[], series=[], selection={}, sources=[], limitations=[reason], **extra)


def measured_result(metrics=None, series=None, runs=(), limitations=(), selection=None, **extra):
    runs = list(runs)
    audits = [{'instance': run.config.get('instance'), 'backend': run.config.get('backend'),
               **fresh_labels(run.config), **run.config['replay_audit']}
              for run in runs if run.config.get('replay_audit')]
    limitations = list(limitations)
    if any(row['n_mismatch'] for row in audits):
        limitations.append('Audit replay completed with message differences; counts are in replay_audits. This does not establish exact prompt equivalence or real test execution.')
    return dict(status="analyzed", metrics=metrics or [], series=series or [],
                selection=selection or {}, limitations=limitations, replay_audits=audits,
                sources=sorted({source for run in runs for source in run.sources}), **extra)


def fresh_profiles(runs):
    profiles = defaultdict(list)
    for run in runs:
        config = run.config
        domain = config["experiment"].removeprefix("figure-02-")
        if domain not in ("filesystem", "memory"):
            raise ValueError(f"Unknown profile domain: {domain}")
        rows, samples, mock = run.jsonl("step_metrics.jsonl"), run.json("tree_rss_samples.json"), run.json("mock_stats.json")
        if len(rows) != config["step_count"] or not samples or len(samples) != config["rss_sample_count"]:
            raise ValueError("Fresh profile step/RSS sample count mismatch")
        policy = config.get('message_policy', 'strict')
        validate_stats(mock, policy)
        if (type(mock.get('cursor')) is not int or type(mock.get('total')) is not int
                or mock['total'] <= 0 or mock['cursor'] != mock['total']):
            raise ValueError("Fresh profile recorded-response replay is incomplete")
        if 'message_policy' in config:
            # FreshRun binds this file's bytes/hash to the manifest before any
            # measurements are accepted. Old manifests keep strict semantics.
            audit = run.json('mock_audit.json')
            actual = summarize_replay_audit([audit], policy)
            validate_latency_audits(run.config, [audit])
            if actual != config.get('replay_audit'):
                raise ValueError('Fresh profile audit summary differs from its manifest-bound export')
            if any(mock.get(key) != audit['stats'].get(key) for key in
                   ('cursor', 'total', 'n_mismatch', 'n_protocol_errors')):
                raise ValueError('Fresh profile stats differ from its audit export')
            if mock.get('ok') is not True:
                raise ValueError('Fresh profile mock did not report success')
        if any(r.get("soft_dirty_error") or r.get("soft_dirty_clear_error") or r.get("soft_dirty_skipped_pages", 0) for r in rows):
            raise ValueError("Fresh profile has incomplete soft-dirty scan")
        for row in rows:
            number(row["soft_dirty_bytes"], "soft_dirty_bytes")
            number(row["action_write_bytes"], "action_write_bytes")
        labels = fresh_labels(config)
        profiles[(domain, labels["cohort"])].append((run, rows, samples, labels))
    metrics, series, selection = [], [], {}
    for (domain, cohort), observations in profiles.items():
        instances = [r[0].config["instance"] for r in observations]
        if len(set(instances)) != len(instances):
            raise ValueError(f"Duplicate fresh profiling instance within {cohort}")
        by_index, values, totals = defaultdict(list), [], []
        labels = observations[0][3]
        for run, rows, samples, _ in observations:
            if domain == "filesystem":
                if "filesystem_baseline_bytes" in run.config:
                    totals.append(number(run.config["filesystem_baseline_bytes"], "filesystem_baseline_bytes"))
            else:
                totals.append(mean([number(r["rss_kb_total"], "rss_kb_total")/1024 for r in samples]))
            for i, row in enumerate(rows):
                value = row["action_write_bytes"]/1024 if domain == "filesystem" else row["soft_dirty_bytes"]/(1 << 20)
                by_index[i].append(value)
                values.append(value)
        positive = [v for v in values if v > 0] if domain == "filesystem" else values
        unit = "KiB" if domain == "filesystem" else "MiB"
        metrics.append(metric("step_delta", mean(positive), unit, len(positive), FRESH, domain=domain, panel="a", **labels))
        complete_totals = len(totals) == len(observations)
        metrics.append(metric("total", mean(totals) if complete_totals else None, "bytes" if domain == "filesystem" else "MiB",
                              len(totals), FRESH if complete_totals else "unavailable", domain=domain, panel="a",
                              reason=None if complete_totals else "Some selected runs lack filesystem_baseline_bytes; no partial-cohort total used.", **labels))
        for i, contributors in sorted(by_index.items()):
            series.append(dict(panel="b", domain=domain, metric="step_delta", x=i+1, x_unit="step", y=mean(contributors),
                               unit=unit, n=len(contributors), evidence_kind=FRESH, **labels))
        selection[cohort] = dict(domain=domain, instances=instances, samples=len(values), positive_samples=len(positive), total_samples=len(totals))
    return measured_result(metrics, series, runs, selection=selection, limitations=[
        "Recorded-response replay measures CPU state; it is not a new live-model search.",
        "Filesystem delta bar selects positive writes, while its line includes zeros. Fresh units are binary KiB/MiB.",
        "RSS total is the mean of per-instance sample means; each plotted step records its actual contributor count."],
        panels={domain: {"status": "analyzed" if any(key[0] == domain for key in profiles) else "unavailable",
                         "reason": None if any(key[0] == domain for key in profiles) else "No successful fresh profile for this domain."}
                for domain in ("filesystem", "memory")})


def fresh_memory(delta_runs):
    series, metrics, populations, histogram_runs = [], [], defaultdict(list), defaultdict(list)
    selection, selected = {}, []
    for run, rows in delta_runs:
        config, labels = run.config, fresh_labels(run.config)
        name = config["experiment"]
        if name not in ("figure-06-memory", "figure-06-adaptive"):
            continue
        selected.append(run)
        if name == "figure-06-memory":
            arm = config.get("memory_policy")
            if arm not in ("none", "skip", "gc", "warm"):
                raise ValueError("Fresh memory curve lacks explicit policy")
            events = [r for r in rows if r.get("kind") == "ckpt"]
            samples = [r for r in rows if r.get("kind") == "memcurve" and r.get("after") == "ckpt"]
            if [r["ev_i"] for r in samples] != [r["ev_i"] for r in events]:
                raise ValueError("Fresh memory curve lacks exactly one sample per checkpoint")
            key = labels["cohort"] + ";instance=" + config["instance"]
            if key in selection:
                raise ValueError("Duplicate fresh memory policy run")
            values = []
            for i, row in enumerate(samples):
                value = (number(row["snapshot_tmpfs_bytes"], "snapshot_tmpfs_bytes") +
                         1024*(number(row["templates_pss_kb"], "templates_pss_kb") +
                               number(row["active_pss_kb"], "active_pss_kb"))) / (1 << 20)
                values.append(value)
                series.append(dict(panel="a", arm=arm, instance=config["instance"], metric="memory", x=i+1,
                                   x_unit="checkpoint", y=value, unit="MiB", evidence_kind=FRESH, **labels))
            metrics.append(metric("final_memory", values[-1], "MiB", len(values), FRESH,
                                  arm=arm, instance=config["instance"], panel="a", **labels))
            selection[key] = dict(checkpoints=len(values), bootstrap_included=True)
        else:
            if not isinstance(config.get("adaptive"), bool):
                raise ValueError("Fresh adaptive experiment lacks explicit adaptive arm")
            checkpoints = [r for r in rows if r.get("kind") == "ckpt"]
            measured = [r for r in checkpoints if not r.get("bootstrap")]
            if not measured:
                raise ValueError("No non-bootstrap adaptive checkpoint events")
            for row in measured:
                strategy = row.get("strategy")
                if strategy not in ("standard", "lightweight") or not config["adaptive"] and strategy != "standard":
                    raise ValueError("Checkpoint strategy contradicts fresh adaptive arm")
                arm = "adaptive_"+strategy if config["adaptive"] else "standard_only"
                populations[(labels["cohort"], arm)].append(number(row["ckpt_wall_ms"], "ckpt_wall_ms"))
            key = labels["cohort"]
            histogram_runs[key].append(run)
            selection.setdefault(key, dict(instances=[], excluded_bootstrap=0, checkpoints=0))
            selection[key]["instances"].append(config["instance"])
            selection[key]["excluded_bootstrap"] += len(checkpoints)-len(measured)
            selection[key]["checkpoints"] += len(measured)
    edges = [10 ** (math.log10(.05) + (math.log10(500)-math.log10(.05))*i/35) for i in range(36)]
    edges[0], edges[-1] = .05, 500.
    for cohort, selected_runs in histogram_runs.items():
        instances = [run.config["instance"] for run in selected_runs]
        if len(set(instances)) != len(instances):
            raise ValueError("Duplicate fresh adaptive instance within one arm/profile/purpose")
    for (cohort, arm), values in sorted(populations.items()):
        runs = histogram_runs[cohort]
        labels = fresh_labels(runs[0].config)
        metrics.append(metric("checkpoint_ms", mean(values), "ms", len(values), FRESH, arm=arm, panel="b", **labels))
        counts = histogram([max(value, .05) for value in values], edges)
        selection[cohort].setdefault("histogram_overflow", {})[arm] = sum(value > edges[-1] for value in values)
        for i, count in enumerate(counts):
            series.append(dict(panel="b", arm=arm, metric="checkpoint_histogram", x=edges[i], x_unit="ms", y=count,
                               unit="events", bin_lo=edges[i], bin_hi=edges[i+1], evidence_kind=FRESH, **labels))
    result = measured_result(metrics, series, selected, selection=selection, limitations=[
        "Memory samples are snapshot tmpfs plus template and active PSS, in MiB; every checkpoint sample is retained.",
        "Adaptive latency uses internal ckpt_wall_ms and excludes explicitly marked bootstrap checkpoints only.",
        "Arms/profiles/purposes remain separate; histogram counts above 500 ms are reported as overflow, never silently discarded."])
    result["panels"] = {panel: dict(status="analyzed" if any(r["panel"] == panel for r in series) else "unavailable",
                                   reason=None if any(r["panel"] == panel for r in series) else "No successful manifest-bound run for this panel.") for panel in ("a", "b")}
    return result


def fresh_fanout(runs):
    series, seen = [], set()
    for run in runs:
        config = run.config
        backend = config["experiment"].removeprefix("figure-08-")
        labels = fresh_labels(config)
        # Backend is the comparison arm; only a common source/purpose may share
        # a fanout plot. The full cohort still distinguishes each producer.
        labels["plot_group"] = ";".join(f"{key}={json.dumps(labels[key])}"
                                        for key in ("run_purpose", "source_identity"))
        rows = run.json("measurements/fanout.json" if backend == "deltabox" else "fanout.json")
        rows = rows["rows"] if isinstance(rows, dict) else rows
        expected = config.get("forks", config.get("expected_forks"))
        if not rows or [r["forks"] for r in rows] != expected:
            raise ValueError("Fresh fanout requested-count mismatch")
        for row in rows:
            n = row["forks"]
            if isinstance(n, bool) or not isinstance(n, int) or n <= 0 or row.get("success") is not True or row.get("success_count") != n:
                raise ValueError("Fresh fanout did not verify every requested child")
            if row.get("estimated"):
                raise ValueError("Fresh fanout cannot import an estimate")
            key = (backend, labels["cohort"], n)
            if key in seen:
                raise ValueError("Duplicate fresh fanout backend/cohort/N; select one run explicitly")
            seen.add(key)
            series.append(dict(panel="a", backend=backend, metric="ready_e2e_ms", x=n, x_unit="children",
                               y=number(row["ready_e2e_ms"], "ready_e2e_ms"), unit="ms", n=1,
                               evidence_kind=FRESH, estimated=False, **labels,
                               protocol=config.get("protocol")))
    return measured_result(series=series, runs=runs,
                           selection=dict(backends=sorted({r["backend"] for r in series}), gpu="not measured in CPU fanout analysis; panel c is a separate CPU model",
                                          missing_backends=sorted({"deltabox", "cube", "e2b"}-{r["backend"] for r in series})),
                           limitations=["Every plotted point is an actual successful run with all requested children verified; no N64 estimate."])


def fresh_war(runs):
    populations, labels_by_cohort, seen = defaultdict(lambda: defaultdict(list)), {}, set()
    for run in runs:
        config, labels = run.config, fresh_labels(run.config)
        arm, key, cohort = config["arm"], config["input_key"], labels["cohort"]
        identity = (cohort, arm, key)
        if arm not in ("ext4", "xfs", "xfs_reflink") or identity in seen:
            raise ValueError("Invalid/duplicate fresh WAR arm/input within one population")
        seen.add(identity)
        labels_by_cohort[cohort] = labels
        rows = run.jsonl(f"measurements/{key}_{arm}.jsonl")
        if len(rows) != config["expected_edits"] or any(r.get("error") for r in rows):
            raise ValueError("Fresh WAR incomplete or infrastructure failure")
        for row in rows:
            if row.get("instance", key) != key or row.get("fs_arm", arm) != arm:
                raise ValueError("Fresh WAR identity mismatch")
            populations[cohort][arm].append(dict(row, instance=key))
    series, selection = [], {}
    for cohort, rows_by_arm in sorted(populations.items()):
        measured, arm_selection = aggregate_war(rows_by_arm)
        labels = labels_by_cohort[cohort]
        series.extend(dict(row, evidence_kind=FRESH, **labels) for row in measured)
        selection[cohort] = dict(arms=arm_selection, **labels)
    return measured_result(series=series, runs=runs, selection=selection, limitations=[
        "Fresh supplied arms only; absent filesystem arms are not backfilled.",
        "Different declared run purposes and experiment configurations are aggregated and plotted independently.",
        "Two-stage order statistics preserve pool-prefixed input identity. Historical shaded range is not used."], historical_annotation=False)


def fresh_correctness(runs):
    metrics = []
    for run in runs:
        data = run.json("measurements/correctness.json")
        if data.get("ok") is not True or not data.get("rows"):
            raise ValueError("Fresh correctness suite failed/empty")
        for row in data["rows"]:
            if row.get("ok") is not True or row.get("returncode") != 0 or not row.get("assertions"):
                raise ValueError("Fresh correctness suite incomplete")
            counts = Counter(a["status"] for a in row["assertions"])
            if counts["fail"] or any(k not in ("pass", "warn") for k in counts):
                raise ValueError("Fresh correctness assertion failed/unknown")
            metrics.append(metric("suite_pass", 1, "boolean", 1, FRESH, suite=row["suite"],
                                  emitted_pass_lines=counts["pass"], warning_lines=counts["warn"]))
    return measured_result(metrics=metrics, runs=runs, selection=dict(suites=len(metrics)), limitations=[
        "Named suite results only; emitted PASS lines are not claimed as distinct paper test cases."])


def fresh_replay(run):
    path = run.result()
    data = run.ev.json(path)
    if data.get("ok") is not True or data.get("instance") != run.config["instance"]:
        raise ValueError("Fresh Replay summary failed or changed instance")
    csv_path = path.with_name("restores.csv")
    run.artifact(csv_path.relative_to(run.path.parent))
    rows = run.ev.csv(csv_path)
    if (not rows or len(rows) != data.get("requested_restores") or len(rows) != data.get("completed_restores")
            or any(r.get("ok") not in ("True", "true", "1") or r.get("rc") != "0" for r in rows)):
        raise ValueError("Fresh Replay events are failed or incomplete")
    for row in rows:
        mismatch = row.get('mock_mismatch')
        validate_stats(dict(message_policy=row.get('message_policy', 'strict'),
                            n_mismatch=0 if mismatch in (None, '', 'None') else int(mismatch),
                            n_protocol_errors=int(row.get('mock_protocol_errors') or 0)),
                       run.config.get('message_policy', 'strict'))
    if run.config.get('message_policy') is not None:
        audit = run.config['replay_audit']
        expected = sum(int(row['target_expansions']) > 0 for row in rows)
        total = sum(0 if row.get('mock_mismatch') in (None, '', 'None') else int(row['mock_mismatch']) for row in rows)
        if audit['reports'] != expected or audit['n_mismatch'] != total:
            raise ValueError('Replay CSV differs from post-measurement audit exports')
    if {r["instance"] for r in rows} != {run.config["instance"]}:
        raise ValueError("Fresh Replay CSV identity mismatch")
    if sorted(int(r["restore_index"]) for r in rows) != list(range(len(rows))):
        raise ValueError("Fresh Replay restore indices are duplicated or incomplete")
    corrected = all(r.get("restore_zero_llm_ms") not in (None, "") for r in rows)
    if corrected:
        for row in rows:
            raw = number(float(row["restore_ms"]), "restore_ms")
            sleep = number(float(row["mock_sleep_ms"]), "mock_sleep_ms")
            replay = number(float(row["replay_ms"]), "replay_ms")
            value = number(float(row["restore_zero_llm_ms"]), "restore_zero_llm_ms")
            if sleep > replay or sleep > raw or not math.isclose(raw-sleep, value, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("Fresh Replay correction differs from the declared LLM wait accounting")
    declared_policy = run.config.get("mock_latency_policy")
    declared_method = run.config.get("replay_timing_method")
    if declared_policy is not None or declared_method is not None:
        methods = {"zero": "zero-latency-wall", "recorded": "recorded-sleep-subtracted"}
        if declared_policy not in methods or declared_method != methods[declared_policy]:
            raise ValueError("Fresh Replay has an unsupported timing identity")
        for item in [data, *rows]:
            if item.get("mock_latency_policy") != declared_policy or item.get("replay_timing_method") != declared_method:
                raise ValueError("Fresh Replay timing identity differs between manifest, summary and CSV")
        if not corrected:
            raise ValueError("Fresh zero-latency Replay lacks compatibility timing fields")
        if declared_policy == "zero":
            for row in rows:
                if (float(row["mock_sleep_ms"]) != 0 or float(row["restore_zero_llm_ms"]) != float(row["restore_ms"])
                        or float(row["replay_zero_llm_ms"]) != float(row["replay_ms"])):
                    raise ValueError("Replay with zero LLM delay must report the measured duration without subtracting sleep")
            backend, key = "replay-zero-llm", "restore_ms"
        else:
            backend, key = "replay-sleep-subtracted-estimate", "restore_zero_llm_ms"
    else:
        if any(item.get("mock_latency_policy") is not None or item.get("replay_timing_method") is not None
               for item in [data, *rows]):
            raise ValueError("Replay timing identity is absent from the manifest")
        backend = "replay-sleep-subtracted-estimate" if corrected else "replay-including-llm"
        key = "restore_zero_llm_ms" if corrected else "restore_ms"
    first = next(r for r in rows if int(r["restore_index"]) == 0)
    record = trace_record(run.config["instance"], [number(float(first["copytree_ms"]), "copytree_ms")],
                          [number(float(r[key]), key) for r in rows])
    record["replay_components"] = {"restore_raw_api_wall_ms": [number(float(r["restore_ms"]), "restore_ms") for r in rows]}
    if corrected:
        record["replay_components"]["mock_sleep_ms"] = [number(float(r["mock_sleep_ms"]), "mock_sleep_ms") for r in rows]
    return backend, record


def fresh_phases(runs):
    """Use actual component timers; names never imply finer attribution than measured."""
    grouped, selection = defaultdict(list), {}
    for run in runs:
        config, labels = run.config, fresh_labels(run.config)
        backend = config["backend"]
        key = (backend, labels["cohort"])
        phases = []
        try:
            data = run.ev.json(run.result())
            if backend == "cube":
                measured = run.json("cube_phases.json")
                events = measured["events"]
                expected = data["iterations"]
                if not events or len(events) != len(expected):
                    raise ValueError("Cube phase event count does not match pilot")
                for event, actual in zip(expected, events):
                    if (actual["event_index"] != event["ev_i"] or actual["kind"] != event["kind"] or
                            actual["snapshot_id"] != event["snapshot_id"] or not actual["phase_names"]):
                        raise ValueError("Cube phase identity differs from measured pilot")
                    total = number(actual["wall_ms"], "wall_ms")
                    components = {"filesystem": number(actual["filesystem_ms"], "filesystem_ms"),
                                  "process": number(actual["process_ms"], "process_ms"),
                                  "control_plane": number(actual["other_ms"], "other_ms"),
                                  "unclassified_api": number(actual["unclassified_ms"], "unclassified_ms")}
                    if not math.isclose(total, sum(components.values()), rel_tol=1e-9, abs_tol=1e-6):
                        raise ValueError("Fresh Cube phase sum does not match the measured API duration")
                    pilot_total = event["checkpoint_wall_ms" if event["kind"] == "ckpt" else "restore_wall_ms"]
                    if not math.isclose(total, pilot_total, rel_tol=1e-9, abs_tol=1e-6):
                        raise ValueError("Cube measured API duration differs from pilot")
                    phases.append(("checkpoint" if event["kind"] == "ckpt" else "restore", total, components))
            elif backend == "replay":
                path = run.result().with_name("restores.csv")
                run.artifact(path.relative_to(run.path.parent))
                rows = run.ev.csv(path)
                first = next(row for row in rows if int(row["restore_index"]) == 0)
                copy = number(float(first["copytree_ms"]), "copytree_ms")
                phases.append(("checkpoint", copy, {"filesystem": copy}))
                for row in rows:
                    fs = number(float(row["copytree_ms"]), "copytree_ms")+number(float(row["rmtree_ms"]), "rmtree_ms")
                    replay = number(float(row["replay_zero_llm_ms"]), "replay_zero_llm_ms")
                    phases.append(("restore", number(float(row["restore_zero_llm_ms"]), "restore_zero_llm_ms"),
                                   {"filesystem": fs, "replay": replay}))
            elif backend == "criu":
                for row in data["ckpts"]:
                    fs = number(row["fs_checkpoint_ms"], "fs_checkpoint_ms")
                    process = number(row["criu_dump_ms"], "criu_dump_ms")
                    phases.append(("checkpoint", number(row["checkpoint_total_ms"], "checkpoint_total_ms"),
                                   {"filesystem":fs, "process":process}))
                for row in data["restore_events"]:
                    total = number(row["restore_total_ms"], "restore_total_ms")
                    fs, process = number(row["fs_restore_ms"], "fs_restore_ms"), number(row["criu_restore_ms"], "criu_restore_ms")
                    phases.append(("restore", total, {"filesystem":fs, "process":process,
                                                      "unclassified_api":number(total-fs-process, "unclassified restore remainder")}))
            elif backend == "fc-diff":
                for row in data["ckpts"]:
                    fs, process = number(row["dm_snapshot_ms"], "dm_snapshot_ms"), number(row["fc_total_ms"], "fc_total_ms")
                    phases.append(("checkpoint",fs+process,{"filesystem":fs,"process":process}))
                for row in data["restore_events"]:
                    fs = number(row["dm_restore"]["dm_restore_ms"], "dm_restore_ms")
                    process = number(row["load"]["fc_load_ms"], "fc_load_ms")
                    merge = number(row["merge"]["merge_ms"], "merge_ms")
                    phases.append(("restore",fs+process+merge,{"filesystem":fs,"process":process,"memory_merge":merge}))
            elif backend == "e2b":
                for iteration in data["iterations"]:
                    for row in iteration["e2b_steps"]:
                        if "phase_windows" in row:
                            from ae.repro.e2b_phases import measured_phases
                            phases.extend(measured_phases(row))
                        else:
                            pause = number(row["pause_ms"], "pause_ms")
                            upload = number(row["snapshot_upload_ms"], "snapshot_upload_ms")
                            phases.append(("checkpoint",number(row["checkpoint_persist_ms"], "checkpoint_persist_ms"),
                                           {"pause":pause,"snapshot_upload":upload}))
                            resume = number(row["resume_ms"], "resume_ms")
                            phases.append(("restore",resume,{"resume":resume}))
            if not phases:
                raise KeyError("No component observations")
            for operation, total, components in phases:
                # Pilot/Go printed timers can be rounded independently to .001 ms.
                if not math.isclose(total, sum(components.values()), rel_tol=1e-9, abs_tol=.003):
                    raise ValueError(f"Fresh {backend} phase sum does not match its timer boundary")
            grouped[key].append((run, labels, phases))
        except KeyError as exc:
            selection.setdefault(labels["cohort"], dict(backend=backend, status="unavailable", reasons=[]))["reasons"].append(
                dict(instance=config.get("instance"), reason=f"Missing raw component field: {exc}"))
        except ValueError as exc:
            if backend == "cube" and "cube_phases.json" not in run.artifacts and config["experiment"] != "figure-01-cube":
                selection.setdefault(labels["cohort"], dict(backend=backend, status="unavailable", reasons=[]))["reasons"].append(
                    dict(instance=config.get("instance"), reason="No manifest-bound Cubelet phase capture; API total cannot supply phases."))
            else:
                raise
    metrics, selected = [], []
    for (backend, cohort), observations in grouped.items():
        if cohort in selection:
            # Never use the success subset of a population with missing phases.
            continue
        instances = [r[0].config["instance"] for r in observations]
        if len(set(instances)) != len(instances):
            raise ValueError("Duplicate fresh phase instance within one population")
        values = defaultdict(list)
        for run, labels, phases in observations:
            selected.append(run)
            for operation, total, components in phases:
                values[(operation,"total")].append(total)
                for name, value in components.items():
                    values[(operation,name)].append(value)
        for (operation,name), samples in values.items():
            metrics.append(metric(name,mean(samples),"ms",len(samples),FRESH,backend=backend,operation=operation,**observations[0][1]))
        selection[cohort] = dict(backend=backend,status="analyzed",instances=instances,
                                replay_audits=[r[0].config.get('replay_audit') for r in observations])
    if not metrics:
        return unavailable("No complete matched fresh phase timer population; baseline totals alone cannot reconstruct Figure 1.",
                           missing=selection)
    return measured_result(metrics=metrics,runs=selected,selection=selection,limitations=[
        "Only actual component timers are used; no published phase ratio or frozen constant is imported.",
        "E2B probe spans are clipped to measured API windows and partitioned without double counting. Overlap and uninstrumented API time, including upload, remain explicit. Older probes expose only pause/upload/resume.",
        "Cube preserves the measured control component and reports the remaining unclassified API time separately. FC total sums snapshot/load/merge/device timers and excludes unmeasured guest readiness.",
        "Replay checkpoint remains a pristine-copy proxy. Legacy replay phases subtract measured mock sleep and are estimates; runs with zero LLM delay report the measured duration with no injected wait."],
        missing_backends=sorted({"cube","e2b","replay","criu","fc-diff"}-{r["backend"] for r in metrics}))


def fresh_derived(delta_runs):
    """A component-sum model is not a measured end-to-end execution."""
    grouped, selected, missing, incomplete_cohorts = defaultdict(list), [], [], set()
    for run, rows in delta_runs:
        if run.config["experiment"] != "table-02-deltabox":
            continue
        labels = fresh_labels(run.config)
        ck = [r for r in rows if r.get("kind") == "ckpt" and not r.get("bootstrap")]
        if not ck or any(r.get("latency_ms") is None or (r.get("worker_exec") or {}).get("wall_ms") is None for r in ck):
            missing.append(dict(instance=run.config["instance"], reason="Missing recorded LLM RTT or action duration", cohort=labels["cohort"]))
            incomplete_cohorts.add(labels["cohort"])
            continue
        floor = sum(number(r["latency_ms"], "latency_ms") + number(r["worker_exec"]["wall_ms"], "worker_exec.wall_ms") for r in ck)
        if floor <= 0:
            missing.append(dict(instance=run.config["instance"], reason="Nonpositive LLM+action floor", cohort=labels["cohort"]))
            incomplete_cohorts.add(labels["cohort"])
            continue
        state = sum(number(r["checkpoint_api_wall_ms"], "checkpoint_api_wall_ms") if r["kind"] == "ckpt"
                    else number(r["restore_api_wall_ms"], "restore_api_wall_ms") for r in rows if r.get("kind") in ("ckpt", "restore"))
        labels = fresh_labels(run.config)
        grouped[(labels["cohort"], group_for(run.config["instance"]))].append((floor, state, labels))
        selected.append(run)
    grouped = {key: value for key, value in grouped.items() if key[0] not in incomplete_cohorts}
    selected = [run for run in selected if fresh_labels(run.config)["cohort"] not in incomplete_cohorts]
    if not grouped:
        return unavailable("No complete fresh LLM RTT + action + controller API component set for a Figure 7 model.",
                           missing=missing, panels={"deltabox": "unavailable", "e2b": "unavailable: matched LLM floor absent"})
    metrics = []
    for (_, group), values in grouped.items():
        floor, state = sum(r[0] for r in values), sum(r[1] for r in values)
        for name, value, unit in (("floor_s", floor/1000, "s"), ("wall_s", (floor+state)/1000, "s"), ("ratio", (floor+state)/floor, "ratio")):
            metrics.append(metric(name, value, unit, len(values), "derived_model", backend="deltabox", group=group,
                                  statistic="ratio-of-sums" if name == "ratio" else "sum",
                                  modeled=True, formula="(sum(recorded LLM RTT + measured action) + sum(measured full controller API duration)) / sum(recorded LLM RTT + measured action)",
                                  **values[0][2]))
    return measured_result(metrics=metrics, runs=selected, selection=dict(missing=missing), limitations=[
        "Derived serialized component-sum model; it does not claim a measured end-to-end latency or asynchronous overlap.",
        "E2B remains unavailable without matched recorded LLM-floor evidence; no archived model is substituted."],
        panels={"deltabox": "derived_model", "e2b": "unavailable: matched LLM floor absent"})


def fresh_e2b_derived(runs):
    from ae.repro.e2b_phases import model_components
    groups, selected, missing, incomplete = defaultdict(list), [], [], set()
    for run in runs:
        if run.config['backend'] != 'e2b':
            continue
        labels = fresh_labels(run.config)
        try:
            floor, state = model_components(run.ev.json(run.result()))
        except KeyError as exc:
            missing.append(dict(instance=run.config['instance'], reason=f'Missing E2B floor evidence: {exc}'))
            incomplete.add(labels['cohort'])
            continue
        groups[(labels['cohort'], group_for(run.config['instance']))].append((floor, state, labels))
        selected.append(run)
    metrics = []
    for (cohort, group), values in groups.items():
        if cohort in incomplete:
            continue
        floor, state = sum(v[0] for v in values), sum(v[1] for v in values)
        for name, value, unit in (('floor_s', floor/1000, 's'), ('wall_s', (floor+state)/1000, 's'), ('ratio', 1+state/floor, 'ratio')):
            metrics.append(metric(name, value, unit, len(values), 'derived_model', backend='e2b', group=group,
                                  statistic='ratio-of-sums' if name == 'ratio' else 'sum',
                                  modeled=True, formula='1 + sum(resume + checkpoint_persist) / sum(served controller build_action RTT + action wall including execution LLM waits)',
                                  **values[0][2]))
    selected = [r for r in selected if fresh_labels(r.config)['cohort'] not in incomplete]
    if not metrics:
        return unavailable('No complete E2B controller RTT/action evidence', missing=missing)
    return measured_result(metrics=metrics, runs=selected, selection=dict(missing=missing), limitations=[
        'E2B serialized component model (worker mode is part of the population), not measured end-to-end wall time. Setup, command transport and controller scaffolding are excluded.',
        'Only served controller build_action RTT is added to action wall; execution LLM waits already inside action wall are not added again. This corrects the archived all-RTT-plus-action formula.'])


def analyze_fresh(input_root, output=None):
    """Accept only complete, successful, hash-bound fresh measurements.

    Different experiments, checkpoint profiles, policy/adaptive arms and run
    purposes never share a statistical population. No raw file is discovered as
    an independent measurement, and no missing panel receives archive values.
    """
    ev = Evidence(input_root, "fresh")
    claimed, excluded_roots = set(), set()
    runs, excluded_runs = [], []
    for path in ev.glob("**/run.json"):
        config = ev.json(path)
        if config.get("analysis_mode") != "fresh-measurement":
            raise ValueError(f"Run manifest is not fresh-measurement: {path}")
        if not isinstance(config.get("experiment"), str) or not config["experiment"]:
            raise ValueError(f"Fresh manifest lacks experiment identity: {path}")
        if not isinstance(config.get("status"), str) or not config["status"]:
            raise ValueError(f"Fresh manifest lacks run status: {path}")
        if config["status"] != "ok":
            excluded_roots.add(path.parent.resolve())
            excluded_runs.append(dict(path=str(path.relative_to(ev.root)), experiment=config["experiment"],
                                      instance=config.get("instance"), input_key=config.get("input_key"),
                                      run_purpose=config.get("run_purpose", "unspecified"), status=config["status"],
                                      error=config.get("error") or f"Run status is {config['status']}; no successful measurement imported."))
            continue
        runs.append(FreshRun(ev, path, claimed))
    if not runs:
        raise ValueError(f"No successful manifest-bound fresh measurements ({len(excluded_runs)} non-successful runs excluded)")
    # Detect loose evidence instead of accepting an archive/raw-results directory.
    for pattern in ("**/*.results.jsonl", "**/pilot_result.json", "**/restores.csv", "**/fanout.json", "**/step_metrics.jsonl",
                    "**/measurements/*.jsonl", "**/correctness.json", "**/tree_rss_samples.json"):
        for path in ev.glob(pattern):
            resolved = path.resolve()
            if resolved not in claimed and not any(resolved.is_relative_to(root) for root in excluded_roots):
                raise ValueError(f"Orphan raw measurement has no successful run manifest: {path}")
    results = {name: unavailable("No successful manifest-bound fresh run for this experiment.") for name in ARCHIVED}
    results["figure-01"] = unavailable("Fresh baseline pilots do not provide matched filesystem/process/control-plane phase timers. Total latency alone cannot reconstruct Figure 1.")
    groups, components, selections, delta_runs, table_runs, phase_runs = defaultdict(list), [], [], [], [], []
    auxiliaries = defaultdict(list)
    for run in runs:
        config = run.config
        name, labels = config["experiment"], fresh_labels(config)
        if name in ("table-02-deltabox", "table-03-slow", "figure-06-memory", "figure-06-adaptive"):
            for key in ("mode", "checkpoint_profile", "adaptive", "run_purpose"):
                if key not in config:
                    raise ValueError(f"DeltaBox grouping dimension missing: {key}")
            rec, rows = fresh_delta(run)
            delta_runs.append((run, rows))
            # Figure 6 arm data is not a Table 2/3 throughput cohort.
            if name.startswith("figure-06-"):
                continue
            backend = "deltabox"
            for field in FRESH_COMPONENTS:
                values = [r[field] for r in rows if r.get(field) is not None]
                if values:
                    components.append(metric(field, mean(values), "ms", len(values), FRESH,
                                             backend=backend, instance=rec["instance"], **labels))
            if config['checkpoint_profile'] in ('async-incremental', 'async-incremental-lazy', 'historical-async-full'):
                # The paper's zero is an overlap MODEL, not an observed zero-cost API.
                checkpoint_rows = [r for r in rows if r.get('kind') == 'ckpt']
                if checkpoint_rows and all(r.get('latency_ms') is not None for r in checkpoint_rows):
                    residuals = [max(0., number(r['checkpoint_api_wall_ms'], 'checkpoint_api_wall_ms')
                                     - number(r['latency_ms'], 'latency_ms')) for r in checkpoint_rows]
                    components.append(metric('checkpoint_masked_model_ms', mean(residuals), 'ms', len(residuals),
                                             'derived-model', backend=backend, instance=rec['instance'], **labels))
        elif name.startswith("table-02-") or name == "figure-01-cube":
            backend = "cube" if name == "figure-01-cube" else name.removeprefix("table-02-")
            if config.get("backend") != backend:
                raise ValueError("Fresh baseline backend identity mismatch")
            if config.get('message_policy') is not None:
                reports = [run.json(p) for p in run.artifacts if 'mock_audit' in Path(p).name and p.endswith('.json')]
                actual = summarize_replay_audit(reports, config['message_policy'])
                if actual != config.get('replay_audit'):
                    raise ValueError('Replay audit summary differs from its manifest-bound exports')
                validate_latency_audits(config, reports)
                if backend != 'replay' and len(reports) != 1:
                    raise ValueError('Baseline requires one post-measurement audit export')
            if backend == "replay":
                backend, rec = fresh_replay(run)
            else:
                data = ev.json(run.result())
                backend_found, rec = parse_pilot(data)
                if backend_found != backend or rec["instance"] != config["instance"] or data.get("tail_error") or data.get("status") == "TAIL_CRASH_OK":
                    raise ValueError("Fresh pilot identity mismatch or incomplete tail")
            counts = config.get("counts", {})
            if any(counts.get(key) != rec[op][0] for key, op in (("checkpoints", "checkpoint"), ("restores", "restore"))):
                raise ValueError("Fresh baseline counts differ from manifest")
            phase_runs.append(run)
            if name == "figure-01-cube":
                continue
        elif name.startswith("figure-02-"):
            auxiliaries["figure-02"].append(run)
            continue
        elif name in ("figure-08-deltabox", "figure-08-cube", "figure-08-e2b"):
            auxiliaries["figure-08"].append(run)
            continue
        elif name in ("figure-09", "correctness"):
            auxiliaries[name].append(run)
            continue
        else:
            raise ValueError(f"Unsupported fresh experiment: {name}")
        groups[(backend, labels["cohort"])].append((rec, labels))
        table_runs.append(run)
        selections.append(dict(path=str(run.path.relative_to(ev.root)), instance=rec["instance"], backend=backend,
                               replay_audit=config.get('replay_audit'),
                               incremental_dump_enabled=config.get("incremental_dump_enabled"), **labels))
    metrics = []
    for (backend, cohort), records in sorted(groups.items()):
        instances = [r[0]["instance"] for r in records]
        if len(set(instances)) != len(instances):
            raise ValueError(f"Duplicate fresh instance for {backend}/{cohort}; choose one run explicitly")
        metrics += [dict(row, **records[0][1]) for row in latency_metrics([r[0] for r in records], backend, FRESH)]
        for field in ("restore_raw_api_wall_ms", "mock_sleep_ms"):
            values = [v for rec, _ in records for v in rec.get("replay_components", {}).get(field, [])]
            if values:
                metrics.append(metric(field, mean(values), "ms", len(values), FRESH, backend=backend, group="All", **records[0][1]))
    if metrics:
        results["table-02"] = measured_result(metrics=metrics, runs=table_runs, selection=dict(runs=selections), limitations=[
            "Full controller API latency is distinct from the paper's internal critical timers.",
            "Experiments, modes, checkpoint profiles, adaptive/policy arms and smoke/full purposes remain separate.",
            "Replay checkpoint is a pristine-copy proxy. Paper replay-sleep-subtracted-estimate subtracts the served completion prefix's recorded RTT after execution, replay-including-llm retains it, and replay-zero-llm reports the measured duration with zero LLM delay. These populations remain separate."])
    if components:
        results["table-03"] = measured_result(metrics=components, runs=table_runs, limitations=[
            "Only fresh internal timer fields are reported; overlapping windows must not be summed as serialized phases."])
    if any(run.config["experiment"].startswith("figure-06-") for run, _ in delta_runs):
        results["figure-06"] = fresh_memory(delta_runs)
    if phase_runs:
        results["figure-01"] = fresh_phases(phase_runs)
    results["figure-07"] = fresh_derived(delta_runs)
    e2b_model = fresh_e2b_derived(phase_runs)
    if e2b_model.get('metrics'):
        delta_model = results['figure-07']
        if delta_model.get('metrics'):
            delta_model['metrics'].extend(e2b_model['metrics'])
            delta_model['limitations'] = [v for v in delta_model['limitations'] if 'E2B remains unavailable' not in v] + e2b_model['limitations']
            delta_model['selection']['e2b'] = e2b_model['selection']
            delta_model['panels']['e2b'] = 'derived_model'
            delta_model['sources'] = sorted(set(delta_model['sources']) | set(e2b_model['sources']))
            delta_model['replay_audits'].extend(e2b_model['replay_audits'])
        else:
            results['figure-07'] = e2b_model
            e2b_model['panels'] = dict(deltabox='unavailable', e2b='derived_model')
    for name, function in (("figure-02", fresh_profiles), ("figure-08", fresh_fanout), ("figure-09", fresh_war), ("correctness", fresh_correctness)):
        if auxiliaries[name]:
            results[name] = function(auxiliaries[name])
    summary = envelope(ev, results)
    coverage_note = ("Only supplied successful runs are analyzed. The declared run_purpose does not prove coverage: "
                     "the complete paper cohort has not been verified (未验证论文完整 cohort).")
    populations = defaultdict(list)
    for run in runs:
        populations[fresh_labels(run.config)["cohort"]].append(run)
    population_counts = []
    for cohort, population in sorted(populations.items()):
        identifiers = sorted({run.config.get("instance") or run.config.get("input_key")
                              for run in population if run.config.get("instance") or run.config.get("input_key")})
        population_counts.append(dict(cohort=cohort, actual_run_count=len(population), actual_instance_count=len(identifiers),
                                      actual_instances=identifiers, paper_cohort_verified=False,
                                      run_purpose=population[0].config.get("run_purpose", "unspecified")))
    actual_instances = sorted({identity for row in population_counts for identity in row["actual_instances"]})
    summary["excluded_runs"] = excluded_runs
    summary["selection"] = dict(actual_run_count=len(runs), actual_instance_count=len(actual_instances),
                                actual_instances=actual_instances, excluded_run_count=len(excluded_runs),
                                paper_cohort_verified=False, populations=population_counts, reason=coverage_note)
    for result in results.values():
        result["limitations"].append(coverage_note)
    return export_summary(summary, output) if output is not None else summary

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("archived", "fresh"), default="archived")
    parser.add_argument("--input", type=Path, help="Archive paper directory or fresh run directory")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.source == "fresh" and args.input is None:
        parser.error("--source fresh requires --input")
    try:
        result = analyze(args.input, args.output) if args.source == "archived" else analyze_fresh(args.input, args.output)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(2, f"Analysis failed: {exc}\n")
    print(f"{result['analysis_mode']}: {len(result['experiments'])} experiments; {args.output / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
