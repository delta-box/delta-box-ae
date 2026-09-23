"""CPU-only Equation 1 model for Figure 8(c), using explicit timing inputs.

This calculates a time fraction, not device telemetry or a GPU-count-weighted
utilization. No GPU or plotting package is imported during calculation.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
import sys


DEFAULT_BATCHES = (16, 64)
SOURCE_KINDS = ("historical", "fresh", "assumed", "mixed")
BACKEND_ORDER = ("deltabox", "cube", "e2b")
POPULATION_FIELDS = (
    "cohort", "population", "plot_group", "source_identity", "source_summary",
    "input_summary", "experiment", "mode", "checkpoint_profile", "adaptive",
    "memory_policy", "prewarm_requested", "prewarm_mode", "run_purpose",
    "message_policy", "baseline_test_runtime", "mock_latency_policy",
    "replay_timing_method", "legacy_timing_policy", "protocol",
)
FORMULAS = {
    "occupation": "(t_gen_s + t_train_s) / (t_sandbox_s + t_gen_s + t_train_s)",
    "occupation_pct": "100 * occupation",
    "staleness_versions": "(t_sandbox_s + t_gen_s) / t_train_s",
}
ASSUMPTIONS = [
    "Expected occupation is a modeled time fraction in a serialized sandbox/generation/training cycle.",
    "This is not measured nvidia-smi utilization or a measured end-to-end RL job.",
    "GPU-count weighting is not applied; generation and training GPU counts are retained as provenance.",
    "Staleness is the modeled sandbox-plus-generation duration divided by one training-step duration.",
    "Only supplied timings are used; a missing sandbox duration is never replaced with zero.",
]


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value) or value < 0 or positive and value == 0:
        bound = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be finite and {bound}")
    return value


def _batch_list(value):
    if not isinstance(value, list) or not value:
        raise ValueError("batches must be a nonempty list")
    batches = [_positive_int(n, "batch") for n in value]
    if len(set(batches)) != len(batches):
        raise ValueError("Duplicate requested batch")
    return batches


def _backend_list(value):
    if not isinstance(value, list) or not value:
        raise ValueError("backends must be a nonempty list")
    backends = [_text(b, "backend") for b in value]
    if len(set(backends)) != len(backends):
        raise ValueError("Duplicate requested backend")
    return backends


def _header(value, kind):
    _object(value, kind)
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError(f"{kind} requires schema_version 1")
    if value.get("kind") != kind:
        raise ValueError(f"Expected kind {kind}")
    if value.get("source_kind") not in SOURCE_KINDS:
        raise ValueError(f"source_kind must be one of {SOURCE_KINDS}")
    _text(value.get("model_label"), "model_label")


def validate_inputs(inputs):
    """Return an independent normalized input object, rejecting incomplete grids."""
    _header(inputs, "gpu-occupation-inputs")
    try:
        json.dumps(inputs, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Inputs and provenance must be finite JSON values") from exc
    normalized = copy.deepcopy(inputs)
    batches = normalized["batches"] = _batch_list(inputs.get("batches", list(DEFAULT_BATCHES)))
    backends = _backend_list(inputs.get("backends"))
    gpu, sandbox = {}, {}
    for field in ("gpu_timings", "sandbox_timings"):
        if not isinstance(normalized.get(field), list):
            raise ValueError(f"{field} must be a list")
    for row in normalized["gpu_timings"]:
        _object(row, "gpu timing")
        n = _positive_int(row.get("n"), "gpu timing n")
        if n in gpu:
            raise ValueError(f"Duplicate GPU timing for N={n}")
        for field in ("t_gen_s", "t_train_s"):
            row[field] = _number(row.get(field), field, positive=field == "t_train_s")
        for field in ("generation_gpus", "training_gpus"):
            _positive_int(row.get(field), field)
        gpu[n] = row
    for row in normalized["sandbox_timings"]:
        _object(row, "sandbox timing")
        backend = _text(row.get("backend"), "sandbox backend")
        n = _positive_int(row.get("n"), "sandbox timing n")
        if backend not in backends:
            raise ValueError(f"Sandbox backend {backend} is not declared in backends")
        if (backend, n) in sandbox:
            raise ValueError(f"Duplicate sandbox timing for {backend} N={n}")
        row["t_sandbox_s"] = _number(row.get("t_sandbox_s"), "t_sandbox_s")
        if not isinstance(row.get("estimated"), bool):
            raise ValueError("sandbox estimated must be an explicit boolean")
        sandbox[backend, n] = row
    for n in batches:
        if n not in gpu:
            raise ValueError(f"Missing GPU timing for N={n}")
        for backend in backends:
            if (backend, n) not in sandbox:
                raise ValueError(f"Missing sandbox timing for {backend} N={n}")
    return normalized


def calculate_occupation(inputs):
    """Calculate Equation 1, retaining timing values, estimates and their sources."""
    inputs = validate_inputs(inputs)
    gpu = {row["n"]: row for row in inputs["gpu_timings"]}
    sandbox = {(row["backend"], row["n"]): row for row in inputs["sandbox_timings"]}
    rows = []
    for backend in inputs["backends"]:
        for n in inputs["batches"]:
            device, host = gpu[n], sandbox[backend, n]
            s, g, t = host["t_sandbox_s"], device["t_gen_s"], device["t_train_s"]
            # Scaling avoids overflow in sums of individually finite durations.
            scale = max(s, g, t)
            occupation = (g / scale + t / scale) / (s / scale + g / scale + t / scale)
            staleness = s / t + g / t
            if not math.isfinite(staleness):
                raise ValueError(f"staleness_versions is not finite for {backend} N={n}")
            rows.append(dict(
                backend=backend, n=n, t_sandbox_s=s, t_gen_s=g, t_train_s=t,
                generation_gpus=device["generation_gpus"], training_gpus=device["training_gpus"],
                occupation=occupation, occupation_pct=100 * occupation,
                staleness_versions=staleness, estimated=host["estimated"],
                source=dict(gpu=copy.deepcopy(device.get("source")),
                            sandbox=copy.deepcopy(host.get("source"))),
            ))
    return dict(
        schema_version=1, kind="gpu-occupation-model", status="ok", modeled=True,
        source_kind=inputs["source_kind"], model_label=inputs["model_label"],
        batches=list(inputs["batches"]), backends=list(inputs["backends"]),
        formulas=dict(FORMULAS), assumptions=list(ASSUMPTIONS), rows=rows,
        estimated_points=[dict(backend=r["backend"], n=r["n"], component="t_sandbox_s")
                          for r in rows if r["estimated"]],
        inputs=inputs, provenance=copy.deepcopy(inputs.get("provenance", {})),
        notes=copy.deepcopy(inputs.get("notes", [])),
    )


def indexed_gpu_cases(suite, *, required_batches=()):
    """Validate supplied cases; only requested batches must form a complete grid.

    A historical preview may carry a source reference instead of the fresh
    worker-result reference. Missing paper slots remain absent for plotting.
    """
    _header(suite, "gpu-timing-suite")
    if suite.get("status") != "ok":
        raise ValueError("GPU suite status must be ok; failed measurements cannot be modeled")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Missing GPU cases")
    indexed, identifiers = {}, set()
    for case in cases:
        _object(case, "GPU case")
        phase = case.get("phase")
        if phase not in ("generation", "training"):
            raise ValueError("GPU phase must be generation or training")
        n = _positive_int(case.get("batch"), "GPU batch")
        identifier = _text(case.get("case_id"), "case_id")
        if (phase, n) in indexed or identifier in identifiers:
            raise ValueError(f"Duplicate/conflicting GPU case: {phase} B{n}")
        if case.get("status") != "ok":
            raise ValueError(f"GPU case {identifier} did not succeed")
        _positive_int(case.get("num_gpus"), "num_gpus")
        _text(case.get("method"), "method")
        if "model_label" in case and case["model_label"] != suite["model_label"]:
            raise ValueError(f"Conflicting model_label in {identifier}")
        timing = _object(case.get("timing_s"), "timing_s")
        _positive_int(timing.get("n"), "timing_s.n")
        values = {key: _number(timing.get(key), f"timing_s.{key}", positive=True)
                  for key in ("mean", "median", "p95", "min", "max")}
        if not (values["min"] <= values["mean"] <= values["max"]
                and values["min"] <= values["median"] <= values["p95"] <= values["max"]):
            raise ValueError(f"Conflicting timing statistics in {identifier}")
        if "result" in case:
            record = _object(case["result"], "case result")
            _text(record.get("path"), "result.path")
            digest = _text(record.get("sha256"), "result.sha256")
            if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
                raise ValueError("result.sha256 must be a SHA-256 digest")
            _positive_int(record.get("bytes"), "result.bytes")
        elif suite["source_kind"] == "fresh" or not case.get("source"):
            raise ValueError(f"Missing hashed result reference for {identifier}")
        indexed[phase, n] = case
        identifiers.add(identifier)
    for n in required_batches:
        for phase in ("generation", "training"):
            if (phase, n) not in indexed:
                raise ValueError(f"Missing GPU case: {phase} B{n}")
    return indexed


def _fanout_source_kind(summary):
    if "source_kind" in summary:
        value = summary["source_kind"]
    else:
        source = summary.get("source")
        if source is not None:
            _text(source, "fanout source")
        value = {"archived": "historical", "fresh": "fresh"}.get(source, "assumed")
    if value not in SOURCE_KINDS:
        raise ValueError("Invalid fanout summary source_kind")
    return value


def inputs_from_measurements(suite, summary, *, batches=DEFAULT_BATCHES):
    """Join successful GPU means with one AE fanout population per backend.

    AE panel-a durations are in milliseconds. Other panels never contribute.
    Unknown fanout provenance is labeled assumed, so it cannot become fresh.
    """
    batches = _batch_list(list(batches))
    cases = indexed_gpu_cases(suite, required_batches=batches)
    _object(summary, "fanout summary")
    if "schema_version" in summary and (type(summary["schema_version"]) is not int or summary["schema_version"] != 1):
        raise ValueError("fanout summary requires schema_version 1")
    experiments = _object(summary.get("experiments"), "fanout experiments")
    experiment = _object(experiments.get("figure-08"), "figure-08 analysis")
    if experiment.get("status") in ("failed", "unavailable", "error"):
        raise ValueError("Figure 8 fanout analysis did not succeed")
    series = experiment.get("series")
    if not isinstance(series, list):
        raise ValueError("Missing figure-08 series")
    rows, seen, populations, common_populations, backends = [], set(), {}, set(), set()
    for index, row in enumerate(series):
        _object(row, "fanout series row")
        if row.get("panel") != "a":
            continue
        backend = _text(row.get("backend"), "fanout backend")
        if backend not in BACKEND_ORDER:
            raise ValueError(f"Unknown fanout backend: {backend}")
        backends.add(backend)
        n = _positive_int(row.get("x"), "fanout x")
        if (backend, n) in seen:
            raise ValueError(f"Duplicate fanout point for {backend} N={n}")
        seen.add((backend, n))
        if row.get("unit") != "ms":
            raise ValueError("Fanout panel-a unit must be ms")
        duration = _number(row.get("y"), "fanout y") / 1000
        if not isinstance(row.get("estimated"), bool):
            raise ValueError("Fanout estimated must be an explicit boolean")
        if n not in batches:
            continue
        population = json.dumps({key: row.get(key) for key in POPULATION_FIELDS},
                                sort_keys=True, allow_nan=False)
        if backend in populations and populations[backend] != population:
            raise ValueError(f"Conflicting fanout populations for {backend}; select one population")
        populations[backend] = population
        common_populations.add(json.dumps({key: row.get(key) for key in
                                           ("plot_group", "source_identity", "run_purpose")},
                                          sort_keys=True, allow_nan=False))
        rows.append(dict(backend=backend, n=n, t_sandbox_s=duration,
                         estimated=row["estimated"],
                         source=dict(series_index=index, series=copy.deepcopy(row))))
    if not backends:
        raise ValueError("Missing figure-08 panel-a fanout points")
    if len(common_populations) > 1:
        raise ValueError("Conflicting fanout comparison populations; select one plot group")
    gpu_kind, host_kind = suite["source_kind"], _fanout_source_kind(summary)
    source_kind = gpu_kind if gpu_kind == host_kind else "mixed"
    inputs = dict(
        schema_version=1, kind="gpu-occupation-inputs", source_kind=source_kind,
        model_label=suite["model_label"], batches=batches,
        backends=[b for b in BACKEND_ORDER if b in backends],
        gpu_timings=[dict(
            n=n, t_gen_s=cases["generation", n]["timing_s"]["mean"],
            t_train_s=cases["training", n]["timing_s"]["mean"],
            generation_gpus=cases["generation", n]["num_gpus"],
            training_gpus=cases["training", n]["num_gpus"],
            source={phase: copy.deepcopy(cases[phase, n]) for phase in ("generation", "training")},
        ) for n in batches],
        sandbox_timings=rows,
        provenance=dict(
            gpu_suite=dict(source_kind=gpu_kind, protocol=copy.deepcopy(suite.get("protocol", {}))),
            fanout_summary=dict(source_kind=host_kind, source=summary.get("source"),
                                selection=copy.deepcopy(experiment.get("selection", {}))),
        ),
        notes=["GPU phase durations use timing_s.mean; fanout panel-a y is converted from ms to s.",
               "Individual case and series records are retained in each timing row's source.",
               "An unlabeled fanout source is treated as assumed input."],
    )
    return validate_inputs(inputs)


def _read_json(path, role):
    path = Path(path)
    content = path.read_bytes()
    value = json.loads(content)
    return value, dict(role=role, path=str(path.resolve()),
                       sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="Explicit gpu-occupation-inputs JSON")
    mode.add_argument("--gpu-results", type=Path, help="Successful gpu-timing-suite JSON")
    parser.add_argument("--fanout-summary", type=Path, help="AE analysis JSON containing figure-08 panel a")
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--plot", action="store_true", help="Also render PNG/PDF with optional matplotlib")
    args = parser.parse_args(argv)
    if bool(args.gpu_results) != bool(args.fanout_summary):
        parser.error("--gpu-results and --fanout-summary must be supplied together")
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError(f"Output already exists: {args.output}")
        if args.input:
            inputs, reference = _read_json(args.input, "normalized-input")
            references = [reference]
        else:
            suite, gpu_reference = _read_json(args.gpu_results, "gpu-suite")
            summary, fanout_reference = _read_json(args.fanout_summary, "fanout-summary")
            inputs = inputs_from_measurements(suite, summary)
            references = [gpu_reference, fanout_reference]
        result = calculate_occupation(inputs)
        result["input_files"] = references
        # Validate serialization as well as numbers before claiming an output path.
        json.dumps(result, allow_nan=False)
        if args.plot:
            if __package__ in (None, ""):
                sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from ae.repro.figure08_plots import _pyplot, plot_gpu_occupation
            _pyplot()  # Check the optional dependency before claiming the output directory.
        args.output.mkdir(parents=True, exist_ok=False)
        if args.plot:
            result["plots"] = plot_gpu_occupation(result, args.output / "plots")
        (args.output / "occupation.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        fields = ["backend", "n", "t_sandbox_s", "t_gen_s", "t_train_s", "generation_gpus",
                  "training_gpus", "occupation", "occupation_pct", "staleness_versions", "estimated",
                  "source_kind", "model_label"]
        with (args.output / "metrics.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(dict(row, source_kind=result["source_kind"], model_label=result["model_label"])
                             for row in result["rows"])
        print(f"Wrote CPU occupation model to {args.output / 'occupation.json'}")
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
