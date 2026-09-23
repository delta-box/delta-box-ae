"""Paper-shaped tables made exclusively from the supplied analysis result.

The layout is fixed even for an empty result.  Cell manifests retain the timer
boundaries and population identities which would obscure the printed table.
This module deliberately has no raw-data or archived-paper-data reader.
"""
from __future__ import annotations

import json
import math


MISSING = "—"
TABLE2_SYSTEMS = (
    ("replay", "replay+cp"), ("fc-diff", "FC-Diff+dm"),
    ("criu", "CRIU+cp"), ("cube", "CubeSandbox"),
    ("e2b", "E2B (diff)"), ("deltabox", "DeltaBox"),
)
TABLE2_GROUPS = (
    ("Django", "Django"), ("SymPy", "SymPy"),
    ("Scientific", "Scientific"), ("Tools/Small", "Tools/Small repos"),
    ("All", "Event Avg"),
)
TABLE3_ROWS = (
    "Overlay ioctl switch", "Fork (stash / template)", "CRIU C/R",
    "Coordination", "Agent-perceived blocking",
)
POPULATION_FIELDS = (
    "backend", "experiment", "mode", "checkpoint_profile", "adaptive",
    "memory_policy", "prewarm_requested", "prewarm_mode", "run_purpose",
    "message_policy", "baseline_test_runtime", "mock_latency_policy",
    "replay_timing_method", "legacy_timing_policy", "source_identity",
    "cohort", "plot_group", "source_summary", "input_summary", "storage_mode",
)
PAPER_STYLE = {
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Times", "Liberation Serif", "STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix", "text.color": "black",
    "figure.facecolor": "white", "axes.facecolor": "white",
}


def _population(row):
    return {key: row[key] for key in POPULATION_FIELDS if key in row}


def _identities(indexed):
    return {json.dumps(_population(row), sort_keys=True) for _, row in indexed}


def _valid(row):
    value, n = row.get("value"), row.get("n")
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0
            and isinstance(n, (int, float)) and not isinstance(n, bool)
            and math.isfinite(n) and n > 0
            and row.get("unit", "ms") == "ms"
            and row.get("statistic", "mean") == "mean")


def _provenance(indexed):
    return [dict(metric_index=index, **{
        key: row[key] for key in (
            "metric", "value", "unit", "n", "statistic", "instance", "group",
            "evidence_kind", *POPULATION_FIELDS,
        ) if key in row
    }) for index, row in indexed]


def _empty(reason, indexed=()):
    return dict(value=None, display=MISSING, n=0, status="unavailable",
                reason=reason, sources=_provenance(indexed))


def _measure(indexed, *, decimals=2, boundary, population=(), aggregate=False):
    """Pool only distinct per-instance means inside one declared population."""
    if len(_identities(population or indexed)) > 1:
        return _empty("Multiple populations target this column; select a population explicitly.", indexed)
    if not indexed or not any(_valid(row) for _, row in indexed):
        return _empty("No directly measured matching timer is available.", indexed)
    if any(not _valid(row) for _, row in indexed):
        return _empty("The supplied matching measurements contain missing or invalid values.", indexed)
    if len(indexed) > 1:
        instances = [row.get("instance") for _, row in indexed]
        if not aggregate or any(item is None for item in instances) or len(set(instances)) != len(instances):
            return _empty("Duplicate or unidentified aggregate rows cannot be pooled safely.", indexed)
    n = sum(row["n"] for _, row in indexed)
    value = sum(row["value"] * row["n"] for _, row in indexed) / n
    return dict(value=value, display=f"{value:.{decimals}f}", n=n,
                status="measured", boundary=boundary,
                aggregation="event-weighted mean" if len(indexed) > 1 else "supplied mean",
                sources=_provenance(indexed))


def _table2_backend(row, source):
    backend = row.get("backend")
    if (backend == "replay-sleep-subtracted-estimate"
            and row.get("mock_latency_policy") == "recorded"
            and row.get("replay_timing_method") == "recorded-sleep-subtracted"):
        return "replay"
    if backend == "replay-zero-llm":
        return "replay"
    if backend == "replay":
        if source != "fresh" or row.get("replay_timing_method") == "zero-latency-wall":
            return backend
        return None
    return backend if backend in {name for name, _ in TABLE2_SYSTEMS} else None


def table2_manifest(result):
    """Return all 60 cells, including missing cells and excluded input rows."""
    accepted, excluded = [], []
    for index, row in enumerate(result.get("metrics", [])):
        backend = _table2_backend(row, result.get("source"))
        reason = None
        if row.get("metric") not in ("checkpoint_ms", "restore_ms"):
            reason = "This field is not a Table 2 checkpoint/restore mean."
        elif backend is None:
            reason = "Backend/timing policy does not match a paper Table 2 column."
        elif backend == "deltabox" and (
                row.get("mode") not in (None, "fast")
                or row.get("experiment") not in (None, "table-02-deltabox")):
            reason = "Table 2 uses the fast table-02-deltabox experiment; other modes remain separate."
        elif row.get("group") not in {key for key, _ in TABLE2_GROUPS}:
            reason = "Workload group has no corresponding paper row."
        if reason:
            excluded.append(dict(metric_index=index, reason=reason))
        else:
            accepted.append((index, row, backend))
    cells = []
    for group, label in TABLE2_GROUPS:
        for backend, system_label in TABLE2_SYSTEMS:
            population = [(i, r) for i, r, b in accepted if b == backend]
            for operation, field in (("ck", "checkpoint_ms"), ("rs", "restore_ms")):
                matches = [(i, row) for i, row in population
                           if row.get("group") == group and row["metric"] == field]
                # The paper prints DeltaBox to 0.01 ms and baselines to 0.1 ms.
                cell = _measure(matches, decimals=2 if backend == "deltabox" else 1,
                                boundary="Supplied checkpoint/restore mean; see source timing policy.",
                                population=population)
                cells.append(dict(row=group, row_label=label, backend=backend,
                                  system_label=system_label, column=operation, **cell))
    return dict(layout="paper-table-02", systems=[dict(backend=b, label=l) for b, l in TABLE2_SYSTEMS],
                rows=[dict(group=g, label=l) for g, l in TABLE2_GROUPS], cells=cells,
                excluded_metrics=excluded,
                notes=["Each system retains its own supplied event-weighted workload means and Event Avg.",
                       "Missing rows and columns are never filled from another experiment or archived paper values."])


def table3_manifest(result):
    """Map direct timers only; never reconstruct serialized work by subtraction."""
    indexed = [(i, row) for i, row in enumerate(result.get("metrics", []))
               if row.get("backend") == "deltabox"]
    by_mode = {mode: [(i, row) for i, row in indexed if row.get("mode") == mode]
               for mode in ("fast", "slow")}
    # Every entry names one direct timer and its actual measurement boundary.
    mapping = {
        (0, "ck"): ("fast", "checkpoint_overlay_ms", "Checkpoint overlay-switch timer."),
        (0, "rs_fast"): ("fast", "restore_fast_ioctl_ms", "Restore ioctl window; overlaps template fork."),
        (0, "rs_slow"): ("slow", "restore_slow_ioctl_ms", "Direct slow restore overlay-switch timer."),
        (1, "ck"): ("fast", "checkpoint_fork_ms", "Checkpoint stash/template fork timer."),
        (1, "rs_fast"): ("fast", "restore_fast_fork_total_ms", "Post-dispatch fork-response window; overlaps ioctl, not pure fork syscall CPU time."),
        (2, "rs_slow"): ("slow", "restore_slow_criu_ms", "Direct slow restore CRIU timer."),
        (3, "rs_fast"): ("fast", "restore_fast_coordination_ms", "Direct serialized coordination outside overlapping phases."),
        (3, "rs_slow"): ("slow", "restore_slow_coordination_ms", "Direct serialized coordination outside overlapping phases."),
        (4, "ck"): ("fast", "agent_perceived_checkpoint_ms", "Explicit agent-perceived checkpoint blocking measurement; not inferred from async configuration."),
        (4, "rs_fast"): ("fast", "restore_wall_ms", "Full controller API latency plus replay waiting; differs from the paper's internal critical-path timer."),
        (4, "rs_slow"): ("slow", "restore_wall_ms", "Full controller API latency plus replay waiting; differs from the paper's internal critical-path timer."),
    }
    cells, used = [], set()
    for row_number, label in enumerate(TABLE3_ROWS):
        for column in ("ck", "rs_fast", "rs_slow"):
            spec = mapping.get((row_number, column))
            if spec:
                mode, field, boundary = spec
                population = by_mode[mode]
                matches = [(i, row) for i, row in population if row["metric"] == field]
                cell = _measure(matches, boundary=boundary, population=population, aggregate=True)
                used.update(i for i, _ in matches)
                if row_number == 4 and column != "ck":
                    direct = [(i, row) for i, row in population if row['metric'] == 'restore_table3_total_ms']
                    if direct:
                        cell = _measure(direct, boundary="Direct component window after active teardown, through restore bookkeeping; excludes dump joins, teardown and replay-ready RPC.",
                                        population=population, aggregate=True)
                        used.update(i for i, _ in direct)
                    elif cell['status'] == 'measured':
                        cell["annotation"] = "†"
                if row_number == 4 and column == "ck" and not matches:
                    model = [(i, row) for i, row in population if row['metric'] == 'checkpoint_masked_model_ms']
                    if model:
                        cell = _measure(model, boundary="Ideal overlap model: per-event max(0, measured checkpoint API ms - recorded LLM window ms). Replay does not directly measure concurrent inference.",
                                        population=population, aggregate=True)
                        if cell['status'] == 'measured':
                            cell.update(status='derived-model', annotation='‖')
                        used.update(i for i, _ in model)
            elif (row_number, column) == (2, "ck"):
                population = by_mode["fast"]
                # This is a configuration label, not a measured duration or a
                # claim that checkpoint blocking is zero.
                if population and len(_identities(population)) == 1 and all(
                        row.get("checkpoint_profile") in ("async-incremental", "async-incremental-lazy", "historical-async-full") for _, row in population):
                    cell = dict(value=None, display="async", n=None, status="configuration",
                                reason="The supplied fast population explicitly records asynchronous checkpointing; the source profile distinguishes full from incremental dumps.",
                                sources=_provenance(population))
                else:
                    cell = _empty("Async checkpoint configuration is not explicitly established.")
            else:
                cell = dict(value=None, display=MISSING, n=None, status="not-applicable",
                            reason="This operation does not use this component.", sources=[])
            cells.append(dict(row=row_number, row_label=label, column=column, **cell))
    excluded = [dict(metric_index=i, reason=(
        "Checkpoint values from the slow experiment do not supply the common fast checkpoint column."
        if row.get("mode") == "slow" and row.get("metric", "").startswith(("checkpoint_", "ckpt_"))
        else "No semantically matching paper cell; timer is retained only in the analysis."))
        for i, row in enumerate(result.get("metrics", [])) if i not in used]
    return dict(layout="paper-table-03", rows=list(TABLE3_ROWS),
                columns=["ck", "rs_fast", "rs_slow"], cells=cells, excluded_metrics=excluded,
                notes=["Fast and slow modes retain separate populations; per-instance means are event weighted only within one mode and population.",
                       "Coordination is not inferred from dispatch, total duration, fork, or ioctl differences; these intervals may overlap.",
                       "The async label describes configuration. Checkpoint overlap, when supplied, is explicitly a model using measured API cost and the recorded inference window.",
                       "New restore totals directly time the component window, excluding teardown/dump joins/replay-ready RPC; full API and legacy critical timers remain in metrics.",
                       "Daggered restore values use the supplied full API/replay blocking interval, not the paper's internal critical-path interval."])


def _canvas(plt, size):
    fig, ax = plt.subplots(figsize=size)
    ax.set_position((.012, .012, .976, .976))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_axis_off()
    return fig, ax


def _paper_style():
    # Freeze the installed concrete font name. A generic "serif" property is
    # resolved at save time, after rc_context has restored the caller's fonts.
    from matplotlib import font_manager
    installed = {font.name for font in font_manager.fontManager.ttflist}
    family = next(name for name in PAPER_STYLE["font.serif"] if name in installed)
    return dict(PAPER_STYLE, **{"font.family": family})


def _rule(ax, y, left=0, right=1, *, heavy=False):
    ax.plot((left, right), (y, y), color="black", lw=1.2 if heavy else .65,
            solid_capstyle="butt", clip_on=False)


def table2(plt, result):
    manifest = table2_manifest(result)
    style = _paper_style()
    with plt.rc_context(style):
        fig, ax = _canvas(plt, (12, 3.29))
        ax.text(0, .985, r"$\bf{Table\ 2.}$ Per-event mean blocking time (ms) on SWE-bench MCTS trajectories. "
                r"$\it{ck/rs}$ = checkpoint/restore; column naming", va="top", fontsize=16.4)
        ax.text(0, .901, r"follows $\it{process}$-$\it{recovery}$+$\it{FS}$-$\it{recovery}$ (supplied AE measurements).",
                va="top", fontsize=16.4)
        _rule(ax, .790, heavy=True)
        # Numeric columns are separate and right-aligned, matching the paper.
        positions = (.174, .249, .321, .394, .468, .540, .617, .694, .767, .843, .922, .992)
        for index, (backend, label) in enumerate(TABLE2_SYSTEMS):
            left, right = positions[index * 2:index * 2 + 2]
            ax.text((left + right) / 2 - .010, .731, label, ha="center", va="center", fontsize=11.5,
                    fontfamily="monospace" if index < 3 else style["font.family"],
                    fontweight="bold" if backend == "deltabox" else "normal")
            _rule(ax, .683, left - .025, right + .004)
            for operation, x in zip(("ck", "rs"), (left, right)):
                ax.text(x, .632, operation + (r"$^\dagger$" if index == 0 and operation == "ck" else ""),
                        ha="right", va="center", fontsize=11.5,
                        fontweight="bold" if backend == "deltabox" else "normal")
        ax.text(0, .632, "Workload", va="center", fontsize=11.5, weight="bold")
        _rule(ax, .585)
        lookup = {(cell["row"], cell["backend"], cell["column"]): cell for cell in manifest["cells"]}
        for (group, label), y in zip(TABLE2_GROUPS, (.526, .456, .386, .316, .208)):
            ax.text(0, y, label, va="center", fontsize=11.5)
            for index, (backend, _) in enumerate(TABLE2_SYSTEMS):
                for operation, x in zip(("ck", "rs"), positions[index * 2:index * 2 + 2]):
                    cell = lookup[group, backend, operation]
                    ax.text(x, y, cell["display"], ha="right", va="center", fontsize=11.5,
                            weight="bold" if backend == "deltabox" else "normal")
        _rule(ax, .264)
        _rule(ax, .156, heavy=True)
        replay_methods = {row.get("replay_timing_method") for row in result.get("metrics", [])
                          if _table2_backend(row, result.get("source")) == "replay"}
        replay_note = ("rs subtracts recorded LLM wait from measured elapsed time."
                       if replay_methods == {"recorded-sleep-subtracted"}
                       else "rs uses the declared timing policy; see the sample manifest.")
        ax.text(.5, .112, "† Replay ck is the per-trace pristine-repo copy; " + replay_note,
                ha="center", va="center", fontsize=11.5)
        ax.text(.5, .048, "Event Avg uses each backend’s own event counts. — = no matching supplied measurement.",
                ha="center", va="center", fontsize=11.5)
    fig.paper_table_manifest = manifest
    return fig


def table3(plt, result):
    manifest = table3_manifest(result)
    with plt.rc_context(_paper_style()):
        fig, ax = _canvas(plt, (6.5, 4.94))
        profiles = {r.get('checkpoint_profile') for r in result.get('metrics', [])}
        historical = profiles == {'historical-async-full'}
        modern = any(r.get('metric') == 'restore_table3_total_ms' for r in result.get('metrics', []))
        captions = (
            r"$\bf{Table\ 3.}$ DeltaBox per-component C/R latency (ms) over the",
            ("SWE-bench MCTS replay (async full dump; lazy restore)." if historical else
             "SWE-bench MCTS replay (supplied AE measurements)."),
            "The fast path forks the warm template (the common case);",
            "the slow path uses the measured CRIU fallback.",
        )
        for caption, y in zip(captions, (.984, .916, .848, .780)):
            ax.text(0, y, caption, va="top", fontsize=18)
        left, right = .076, .923
        _rule(ax, .704, left, right, heavy=True)
        xs = (.601, .749, .912)
        ax.text(left + .012, .659, "Component", va="center", fontsize=15.5, weight="bold")
        for label, x in zip(("ck", "rs (fast)", "rs (slow)"), xs):
            ax.text(x, .659, label, ha="right", va="center", fontsize=15.5, weight="bold")
        _rule(ax, .615, left, right)
        lookup = {(cell["row"], cell["column"]): cell for cell in manifest["cells"]}
        for row, y in enumerate((.569, .508, .447, .386, .287)):
            label = TABLE3_ROWS[row] + (r"$^\S$" if row == 3 else "")
            if row == 4:
                label = "Component window /\nck overlap model" if modern else "Agent-perceived\nblocking"
            ax.text(left + .012, y, label, va="center", fontsize=15.5,
                    weight="bold" if row == 4 else "normal", linespacing=1.25)
            for column, x in zip(("ck", "rs_fast", "rs_slow"), xs):
                cell = lookup[row, column]
                annotation = {"†": r"$^\dagger$", "‖": r"$^{\parallel}$"}.get(cell.get("annotation"), "")
                ax.text(x, y + (.023 if row == 4 else 0), cell["display"] + annotation,
                        ha="right", va="center", fontsize=15.5, weight="bold" if row == 4 else "normal")
        _rule(ax, .340, left, right)
        _rule(ax, .191, left, right, heavy=True)
        notes = ((
            "§ Measured serialized work outside fork/CRIU windows.",
            r"$\parallel$ Checkpoint: ideal overlap model, not zero API cost.",
            "Restore: component window; full API retained separately.",
        ) if modern else (
            "§ Serialized coordination outside overlapped phases; — = unavailable.",
            "† Restore: full controller API latency + replay waiting;",
            "different from the paper’s internal critical-path timer.",
        ))
        for text, y in zip(notes, (.144, .085, .026)):
            ax.text(.5, y, text, ha="center", va="center", fontsize=14)
    fig.paper_table_manifest = manifest
    return fig
