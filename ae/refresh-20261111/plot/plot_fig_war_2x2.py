#!/usr/bin/env python3
"""plot_fig_war_2x2.py — candidate 2x2 version of the WAR figure.

Left column keeps the per-edit copy-up and physical-I/O panels of
plot_fig_war.py. Right column adds (c) DeltaFS lookup latency vs. layer-stack
depth (camera-ready E4, pinned CPUs at the maximum P-state, samedir layout,
file in the bottom layer) and (d) the mean CRIU restore time before
finalization added by the incremental chain (30 trials per depth, 40 MiB).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib.pyplot as plt
from paperstyle import C, FS, style_axes  # noqa: E402
from matplotlib.transforms import Bbox
BINS_KB = [(1, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 256)]
BIN_LABELS = ["1-8", "8-16", "16-32", "32-64", "64-128", "128-256"]

# Every stroke stays >= 0.5 pt once the figure is placed at 0.95\columnwidth.
plt.rcParams.update({"axes.linewidth": 0.6, "xtick.major.width": 0.6,
                     "ytick.major.width": 0.6, "xtick.minor.width": 0.6,
                     "ytick.minor.width": 0.6, "grid.linewidth": 0.6})

# Locked plotting inputs, recovered from the delivered figure's raw records.
INPUT_DATA = json.loads(Path(__file__).with_name("figure9-data.json").read_text())
TREE_DEPTH = 16  # deepest layer stack / chain our 30-iteration trees reach
AGENT_REGIME_B = (12.1e3, 66.4e3)


# Preserve the original PDF canvas so the other three panels keep their size.
EXPORT_BBOX_INCHES = (0.013307854546440949, 0.009982638888888888,
                     3.392641111111111, 2.290529513888889)


def save(fig, out_path, also_png=False, dpi=300):
    bbox = Bbox.from_bounds(*EXPORT_BBOX_INCHES)
    fig.savefig(str(out_path), bbox_inches=bbox, pad_inches=0)
    if also_png:
        fig.savefig(str(Path(out_path).with_suffix(".png")), dpi=dpi,
                    bbox_inches=bbox, pad_inches=0)


def grid(ax):
    style_axes(ax, ygrid=False, xgrid=False)
    ax.grid(axis="y", linestyle="--", alpha=0.6, linewidth=0.6, zorder=0)


def title(ax, text):
    ax.set_title(text, fontsize=7.5, loc="left", pad=1.5, fontweight="bold")


def lookup_series():
    return INPUT_DATA["lookup"]


def criu_series():
    data = INPUT_DATA["criu_record"]
    assert data["metric"] == "CRIU restore log interval before finalization"
    rows = sorted(data["summary"], key=lambda row: row["chain"])
    assert [r["chain"] for r in rows] == [2, 4, 8, 16, 20, 24, 32, 40, 48, 50]
    assert all(r["n"] == 30 for r in rows)
    base = rows[0]["mean_ms"]
    return [(row["chain"], row["mean_ms"] - base) for row in rows]


def plot(agg, out_pdf):
    fig, axes = plt.subplots(2, 2, figsize=(3.4, 2.3))
    (ax_a, ax_c), (ax_b, ax_d) = axes
    # Panel (a) now has exact file sizes; panel (b) retains its original bins.

    arm_styles = {
        "ext4": ("^", "#FF8C00", "w/o reflink (ext4)", "--", 4.0, 5),
        "xfs": ("s", "#6A5ACD", "w/o reflink (xfs)", "-", 3.5, 3),
        "xfs_reflink": ("d", "#006400", "with reflink", "-", 3.5, 4),
    }
    centers = [(lo + hi) / 2 * 1024 for lo, hi in BINS_KB]
    for ax in (ax_a, ax_b):
        ax.axvspan(*AGENT_REGIME_B, color=C.BAND, alpha=0.25, zorder=0, lw=0)
    for arm in ("ext4", "xfs", "xfs_reflink"):
        marker, color, label, ls_a, ms_a, z_a = arm_styles[arm]
        xs, ya, yb = [], [], []
        for i, bl in enumerate(BIN_LABELS):
            d = agg.get(arm, {}).get(bl, {})
            if d.get("n_instances", 0):
                xs.append(centers[i]); ya.append(d["cu_p50"]); yb.append(d["ph_p50"])
        primary = sorted((row for row in INPUT_DATA["fixed_overwrite"]["groups"]
                          if row["arm"] == arm and row["write_bytes"] == 4096),
                         key=lambda row: row["file_size_bytes"])
        assert len(primary) == 9 and all(row["metrics"]["copyup_bytes"]["n"] == 30 for row in primary)
        fixed_xs = [row["file_size_bytes"] for row in primary]
        fixed_ys = [row["metrics"]["phys_bytes"]["mean"] / 1024 for row in primary]
        ax_a.plot(fixed_xs, fixed_ys, marker=marker, ms=ms_a, lw=1.2, color=color, ls=ls_a, label=label, zorder=z_a, clip_on=False)
        ax_b.plot(xs, yb, marker=marker, ms=3.5, lw=1.2, color=color, zorder=3)
    for ax, text in ((ax_a, "(a) Copy-up write I/O"), (ax_b, "(b) Physical I/O")):
        ax.set_xscale("log"); ax.set_yscale("log")
        grid(ax)
        ax.set_ylabel("bytes/edit", fontsize=FS.COL_LEGEND, labelpad=1)
        ax.xaxis.set_minor_locator(plt.NullLocator())
        title(ax, text)
    # Panel (a): measured loop write I/O on linear axes; 30 runs per point.
    # Panel (b) retains its original logarithmic bytes/edit scale.
    ax_a.set_yscale("linear")
    ax_a.set_xscale("linear")
    ax_a.set_xlim(0, 1080 * 1024)
    ax_a.set_ylim(0, 1200)
    ax_a.set_yticks([0, 512, 1024])
    ax_a.set_xticks([0, 512 * 1024, 1024 * 1024])
    ax_a.set_xticklabels(["0", "512K", "1M"])
    ax_a.set_ylabel("KiB/edit", fontsize=FS.COL_LEGEND, labelpad=1)
    ax_a.yaxis.set_label_coords(-0.18, 0.5)  # Preserve left margin with the four-digit tick.
    shown = {"1-8": "1-8K", "16-32": "16-32K", "128-256": "128-256K"}
    ticks = [(centers[i], shown[bl]) for i, bl in enumerate(BIN_LABELS) if bl in shown]
    ax_b.set_xticks([t for t, _ in ticks])
    ax_b.set_xticklabels([l for _, l in ticks], fontsize=FS.COL_LEGEND - 0.5)
    ax_a.tick_params(axis="x", labelbottom=True)
    ax_a.set_xlabel("file size", fontsize=FS.COL_LEGEND, labelpad=1)
    ax_b.set_xlabel("edited file size", fontsize=FS.COL_LEGEND, labelpad=1)
    ax_a.legend(loc="upper left", fontsize=FS.COL_LEGEND - 1.5, handlelength=1.4,
                labelspacing=0.2, borderaxespad=0.1)

    ax_c.axvspan(1, TREE_DEPTH, color=C.BAND, alpha=0.25, zorder=0, lw=0)
    ax_d.axvspan(0, TREE_DEPTH, color=C.BAND, alpha=0.25, zorder=0, lw=0)
    lookups = lookup_series()
    for state, label, color, marker in (("cold", "cold cache", "#B22222", "o"),
                                        ("switch", "after switch", "#1F4E99", "d")):
        xs, ys = zip(*lookups[state])
        ax_c.plot(xs, ys, marker=marker, ms=3, lw=1.2, color=color, label=label, zorder=3)
    ax_c.set_xscale("log", base=2); ax_c.set_yscale("log")
    ax_c.set_ylabel(r"lookup ($\mu$s)", fontsize=FS.COL_LEGEND, labelpad=1)
    ax_c.set_xlabel("overlay layers", fontsize=FS.COL_LEGEND, labelpad=1)
    ax_c.legend(loc="upper left", fontsize=FS.COL_LEGEND - 1.5, handlelength=1.4,
                labelspacing=0.2, borderaxespad=0.1)
    title(ax_c, "(c) DeltaFS lookup")

    xs, ys = zip(*[(depth, latency) for depth, latency in criu_series() if depth <= 40])
    ax_d.plot(xs, ys, marker="s", ms=2.5, lw=1.2, color="#444444", zorder=3, clip_on=False)
    ax_d.set_xlim(0, 40)
    ax_d.set_xticks([0, 10, 20, 30, 40])
    ax_d.set_ylabel(r"$\Delta$restore (ms)", fontsize=FS.COL_LEGEND, labelpad=1)
    ax_d.set_xlabel("CRIU chain length", fontsize=FS.COL_LEGEND, labelpad=1)
    title(ax_d, "(d) CRIU lazy restore")

    ax_c.set_xticks([2, 16, 128])
    ax_c.set_xticklabels(["2", "16", "128"])
    ax_c.xaxis.set_minor_locator(plt.NullLocator())
    for ax in (ax_c, ax_d):
        grid(ax)
    for ax in axes.flat:
        ax.tick_params(labelsize=FS.COL_LEGEND - 0.5, pad=1.5)

    # Preserve the delivered figure's panel positions so changing panel (d)'s
    # data and tick widths does not move or resize panels (a)-(c).
    panel_positions = [
        (0.10279326195810355, 0.6281136775362319, 0.36211574780586275, 0.3227468297101449),
        (0.6194542111089671, 0.6281136775362319, 0.36211574780586275, 0.3227468297101449),
        (0.10279326195810355, 0.12814764492753628, 0.36211574780586275, 0.3227468297101449),
        (0.6194542111089671, 0.12814764492753628, 0.36211574780586275, 0.3227468297101449),
    ]
    for ax, position in zip(axes.flat, panel_positions):
        ax.set_position(position)
    save(fig, out_pdf, also_png=True, dpi=300)
    print(f"wrote {out_pdf}")


def main():
    import matplotlib
    if matplotlib.__version__ != "3.5.1":
        raise RuntimeError("Rebuild this figure with Matplotlib 3.5.1 to preserve the original axes and font layout; see BUILDING.md.")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out_pdf", default=str(Path(__file__).resolve().parents[1] / "source/fig_war_2x2.pdf"))
    args = ap.parse_args()
    plot(INPUT_DATA["war_aggregate"], args.out_pdf)


if __name__ == "__main__":
    main()
