"""Paper Figure 2 geometry, populated only by the supplied fresh analysis.

The reference has a short, dual-x-axis horizontal bar panel and a taller
dual-y-axis step distribution. Historical readers, constants and sample
filters from the paper scripts are not used here.
"""
from __future__ import annotations

import json
import math


DOMAINS = ("filesystem", "memory")
POPULATION_FIELDS = (
    "backend", "experiment", "mode", "checkpoint_profile", "adaptive",
    "memory_policy", "prewarm_requested", "prewarm_mode", "run_purpose",
    "message_policy", "baseline_test_runtime", "mock_latency_policy",
    "replay_timing_method", "legacy_timing_policy", "source_identity",
    "cohort", "plot_group", "source_summary", "input_summary",
)
BYTES_PER_UNIT = {"bytes": 1, "B": 1, "KiB": 1024, "MiB": 1 << 20,
                  "KB": 1000, "MB": 1_000_000}
BLUE, BLUE_EDGE = "#87CEEB", "#4a8db8"
ORANGE, ORANGE_EDGE, LINE = "#FFA500", "#cc7a00", "#ff8c00"
TICK = AXIS = LEGEND = 6.8
PAPER_RC = {
    "pdf.fonttype": 42, "ps.fonttype": 42,
    # Persist the concrete face when the caller saves outside rc_context.
    "font.family": "STIXGeneral", "mathtext.fontset": "stix", "font.size": 9,
    "text.color": "black", "axes.labelcolor": "black",
    "figure.facecolor": "white", "axes.facecolor": "white",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": False, "legend.frameon": False, "hatch.linewidth": .5,
}


def _finite(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _safe(value):
    """Keep diagnostics valid JSON even when a rejected input contains NaN."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _population(row):
    return {field: row.get(field) for field in POPULATION_FIELDS if row.get(field) is not None}


def _reason(row, field):
    if row.get("evidence_kind") not in (None, "fresh_raw_events"):
        return row.get("reason") or "This row is not a measured fresh-analysis value."
    if row.get("unit") not in BYTES_PER_UNIT:
        return "A known byte unit is required; historical unit labels are not reinterpreted."
    if not _finite(row.get(field)):
        return row.get("reason") or "A finite, nonnegative measured value is required."
    if not _finite(row.get("n"), positive=True):
        return "The measurement must have a positive contributor count."
    if row.get("statistic", "mean") != "mean":
        return "This layout requires the supplied mean, not another statistic."
    return None


def _slot(indexed, *, domain, metric, display_unit, conflict=None, series=False):
    slot = dict(domain=domain, metric=metric, status="unavailable", reason=None,
                displayed_unit=display_unit, sources=[dict(index=i, row=_safe(row)) for i, row in indexed])
    slot.update(dict(points=[]) if series else dict(displayed_value=None, value_bytes=None))
    if conflict:
        slot["reason"] = conflict
        return slot
    if not indexed:
        slot["reason"] = "No measured step series is available." if series else "No measured matching mean is available."
        return slot
    if not series and len(indexed) != 1:
        slot["reason"] = "Duplicate aggregate means; no value is selected or averaged implicitly."
        return slot
    for _, row in indexed:
        reason = _reason(row, "y" if series else "value")
        if reason:
            slot["reason"] = reason
            return slot
        if series and (not _finite(row.get("x"), positive=True)
                       or int(row["x"]) != row["x"] or row.get("x_unit") != "step"):
            slot["reason"] = "Step positions must be positive integers with x_unit=step."
            return slot
    if series and len({row["x"] for _, row in indexed}) != len(indexed):
        slot["reason"] = "Duplicate step positions; no per-step values are pooled implicitly."
        return slot
    target = BYTES_PER_UNIT[display_unit]
    if series:
        slot["points"] = [dict(x=row["x"], y=row["y"] * BYTES_PER_UNIT[row["unit"]] / target,
                               n=row["n"], source_index=index, source_unit=row["unit"],
                               source_value=row["y"], conversion_factor=BYTES_PER_UNIT[row["unit"]] / target)
                          for index, row in sorted(indexed, key=lambda item: item[1]["x"])]
    else:
        index, row = indexed[0]
        factor = BYTES_PER_UNIT[row["unit"]] / target
        slot.update(displayed_value=row["value"] * factor,
                    value_bytes=row["value"] * BYTES_PER_UNIT[row["unit"]], n=row["n"],
                    source_unit=row["unit"], source_value=row["value"], conversion_factor=factor)
    slot["status"] = "measured"
    return slot


def _domain(result, domain):
    metrics = [(i, row) for i, row in enumerate(result.get("metrics", []))
               if row.get("domain") == domain and row.get("panel", "a") == "a"
               and row.get("metric") in ("step_delta", "total")]
    series = [(i, row) for i, row in enumerate(result.get("series", []))
              if row.get("domain") == domain and row.get("panel", "b") == "b"
              and row.get("metric") == "step_delta"]
    identities = sorted({json.dumps(_population(row), sort_keys=True) for _, row in metrics + series})
    conflict = ("Multiple populations target this domain; select one explicitly. "
                "No means, totals or step series are combined across populations."
                if len(identities) > 1 else None)
    bars = {name: _slot([(i, row) for i, row in metrics if row["metric"] == name],
                        domain=domain, metric=name,
                        display_unit="bytes" if domain == "filesystem" else "MB", conflict=conflict)
            for name in ("step_delta", "total")}
    line = _slot(series, domain=domain, metric="step_delta",
                 display_unit="KB" if domain == "filesystem" else "MB", conflict=conflict, series=True)
    ratio, ratio_reason = None, "Both means from the same population are required."
    delta, total = bars["step_delta"], bars["total"]
    if delta["status"] == total["status"] == "measured":
        if delta["value_bytes"] > 0:
            ratio, ratio_reason = total["value_bytes"] / delta["value_bytes"], None
        else:
            ratio_reason = "A zero per-step delta has no finite total-to-delta ratio."
    statuses = [slot["status"] for slot in (*bars.values(), line)]
    return dict(status="measured" if all(s == "measured" for s in statuses) else
                       "partial" if "measured" in statuses else "unavailable",
                populations=[json.loads(identity) for identity in identities],
                population_conflict=bool(conflict), metrics=bars, series=line,
                total_to_delta_ratio=ratio, ratio_reason=ratio_reason,
                analysis_panel=_safe(result.get("panels", {}).get(domain)))


def _limits(domains):
    fs_values = [slot["value_bytes"] for slot in domains["filesystem"]["metrics"].values()
                 if slot["status"] == "measured" and slot["value_bytes"] > 0]
    floor = 100.
    if fs_values and min(fs_values) <= floor:
        floor = 10. ** math.floor(math.log10(min(fs_values) / 2))
    mem_values = [slot["displayed_value"] for slot in domains["memory"]["metrics"].values()
                  if slot["status"] == "measured"]
    fs_points, mem_points = (domains[domain]["series"]["points"] for domain in DOMAINS)
    # Reference bounds are presentation settings, not reference measurements.
    # Retain the reference scale for small/partial runs, and expand for data.
    return dict(filesystem_bytes=[floor, max(6e8, max(fs_values, default=0.) * 18)],
                memory_MB=[0., max(850., max(mem_values, default=0.) * 1.5)],
                step=[0., max(30.5, max((p["x"] for p in fs_points + mem_points), default=0) + .5)],
                file_KB=[0., max(.475, max((p["y"] for p in fs_points), default=0.) * 1.2)],
                dirty_MB=[0., max(170., max((p["y"] for p in mem_points), default=0.) * 1.2)])


def figure2_metadata(result):
    """Return exact display conversions and diagnostics outside the figure."""
    domains = {domain: _domain(result, domain) for domain in DOMAINS}
    prefix = "Figure 2. Per-step changes and total sandbox state in this run."
    return dict(
        layout="paper-figure-02", format_source="plot/plot_fig_motiv_combo_v2.py",
        format_reference="ae/docs/paper-plotting-reference.json", figsize_inches=[3.4, 1.4],
        savefig={"bbox_inches": "tight", "pad_inches": .035},
        caption_bold_prefix=prefix,
        caption=(prefix + " (a) Mean per-step change vs. total state for the filesystem and process memory. "
                 "(b) Per-step filesystem edit size and worker dirty pages. KB and MB are decimal; "
                 "N/A denotes unavailable measurements."),
        domains=domains, axis_limits=_limits(domains),
        unit_conventions=dict(bytes_per_KB=1000, bytes_per_MB=1_000_000,
                              bytes_per_KiB=1024, bytes_per_MiB=1 << 20),
        notes=[
            "Only the supplied fresh canonical analysis is read; no archived values or paper annotations are loaded.",
            "Filesystem and memory retain independent populations on their separate axes; their instance sets may differ.",
            "Each metric and point records its original unit, conversion factor and actual contributor count.",
            "The filesystem mean delta selects writing steps; the filesystem step series includes measured zero writes.",
            "Memory total is process-tree RSS; the per-step delta is worker soft-dirty memory, not VM allocation.",
            "The supplied mean and total may have different contributor counts; no statistic is recomputed from plotted points.",
            "Every supplied step is retained, including single-contributor steps; no smoothing or 30-step truncation is applied.",
            "Gaps in the memory series remain gaps; neither missing steps nor missing measurements are filled with zero.",
            "A filesystem bar ends at its measured byte value on the log axis; the positive display origin is not added to the value.",
            "Zero-byte filesystem values have a zero label and no logarithmic bar; N/A is never a numeric zero.",
        ],
    )


def _byte_label(value):
    if value < 1000:
        return f"{value:.0f} B" if value >= 1 or value == 0 else f"{value:.2g} B"
    if value < 1e6:
        return f"{value / 1000:.1f} KB"
    if value < 1e9:
        return f"~{value / 1e6:.0f} MB"
    return f"~{value / 1e9:.1f} GB"


def _ratio_label(value):
    if value >= 1000:
        exponent = int(math.floor(math.log10(value)))
        mantissa = value / 10 ** exponent
        return rf"$\sim${mantissa:.1f}$\cdot$10$^{{{exponent}}}\times$"
    return f"~{value:.1f}" + r"$\times$"


def _linear_ticks(ax, limit, default, *, axis="x"):
    from matplotlib.ticker import MaxNLocator
    ticks = default if limit <= default[-1] * 1.5 else [
        value for value in MaxNLocator(nbins=4).tick_values(0, limit) if 0 <= value <= limit]
    (ax.set_xticks if axis == "x" else ax.set_yticks)(ticks)


def _draw_bar(ax, slot, y, *, logarithmic=False, limit):
    if slot["status"] != "measured":
        ax.text(.04, y, "N/A", transform=ax.get_yaxis_transform(),
                ha="left", va="center", fontsize=LEGEND, color="#666666")
        return
    value = slot["displayed_value"]
    delta = slot["metric"] == "step_delta"
    color, edge, hatch = (ORANGE, ORANGE_EDGE, "////") if delta else (BLUE, BLUE_EDGE, "xxxx")
    floor = ax.get_xlim()[0] if logarithmic else 0.
    if not logarithmic or value > 0:
        ax.barh(y, value - floor, .45, left=floor, color=color, alpha=.6,
                edgecolor=edge, linewidth=.6, hatch=hatch, zorder=3)
    label = _byte_label(value) if logarithmic else (f"{value:.1f} MB" if delta else f"~{value:.0f} MB")
    if logarithmic and value == 0:
        ax.text(.02, y, label, transform=ax.get_yaxis_transform(), va="center", ha="left", fontsize=LEGEND)
    else:
        x = value * 1.8 if logarithmic else value + limit * .013
        ax.text(x, y, label, va="center", ha="left", fontsize=LEGEND)


def _panel_a(fig, grid, metadata):
    from matplotlib.patches import Patch

    domains, limits = metadata["domains"], metadata["axis_limits"]
    memory = fig.add_subplot(grid, label="figure2-a-memory")
    memory.set_xlim(limits["memory_MB"])
    memory.set_ylim(.25, 3.1)
    memory.set_yticks([2.475, .825], ["FS", "Mem"], fontsize=TICK)
    memory.tick_params(axis="x", labelsize=TICK - .5)
    _linear_ticks(memory, limits["memory_MB"][1], [0, 200, 400, 600])
    memory.set_xlabel("Memory (MB, linear)", fontsize=AXIS)
    memory.set_axisbelow(True)
    memory.grid(axis="x", linestyle="--", alpha=.3, linewidth=.5, zorder=0)
    filesystem = memory.twiny()
    filesystem.set_label("figure2-a-filesystem")
    filesystem.set_xscale("log")
    filesystem.set_xlim(limits["filesystem_bytes"])
    lo, hi = limits["filesystem_bytes"]
    ticks = [10. ** exponent for exponent in range(
        2 * math.floor(math.log10(lo) / 2), 2 * math.ceil(math.log10(hi) / 2) + 1, 2)
             if lo <= 10. ** exponent <= hi]
    filesystem.set_xticks(ticks)
    filesystem.set_xticklabels([_byte_label(tick).replace("~", "").replace(".0", "").replace(" ", "")
                                for tick in ticks], fontsize=TICK - .5)
    filesystem.tick_params(axis="x", which="minor", top=False)
    filesystem.set_xlabel("Filesystem (log)", fontsize=AXIS, labelpad=5.)
    filesystem.spines["top"].set_visible(True)
    filesystem.spines["top"].set_color("black")
    for domain, ax, ys, log, upper in (
        ("memory", memory, (1.05, .60), False, limits["memory_MB"][1]),
        ("filesystem", filesystem, (2.70, 2.25), True, hi),
    ):
        for metric, y in zip(("step_delta", "total"), ys):
            _draw_bar(ax, domains[domain]["metrics"][metric], y, logarithmic=log, limit=upper)
        ratio = domains[domain]["total_to_delta_ratio"]
        if ratio is not None:
            ax.text(.98, ys[0], _ratio_label(ratio), transform=ax.get_yaxis_transform(),
                    ha="right", va="center", fontsize=TICK, weight="bold")
    scale = .62
    position = memory.get_position()
    position = [position.x0, position.y0, position.width, position.height * scale]
    memory.set_position(position)
    filesystem.set_position(position)
    handles = [Patch(facecolor=color, edgecolor=edge, hatch=hatch, alpha=.6, linewidth=.6)
               for color, edge, hatch in ((ORANGE, ORANGE_EDGE, "////"), (BLUE, BLUE_EDGE, "xxxx"))]
    memory.legend(handles, [r"Per-step $\Delta$", "Total"], loc="lower center",
                  bbox_to_anchor=(.5, 1.01 / scale), ncol=2, fontsize=LEGEND,
                  handlelength=1., columnspacing=1., labelspacing=.2, borderpad=.1, handletextpad=.4)
    memory.set_title(r"(a) Per-step $\Delta$ vs. total", fontsize=AXIS + 1,
                     y=-.54 / scale, weight="bold")


def _panel_b(fig, grid, metadata):
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    domains, limits = metadata["domains"], metadata["axis_limits"]
    filesystem = fig.add_subplot(grid, label="figure2-b-filesystem")
    memory = filesystem.twinx()
    memory.set_label("figure2-b-memory")
    points = domains["filesystem"]["series"]["points"]
    if points:
        filesystem.bar([p["x"] for p in points], [p["y"] for p in points], width=.8,
                       color=BLUE, alpha=.75, edgecolor=BLUE_EDGE, linewidth=.3, zorder=3)
    else:
        filesystem.text(.04, .93, "File edit: N/A", transform=filesystem.transAxes,
                        fontsize=LEGEND, color=BLUE_EDGE, ha="left", va="top")
    points = domains["memory"]["series"]["points"]
    if points:
        xs, ys, previous = [], [], None
        for point in points:
            if previous is not None and point["x"] > previous + 1:
                xs.append(float("nan"))
                ys.append(float("nan"))
            xs.append(point["x"])
            ys.append(point["y"])
            previous = point["x"]
        memory.plot(xs, ys, color=LINE, marker="o", ms=2.4, lw=1.1, zorder=4)
    else:
        filesystem.text(.04, .80, "Worker dirty: N/A", transform=filesystem.transAxes,
                        fontsize=LEGEND, color=ORANGE_EDGE, ha="left", va="top")
    filesystem.set_xlim(limits["step"])
    _linear_ticks(filesystem, limits["step"][1], [0, 10, 20, 30])
    filesystem.set_ylim(limits["file_KB"])
    _linear_ticks(filesystem, limits["file_KB"][1], [0, .1, .2, .3, .4], axis="y")
    memory.set_ylim(limits["dirty_MB"])
    _linear_ticks(memory, limits["dirty_MB"][1], [0, 50, 100, 150], axis="y")
    filesystem.set_xlabel("MCTS step", fontsize=AXIS)
    filesystem.set_ylabel(r"File $\Delta$ (KB)", fontsize=AXIS)
    memory.set_ylabel("Worker dirty (MB)", fontsize=AXIS)
    filesystem.tick_params(axis="both", labelsize=TICK)
    memory.tick_params(axis="y", labelsize=TICK)
    memory.spines["right"].set_visible(True)
    memory.spines["right"].set_color("black")
    memory.spines["right"].set_linewidth(.8)
    filesystem.set_axisbelow(True)
    filesystem.grid(axis="y", linestyle="--", alpha=.4, linewidth=.5, zorder=0)
    handles = [Patch(facecolor=BLUE, edgecolor=BLUE_EDGE, alpha=.75, linewidth=.3),
               Line2D([], [], color=LINE, marker="o", markersize=2.4, linewidth=1.1)]
    filesystem.legend(handles, [r"File edit $\Delta$", "Worker dirty pages"], loc="lower left",
                      bbox_to_anchor=(-.16, 1.01), ncol=2, fontsize=LEGEND,
                      handlelength=1., columnspacing=.8, labelspacing=.2, borderpad=.1, handletextpad=.4)
    filesystem.set_title("(b) Per-step distribution", fontsize=AXIS + 1, y=-.55, weight="bold")


def figure2(plt, result):
    """Return a paper-shaped figure; callers append ``metadata['caption']``."""
    metadata = figure2_metadata(result)
    with plt.rc_context(PAPER_RC):
        fig = plt.figure(figsize=(3.4, 1.4))
        grid = fig.add_gridspec(1, 2, wspace=.62, left=.14, right=.88, top=.84, bottom=.16)
        _panel_a(fig, grid[0, 0], metadata)
        _panel_b(fig, grid[0, 1], metadata)
    return fig
