"""Unified matplotlib style for paper figures, aligned to copier's palette.

Convention (mirrors copier's eval figures):
  - SUT ("ours")        = forest green #228B22 (bars) / #006400 (lines)
                          marker: diamond 'D'
  - Bar-plot baseline   = orange  #FFA500
  - Line-plot baseline  = black   #000000   marker: circle 'o'
  - 2nd comparison      = saddle  #8B4513
  - 3rd comparison      = slate   #6A5ACD
  - Extras              = sky blue / lime / plum / dark red
  - Grid                = horizontal-only dashed, linestyle='--', alpha=0.6
  - Legend              = frameless, placed outside the panel
  - Sub-captions        = "(a) Title" bold, below the axes
  - Fonts               = serif, pdf.fonttype=42 (ACM camera-ready)

Usage:
    from paperstyle import C, M, style_axes, panel_label, save
    fig, ax = plt.subplots(...)
    ax.plot(xs, ys, color=C.SYS, marker=M.SYS, label="δSandbox")
    ax.plot(xs, ys, color=C.BASELINE, marker=M.BASELINE, label="Docker")
    style_axes(ax)
    save(fig, "/path/fig.pdf")

Style is auto-applied on import; callers can still mutate plt.rcParams later.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ──────────────────────────────────────────────────────────────────────────
# Palette  (copier-aligned)
# ──────────────────────────────────────────────────────────────────────────

class C:
    """Semantic color constants. Never inline a hex code for our system."""
    # ─── System-vs-baseline axis ─────────────────────────────────────────
    SYS         = "#228B22"   # forest green — OURS in bar plots (copier's bar SUT)
    SYS_LINE    = "#006400"   # deep green — OURS in line plots (copier's line SUT)
    SYS_EDGE    = "#1b5e20"   # darker green for bar edges
    BASELINE    = "#FFA500"   # orange — copier's "Baseline" key in bar plots
    BASELINE_EDGE = "#cc7a00"
    DOCKER      = "#000000"   # black — line-plot baseline
    LINE_BASE   = "#000000"   # alias

    # ─── Comparison systems (copier's recurring extras) ──────────────────
    # NOTE 2026-05-17: copier's `#8B4513` saddle brown LOOKS okay only because
    # gnuplot applies a fill pattern over it (looks like dotted brown on white).
    # Pure solid `#8B4513` in matplotlib without hatching is muddy. We swap
    # COMP1 to slate purple-blue (copier's actual line-plot DMA color, Fig 9).
    COMP1       = "#6A5ACD"   # slate purple-blue (copier's "DMA" Fig 9-10)
    COMP1_EDGE  = "#3f3287"
    COMP2       = "#87CEEB"   # sky blue (copier "Zero Copy")
    COMP2_EDGE  = "#4a8db8"
    SKYBLUE     = "#87CEEB"   # copier "Zero Copy"
    DEEPSKY     = "#00BFFF"   # copier "zIO"-bright
    LIME        = "#9ACD32"   # copier "AVX2"
    PLUM        = "#DDA0DD"   # copier "io_uring"
    PURPLE      = "#800080"   # copier "io_uring-batch"

    # ─── Strategy axis (within \sys) ─────────────────────────────────────
    LW          = "#228B22"   # = SYS, lightweight ckpt
    STD         = "#FFA500"   # = BASELINE, standard ckpt
    PRE         = "#8B0000"   # dark red, predump

    # ─── Binary categorical (mode A / mode B) ────────────────────────────
    A           = "#1565c0"   # blue
    B           = "#ef6c00"   # orange

    # ─── Outcome (positive/negative semantic) ────────────────────────────
    WIN         = "#228B22"   # = SYS
    LOSS        = "#8B0000"   # dark red

    # ─── Annotation / band / scaffolding ─────────────────────────────────
    NEUTRAL     = "#666666"   # general grey
    GRID        = "#999999"   # reference lines
    HINT        = "#444444"   # secondary line / fit
    BAND        = "#ffd54f"   # amber regime band
    BAND_DARK   = "#8d6e00"   # band label text


class M:
    """Marker constants. Same marker for the same role in every figure."""
    SYS         = "D"   # diamond — ours
    BASELINE    = "o"   # circle — orange baseline (bar plots use bars not markers)
    DOCKER      = "o"   # alias
    COMP1       = "s"   # square — saddle brown comparison
    COMP2       = "^"   # triangle — slate blue comparison
    SKYBLUE     = "v"
    LIME        = "P"
    A           = "o"
    B           = "s"


# ──────────────────────────────────────────────────────────────────────────
# Hatching (B&W-printable bar plots, mirrors copier's gnuplot fill patterns)
# ──────────────────────────────────────────────────────────────────────────
# In copier's eval figures, every bar has a `set style fill pattern N`. This
# lets the figure stay readable in B&W print and helps color-blind readers.
# Use these constants when calling ax.bar(..., hatch=H.X, edgecolor=C.X_EDGE).
# Convention: SUT bar stays SOLID (no hatch) so "ours" pops; baselines hatched.

class H:
    SYS         = ""           # solid — SUT stands out
    BASELINE    = "////"       # diagonal — copier pattern 5
    COMP1       = "xxxx"       # cross  — copier pattern 2/6
    COMP2       = "...."       # dots   — copier pattern 1
    A           = "////"
    B           = "...."


# Convenience tuple iter order: (color, edge, hatch, role_label)
ROLES_3_BAR = [
    (C.SYS,      C.SYS_EDGE,      H.SYS,      "ours"),
    (C.BASELINE, C.BASELINE_EDGE, H.BASELINE, "baseline"),
    (C.COMP1,    C.COMP1_EDGE,    H.COMP1,    "compare-1"),
]


# ──────────────────────────────────────────────────────────────────────────
# Fontsize policy (copier-aligned, computed from LaTeX scaling)
# ──────────────────────────────────────────────────────────────────────────
#
# Target print fontsizes after LaTeX scales the PDF to fit the column / page:
#   tick label  ~7pt       (slightly smaller than 10pt body)
#   axis label  ~8.5pt     (clearly readable)
#   panel label ~9.5pt bold (≈ caption myfont 9.5pt, so panel headings
#                            visually outrank tick labels)
#   legend      ~7pt       (= tick)
#
# Source fontsizes (in matplotlib) are sized up to compensate for the
# LaTeX-side scaling factor:
#   COL_*   = single-column figure (`\includegraphics[width=\columnwidth]`,
#             figsize_w ~ 3.3-3.4 in, scale ~0.95)
#   CROSS_* = cross-column figure  (`figure*` + `\includegraphics[width=\linewidth]`,
#             figsize_w ~ 8.0 in, scale ~0.875)
#
# If a script's figsize_w deviates significantly (e.g. fig_upper_sweep uses
# figsize_w=7 inside `\columnwidth`), fontsizes should be hand-tuned.

class FS:
    """Unified fontsize constants. Targets BODY TEXT SIZE (10pt) in print
    for tick / axis-label / panel-label across all figures.

    Empirical scaling (acmart sigplan 10pt, measured 2026-05-19):
        \\begin{figure*}: \\linewidth = 505.89pt = 7.026in
        \\begin{figure}:  \\linewidth = 240.94pt = 3.346in
    Matplotlib `bbox_inches='tight'` shrinks source PDF to fit content
    closely, so the effective scale is near 1:1 (~0.98-1.02). We therefore
    just set source = target_print_size = 10pt for both single-col and
    cross-col figures. Panel label adds weight='bold' via panel_label().
    Legend stays smaller (8pt) — not specified by spec."""
    COL_TICK    = 9.0
    COL_AXIS    = 9.0
    COL_PANEL   = 9.0
    COL_LEGEND  = 7.5

    # Cross-column figures are saved at figsize≈8" wide and \includegraphics
    # stretches them to \linewidth (figure*, ~7.03 in) — a 1.106× upscale that
    # would otherwise leave text noticeably larger than single-column figures
    # (which scale at 0.997×). Source size pre-divided by 1.106 so rendered
    # text lands at the same print-pt as COL_* figures.
    CROSS_TICK   = 8.0
    CROSS_AXIS   = 8.0
    CROSS_PANEL  = 8.0
    CROSS_LEGEND = 6.8


# ──────────────────────────────────────────────────────────────────────────
# Style application
# ──────────────────────────────────────────────────────────────────────────

def apply_paper_style() -> None:
    """Set global rcParams for paper figures. Idempotent."""
    plt.rcParams.update({
        "pdf.fonttype":      42,
        "ps.fonttype":       42,
        # Unify text + math + bold in a single serif family (STIX). Without
        # explicit font.serif, matplotlib falls back to DejaVu Serif which
        # mismatches the STIX math output. Putting STIX first forces both
        # the regular text glyphs AND the math glyphs through the same
        # STIX shapes, so $N$ italic visually matches "(fan-out width)".
        "font.family":       "serif",
        "font.serif":        ["STIXGeneral", "STIX Two Text", "DejaVu Serif"],
        "mathtext.fontset":  "stix",
        "font.size":         9,
        "axes.labelsize":    9,
        "axes.titlesize":    9,
        "xtick.labelsize":   7.5,
        "ytick.labelsize":   7.5,
        "legend.fontsize":   8,
        "legend.frameon":    False,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.grid":         False,        # caller turns on per-axis via style_axes
        "grid.linestyle":    "--",
        "grid.alpha":        0.6,
        "grid.linewidth":    0.5,
        "lines.linewidth":   1.6,
        "lines.markersize":  6,
        "hatch.linewidth":   0.5,           # finer hatching for paper print
        "savefig.bbox":      "tight",
        "savefig.pad_inches": 0.02,
    })


def style_axes(ax, *, ygrid: bool = True, xgrid: bool = False,
               despine: bool = True) -> None:
    """Per-axis style: horizontal-only dashed grid, despine top/right."""
    if despine:
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
    if ygrid:
        ax.grid(axis="y", linestyle="--", alpha=0.6, linewidth=0.5, zorder=0)
    if xgrid:
        ax.grid(axis="x", linestyle="--", alpha=0.6, linewidth=0.5, zorder=0)


def panel_label(ax, text: str, *, y: float = -0.22, fontsize: int = 10,
                weight: str = "bold") -> None:
    """Place a `(a) Title` bold sub-caption BELOW the axes (copier convention)."""
    ax.text(0.5, y, text, transform=ax.transAxes,
            ha="center", va="top", fontsize=fontsize, weight=weight)


def outside_legend(fig, handles, labels, *, ncol: int | None = None,
                   y: float = 1.02, **kw) -> None:
    """Single frameless legend ABOVE all panels (copier idiom)."""
    if ncol is None:
        ncol = len(labels)
    fig.legend(handles, labels,
               loc="upper center", ncol=ncol,
               bbox_to_anchor=(0.5, y),
               frameon=False,
               handlelength=1.5, handletextpad=0.4, columnspacing=1.2,
               **kw)


def save(fig, out_path: str, also_png: bool = False, dpi: int = 200) -> None:
    """Save fig with tight bbox. Optional matching .png for previews."""
    fig.savefig(str(out_path), bbox_inches="tight", pad_inches=0.02)
    if also_png:
        p = str(out_path)
        if p.endswith(".pdf"):
            fig.savefig(p[:-4] + ".png", dpi=dpi,
                        bbox_inches="tight", pad_inches=0.02)


# Auto-apply on import (callers cannot forget).
apply_paper_style()
