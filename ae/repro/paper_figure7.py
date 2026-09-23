"""Paper Figure 7 layout using only the supplied current-run component model.

The visual format follows ``plot/plot_fig_end2end_2sys.py`` at the paper
revision recorded in ae/docs/paper-plotting-reference.json. No reference
measurements or historical data readers are imported.
"""
from __future__ import annotations

import json
import math


GROUPS = ("Django", "SymPy", "Scientific", "Tools/Small")
SYSTEMS = ("deltabox", "e2b")
LABELS = {"deltabox": "δSandbox", "e2b": "self-hosted e2b-infra (diff)"}
COLORS = {"deltabox": "#FFA500", "e2b": "#87CEEB"}
EDGES = {"deltabox": "#cc7a00", "e2b": "#4a8db8"}
HATCHES = {"deltabox": "////", "e2b": "xxxx"}
POPULATION_FIELDS = (
    "cohort", "plot_group", "source_identity", "source_summary", "input_summary",
    "experiment", "mode", "checkpoint_profile", "adaptive", "memory_policy",
    "prewarm_requested", "prewarm_mode", "run_purpose", "message_policy",
    "baseline_test_runtime", "mock_latency_policy", "replay_timing_method",
    "legacy_timing_policy",
)
FIELDS = {"ratio": "ratio", "floor_s": "s", "wall_s": "s"}
PAPER_RC = {
    "pdf.fonttype": 42, "ps.fonttype": 42,
    # A concrete family remains STIX when the caller saves after rc_context.
    "font.family": "STIXGeneral", "mathtext.fontset": "stix", "font.size": 9,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": False, "legend.frameon": False, "hatch.linewidth": .5,
}


def _slot(result, group, backend, x):
    indexed = [(index, row) for index, row in enumerate(result.get("metrics", []))
               if row.get("group") == group and row.get("backend") == backend
               and row.get("metric") in FIELDS]
    slot = dict(group=group, backend=backend, label=LABELS[backend], x=x,
                status="unavailable", displayed_ratio=None, modeled=None,
                available_metrics=[dict(metric_index=index, **row) for index, row in indexed])
    rows = [row for _, row in indexed]
    if not rows:
        slot["reason"] = "No matching current-run ratio is supplied."
        return slot
    identities = {json.dumps({field: row.get(field) for field in POPULATION_FIELDS},
                             sort_keys=True) for row in rows}
    if len(identities) != 1:
        slot["reason"] = "Multiple populations target this slot; no value selected or averaged."
        return slot
    if len({row["metric"] for row in rows}) != len(rows):
        slot["reason"] = "Duplicate aggregate metric; no value selected or averaged."
        return slot
    # A ratio of group sums has a different statistic name from its summed
    # durations, while still referring to exactly the same input population.
    compatible_statistics = (len({row.get("statistic") for row in rows}) <= 1 or
                             all(row.get("statistic") == (
                                 "ratio-of-sums" if row["metric"] == "ratio" else "sum")
                                 for row in rows))
    if len({row.get("n") for row in rows}) > 1 or not compatible_statistics:
        slot["reason"] = "The supplied component metrics have different counts or statistics."
        return slot
    if any(not isinstance(row.get("value"), (int, float)) or isinstance(row.get("value"), bool)
           or not math.isfinite(row["value"]) or row["value"] < 0
           or row.get("unit", FIELDS[row["metric"]]) != FIELDS[row["metric"]] for row in rows):
        slot["reason"] = "The supplied component metrics must have finite nonnegative values and matching units."
        return slot
    ratio = next((row for row in rows if row["metric"] == "ratio"), None)
    if ratio is None:
        slot["reason"] = "No supplied ratio; durations are retained without reconstructing a ratio."
        return slot
    modeled = ratio.get("modeled") is True or ratio.get("evidence_kind") == "derived_model"
    slot.update(status="derived_model" if modeled else "supplied", reason=None,
                displayed_ratio=ratio["value"], modeled=modeled,
                n=ratio.get("n"), statistic=ratio.get("statistic"),
                evidence_kind=ratio.get("evidence_kind"))
    return slot


def figure7_metadata(result):
    """Keep all eight paper slots and each supplied population's provenance."""
    return dict(
        format_source="plot/plot_fig_end2end_2sys.py",
        format_reference="ae/docs/paper-plotting-reference.json",
        figsize_inches=[3.4, 1.62],
        savefig={"bbox_inches": "tight", "pad_inches": .035},
        caption=("Figure 7. Time normalized to LLM+action latency (1.0×), "
                 "grouped by workload and system. N/A: no matching result."),
        caption_bold_prefix="Figure 7.",
        group_order=list(GROUPS), system_order=list(SYSTEMS),
        slots=[_slot(result, group, backend, group_index + (system_index - .5) * .36)
               for group_index, group in enumerate(GROUPS)
               for system_index, backend in enumerate(SYSTEMS)],
        excluded_metrics=[dict(metric_index=index, reason="No corresponding paper group, system or ratio component.")
                          for index, row in enumerate(result.get("metrics", []))
                          if row.get("group") not in GROUPS or row.get("backend") not in SYSTEMS
                          or row.get("metric") not in FIELDS],
        notes=[
            "Saved ratios are used verbatim; saved component durations do not replace or recalculate them.",
            "The plotted systems use serialized component-sum models; this is not a measured end-to-end execution or asynchronous overlap.",
            "The 1.0× boundary splits the normalized bar for display and does not add a measured component.",
            "Missing slots have no numeric bar, inferred zero or archived replacement.",
            "Source identities, counts and statistics stay with each slot; conflicting populations are never pooled.",
        ],
    )


def figure7(plt, result):
    """Return the compact two-system paper figure, keeping diagnostics outside."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.ticker import MaxNLocator

    metadata = figure7_metadata(result)
    maximum = max((slot["displayed_ratio"] for slot in metadata["slots"]
                   if slot["displayed_ratio"] is not None), default=0.)
    with plt.rc_context(PAPER_RC):
        fig, ax = plt.subplots(figsize=(3.4, 1.62))
        ax.set_xlim(-.55, 3.55)
        ax.set_ylim(0, max(2.38, maximum + .45, maximum * 1.15))
        if ax.get_ylim()[1] == 2.38:
            ax.set_yticks((0, 1, 2))
        else:
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3))
        for slot in metadata["slots"]:
            x, backend, ratio = slot["x"], slot["backend"], slot["displayed_ratio"]
            if ratio is None:
                ax.text(x, .055, "N/A", transform=ax.get_xaxis_transform(),
                        ha="center", va="bottom", fontsize=6.5, color="#666666")
                continue
            floor, overhead = min(1., ratio), max(0., ratio - 1.)
            if floor > 0:
                ax.bar(x, floor, .36, color=COLORS[backend], alpha=.22,
                       edgecolor=EDGES[backend], linewidth=.45, hatch=HATCHES[backend])
            if overhead > 0:
                ax.bar(x, overhead, .36, bottom=floor, color=COLORS[backend], alpha=.50,
                       edgecolor=EDGES[backend], linewidth=.7, hatch=HATCHES[backend])
            ax.text(x, ratio + .06, f"{ratio:.2f}×", ha="center", va="bottom",
                    fontsize=7, color=EDGES[backend], fontweight="bold")
        handles = [Patch(facecolor=COLORS[backend], edgecolor=EDGES[backend], alpha=.50,
                         hatch=HATCHES[backend], label=LABELS[backend]) for backend in SYSTEMS]
        handles.append(Line2D([0], [0], color="#1b5e20", linestyle="--", linewidth=.8,
                              alpha=.7, label="1.0× = LLM+action line"))
        ax.legend(handles=handles, fontsize=7.5, loc="upper center", bbox_to_anchor=(.5, 1.16),
                  ncol=2, handlelength=1.2, handletextpad=.4, borderpad=.2,
                  columnspacing=.9, labelspacing=.3, frameon=False)
        ax.set_xticks(range(len(GROUPS)), GROUPS, fontsize=9)
        ax.tick_params(axis="y", labelsize=9)
        ax.set_ylabel("time / LLM+action", fontsize=9)
        ax.axhline(1., color="#1b5e20", linestyle="--", linewidth=.6, alpha=.5, zorder=0)
        ax.set_axisbelow(True)
        ax.grid(axis="y", linestyle="--", alpha=.6, linewidth=.5, zorder=0)
        fig.tight_layout(pad=.3)
    return fig
