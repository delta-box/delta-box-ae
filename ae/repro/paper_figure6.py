"""Paper Figure 6 layout, drawn only from the supplied canonical analysis.

Memory policies and checkpoint-latency populations occupy separate panels.
Their different experiment identities are intentional; two populations for
the same curve or histogram arm are never pooled or selected implicitly.
"""
from __future__ import annotations

import json
import math


MEMORY = (
    ("skip", "LW-skip", "#006400", "d", "-"),
    ("gc", "reachability GC", "#6CB4F0", "^", "--"),
    ("none", "none", "#FF8C00", "s", "-"),
    ("warm", "async-warm", "#6A5ACD", ">", "--"),
)
HISTOGRAM = (
    ("standard_only", "Std", "#666666", "#666666", "", 1.),
    ("adaptive_lightweight", "LW", "#87CEEB", "#4a8db8", "xxxx", .55),
    ("adaptive_standard", "std", "#FFA500", "#cc7a00", "////", .45),
)
POPULATION_FIELDS = (
    "cohort", "plot_group", "source_identity", "source_summary", "input_summary",
    "experiment", "mode", "checkpoint_profile", "adaptive", "memory_policy",
    "prewarm_requested", "prewarm_mode", "run_purpose", "message_policy",
    "baseline_test_runtime", "mock_latency_policy", "replay_timing_method",
    "legacy_timing_policy", "evidence_kind", "instance",
)
PAPER_RC = {
    "pdf.fonttype": 42, "ps.fonttype": 42, "font.family": "STIXGeneral",
    "mathtext.fontset": "stix", "font.size": 9, "axes.labelsize": 9,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": False, "legend.frameon": False, "hatch.linewidth": .5,
    "figure.facecolor": "white", "axes.facecolor": "white",
}
MEMORY_FACTORS = {"MiB": (1 << 20) / 1_000_000, "MB": 1., "MB_archived": 1.}
X_LABELS = {"checkpoint": "Checkpoint index", "mcts_iteration": "MCTS iteration",
            "iteration": "Iteration"}


def _number(value, *, integer=False, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0)
            and (not integer or int(value) == value))


def _population(row):
    return {key: row[key] for key in POPULATION_FIELDS if key in row}


def _identity(row):
    return json.dumps(_population(row), sort_keys=True)


def _selection(result, row):
    cohort = row.get("cohort")
    if not cohort:
        return {}
    key = (cohort + ";instance=" + row["instance"]
           if row.get("panel") == "a" and row.get("instance") else cohort)
    value = result.get("selection", {}).get(key, {})
    return dict(value) if isinstance(value, dict) else {}


def _slot(result, panel, arm, label, series_metric, summary_metric):
    indexed = [(i, row) for i, row in enumerate(result.get("series", []))
               if row.get("panel") == panel and row.get("arm") == arm
               and row.get("metric") == series_metric]
    metrics = [(i, row) for i, row in enumerate(result.get("metrics", []))
               if row.get("panel") == panel and row.get("arm") == arm
               and row.get("metric") == summary_metric]
    rows = [row for _, row in indexed]
    all_rows = rows + [row for _, row in metrics]
    identities = sorted({_identity(row) for row in all_rows})
    slot = dict(panel=panel, arm=arm, label=label, status="unavailable", reason=None,
                source_series_indices=[i for i, _ in indexed],
                source_metric_indices=[i for i, _ in metrics],
                populations=[json.loads(identity) for identity in identities],
                supplied_series=[{key: row[key] for key in
                                  ("x", "x_unit", "y", "unit", "bin_lo", "bin_hi", "count")
                                  if key in row} for row in rows],
                supplied_metrics=[dict(row) for _, row in metrics],
                selection=_selection(result, rows[0]) if rows else {})
    if len(identities) > 1:
        slot["reason"] = "Multiple populations target the same arm; no values pooled or overwritten."
    elif not rows:
        slot["reason"] = "No supplied measurements for this arm."
    elif len(metrics) > 1:
        slot["reason"] = "Duplicate summary metrics; no count or final value selected implicitly."
    elif any(row.get("evidence_kind") == "published_adjustment" for row in all_rows):
        slot["reason"] = "Historical plotting adjustments are not measured samples."
    return slot, rows, [row for _, row in metrics]


def _memory_slot(result, arm, label):
    slot, rows, metrics = _slot(result, "a", arm, label, "memory", "final_memory")
    slot.update(points=[], x_unit=None, displayed_unit="MB", conversions=[])
    if slot["reason"]:
        return slot
    if any(not _number(row.get("x"), integer=True)
           or not _number(row.get("y"), positive=True)
           or row.get("unit") not in MEMORY_FACTORS for row in rows):
        slot["reason"] = "Memory samples need nonnegative indices and positive finite values in a declared memory unit."
        return slot
    if len({row["x"] for row in rows}) != len(rows):
        slot["reason"] = "Duplicate memory index; no curves averaged or concatenated."
        return slot
    units = {row.get("x_unit") for row in rows}
    if len(units) != 1 or next(iter(units)) not in X_LABELS:
        slot["reason"] = "Memory samples do not establish a common checkpoint or iteration axis."
        return slot
    rows = sorted(rows, key=lambda row: row["x"])
    if metrics:
        final = metrics[0]
        if (not _number(final.get("n"), integer=True) or final["n"] != len(rows)
                or not _number(final.get("value"), positive=True)
                or final.get("unit") not in MEMORY_FACTORS
                or not math.isclose(final["value"] * MEMORY_FACTORS[final["unit"]],
                                    rows[-1]["y"] * MEMORY_FACTORS[rows[-1]["unit"]],
                                    rel_tol=1e-9, abs_tol=1e-9)):
            slot["reason"] = "Final-memory summary does not match the supplied curve and checkpoint count."
            return slot
    slot.update(status="measured", x_unit=next(iter(units)),
                points=[dict(x=row["x"], y=row["y"] * MEMORY_FACTORS[row["unit"]]) for row in rows],
                conversions=[dict(from_unit=unit, to_unit="MB", factor=MEMORY_FACTORS[unit])
                             for unit in sorted({row["unit"] for row in rows})])
    return slot


def _histogram_slot(result, arm, label):
    slot, rows, metrics = _slot(result, "b", arm, label, "checkpoint_histogram", "checkpoint_ms")
    slot.update(bins=[], event_count=None, displayed_event_count=None,
                binned_event_count=None, overflow_event_count=None)
    if slot["reason"]:
        return slot
    if any(not _number(row.get("bin_lo"), positive=True)
           or not _number(row.get("bin_hi"), positive=True)
           or row["bin_hi"] <= row["bin_lo"]
           or not _number(row.get("y"), integer=True)
           or row.get("unit") != "events" or row.get("x_unit") != "ms"
           or ("count" in row and row["count"] != row["y"]) for row in rows):
        slot["reason"] = "Histogram bins need positive millisecond edges and nonnegative integer event counts."
        return slot
    rows = sorted(rows, key=lambda row: row["bin_lo"])
    if any(not math.isclose(left["bin_hi"], right["bin_lo"], rel_tol=1e-12, abs_tol=0.)
           for left, right in zip(rows, rows[1:])):
        slot["reason"] = "Histogram bins overlap, repeat or have gaps; no implicit rebinning."
        return slot
    binned = sum(int(row["y"]) for row in rows)
    overflow = slot["selection"].get("histogram_overflow", {}).get(arm, 0)
    slot.update(binned_event_count=binned, overflow_event_count=overflow)
    if not _number(overflow, integer=True):
        slot["reason"] = "The source histogram has no valid overflow count."
        return slot
    total = metrics[0].get("n") if metrics else binned + overflow
    slot["event_count"] = total
    if (not _number(total, integer=True)
            or (metrics and (metrics[0].get("unit") != "ms"
                             or not _number(metrics[0].get("value"))))
            or total != binned + overflow):
        slot["reason"] = "Histogram counts do not close against the independent same-arm checkpoint count."
        return slot
    slot.update(status="measured", displayed_event_count=binned,
                bins=[dict(lo=row["bin_lo"], hi=row["bin_hi"], count=int(row["y"])) for row in rows])
    return slot


def figure6_metadata(result):
    """Return JSON-serializable display mappings and every unavailable-arm reason."""
    memory = [_memory_slot(result, arm, label) for arm, label, *_ in MEMORY]
    histograms = [_histogram_slot(result, arm, label) for arm, label, *_ in HISTOGRAM]
    x_units = {slot["x_unit"] for slot in memory if slot["status"] == "measured"}
    if len(x_units) > 1:
        for slot in memory:
            if slot["status"] == "measured":
                slot.update(status="unavailable", points=[],
                            reason="Memory arms use different x-axis semantics; no alignment inferred.")
        x_units = set()
    x_unit = next(iter(x_units), "checkpoint")
    lw, adaptive_std = histograms[1:]
    if lw["status"] == adaptive_std["status"] == "measured":
        reason = None
        if lw["populations"] != adaptive_std["populations"]:
            reason = "Adaptive subpopulations have different identities; they cannot form one stacked histogram."
        elif [(b["lo"], b["hi"]) for b in lw["bins"]] != [(b["lo"], b["hi"]) for b in adaptive_std["bins"]]:
            reason = "Adaptive subpopulations have different bin edges; no counts rebinned or pooled."
        if reason:
            for slot in (lw, adaptive_std):
                slot.update(status="unavailable", reason=reason, bins=[])
    measured_memory = [slot for slot in memory if slot["status"] == "measured"]
    bootstrap = bool(measured_memory) and all(slot["selection"].get("bootstrap_included") is True
                                              for slot in measured_memory)
    description = ("checkpoint index" if x_unit == "checkpoint" else
                   "MCTS iteration" if x_unit == "mcts_iteration" else "iteration")
    prefix = "Figure 6. Bounding memory under deep search."
    caption = (prefix + f" (a) Memory footprint vs. {description} under four retention policies"
               + (" (including bootstrap)" if bootstrap else "")
               + ". (b) Per-event checkpoint latency with and without lightweight-skip.")
    if any(slot["status"] != "measured" for slot in memory + histograms):
        caption += " N/A: unavailable measurements."
    overflow_notes = [f"{slot['overflow_event_count']} {slot['label']} "
                      f"event{'s' if slot['overflow_event_count'] != 1 else ''} above {slot['bins'][-1]['hi']:g} ms"
                      for slot in histograms if slot["status"] == "measured" and slot["overflow_event_count"]]
    if overflow_notes:
        caption += " Outside histogram range: " + "; ".join(overflow_notes) + "."
    return dict(
        format_source="plot/plot_fig_mem_combo.py",
        format_reference="ae/docs/paper-plotting-reference.json",
        figsize_inches=[3.4, 1.55], width_ratios=[1.3, 1.],
        savefig={"bbox_inches": "tight", "dpi": 180, "pad_inches": .035},
        caption=caption, caption_bold_prefix=prefix,
        panels={"a": dict(xlabel=X_LABELS[x_unit], ylabel="Memory (MB)", yscale="log",
                          title="(a) Memory vs. checkpoints" if x_unit == "checkpoint"
                          else "(a) Memory vs. search depth"),
                "b": dict(xlabel="Per-event ckpt latency (ms)", ylabel="ckpt events", xscale="log",
                          title="(b) Lightweight-skip latency")},
        slots=memory + histograms,
        notes=[
            "The supplied memory experiment and adaptive experiment are independent panels, not pooled populations.",
            "Each memory arm requires one trace/configuration/source; its named retention policy may differ from the other arms.",
            "Adaptive LW and adaptive standard counts are stacked only for the same population and identical bin edges; standard-only remains independent.",
            "MiB is converted to decimal MB by multiplying by 2^20/10^6. Supplied MB and archived MB retain their declared scale.",
            "Checkpoint indices include bootstrap only where the source selection says so; they are not relabelled MCTS iterations.",
            "Fresh memory samples include snapshot tmpfs plus template and active PSS, rather than resident memory alone.",
            "Histogram bin edges/counts are drawn as supplied. The canonical analyzer places latencies below 0.05 ms in the first bin; raw means stay unchanged.",
            "No new clipping, clamping or histogram rebinning is applied here. Legend counts describe supplied bins; any source overflow is retained separately and stated in the caption.",
            "Total, binned and overflow counts must close for each independent arm. Overflow event latencies are not inferred from their count.",
            "The source paper style sets savefig.bbox=tight; explicit tight bounds retain its labels around the nominal 3.4 by 1.55 inch canvas.",
            "N/A is a nonnumeric placeholder; missing policies and subpopulations are never inferred as zero.",
        ],
    )


def _style_axis(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(labelsize=9)
    ax.set_axisbelow(True)
    ax.grid(axis="y", linestyle="--", alpha=.6, linewidth=.5, zorder=0)


def _draw_memory(ax, slots):
    by_arm = {slot["arm"]: slot for slot in slots}
    points, missing = [], []
    for arm, label, color, marker, linestyle in MEMORY:
        slot = by_arm[arm]
        if slot["status"] != "measured":
            missing.append((label, color))
            continue
        values = slot["points"]
        ax.plot([point["x"] for point in values], [point["y"] for point in values],
                color=color, marker=marker, linestyle=linestyle, ms=3.2, lw=1.,
                markevery=3, label=label, zorder=3)
        points.extend(values)
    ax.set_yscale("log")
    xmin = min((p["x"] for p in points), default=1.)
    xmax = max((p["x"] for p in points), default=28.)
    margin = max(1., xmax - xmin) * .05
    ax.set_xlim(min(-.35, xmin - margin), max(29.35, xmax + margin))
    ax.set_ylim(min(60., min((p["y"] for p in points), default=75.) / 1.12),
                max(3000., max((p["y"] for p in points), default=2300.) * 1.12))
    if ax.get_xlim()[1] == 29.35:
        ax.set_xticks([0, 10, 20])
    for i, (label, color) in enumerate(missing):
        ax.text(.04, .94 - .22 * i, label + ": N/A", transform=ax.transAxes,
                ha="left", va="top", fontsize=6.4, color=color)


def _draw_histogram(ax, slots):
    from matplotlib.patches import Patch

    by_arm = {slot["arm"]: slot for slot in slots}
    plotted, handles, labels = [], [], []
    for arm, label, color, edge, hatch, alpha in HISTOGRAM:
        slot = by_arm[arm]
        measured = slot["status"] == "measured"
        labels.append(f"{label} ({slot['displayed_event_count']})" if measured else f"{label} (N/A)")
        if arm == "standard_only":
            handles.append(Patch(facecolor="none", edgecolor=color, linewidth=1., linestyle="--"))
        else:
            handles.append(Patch(facecolor=color, edgecolor=edge, hatch=hatch,
                                 alpha=alpha, linewidth=.4))
        if not measured:
            continue
        bins = slot["bins"]
        heights = [b["count"] for b in bins]
        if arm == "standard_only":
            ax.stairs(heights, [b["lo"] for b in bins] + [bins[-1]["hi"]],
                      color=color, linewidth=1., linestyle="--", zorder=3)
        else:
            lw = by_arm["adaptive_lightweight"]
            bottom = ([b["count"] for b in lw["bins"]]
                      if arm == "adaptive_standard" and lw["status"] == "measured" else [0] * len(bins))
            ax.bar([b["lo"] for b in bins], heights, width=[b["hi"] - b["lo"] for b in bins],
                   align="edge", bottom=bottom, color=color, alpha=alpha, edgecolor=edge,
                   linewidth=.4, hatch=hatch, zorder=2)
            heights = [height + base for height, base in zip(heights, bottom)]
        plotted.append((arm, bins, heights))
    ax.set_xscale("log")
    ax.set_xlim(min(.05, min((bins[0]["lo"] for _, bins, _ in plotted), default=.05)),
                max(500., max((bins[-1]["hi"] for _, bins, _ in plotted), default=500.)))
    if all(math.isclose(actual, reference, rel_tol=1e-12) for actual, reference
           in zip(ax.get_xlim(), (.05, 500.))):
        ax.set_xticks([1., 100.])
    maximum = max((max(heights) for _, _, heights in plotted), default=1)
    # Keep the paper's upper-left legend clear of a new run's LW peak. In the
    # published distribution that peak is already below the legend; a small
    # fresh population may otherwise put its largest bin behind the text.
    legend_peak = max((height for _, bins, heights in plotted
                       for b, height in zip(bins, heights) if b["lo"] < 10), default=0)
    ax.set_ylim(0, max(1., maximum * 1.06, legend_peak * 2.8))
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(0., 1.07), fontsize=7.5,
              handlelength=1., handletextpad=.3, labelspacing=.15, borderpad=.2,
              frameon=False)


def figure6(plt, result):
    """Return one compact two-panel figure, including fixed missing-arm slots."""
    from matplotlib.lines import Line2D

    metadata = figure6_metadata(result)
    with plt.rc_context(PAPER_RC):
        fig, axes = plt.subplots(1, 2, figsize=(3.4, 1.55),
                                 gridspec_kw={"width_ratios": [1.3, 1.]})
        _draw_memory(axes[0], [slot for slot in metadata["slots"] if slot["panel"] == "a"])
        _draw_histogram(axes[1], [slot for slot in metadata["slots"] if slot["panel"] == "b"])
        for ax, panel in zip(axes, ("a", "b")):
            settings = metadata["panels"][panel]
            ax.set_xlabel(settings["xlabel"], fontsize=9, labelpad=1)
            ax.set_ylabel(settings["ylabel"], fontsize=9)
            _style_axis(ax)
            ax.text(.5, -.66, settings["title"], transform=ax.transAxes,
                    ha="center", va="top", fontsize=8, weight="bold")
        handles = [Line2D([], [], color=color, marker=marker, linestyle=linestyle,
                          markersize=3.2, linewidth=1.)
                   for _, _, color, marker, linestyle in MEMORY]
        fig.legend(handles, [label for _, label, *_ in MEMORY], loc="upper center",
                   bbox_to_anchor=(.5, .995), ncol=4, fontsize=7., frameon=False,
                   handlelength=1.4, handletextpad=.3, columnspacing=1.1)
        fig.subplots_adjust(left=.115, right=.985, top=.80, bottom=.42, wspace=.48)
    return fig
