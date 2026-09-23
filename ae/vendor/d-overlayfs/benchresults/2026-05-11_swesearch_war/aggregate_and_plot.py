#!/usr/bin/env python3
"""aggregate_and_plot.py — Read per-edit JSONL results from swe-search replay
across 3 FS arms, bin by file size, plot WAR vs file-size figure.

Output:
  - aggregate.json (per-bin medians + p25-p75)
  - fig_war_real.pdf (drop-in replacement for current fig_war.pdf)
"""
import argparse
import glob
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, "/home/dong/d-overlayfs/benchmarks")

import matplotlib.pyplot as plt
from paperstyle import C, M, style_axes, save  # noqa: E402  (auto-applies style)


# Bins (KB), inclusive lower / exclusive upper. Spans 1 KB to 256 KB.
BINS_KB = [(1, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 256)]
BIN_LABELS = ["1-8", "8-16", "16-32", "32-64", "64-128", "128-256"]


def bin_for(size_bytes: int) -> int | None:
    kb = size_bytes / 1024
    for i, (lo, hi) in enumerate(BINS_KB):
        if lo <= kb < hi:
            return i
    return None


def percentile(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    idx = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[idx]


def load_results(results_dir: str):
    """Returns dict: {fs_arm: list of edit dicts}."""
    rows = {"ext4": [], "xfs": [], "xfs_reflink": []}
    for p in sorted(glob.glob(f"{results_dir}/*.jsonl")):
        fn = os.path.basename(p)[:-len(".jsonl")]
        # parse <instance>_<fs_arm>
        for fs in ("xfs_reflink", "xfs", "ext4"):  # longest first
            if fn.endswith(f"_{fs}"):
                inst = fn[:-len(fs)-1]
                for line in open(p):
                    line = line.strip()
                    if not line: continue
                    r = json.loads(line)
                    r["instance"] = inst
                    rows[fs].append(r)
                break
    return rows


def aggregate(rows_by_arm):
    """For each (arm, bin), compute per-instance median first, then bin
    median, so MCTS-retry-heavy instances don't dominate.

    Per-instance median means: for each unique (instance, file_path), compute
    median copyup & phys across that instance's edits. Then put one sample
    per (instance, file_path) into the appropriate file-size bin. The bin's
    reported median is the median across instances in that bin.
    """
    from collections import defaultdict
    agg = {}
    for arm, rows in rows_by_arm.items():
        # Group: (instance, file_path) → list of edits
        groups = defaultdict(list)
        for r in rows:
            if not r.get("applied_ok"): continue
            groups[(r["instance"], r["file_path"])].append(r)
        # Per-(inst, file) median
        per_instance = []
        for (inst, fp), edits in groups.items():
            cu = sorted(e["copyup_bytes"] for e in edits)
            ph = sorted(e["phys_bytes"] for e in edits)
            sizes = sorted(e["file_size_bytes"] for e in edits)
            per_instance.append({
                "instance": inst, "file_path": fp,
                "file_size": sizes[len(sizes)//2],
                "n_edits": len(edits),
                "cu_median": cu[len(cu)//2],
                "ph_median": ph[len(ph)//2],
            })
        # Now bin per-instance points
        agg[arm] = {}
        by_bin = [[] for _ in BINS_KB]
        for s in per_instance:
            b = bin_for(s["file_size"])
            if b is None: continue
            by_bin[b].append(s)
        for i, bn in enumerate(by_bin):
            cu = [s["cu_median"] for s in bn]
            ph = [s["ph_median"] for s in bn]
            agg[arm][BIN_LABELS[i]] = {
                "n_instances": len(bn),
                "n_edits": sum(s["n_edits"] for s in bn),
                "cu_p25": percentile(cu, 25),
                "cu_p50": percentile(cu, 50),
                "cu_p75": percentile(cu, 75),
                "ph_p25": percentile(ph, 25),
                "ph_p50": percentile(ph, 50),
                "ph_p75": percentile(ph, 75),
            }
    return agg


def plot(agg, out_pdf: str):
    bin_centers = [(lo + hi) / 2 * 1024 for lo, hi in BINS_KB]  # bytes
    arm_styles = {
        "ext4":        (M.DOCKER, C.LOSS, "without reflink (ext4)"),
        "xfs":         (M.FC,     C.COPY, "without reflink (xfs)"),
        "xfs_reflink": (M.SYS,    C.SYS,  "with reflink"),
    }

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(3.3, 3.8), sharex=True)
    AGENT_REGIME_B = (12.1e3, 66.4e3)  # IQR (p25-p75) of edited-file sizes across 186 unique (instance, file) pairs from 4 trace pools (claude/mcts+linear, mimo/mcts+linear)

    for ax in (ax1, ax2):
        ax.axvspan(*AGENT_REGIME_B, color=C.BAND, alpha=0.25, zorder=0)

    for arm in ("ext4", "xfs", "xfs_reflink"):
        marker, color, label = arm_styles[arm]
        xs, ys_a, ys_a_lo, ys_a_hi = [], [], [], []
        ys_b, ys_b_lo, ys_b_hi = [], [], []
        for i, bl in enumerate(BIN_LABELS):
            d = agg.get(arm, {}).get(bl, {})
            if d.get("n_instances", 0) == 0: continue
            xs.append(bin_centers[i])
            # WAR units: bytes per 1000 B logical change (we don't have logical
            # change so use absolute bytes; that's still a meaningful comparison
            # across arms at the same file_size bin)
            ys_a.append(d["cu_p50"]); ys_a_lo.append(d["cu_p25"]); ys_a_hi.append(d["cu_p75"])
            ys_b.append(d["ph_p50"]); ys_b_lo.append(d["ph_p25"]); ys_b_hi.append(d["ph_p75"])
        ax1.plot(xs, ys_a, marker=marker, ms=4, lw=1.4, color=color,
                 label=label, zorder=3)
        ax2.plot(xs, ys_b, marker=marker, ms=4, lw=1.4, color=color, zorder=3)

    for ax, title in ((ax1, "(a) Copy-up duplicated data"),
                      (ax2, "(b) Physical I/O")):
        ax.set_xscale("log"); ax.set_yscale("log")
        style_axes(ax, ygrid=True, xgrid=False)
        ax.set_ylabel("bytes per edit", fontsize=8)
        ax.set_title(title, fontsize=8.5, loc="left", pad=3)
        ax.tick_params(axis="both", which="minor", length=2)
        ax.xaxis.set_minor_locator(plt.NullLocator())

    # Custom x-axis ticks at bin centers
    xtick_pos = []
    xtick_labels = []
    for i, (lo, hi) in enumerate(BINS_KB):
        if any(agg.get(a, {}).get(BIN_LABELS[i], {}).get("n_instances", 0) > 0 for a in agg):
            xtick_pos.append(bin_centers[i])
            xtick_labels.append(f"{lo}-{hi}KB")
    ax2.set_xticks(xtick_pos)
    ax2.set_xticklabels(xtick_labels, fontsize=6, rotation=30)
    ax2.set_xlabel("edited file size", fontsize=8)
    ax1.tick_params(axis="x", labelbottom=True, labelsize=6, rotation=30)

    # Regime band label: place inside the band, just above the lower edge.
    # Place label near top of (a) panel, above the data lines
    import math
    lo, hi = AGENT_REGIME_B
    band_center = math.sqrt(lo * hi)
    ax1.text(band_center, ax1.get_ylim()[1] * 0.6, f"typical agent range\n({lo/1024:.0f}-{hi/1024:.0f} KB)",
             fontsize=5.5, color=C.BAND_DARK, ha="center", va="top", style="italic")
    # Legend: bottom-right of (a), well clear of band and data.
    ax1.legend(loc="lower right", fontsize=6,
               handlelength=1.5, labelspacing=0.3,
               bbox_to_anchor=(1.0, 0.0))
    fig.tight_layout(pad=0.3, h_pad=0.8)
    save(fig, out_pdf, also_png=False)
    print(f"wrote {out_pdf}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="/home/dong/d-overlayfs/benchresults/2026-05-11_swesearch_war")
    ap.add_argument("--out_pdf", default="/home/dong/d-overlayfs/rollbackable-sandbox-paper/fig_war.pdf")
    ap.add_argument("--out_json", default="/home/dong/d-overlayfs/benchresults/2026-05-11_swesearch_war/aggregate.json")
    args = ap.parse_args()

    rows = load_results(args.results)
    print(f"loaded edits per arm: {{a: len(rows[a]) for a in rows}}")
    for a in rows:
        print(f"  {a}: {len(rows[a])}")
    agg = aggregate(rows)

    print("\n=== aggregate (per-instance median, then bin median) ===")
    print(f"{'bin':<10} {'arm':<14} {'n_inst':>7} {'n_edits':>8} {'cu_p25':>9} {'cu_p50':>9} {'cu_p75':>9} {'ph_p50':>9}")
    for bl in BIN_LABELS:
        for arm in ("ext4", "xfs", "xfs_reflink"):
            d = agg.get(arm, {}).get(bl, {})
            if d.get("n_instances", 0) == 0: continue
            print(f"{bl:<10} {arm:<14} {d['n_instances']:>7} {d['n_edits']:>8} "
                  f"{d['cu_p25']/1024:>7.1f}KB {d['cu_p50']/1024:>7.1f}KB {d['cu_p75']/1024:>7.1f}KB "
                  f"{d['ph_p50']/1024:>7.1f}KB")

    with open(args.out_json, "w") as f:
        json.dump(agg, f, indent=2)
    plot(agg, args.out_pdf)


if __name__ == "__main__":
    main()
