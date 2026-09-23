#!/usr/bin/env python3
"""Render canonical analysis JSON as PDF/PNG; never load or backfill raw data."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ae.repro.common import write_json

COLORS = {"deltabox": "#176b55", "cube": "#457fac", "e2b": "#df8c2b", "replay": "#7e6298",
          "fc-diff": "#8e963c", "criu": "#8c6455", "skip": "#176b55", "gc": "#457fac", "none": "#df8c2b", "warm": "#7e6298",
          "ext4": "#df8c2b", "xfs": "#7e6298", "xfs_reflink": "#176b55"}
LABELS = {"deltabox": "DeltaBox", "cube": "CubeSandbox", "e2b": "E2B", "replay": "Replay+copy", "replay-zero-llm": "Replay+copy\nzero LLM delay",
          "replay-sleep-subtracted-estimate": "Replay+copy\npaper RTT accounting",
          "replay-including-llm": "Replay+copy\nincluding LLM wait", "fc-diff": "FC-Diff+dm", "criu": "CRIU+copy",
          "skip": "LW-skip", "gc": "Reachability GC", "none": "No policy", "warm": "Async-warm", "ext4": "ext4", "xfs": "XFS", "xfs_reflink": "XFS+reflink"}
GROUPS = ("Django", "SymPy", "Scientific", "Tools/Small", "All")


def fmt(value):
    if value is None:
        return "unavailable"
    return f"{value:,.3f}" if abs(value) < 100 else f"{value:,.1f}"


def table(plt, rows, columns, title, *, col_widths=None, figure_width=None):
    fig, ax = plt.subplots(figsize=(figure_width or max(8, len(columns)*1.4), max(2.7, len(rows)*.27+1.6)))
    ax.set_axis_off()
    tab = ax.table(cellText=rows, colLabels=columns, colWidths=col_widths, loc="center", cellLoc="center")
    tab.auto_set_font_size(False)
    tab.set_fontsize(8)
    tab.scale(1, 1.35)
    header_lines = max(label.count("\n") + 1 for label in columns)
    for (i, _), cell in tab.get_celld().items():
        cell.set_edgecolor("#d9dfe3")
        if i == 0:
            cell.set_height(cell.get_height() * header_lines)
            cell.set_facecolor("#e9eff2")
            cell.set_text_props(weight="bold")
    ax.set_title(title, loc="left", pad=20)
    return fig


def table2_columns(metrics):
    """Short display labels with the complete identities saved in plots.json."""
    systems = list(dict.fromkeys((r["backend"], r.get("cohort", "")) for r in metrics))
    counts, seen = defaultdict(int), defaultdict(int)
    for backend, _ in systems:
        counts[backend] += 1
    columns = []
    for backend, cohort in systems:
        seen[backend] += 1
        label = LABELS.get(backend, backend)
        if counts[backend] > 1:
            label += f" [{seen[backend]}]"
        columns.append(dict(label=label, backend=backend, cohort=cohort))
    return columns


def _archived_table2(plt, result):
    metrics = result["metrics"]
    systems = table2_columns(metrics)
    lookup = {(r["backend"], r.get("cohort", ""), r["group"], r["metric"]): r for r in metrics}
    columns = ["Workload"] + [system["label"] + "\nck / rs (ms)" for system in systems]
    rows = []
    for group in GROUPS:
        cells = [group]
        for system in systems:
            backend, cohort = system["backend"], system["cohort"]
            ck = lookup.get((backend, cohort, group, "checkpoint_ms"), {})
            rs = lookup.get((backend, cohort, group, "restore_ms"), {})
            cells.append(fmt(ck.get("value"))+" / "+fmt(rs.get("value")))
        rows.append(cells)
    return table(plt, rows, columns, "Checkpoint and restore latency")


def _archived_table3(plt, result):
    # Fresh results may have a row per instance. Pool with the recorded event n.
    grouped = defaultdict(list)
    for row in result["metrics"]:
        grouped[(row.get("backend", ""), row.get("mode", row.get("cohort", "")), row["metric"])].append(row)
    rows = []
    for (backend, mode, name), values in sorted(grouped.items()):
        valid = [r for r in values if r["value"] is not None]
        n = sum(r["n"] for r in valid)
        value = sum(r["value"]*r["n"] for r in valid)/n if n else None
        rows.append([LABELS.get(backend, backend), mode, name, str(n), fmt(value)])
    # Timer identifiers must remain intact: equal-width cells clipped names such
    # as checkpoint_sync_no_dump_ms and restore_fast_dispatch_ms in the PNG.
    return table(plt, rows, ["Backend", "Mode / cohort", "Timer field", "Events", "Mean (ms)"],
                 "Available component timers", col_widths=(.13, .16, .46, .10, .15), figure_width=10)


def _archived_figure1(plt, result):
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
    categories = ("filesystem", "process", "guest_readiness", "control_plane", "replay", "memory_merge", "unclassified_api", "pause", "snapshot_upload", "resume")
    colors = ("#67a9cf", "#e5ac54", "#68a89b", "#adb5bf", "#8c78ac", "#ab776a", "#acacac", "#7a8fba", "#91aa70", "#b085aa")
    lookup = {(r["backend"], r["operation"], r["metric"]): r["value"] for r in result["metrics"]}
    backends = [b for b in ("e2b", "cube", "replay", "criu", "fc-diff") if any(row["backend"] == b for row in result["metrics"])]
    legend = {}
    for ax, operation in zip(axes, ("checkpoint", "restore")):
        for x, backend in enumerate(backends):
            bottom = 0
            for category, color in zip(categories, colors):
                value = lookup.get((backend, operation, category))
                if value is None:
                    continue
                bars = ax.bar(x, value, bottom=bottom, color=color, width=.6, edgecolor="white", label=category.replace("_", " "))
                legend.setdefault(category, bars)
                bottom += value
            if (backend, operation, "total") in lookup:
                ax.annotate(f"{bottom:,.1f}", (x, bottom), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=8)
        ax.set_xticks(range(len(backends)), [LABELS[b]+("*" if result.get("source") != "fresh" and b in ("e2b","replay") else "") for b in backends])
        ax.set_title(operation.capitalize(), loc="left")
        ax.set_ylabel("Measured timer boundary (ms)")
        ax.margins(y=.2)
        if operation == "restore":
            ax.set_yscale("log")
    fig.legend(list(legend.values()), [key.replace("_", " ") for key in legend], loc="upper center", ncol=min(5,len(legend)), fontsize=8)
    fig.subplots_adjust(top=.8, bottom=.2, wspace=.3)
    return fig


def table2(plt, result):
    if result.get('source') == 'archived':
        return _archived_table2(plt, result)
    from ae.repro.paper_tables import table2 as render_table
    return render_table(plt, result)


def table3(plt, result):
    if result.get('source') == 'archived':
        return _archived_table3(plt, result)
    from ae.repro.paper_tables import table3 as render_table
    return render_table(plt, result)


def figure1(plt, result):
    if result.get('source') == 'archived':
        return _archived_figure1(plt, result)
    from ae.repro.paper_figure1 import figure1 as render_figure
    return render_figure(plt, result)


PAPER_LAYOUT_KEYS = frozenset(('table-02', 'table-03', 'figure-01', 'figure-02', 'figure-06', 'figure-07', 'figure-09'))


def paper_mapping(key, result):
    if key == 'figure-01':
        from ae.repro.paper_figure1 import figure1_metadata
        return figure1_metadata(result)
    if key == 'figure-02':
        from ae.repro.paper_figure2 import figure2_metadata
        return figure2_metadata(result)
    if key == 'figure-06':
        from ae.repro.paper_figure6 import figure6_metadata
        return figure6_metadata(result)
    if key == 'figure-07':
        from ae.repro.paper_figure7 import figure7_metadata
        return figure7_metadata(result)
    if key == 'figure-09':
        from ae.repro.paper_figure9 import figure9_metadata
        return figure9_metadata(result)
    from ae.repro.paper_tables import table2_manifest, table3_manifest
    return (table2_manifest if key == 'table-02' else table3_manifest)(result)


def figure2(plt, result):
    if result.get('source') != 'archived':
        from ae.repro.paper_figure2 import figure2 as render_figure
        return render_figure(plt, result)
    return _archived_figure2(plt, result)


def _archived_figure2(plt, result):
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
    metrics = {(r["domain"], r["metric"]): r for r in result["metrics"]}
    # Common byte axis is explicitly marked historical display conversion.
    for i, domain in enumerate(("filesystem", "memory")):
        for key, offset, color, label in (("total", -.17, "#457fac", "Total"), ("step_delta", .17, "#df8c2b", "Step delta")):
            row = metrics.get((domain, key))
            if row is None or row["value"] is None:
                continue
            factor = {"MB": 1e6, "MB_archived": 1e6, "KB_historical": 1000, "KiB": 1024, "MiB": 1 << 20, "bytes": 1}[row["unit"]]
            axes[0].bar(i+offset, row["value"]*factor, width=.34, color=color, label=label if i == 0 or ("filesystem", key) not in metrics else None)
    axes[0].set_yscale("log")
    axes[0].set_xticks([0, 1], ["Filesystem", "Memory"])
    axes[0].set_ylabel("Bytes (log scale; see recorded unit conventions)")
    axes[0].legend()
    axes[0].set_title("Mean delta and total", loc="left")
    secondary = axes[1].twinx()
    for domain, ax in (("filesystem", axes[1]), ("memory", secondary)):
        rows = [r for r in result["series"] if r["domain"] == domain]
        if domain == "filesystem":
            ax.bar([r["x"] for r in rows], [r["y"] for r in rows], color="#457fac", alpha=.6, label="Filesystem")
        else:
            ax.plot([r["x"] for r in rows], [r["y"] for r in rows], color="#df8c2b", marker=".", label="Memory dirty")
    axes[1].set_xlabel("MCTS step")
    fs_units = {r["unit"] for r in result["series"] if r.get("domain") == "filesystem"}
    axes[1].set_ylabel("Filesystem delta (" + ("KiB" if fs_units == {"KiB"} else "historical KB") + ")")
    secondary.set_ylabel("Memory dirty (MiB)")
    axes[1].set_title("Per-step distribution", loc="left")
    fig.subplots_adjust(bottom=.23, left=.08, right=.92, wspace=.4)
    return fig


def figure6(plt, result):
    if result.get('source') != 'archived':
        from ae.repro.paper_figure6 import figure6 as render_figure
        return render_figure(plt, result)
    return _archived_figure6(plt, result)


def _archived_figure6(plt, result):
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
    for arm in ("skip", "gc", "none", "warm"):
        rows = sorted((r for r in result["series"] if r["panel"] == "a" and r["arm"] == arm), key=lambda r: r["x"])
        by_instance = defaultdict(list)
        for row in rows:
            by_instance[row.get("instance", "")].append(row)
        for instance, values in by_instance.items():
            axes[0].plot([r["x"] for r in values], [r["y"] for r in values], color=COLORS[arm],
                         label=LABELS[arm] + (" / " + instance if len(by_instance) > 1 else ""), marker=".")
    axes[0].set_yscale("log")
    axes[0].set_xlabel("Checkpoint index")
    axes[0].set_ylabel("Combined footprint (" + ("MiB" if result.get("source") == "fresh" else "archived MB") + ")")
    axes[0].set_title("Memory policies, fork-only", loc="left")
    if any(r["panel"] == "a" for r in result["series"]):
        axes[0].legend(fontsize=8)
    else:
        axes[0].text(.5, .5, "Panel unavailable: no fresh memory-policy run", transform=axes[0].transAxes, ha="center", fontsize=8)
    populations = {arm: sorted((r for r in result["series"] if r["panel"] == "b" and r["arm"] == arm), key=lambda r: r["x"])
                   for arm in ("adaptive_lightweight", "adaptive_standard", "standard_only")}
    lw = populations["adaptive_lightweight"]
    for arm, color, bottom, label in (("adaptive_lightweight", "#67a9cf", None, "Adaptive: lightweight"),
                                      ("adaptive_standard", "#e5ac54", [r["y"] for r in lw], "Adaptive: standard")):
        rows = populations[arm]
        if rows:
            axes[1].bar([r["bin_lo"] for r in rows], [r["y"] for r in rows], width=[r["bin_hi"]-r["bin_lo"] for r in rows],
                        bottom=bottom if bottom else None, align="edge", color=color, alpha=.75, label=label)
    rows = populations["standard_only"]
    if rows:
        axes[1].stairs([r["y"] for r in rows], [r["bin_lo"] for r in rows]+[rows[-1]["bin_hi"]], color="#444444", linestyle="--", label="Standard-only")
    axes[1].set_xscale("log")
    axes[1].set_xlabel("Checkpoint latency (ms)")
    axes[1].set_ylabel("Events")
    axes[1].set_title("Lightweight-skip distribution", loc="left")
    if any(populations.values()):
        axes[1].legend(fontsize=8)
    else:
        axes[1].text(.5, .5, "Panel unavailable: no fresh adaptive run", transform=axes[1].transAxes, ha="center", fontsize=8)
    fig.subplots_adjust(bottom=.23, wspace=.3)
    return fig


def figure7(plt, result):
    if result.get('source') != 'archived':
        from ae.repro.paper_figure7 import figure7 as render_figure
        return render_figure(plt, result)
    return _archived_figure7(plt, result)


def _archived_figure7(plt, result):
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    lookup = {(r["group"], r["backend"]): r["value"] for r in result["metrics"] if r["metric"] == "ratio"}
    groups = [group for group in GROUPS[:-1] if any(g == group for g, _ in lookup)]
    backends = [backend for backend in ("deltabox", "e2b") if any(b == backend for _, b in lookup)]
    width = .7 / max(1, len(backends))
    for i, backend in enumerate(backends):
        items = [(j, lookup[group, backend]) for j, group in enumerate(groups) if (group, backend) in lookup]
        positions = [j+(i-(len(backends)-1)/2)*width for j, _ in items]
        values = [v for _, v in items]
        ax.bar(positions, [1]*len(values), width=width, color=COLORS[backend], alpha=.25)
        suffix = " (component model)" if result.get("source") == "fresh" else " (warm-worker model)" if backend == "e2b" else " (archived aggregate)"
        ax.bar(positions, [v-1 for v in values], bottom=1, width=width, color=COLORS[backend], alpha=.85, label=LABELS[backend]+suffix)
        for x, value in zip(positions, values):
            ax.annotate(f"{value:.2f}×", (x, value), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=9)
    ax.axhline(1, color="#777777", linestyle="--", linewidth=.8)
    ax.set_xticks(range(len(groups)), groups)
    ax.set_ylabel("Component total / own LLM+action floor")
    ax.set_ylim(0, max([2.35]+[value*1.15 for value in lookup.values()]))
    ax.legend(fontsize=8, loc="upper left")
    fig.subplots_adjust(bottom=.23)
    return fig


def figure8(plt, result):
    has_primitive = any(r["panel"] == "primitive" for r in result["series"])
    if has_primitive:
        fig, axes = plt.subplots(1, 2, figsize=(10, 4.5))
    else:
        fig, ax = plt.subplots(figsize=(7, 4.5))
        axes = [ax]
    for backend in ("deltabox", "cube", "e2b"):
        rows = sorted((r for r in result["series"] if r["panel"] == "a" and r["backend"] == backend), key=lambda r: r["x"])
        if not rows:
            continue
        measured = [r for r in rows if not r.get("estimated")]
        axes[0].plot([r["x"] for r in measured], [r["y"] for r in measured], color=COLORS[backend], marker="o", label=LABELS[backend])
        estimated = [r for r in rows if r.get("estimated")]
        if estimated:
            tail = [measured[-1]]+estimated
            axes[0].plot([r["x"] for r in tail], [r["y"] for r in tail], color=COLORS[backend], linestyle="--", marker="D",
                         markerfacecolor="white", label="E2B N64 estimate")
    axes[0].set_ylabel("All children ready + verified (ms)")
    axes[0].set_title("CPU sandbox fan-out", loc="left")
    axes[0].legend(fontsize=8)
    if has_primitive:
        rows = [r for r in result["series"] if r["panel"] == "primitive"]
        axes[1].plot([r["x"] for r in rows], [r["y"] for r in rows], color=COLORS["deltabox"], marker="o")
        axes[1].set_ylabel("Per-child latency (ms)")
        axes[1].set_title("Separate fork primitive, nine templates", loc="left")
    for ax in axes:
        ax.set_xscale("log", base=2)
        ax.set_yscale("log")
        ax.set_xticks([1, 4, 16, 64], ["1", "4", "16", "64"])
        ax.set_xlabel("Children requested")
    fig.subplots_adjust(bottom=.23, wspace=.3)
    return fig


def figure9(plt, result):
    if result.get('source') == 'fresh':
        from ae.repro.paper_figure9 import figure9 as render_figure
        return render_figure(plt, result)
    return _archived_figure9(plt, result)


def _archived_figure9(plt, result):
    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    for arm, marker in (("ext4", "^"), ("xfs", "s"), ("xfs_reflink", "D")):
        for panel, ax in zip(("a", "b"), axes):
            rows = sorted((r for r in result["series"] if r["arm"] == arm and r["panel"] == panel and r["n_units"]), key=lambda r: r["x"])
            if not rows:
                continue
            ax.plot([r["x"] for r in rows], [r["y"] for r in rows], color=COLORS[arm], marker=marker,
                    linestyle="--" if arm == "ext4" and panel == "a" else "-", label=LABELS[arm])
    for ax in axes:
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_ylabel("Bytes per edit")
        if result.get("source") != "fresh" and result.get("historical_annotation", True):
            ax.axvspan(12.1e3, 66.4e3, color="#eadcaa", alpha=.25)
    axes[0].set_title("Copy-up duplicated data", loc="left")
    axes[1].set_title("Physical I/O", loc="left")
    axes[0].legend(fontsize=8)
    centers = [(lo+hi)/2*1024 for lo, hi in ((1, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 256))]
    axes[1].set_xticks(centers, ["1–8", "8–16", "16–32", "32–64", "64–128", "128–256"])
    axes[1].set_xlabel("Original edited-file size bin (KiB)")
    fig.subplots_adjust(bottom=.2, hspace=.25)
    return fig


RENDERERS = {"table-02": table2, "table-03": table3, "figure-01": figure1, "figure-02": figure2,
             "figure-06": figure6, "figure-07": figure7, "figure-08": figure8, "figure-09": figure9}
NOTES = {"table-02": "Separate backend cohorts. Archive analysis is not a new measurement; DeltaBox differs from published totals.",
         "table-03": "Available timers only; phase windows overlap. Archived slow total uses eight complete runs, not all twelve attempts.",
         "figure-01": "* E2B phase ratios and Replay values use explicitly tagged historical plotting adjustments.",
         "figure-02": "Archived profiling. Filesystem bar excludes zero writes; memory line omits its single-contributor last step. See JSON unit notes.",
         "figure-06": "Memory: one fork-only trace, 28 checkpoints per arm. Histogram: 831 LW + 250 adaptive standard; 1081 standard-only.",
         "figure-07": "E2B is a component-based warm-worker model, not a measured warm end-to-end execution.",
         "figure-08": "E2B N64 = 4 × mean(two N16 measurements); native N64 failed. Panel (b) uses the GPU runner; panel (c) has a separate CPU theory calculator.",
         "figure-09": "Two-stage order statistics over (pool-instance, file). Shading is the frozen historical annotation."}


def audit_note(result):
    groups = {row.get('plot_group') for row in result.get('metrics', []) + result.get('series', [])}
    audits = [row for row in result.get('replay_audits', []) if row.get('plot_group') in groups]
    differences = sum(row['n_mismatch'] for row in audits)
    if not differences:
        return ''
    dropped = sum(row['audit_records_dropped'] for row in audits)
    omitted = sum(row['audit_payloads_omitted'] for row in audits)
    return f'AUDIT REPLAY: {differences} message differences; dropped={dropped}, omitted={omitted}. See audit evidence.'


def render(input_path, output):
    # Optional dependency: the analysis module and its tests do not import it.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.titlesize": 11, "figure.dpi": 120})
    path, output = Path(input_path), Path(output)
    content = path.read_bytes()
    summary = json.loads(content)
    if summary.get("schema_version") != 1 or summary.get("source") not in ("archived", "fresh"):
        raise ValueError("Expected canonical analysis schema_version=1 and source")
    output.mkdir(parents=True, exist_ok=True)
    artifacts, unavailable = [], []
    for key, result in summary["experiments"].items():
        if key not in RENDERERS or not (result.get("metrics") or result.get("series")):
            unavailable.append(dict(experiment=key, status=result.get("status"), reasons=result.get("limitations", [])))
            continue
        result = dict(result, source=summary["source"])
        variants = [("", result)]
        paper_layout = summary['source'] == 'fresh' and key in PAPER_LAYOUT_KEYS
        if paper_layout:
            # Independent statistics occupy the paper's separate domains,
            # panels, columns or bars. Each renderer rejects conflicting
            # populations for the same plotted statistic.
            groups = sorted({row.get('plot_group', '')
                             for row in result.get('metrics', []) + result.get('series', [])})
            variants = [('', dict(result, plot_populations=groups))]
        elif summary["source"] == "fresh":
            # Plot each population independently; a line must never join different
            # checkpoint profiles or quick-check/full measurements at the same x.
            field = ("run_purpose" if key == "figure-08" and not any(
                "plot_group" in row for row in result["metrics"]+result["series"]) else "plot_group")
            keys = sorted({r.get(field, "") for r in result["metrics"]+result["series"]})
            if len(keys) > 1:
                variants = [(f"-population-{i+1}", dict(result,
                    metrics=[r for r in result["metrics"] if r.get(field, "") == group],
                    series=[r for r in result["series"] if r.get(field, "") == group],
                    plot_population=group)) for i, group in enumerate(keys)]
            else:
                variants = [("", dict(result, plot_population=keys[0]))]
        for suffix_name, variant in variants:
            fig = RENDERERS[key](plt, variant)
            label = "ARCHIVED DATA ANALYSIS" if summary["source"] == "archived" else "FRESH RUN ANALYSIS"
            note = NOTES[key] if summary["source"] == "archived" else "Fresh supplied population only. No archived values or historical annotations are used."
            if summary['source'] == 'fresh' and audit_note(variant):
                note = audit_note(variant)
            # Point-sized spacing also works for short table figures, where a
            # fixed fraction of figure height made the two footer lines overlap.
            if not paper_layout:
                height_points = fig.get_size_inches()[1] * 72
                fig.text(.02, 24 / height_points, label+"  ·  "+key, fontsize=8, color="#555555", weight="bold")
                fig.text(.02, 10 / height_points, note, fontsize=7, color="#555555")
            mapping = paper_mapping(key, variant) if paper_layout else None
            save_options = dict(dpi=200 if paper_layout else 180, bbox_inches="tight")
            if paper_layout:
                save_options['pad_inches'] = 0
                save_options.update({name: value for name, value in (mapping or {}).get('savefig', {}).items()
                                     if name in ('dpi', 'bbox_inches', 'pad_inches')})
            name = (key+"-cpu" if key == "figure-08" else key) + suffix_name
            for suffix in ("pdf", "png"):
                destination = output / f"{name}.{suffix}"
                with plt.rc_context({'pdf.fonttype': 42, 'ps.fonttype': 42} if paper_layout else {}):
                    fig.savefig(destination, **save_options)
                artifacts.append(dict(experiment=key, path=str(destination.resolve()), population=variant.get("plot_population"),
                                      **({'layout': 'paper', 'populations': variant['plot_populations'],
                                          'data_mapping': mapping, 'audit_note': audit_note(variant)} if paper_layout else {}),
                                      **({"columns": table2_columns(variant["metrics"])} if key == "table-02" and not paper_layout else {}),
                                      sha256=hashlib.sha256(destination.read_bytes()).hexdigest()))
            plt.close(fig)
    if not artifacts:
        raise ValueError("No supported plots in supplied summary")
    manifest = dict(schema_version=1, input=str(path.resolve()), input_sha256=hashlib.sha256(content).hexdigest(),
                    source=summary["source"], artifacts=artifacts, unavailable=unavailable, skipped=summary.get("skipped", []))
    manifest['rendering'] = dict(layout_version='paper-tables-figures-1-2-6-7-9-v3',
        files=[dict(path=str(source.relative_to(Path(__file__).resolve().parents[2])),
                    sha256=hashlib.sha256(source.read_bytes()).hexdigest())
               for source in (Path(__file__).resolve(), Path(__file__).with_name('paper_tables.py'),
                              Path(__file__).with_name('paper_figure1.py'), Path(__file__).with_name('paper_figure2.py'),
                              Path(__file__).with_name('paper_figure6.py'), Path(__file__).with_name('paper_figure7.py'),
                              Path(__file__).with_name('paper_figure9.py'))])
    write_json(output / "plots.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = render(args.input, args.output)
    except ImportError as exc:
        parser.exit(2, f"Plotting requires matplotlib: {exc}\n")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"Plotting failed: {exc}\n")
    print(f"Rendered {len(result['artifacts'])} files; {args.output / 'plots.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
