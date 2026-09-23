"""Paper Figure 1 layout, using only the supplied canonical phase metrics.

The format follows ``plot/plot_fig_e2b_cube_compare.py`` at the paper commit
recorded in ae/docs/paper-plotting-reference.json. Its historical data reader,
fixed E2B proportions and Replay constants are deliberately not imported.
"""
from __future__ import annotations

import math


METHODS = ("e2b", "cube", "replay")
DISPLAY = {"e2b": "E2B", "cube": "CubeSandbox", "replay": "copy+replay"}
XPOS = {"e2b": 0.0, "cube": 0.95, "replay": 2.25}
CATEGORIES = ("filesystem", "process", "guest_readiness", "control_plane", "replay", "unclassified_api", "overlapping_phases")
LEGEND_LABELS = ("filesystem", "memory", "guest-ready", "control-plane", "replay", "unclassified", "overlap")
COLORS = {"filesystem": "#87CEEB", "process": "#FFA500", "guest_readiness": "#66C2A5",
          "control_plane": "#d3d3d3", "replay": "#6A5ACD"}
EDGES = {"filesystem": "#4a8db8", "process": "#cc7a00", "guest_readiness": "#3f8f74",
         "control_plane": "#9a9a9a", "replay": "#3f3287"}
HATCHES = {"filesystem": "xxxx", "process": "////", "guest_readiness": "\\\\",
           "control_plane": "", "replay": "++"}
COLORS.update(unclassified_api="#e6b8b8", overlapping_phases="#b4a7d6")
EDGES.update(unclassified_api="#8b4e4e", overlapping_phases="#5e4a82")
HATCHES.update(unclassified_api="..", overlapping_phases="oo")
ALPHA = .75
POPULATION_FIELDS = (
    "cohort", "plot_group", "source_identity", "experiment", "mode", "checkpoint_profile",
    "adaptive", "memory_policy", "prewarm_requested", "prewarm_mode", "run_purpose",
    "message_policy", "baseline_test_runtime", "mock_latency_policy", "replay_timing_method",
    "legacy_timing_policy",
)

# These are presentation settings, never reference measurements. The restore
# limits match the paper; expand them only if actual measurements would clip.
PAPER_RC = {
    "pdf.fonttype": 42, "ps.fonttype": 42,
    # Concrete family names persist on Text artists after rc_context exits;
    # generic "serif" would resolve against the caller's rcParams at save time.
    "font.family": "STIXGeneral",
    "mathtext.fontset": "stix", "font.size": 9,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": False, "legend.frameon": False, "hatch.linewidth": .5,
}


def _slot(result, backend, operation):
    """Require a complete, same-population decomposition before drawing a bar."""
    rows = [r for r in result.get("metrics", [])
            if r.get("backend") == backend and r.get("operation") == operation]
    slot = dict(backend=backend, label=DISPLAY[backend], operation=operation,
                status="unavailable", displayed_total_ms=None, displayed_phases_ms={},
                available_metrics=[dict(row) for row in rows])
    if not rows:
        missing = [entry for entry in result.get("selection", {}).values()
                   if isinstance(entry, dict) and entry.get("backend") == backend
                   and entry.get("status") == "unavailable"]
        slot["reason"] = ("No measured canonical phase metrics in this population."
                          if not missing else "Canonical analysis reports unavailable phases.")
        if missing:
            slot["analysis_missing"] = missing
        return slot
    # Individual identity fields still matter if a hand-built canonical summary
    # omits its combined cohort/plot_group label.
    if len({tuple(r.get(field) for field in POPULATION_FIELDS) for r in rows}) > 1:
        slot["reason"] = "Multiple populations; no phase values pooled or overwritten."
        return slot
    if len({r["metric"] for r in rows}) != len(rows):
        slot["reason"] = "Duplicate phase metric; no value selected implicitly."
        return slot
    if any(r.get("evidence_kind") == "published_adjustment" for r in rows):
        slot["reason"] = "Historical plotting adjustments are not measured phases."
        return slot
    if any(r.get("unit") != "ms" or not isinstance(r.get("value"), (int, float))
           or isinstance(r.get("value"), bool) or not math.isfinite(r["value"])
           or r["value"] < 0 for r in rows):
        slot["reason"] = "Phase metrics must contain finite, nonnegative measured milliseconds."
        return slot
    lookup = {r["metric"]: r for r in rows}
    phases = {name: lookup[name]["value"] for name in CATEGORIES if name in lookup and (lookup[name]["value"] or name not in ("unclassified_api", "overlapping_phases"))}
    unmapped = [r["metric"] for r in rows if r["metric"] not in (*CATEGORIES, "total")
               and r["value"] != 0]
    if unmapped:
        slot["unmapped_metrics"] = unmapped
        slot["reason"] = (
            "E2B pause, snapshot_upload and resume API timers do not separate filesystem, "
            "memory or guest readiness."
            if backend == "e2b" and set(unmapped) & {"pause", "snapshot_upload", "resume"}
            else "Measured phases have no same-semantics paper legend category; "
                 "unclassified API time is not control-plane time."
        )
        return slot
    total = lookup.get("total", {}).get("value")
    if total is None or not phases:
        slot["reason"] = "A measured total and named phases are required; totals do not infer a split."
        return slot
    if len({r.get("n") for r in rows}) > 1 or len({r.get("statistic") for r in rows}) > 1:
        slot["reason"] = "Phase counts or statistics differ from the total population."
        return slot
    if not math.isclose(sum(phases.values()), total, rel_tol=1e-9, abs_tol=.003):
        slot["reason"] = "Measured paper categories do not close against the total; no residual is assigned."
        return slot
    slot.update(status="measured", reason=None, displayed_total_ms=total,
                displayed_phases_ms=phases)
    return slot


def figure1_metadata(result):
    """JSON-serializable plotting provenance and reasons for every empty slot."""
    return dict(
        format_source="plot/plot_fig_e2b_cube_compare.py",
        format_reference="ae/docs/paper-plotting-reference.json",
        figsize_inches=[3.4, 1.9],
        caption=("Figure 1. Per-event checkpoint/restore cost of existing approaches on SWE-Search (AE), decomposed into filesystem, "
                 "process/VM, and other phases (restore on a log axis; N/A: unavailable phases)."),
        system_order=list(METHODS),
        legend=[dict(metric=metric, label=label, color=COLORS[metric], hatch=HATCHES[metric])
                for metric, label in zip(CATEGORIES, LEGEND_LABELS)],
        slots=[_slot(result, backend, operation)
               for operation in ("checkpoint", "restore") for backend in METHODS],
        excluded_backends=sorted({r.get("backend") for r in result.get("metrics", [])
                                  if r.get("backend") and r["backend"] not in METHODS}),
        notes=[
            "Only canonical metrics from this supplied population are used; no raw or reference data is loaded.",
            "The paper legend's memory label denotes the canonical process/VM phase, as in the source plot.",
            "N/A is a nonnumeric placeholder; unavailable systems have no bar or inferred zero.",
            "Unclassified and overlapping measured time retain explicit extra legend categories; neither is relabelled control-plane.",
            "Bars require measured named phases that close against the same-population total.",
            "The restore axis is logarithmic; segment heights are not proportional to time.",
            "Replay checkpoint is the canonical pristine-copy proxy; retain the analysis timing and audit limitations.",
            "CRIU and FC-Diff are not systems in paper Figure 1 and are retained only in the analysis data.",
        ],
    )


def _style_axis(ax, title):
    ax.set_xlim(-.6, 2.85)
    ax.set_xticks([XPOS[backend] for backend in METHODS])
    ax.set_xticklabels([DISPLAY[backend] for backend in METHODS], fontsize=5.1)
    ax.tick_params(axis="y", labelsize=5.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(axis="y", which="major", linestyle="--", alpha=.4, linewidth=.5, zorder=0)
    ax.set_title(title, fontsize=6.8, y=-.30, weight="bold")


def _draw(ax, slots, *, logarithmic=False):
    measured = [slot for slot in slots if slot["status"] == "measured"]
    maximum = max((slot["displayed_total_ms"] for slot in measured), default=0.)
    if logarithmic:
        positive = [slot["displayed_total_ms"] for slot in measured if slot["displayed_total_ms"] > 0]
        ymin = min(30., min(positive) / 2) if positive else 30.
        ymax = max(45000., maximum * 1.22)
        ax.set_yscale("log")
        ax.set_ylim(ymin, ymax)
        exponents = range(math.ceil(math.log10(ymin)), math.floor(math.log10(ymax)) + 1)
        ticks = [10. ** exponent for exponent in exponents]
        ax.set_yticks(ticks)
        ax.set_yticklabels([f"{tick / 1000:g}k" if tick >= 1000 else f"{tick:g}" for tick in ticks])
    else:
        # Keep partial measurements on the paper's scale, so a small observed
        # bar does not become visually larger merely because other systems are
        # missing. Expand only when a real new measurement would be clipped.
        ax.set_ylim(0, max(3500., maximum * 1.22))
        if ax.get_ylim()[1] == 3500.:
            ax.set_yticks(list(range(0, 3500, 500)))
        ax.set_ylabel("blocking (ms)", fontsize=6.2)
    for slot in slots:
        x = XPOS[slot["backend"]]
        if slot["status"] != "measured":
            ax.text(x, .055, "N/A", transform=ax.get_xaxis_transform(),
                    ha="center", va="bottom", fontsize=5.6, color="#666666")
            continue
        total, bottom = slot["displayed_total_ms"], 0.
        for category in CATEGORIES:
            value = slot["displayed_phases_ms"].get(category, 0.)
            if value <= 0:
                continue
            ax.bar(x, value, bottom=bottom, width=.62, color=COLORS[category], alpha=ALPHA,
                   hatch=HATCHES[category], edgecolor=EDGES[category], linewidth=.5, zorder=3)
            pct = 100 * value / total if total else 0.
            if logarithmic:
                lo, hi = max(bottom, ymin), bottom + value
                force = slot["backend"] == "replay" and category == "filesystem"
                if (pct >= 8 or force) and hi > lo and hi / lo >= 1.5:
                    ax.text(x, (lo * hi) ** .5, f"{pct:.0f}%", ha="center", va="center",
                            fontsize=5.4, weight="bold", zorder=4)
            elif pct >= 9 and value >= .06 * ax.get_ylim()[1]:
                ax.text(x, bottom + value / 2, f"{pct:.0f}%", ha="center", va="center",
                        fontsize=5.3, weight="bold", zorder=4)
            bottom += value
        if logarithmic and total == 0:
            ax.text(x, .055, "0", transform=ax.get_xaxis_transform(),
                    ha="center", va="bottom", fontsize=5.6, weight="bold")
        else:
            ax.text(x, total * 1.06 if logarithmic else total + .02 * maximum,
                    f"{total:.0f}", ha="center", va="bottom", fontsize=5.6, weight="bold")


def figure1(plt, result):
    """Return the compact paper figure; put diagnostics in ``figure1_metadata``."""
    from matplotlib.patches import Patch

    metadata = figure1_metadata(result)
    with plt.rc_context(PAPER_RC):
        fig, axes = plt.subplots(1, 2, figsize=(3.4, 1.9))
        for ax, operation, title in zip(axes, ("checkpoint", "restore"),
                                        ("(a) Checkpoint", "(b) Restore")):
            slots = [slot for slot in metadata["slots"] if slot["operation"] == operation]
            _draw(ax, slots, logarithmic=operation == "restore")
            _style_axis(ax, title)
        shown = [c for c in CATEGORIES if c not in ("unclassified_api", "overlapping_phases") or
                 any(slot["displayed_phases_ms"].get(c, 0) > 0 for slot in metadata["slots"])]
        handles = [Patch(facecolor=COLORS[category], alpha=ALPHA, edgecolor=EDGES[category],
                         hatch=HATCHES[category], linewidth=.6) for category in shown]
        labels = [LEGEND_LABELS[CATEGORIES.index(c)] for c in shown]
        fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(.5, .875),
                   ncol=5 if len(shown) == 5 else 4, frameon=False, fontsize=5.5 if len(shown) > 5 else 6.2, handlelength=1.5,
                   handleheight=1.35, columnspacing=.7, handletextpad=.32)
        fig.subplots_adjust(left=.135, right=.975, top=.85, bottom=.26, wspace=.40)
    return fig
