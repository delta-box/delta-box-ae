"""Standalone Figure 8(b)/(c) renderers with optional, lazy matplotlib imports.

The supplied suite/model JSON is the only numerical source. Historical inputs,
partial timing matrices and estimated sandbox durations remain visible.
"""
from __future__ import annotations

import math
from pathlib import Path
import textwrap

from .gpu_occupation import (
    BACKEND_ORDER, _backend_list, _batch_list, _header, _number, _object,
    _positive_int, indexed_gpu_cases,
)


TIMING_BATCHES = (1, 4, 16, 64)
BACKEND_LABELS = {"deltabox": "δSandbox", "cube": "CubeSandbox", "e2b": "E2B"}
THEORY_LABELS = {
    "historical": "Historical-input model", "fresh": "Fresh-input model",
    "assumed": "Assumed-input model", "mixed": "Mixed-input model",
}
TIMING_LABELS = {
    "historical": "Historical GPU timing records", "fresh": "Fresh GPU timing records",
    "assumed": "Assumed timing inputs", "mixed": "Mixed-source timing inputs",
}
PAPER_RC = {
    "font.family": "STIXGeneral", "mathtext.fontset": "stix", "font.size": 10,
    "pdf.fonttype": 42, "ps.fonttype": 42,
    "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False, "hatch.linewidth": .6,
    "figure.facecolor": "white", "axes.facecolor": "white",
}


def _new_output(output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Plot output already exists: {output_dir}")
    return output_dir


def _pyplot():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ImportError("Plotting requires the optional ae/requirements-analysis.txt dependencies") from exc
    return plt


def _save(figure, output_dir, stem):
    # mkdir is exclusive even if another process claimed the path after validation.
    output_dir.mkdir(parents=True, exist_ok=False)
    files = []
    for extension in ("png", "pdf"):
        path = output_dir / f"{stem}.{extension}"
        figure.savefig(path, dpi=240, bbox_inches="tight", pad_inches=.12)
        files.append(str(path.resolve()))
    return files


def plot_gpu_timing(suite: dict, output_dir: Path) -> dict:
    """Plot successful mean phase times, retaining absent paper cases as missing."""
    output_dir = _new_output(output_dir)
    cases = indexed_gpu_cases(suite)
    if any(n not in TIMING_BATCHES for _, n in cases):
        raise ValueError("Figure 8(b) supports batch sizes 1, 4, 16 and 64")
    slots = [dict(phase=phase, batch=n,
                  mean_s=float(cases[phase, n]["timing_s"]["mean"]) if (phase, n) in cases else None,
                  num_gpus=cases[phase, n]["num_gpus"] if (phase, n) in cases else None)
             for phase in ("generation", "training") for n in TIMING_BATCHES]
    missing = [dict(phase=s["phase"], batch=s["batch"]) for s in slots if s["mean_s"] is None]
    provenance = f"{TIMING_LABELS[suite['source_kind']]} · {suite['model_label']}"
    gpu_count_labels = []
    for phase in ("generation", "training"):
        counts = {}
        for slot in slots:
            if slot["phase"] == phase and slot["num_gpus"] is not None:
                counts.setdefault(slot["num_gpus"], []).append(slot["batch"])
        if counts:
            groups = [f"B{'/'.join(map(str, batches))} = {count}" for count, batches in counts.items()]
            gpu_count_labels.append(f"{phase.capitalize()} GPUs: " + "; ".join(groups) + ".")
    footer = "\n".join([
        "Mean duration; model loading and explicit warmup excluded; first-use work may remain.",
        *(textwrap.fill(label, width=98) for label in gpu_count_labels),
    ])
    if missing:
        footer += "\n" + textwrap.fill("Partial suite — missing: " + ", ".join(
            f"{s['phase']} B{s['batch']}" for s in missing), width=98)
    plt = _pyplot()
    with plt.rc_context(PAPER_RC):
        # Keep the axes' physical height and leave room for every footer line.
        extra_height = .16 * footer.count("\n")
        height = 3.8 + extra_height
        fig, axis = plt.subplots(figsize=(6.5, height))
        try:
            fig.subplots_adjust(left=.11, right=.97, top=1 - .76 / height,
                                bottom=(1.026 + extra_height) / height)
            fig.suptitle("Figure 8(b) · GPU timing", y=.975, fontsize=13)
            axis.set_title(provenance, fontsize=10, pad=14)
            styles = {"generation": ("#FF8C00", "-", "o"), "training": ("#6CB4F0", "--", "s")}
            for phase, (color, linestyle, marker) in styles.items():
                values = [s["mean_s"] if s["mean_s"] is not None else math.nan
                          for s in slots if s["phase"] == phase]
                axis.plot(range(4), values, color=color, linestyle=linestyle, marker=marker,
                          markersize=5, linewidth=1.8, label=phase.capitalize())
            axis.set_xticks(range(4), [str(n) for n in TIMING_BATCHES])
            axis.set_xlim(-.15, 3.15)
            maximum = max(s["mean_s"] for s in slots if s["mean_s"] is not None)
            axis.set_ylim(0, maximum * 1.18)
            axis.set_xlabel("Batch size (B)")
            axis.set_ylabel("Mean time (seconds)")
            axis.set_axisbelow(True)
            axis.grid(axis="y", linestyle="--", alpha=.35, linewidth=.6)
            axis.legend(loc="upper left", ncol=2)
            fig.text(.11, .12 / height, footer, fontsize=8.5, va="bottom")
            files = _save(fig, output_dir, "figure-08b")
        finally:
            plt.close(fig)
    return dict(schema_version=1, kind="figure-08b-plot", status="partial" if missing else "ok",
                source_kind=suite["source_kind"], provenance_label=provenance,
                statistic="mean", unit="s", slots=slots, missing_cases=missing,
                gpu_count_labels=gpu_count_labels, footer=footer, files=files)


def _occupation_rows(result):
    _header(result, "gpu-occupation-model")
    if result.get("status") != "ok":
        raise ValueError("Only a successful occupation model can be plotted")
    batches = _batch_list(result.get("batches"))
    backends = _backend_list(result.get("backends"))
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Missing occupation rows")
    indexed = {}
    for row in rows:
        _object(row, "occupation row")
        backend = row.get("backend")
        n = _positive_int(row.get("n"), "occupation n")
        if backend not in backends or n not in batches:
            raise ValueError("Occupation row is outside the declared batch/backend grid")
        if (backend, n) in indexed:
            raise ValueError(f"Duplicate occupation row for {backend} N={n}")
        occupation = _number(row.get("occupation"), "occupation")
        percentage = _number(row.get("occupation_pct"), "occupation_pct")
        _number(row.get("staleness_versions"), "staleness_versions")
        if occupation > 1 or percentage > 100 or not math.isclose(100 * occupation, percentage, rel_tol=1e-10):
            raise ValueError("Occupation must be a consistent ratio in [0,1] and percentage in [0,100]")
        for field in ("t_sandbox_s", "t_gen_s", "t_train_s"):
            _number(row.get(field), field, positive=field == "t_train_s")
        for field in ("generation_gpus", "training_gpus"):
            _positive_int(row.get(field), field)
        if not isinstance(row.get("estimated"), bool):
            raise ValueError("Occupation estimated must be an explicit boolean")
        indexed[backend, n] = row
    for backend in backends:
        for n in batches:
            if (backend, n) not in indexed:
                raise ValueError(f"Missing occupation row for {backend} N={n}")
    ordered = [b for b in BACKEND_ORDER if b in backends] + [b for b in backends if b not in BACKEND_ORDER]
    return ordered, sorted(batches), indexed


def plot_gpu_occupation(result: dict, output_dir: Path) -> dict:
    """Render the supplied model percentages, visibly labeling provenance/estimates."""
    output_dir = _new_output(output_dir)
    backends, batches, rows = _occupation_rows(result)
    estimated = [dict(backend=backend, n=n) for backend in backends for n in batches
                 if rows[backend, n]["estimated"]]
    provenance = f"{THEORY_LABELS[result['source_kind']]} · {result['model_label']}"
    footer = "Equation 1 time fraction; this is not GPU telemetry or GPU-count-weighted utilization."
    if estimated:
        footer += "\n" + textwrap.fill("* Estimated sandbox input: " + ", ".join(
            f"{BACKEND_LABELS.get(r['backend'], r['backend'])} N={r['n']}" for r in estimated), width=95)
    plt = _pyplot()
    with plt.rc_context(PAPER_RC):
        height = max(3.7, 1.9 + .58 * len(backends))
        fig, axis = plt.subplots(figsize=(6.7, height))
        try:
            fig.subplots_adjust(left=.18, right=.965, top=.76, bottom=.28)
            fig.suptitle("Figure 8(c) · Expected GPU occupation", y=.975, fontsize=13)
            fig.text(.5, .895, provenance, ha="center", fontsize=10)
            bar_height = .7 / len(batches)
            fallback_colors = ("#B7D7A8", "#D5B5D9", "#D9C5A0")
            for batch_index, n in enumerate(batches):
                offset = (batch_index - (len(batches) - 1) / 2) * bar_height
                positions = [i + offset for i in range(len(backends))]
                percentages = [rows[backend, n]["occupation_pct"] for backend in backends]
                color = {16: "#FFA500", 64: "#87CEEB"}.get(n, fallback_colors[batch_index % 3])
                axis.barh(positions, percentages, height=bar_height * .9, label=f"N = {n}",
                          color=color, edgecolor="#666666", linewidth=.5,
                          hatch="///" if n == 64 else None)
                for backend, y, percentage in zip(backends, positions, percentages):
                    label = f"{percentage:.1f}%" + ("*" if rows[backend, n]["estimated"] else "")
                    inside = percentage >= 86
                    axis.text(percentage - 1.2 if inside else percentage + 1.2, y, label,
                              ha="right" if inside else "left", va="center", fontsize=9)
            axis.set_yticks(range(len(backends)), [BACKEND_LABELS.get(b, b) for b in backends])
            axis.invert_yaxis()
            axis.set_xlim(0, 100)
            axis.set_xticks((0, 25, 50, 75, 100))
            axis.set_xlabel("Expected GPU occupation (%)")
            axis.set_axisbelow(True)
            axis.grid(axis="x", linestyle="--", alpha=.35, linewidth=.6)
            handles, labels = axis.get_legend_handles_labels()
            fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(.56, .862),
                       ncol=min(4, len(batches)), fontsize=10)
            fig.text(.02, .025, footer, fontsize=8.5, va="bottom")
            files = _save(fig, output_dir, "figure-08c")
        finally:
            plt.close(fig)
    return dict(schema_version=1, kind="figure-08c-plot", status="ok", source_kind=result["source_kind"],
                provenance_label=provenance, backends=backends, batches=batches,
                estimated_points=estimated, files=files)
