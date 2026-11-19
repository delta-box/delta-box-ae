#!/usr/bin/env python3
"""Figure 1: updated E2B phase7, Replay and Cube balance cohorts.

Replay checkpoint is the first recorded pristine-repository setup copy per
trace (12 samples), preserving the paper's existing footnote definition.
Restore uses all 334 restore_ms records, including subprocess exit. Its
filesystem phase is rmtree + copytree; process is subprocess time outside
run_search; replay is the recorded run_search wall with zero injected delay.
Cube uses all 317/334 aligned events from cube-balance. Filesystem checkpoint
includes rootfs_dump; process/VM includes memory backing-file preparation
(memory_prepare) plus memory_dump, or shim_update_restore on restore.
Classification follows the state being saved, consistently with E2B memory export,
not whether the operation happens to use a host filesystem syscall. Guest readiness uses the measured first RPC.
All components join the same sandbox, snapshot, and request interval.
Evidence: build-info/cube-balance-unification-20261108/.
E2B uses 317/334 aligned phase6+phase7 events with exact OTEL intervals.
Nested VM close is removed from rootfs time; rootfs/UFFD overlap and
unassigned intervals stay other. Process/VM includes measured envd initialization
and the synchronous old-VM termination path. The separate first-RPC interval
is not split into inferred memory, filesystem, or waiting components.
The legacy JSON key guest_readiness now denotes only the measured first RPC.
Category revision: build-info/figure1-e2b-attribution-20261109/.
Evidence: build-info/e2b-phase7-unification-20261108/.
"""
from pathlib import Path
import json
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from paperstyle import style_axes, save

PLOT_DIR = Path(__file__).resolve().parent
OUT = PLOT_DIR.parent / "source" / "fig_e2b_cube_compare.pdf"

ALPHA = 0.75
CATS = ["filesystem", "process", "guest_readiness", "other", "replay"]
LABELS = {"filesystem": "filesystem", "process": "process",
          "guest_readiness": "first agent RPC", "other": "other synchronous work",
          "replay": "replay (no LLM)"}
COLORS = {"filesystem": "#87CEEB", "process": "#FFA500",
          "guest_readiness": "#66C2A5",   # muted teal-green (ColorBrewer Set2)
          "other": "#d3d3d3", "replay": "#C4B8EA"}
EDGES = {"filesystem": "#4a8db8", "process": "#cc7a00",
         "guest_readiness": "#3f8f74", "other": "#9a9a9a",
         "replay": "#9A89BE"}
HATCHES = {"filesystem": "xxxx", "process": "////",
           "guest_readiness": "\\\\", "other": "", "replay": "++"}
METHODS = ["E2B", "CubeSandbox", "copytree+replay"]
DISPLAY = {"E2B": "E2B", "CubeSandbox": "CubeSandbox", "copytree+replay": "copy+replay"}
XPOS = {"E2B": 0.0, "CubeSandbox": 0.95, "copytree+replay": 2.25}
PANEL_TITLE_Y = -0.30
LIMITED = set()


def draw(ax, data, flow, title, show_ylabel=True):
    ymax = max(data[m][flow]["total"] for m in METHODS)
    for m in METHODS:
        x = XPOS[m]
        d = data[m][flow]
        bottom = 0.0
        for cat in CATS:
            v = d.get(cat, 0.0)
            pct = 100 * v / d["total"] if d["total"] else 0
            ax.bar(x, v, bottom=bottom, width=0.62, color=COLORS[cat], alpha=ALPHA,
                   hatch=HATCHES[cat], edgecolor=EDGES[cat], linewidth=0.5, zorder=3)
            if pct >= 9 and v >= 0.06 * ymax:           # only the percentage
                ax.text(x, bottom + v / 2, f"{pct:.0f}%", ha="center", va="center",
                        fontsize=5.3, color="black", weight="bold", zorder=4)
            bottom += v
        ax.text(x, d["total"] + 0.02 * ymax, f"{d['total']:.1f}",
                ha="center", va="bottom", fontsize=5.6, weight="bold")
    ax.set_xticks([XPOS[m] for m in METHODS])
    labels = [(DISPLAY[m] + "*" if m in LIMITED else DISPLAY[m]) for m in METHODS]
    ax.set_xticklabels(labels, fontsize=5.1, rotation=0, ha="center")
    if show_ylabel:
        ax.set_ylabel("blocking (ms)", fontsize=6.2)
    ax.set_ylim(0, ymax * 1.22)
    ax.set_xlim(-0.6, 2.85)
    ax.tick_params(axis="y", labelsize=5.6)
    style_axes(ax)
    ax.set_axisbelow(True)
    ax.grid(axis="y", linestyle="--", alpha=0.4, linewidth=0.5, zorder=0)
    ax.set_title(title, fontsize=6.8, y=PANEL_TITLE_Y, weight="bold")


def draw_log(ax, data, flow, title, ymin=30.0, ymax=45000.0):
    """Stacked bars on a LOG y-axis (restore panel: copytree+replay exceeds the other baselines).
    NOTE: on log, segment heights are NOT proportional; the % labels are exact."""
    for m in METHODS:
        x = XPOS[m]
        d = data[m][flow]
        bottom = 0.0
        for cat in CATS:
            v = d.get(cat, 0.0)
            if v > 0:
                ax.bar(x, v, bottom=bottom, width=0.62, color=COLORS[cat],
                       alpha=ALPHA, hatch=HATCHES[cat], edgecolor=EDGES[cat],
                       linewidth=0.5, zorder=3)
                lo, hi = max(bottom, ymin), bottom + v
                pct = 100 * v / d["total"] if d["total"] else 0
                force = (m == "copytree+replay" and cat == "filesystem")
                if (pct >= 8 or force) and hi > lo and hi / lo >= 1.5:
                    ax.text(x, (lo * hi) ** 0.5, f"{pct:.0f}%", ha="center",
                            va="center", fontsize=5.4, color="black",
                            weight="bold", zorder=4)
            bottom += v
        ax.text(x, d["total"] * 1.06, f"{d['total']:.1f}", ha="center",
                va="bottom", fontsize=5.6, weight="bold")
    ax.set_yscale("log")
    ax.set_ylim(ymin, ymax)
    ax.set_yticks([100, 1000, 10000])
    ax.set_yticklabels(["100", "1k", "10k"])
    ax.set_xlim(-0.6, 2.85)
    ax.set_xticks([XPOS[m] for m in METHODS])
    ax.set_xticklabels([(DISPLAY[m] + "*" if m in LIMITED else DISPLAY[m])
                        for m in METHODS],
                       fontsize=5.1, rotation=0, ha="center")
    ax.tick_params(axis="y", labelsize=5.6)
    ax.set_axisbelow(True)
    ax.grid(axis="y", which="major", linestyle="--", alpha=0.4, linewidth=0.5, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_title(title, fontsize=6.8, y=PANEL_TITLE_Y, weight="bold")



def main():
    data = json.loads((PLOT_DIR / "figure1-data.json").read_text())
    for flows in data.values():
        for row in flows.values():
            assert abs(sum(row.get(c, 0.0) for c in CATS) - row["total"]) < 1e-6
    # single column, side by side: ck = linear (left), rs = LOG (right) so
    # copytree+replay (14.2s), E2B (0.4s), and Cube (0.6s) share one axis.
    fig, (ax_ck, ax_rs) = plt.subplots(1, 2, figsize=(3.4, 1.9))
    draw(ax_ck, data, "ck", "(a) Checkpoint", show_ylabel=True)
    draw_log(ax_rs, data, "rs", "(b) Restore")

    active = [c for c in CATS if any(row.get(c, 0.0) > 0
                                   for flows in data.values() for row in flows.values())]
    handles = [Patch(facecolor=COLORS[c], alpha=ALPHA, edgecolor=EDGES[c],
                     hatch=HATCHES[c], linewidth=0.6) for c in active]
    short_labels = {"filesystem": "filesystem", "process": "process / VM",
                    "guest_readiness": "guest-ready", "other": "other",
                    "replay": "replay"}
    leg_labels = [short_labels[c] for c in active]
    fig.legend(handles, leg_labels, loc="lower center", bbox_to_anchor=(0.5, 0.875),
               ncol=5, frameon=False, fontsize=6.2, handlelength=1.5,
               handleheight=1.35, columnspacing=0.7, handletextpad=0.32)
    fig.subplots_adjust(left=0.135, right=0.975, top=0.85, bottom=0.26, wspace=0.40)
    save(fig, str(OUT), also_png=False)

    for flow in ("ck", "rs"):
        for m in METHODS:
            d = data[m][flow]
            print(f"  {flow} {m:16s} total={d['total']:8.1f}  " +
                  " ".join(f"{c}={d[c]:.1f}" for c in CATS if c in d))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
