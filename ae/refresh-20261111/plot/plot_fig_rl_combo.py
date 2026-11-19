#!/usr/bin/env python3
"""Figure 8 with the user-approved client-reuse / 2 MiB page-read inputs.

All-child state verification remains included. Per-point samples and statistics
are recorded in figure8-provenance.json; the plotted series is not uniformly
four-run medians. DeltaBox and GPU inputs are unchanged. Panel (c) uses the
original occupation equation.
"""
from pathlib import Path
import json
import numpy as np
import matplotlib.pyplot as plt
from paperstyle import FS, style_axes, save, panel_label

PLOT_DIR = Path(__file__).resolve().parent
DATA_FILE = PLOT_DIR / "figure8-data.json"
OUT_PDF = PLOT_DIR.parent / "source" / "fig_rl_combo.pdf"

def panel_fanout(ax, data):
    # Three sandboxes, native fork/clone path, verified readiness vs fork count.
    forks, series, estimates = data
    style = {
        "DeltaBox":    ("#006400", "d"),
        "CubeSandbox": ("#6CB4F0", "o"),
        "E2B":         ("#FF8C00", "s"),
    }
    handles, labels = [], []
    for name in ["DeltaBox", "CubeSandbox", "E2B"]:
        ys = series[name]
        color, marker = style[name]
        xs = [f for f, y in zip(forks, ys) if y is not None]
        yy = [y for y in ys if y is not None]
        h, = ax.plot(xs, yy, color=color, marker=marker, linewidth=1.0,
                     markersize=4)
        handles.append(h); labels.append(name)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_ylim(20, 20000)
    ax.set_xticks(forks)
    ax.set_xticklabels([str(f) for f in forks])
    ax.set_xlabel("$N$ (forks)", fontsize=FS.CROSS_AXIS)
    ax.set_ylabel("Fan-out latency (ms)", fontsize=FS.CROSS_AXIS)
    ax.tick_params(labelsize=FS.CROSS_TICK)
    style_axes(ax)
    return handles, labels


def panel_tgpu(ax, Bs, tgen, _Bs_tr, ttrain):
    # Only the two curves that feed panel (c) are kept:
    #   Qwen2.5-7B T_gen = dark orange
    #   T_train 7B-LoRA  = deep green dashed
    palette = {
        "Qwen2.5-7B-Instruct": ("#FF8C00", "s", "Qwen2.5-7B"),
    }
    for label, ys in tgen.items():
        if label not in palette:
            continue
        c, m, disp = palette[label]
        ax.plot(Bs, ys, color=c, marker=m, label=f"$T_{{gen}}$ {disp}",
                linewidth=1.0, markersize=4)
    ax.plot(Bs, ttrain, color="#6CB4F0", marker="d",
            label="$T_{train}$ Qwen2.5-7B",
            linewidth=1.2, markersize=4, linestyle="--")
    ax.set_xscale("log", base=2)
    ax.set_xticks(Bs)
    ax.set_xticklabels([str(b) for b in Bs])
    ax.set_xlabel("$B$ (batch / concurrent rollouts)", fontsize=FS.CROSS_AXIS)
    ax.set_ylabel("Time per step (s)", fontsize=FS.CROSS_AXIS)
    ax.tick_params(labelsize=FS.CROSS_TICK)
    # With only the two 7B curves left, scale the ylim to the data plus a small
    # headroom band for the 2-entry legend (the old fixed 0..11 was sized for the
    # dropped Qwen3-14B T_gen peak and would now leave the panel mostly empty).
    _peak = max(max(tgen["Qwen2.5-7B-Instruct"]), max(ttrain))
    ax.set_ylim(0, _peak * 1.45)
    style_axes(ax)
    ax.legend(fontsize=FS.CROSS_LEGEND, loc="upper left",
              bbox_to_anchor=(0.0, 1.02),
              handlelength=1.4, handletextpad=0.4,
              labelspacing=0.25, borderpad=0.2)


def panel_util(ax, by):
    # Horizontal bar layout: backend names sit on y-axis (horizontal text,
    # no rotation) and the bars extend rightward. Each backend gets its
    # own copier-aligned colour (same colour family as the lines in
    # panel (a)). N=16 = solid fill, N=64 = same colour but lighter +
    # diagonal hatch — readable as "same backend, different N".
    # JSON keys use "FC" (not "Firecracker") — map to display labels.
    order = ["DeltaBox", "CubeSandbox", "E2B"]
    display = {"DeltaBox": "DeltaBox", "CubeSandbox": "CubeSandbox", "E2B": "E2B"}
    n16 = [by[k][16] for k in order]
    n64 = [by[k][64] for k in order]
    y = np.arange(len(order))
    h = 0.38  # bar height (in y-data units)

    # Copier Figs 11/12 bar palette: #FFA500 orange (Baseline-pattern) +
    # #87CEEB sky blue (zIO/Zero-Copy-pattern). Pastel fills + visible hatch
    # so the two N values read by shape, not just colour.
    color16 = "#FFA500"; edge16 = "#cc7a00"
    color64 = "#87CEEB"; edge64 = "#4a8db8"
    b1 = ax.barh(y - h / 2, n16, height=h, color=color16, alpha=0.45,
                 edgecolor=edge16, linewidth=0.6, hatch="////",
                 label="$N{=}16$")
    b2 = ax.barh(y + h / 2, n64, height=h, color=color64, alpha=0.55,
                 edgecolor=edge64, linewidth=0.6, hatch="xxxx",
                 label="$N{=}64$")
    # Value labels at the right end of each bar.
    for rects, vals in ((b1, n16), (b2, n64)):
        for r, v in zip(rects, vals):
            ax.text(r.get_width() + 2.5, r.get_y() + r.get_height() / 2,
                    f"{v:.0f}", ha="left", va="center",
                    fontsize=FS.CROSS_TICK - 1)

    ax.set_yticks(y)
    ax.set_yticklabels([display[k] for k in order], fontsize=FS.CROSS_TICK)
    ax.invert_yaxis()  # DeltaBox at top (matches panel-(a)/(b) line order)
    ax.set_xlabel("Sync GPU occupation (%)", fontsize=FS.CROSS_AXIS)
    # 0..125 leaves headroom on the right for value labels and any small
    # legend overshoot.
    ax.set_xlim(0, 125)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.tick_params(axis="x", labelsize=FS.CROSS_TICK)
    style_axes(ax, ygrid=False, xgrid=True)

    # Legend in right-edge headroom (x ∈ [100, 125]) — both swatches use
    # the same green/blue palette as the actual bars.
    ax.legend(fontsize=FS.CROSS_LEGEND,
              loc="center right", ncol=1,
              bbox_to_anchor=(1.0, 0.30),
              handlelength=1.5, handletextpad=0.4, labelspacing=0.3,
              borderpad=0.3, frameon=False)



def main():
    data = json.loads(DATA_FILE.read_text())
    fan = (data["forks"], data["fanout_ms"], {})
    Bs = Bs_tr = data["forks"]
    tgen = {"Qwen2.5-7B-Instruct": data["tgen_s"]}
    ttrain = data["ttrain_s"]
    util = {}
    for backend, series in data["fanout_ms"].items():
        util[backend] = {}
        for n in (16, 64):
            i = Bs.index(n)
            busy = data["tgen_s"][i] + data["ttrain_s"][i]
            util[backend][n] = 100 * busy / (series[i] / 1000 + busy)

    # 1x6 GridSpec: a leftmost spacer pushes (a) and the legend rightward
    # together. Spacer ratio = 0.47 yields ~5.3% of figure width as left
    # margin (vs ~2.3% with the prior 0.20 spacer) — i.e. an additional
    # ~0.03 figure-fraction shift to the right for panel (a).
    fig = plt.figure(figsize=(8.0, 1.3))
    gs = fig.add_gridspec(1, 6,
                          width_ratios=[0.47, 1.60, 1.70, 1.55, 1.20, 2.30],
                          wspace=0.0)
    ax_left = fig.add_subplot(gs[0, 0])
    ax_a    = fig.add_subplot(gs[0, 1])
    ax_leg  = fig.add_subplot(gs[0, 2])
    ax_b    = fig.add_subplot(gs[0, 3])
    ax_gap  = fig.add_subplot(gs[0, 4])
    ax_c    = fig.add_subplot(gs[0, 5])
    ax_left.axis("off")
    ax_leg.axis("off")
    ax_gap.axis("off")

    handles, labels = panel_fanout(ax_a, fan)
    panel_tgpu(ax_b, Bs, tgen, Bs_tr, ttrain)
    panel_util(ax_c, util)

    # Full names (no abbreviation) — labels stay as "Firecracker" and
    # "CubeSandbox" from BACKENDS, no remapping.
    legend_labels = list(labels)
    # Legend in the lower portion of the spacer:
    #   - x=0.36 (= prev 0.30 + 0.06) shifts the legend rightward within
    #     the spacer so the labels sit clearly past panel (a)'s tick area;
    #   - y=0.25 drops it below panel-(a)'s tall right-side tail.
    ax_leg.legend(handles, legend_labels,
                  loc="center", bbox_to_anchor=(0.36, 0.25),
                  fontsize=FS.CROSS_LEGEND,
                  handlelength=1.3, handletextpad=0.35,
                  labelspacing=0.55, borderpad=0.3,
                  frameon=False)

    panel_label(ax_a, "(a) Substrate fan-out latency", y=-0.36, fontsize=FS.CROSS_PANEL)
    panel_label(ax_b, "(b) $T_{gen}$ vs $T_{train}$",  y=-0.36, fontsize=FS.CROSS_PANEL)
    panel_label(ax_c, "(c) Expected GPU occupation (7B)", y=-0.36, fontsize=FS.CROSS_PANEL)
    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    save(fig, str(OUT_PDF))
    print(f"wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
