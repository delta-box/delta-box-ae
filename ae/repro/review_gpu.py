"""Figure 8 GPU stages in the one-click evaluator; CPU-only import and planning."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import gpu_protocol
from .common import configured_path, file_record, write_json
from .gpu_occupation import indexed_gpu_cases, inputs_from_measurements

GPU = "figure-08-gpu"
THEORY = "figure-08-theory"
FANOUT = ("figure-08-deltabox", "figure-08-cube", "figure-08-e2b")
RESOURCE_HELP = (
    "GPU resources/configuration are unavailable or busy. Contact the authors to allocate "
    "GPU resources for the AE machine. / GPU 资源或配置不可用，或资源繁忙；"
    "请联系作者为 AE 机器分配 GPU 资源。"
)


def _load(path):
    return json.loads(Path(path).read_text())


def _verified(record):
    path = Path(record["path"])
    if file_record(path) != record:
        raise ValueError("Changed GPU artifact: " + str(path))
    return path


def verify_suite(path, release, batches):
    """Check case bytes and producer identities before reuse, plotting or modeling."""
    suite = _load(path)
    if suite.get("source_kind") != "fresh" or suite.get("release") != release:
        raise ValueError("GPU suite has a different source identity")
    cases = indexed_gpu_cases(suite, required_batches=batches)
    if set(cases) != {(phase, batch) for phase in gpu_protocol.PHASES for batch in batches}:
        raise ValueError("GPU suite does not match the selected matrix")
    root = Path(path).parent
    config_path = root / "config.json"
    config_sha = file_record(config_path)["sha256"]
    if config_sha != suite["config_sha256"] or _load(config_path) != suite["protocol"]:
        raise ValueError("GPU effective configuration changed")
    for case in cases.values():
        record = case["result"]
        result_path = (root / record["path"]).resolve()
        if not result_path.is_relative_to(root.resolve()):
            raise ValueError("GPU result path escapes its suite")
        result_record = file_record(result_path)
        if any(result_record[key] != record[key] for key in ("sha256", "bytes")):
            raise ValueError("GPU worker result changed")
        raw = _load(result_path)
        timing = gpu_protocol.validate_result(raw, gpu_protocol.case_by_id(suite["protocol"], case["case_id"]),
                                              config_sha, suite["protocol"]["devices"][:case["num_gpus"]])
        if timing != case["timing_s"]:
            raise ValueError("GPU aggregate differs from the worker result")
        for field in ("worker_source", "protocol_source"):
            if raw[field + "_sha256"] != suite[field]["sha256"]:
                raise ValueError("GPU worker source differs")
    return suite


def run_gpu(review):
    row = dict(experiment=GPU, status="checking", planned_jobs=8, successful_jobs=0, reasons=[])
    review.record["coverage"].append(row)
    setting = review.config.get("gpu", {})
    if not isinstance(setting, dict):
        raise ValueError("gpu must contain a config path. " + RESOURCE_HELP)
    if setting.get("enabled") is False:
        raise ValueError("The host GPU configuration is disabled. " + RESOURCE_HELP)
    path = configured_path(review.config, "gpu.config", required=False) or gpu_protocol.DEFAULT_CONFIG
    config = gpu_protocol.load_config(path, smoke=bool(review.limits))
    if config["allow_busy"]:
        raise ValueError("One-click performance evaluation requires idle GPUs")
    expected_batches = [1] if review.limits else list(gpu_protocol.BATCHES)
    if config["batches"] != expected_batches or set(config["phases"]) != set(gpu_protocol.PHASES):
        raise ValueError("One-click GPU evaluation requires the complete generation/training matrix")
    signature = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    cases = gpu_protocol.cases(config)
    row.update(planned_jobs=len(cases), config_sha256=signature, config_source=file_record(path))
    config_path = review.output / "configs" / review.attempt / (GPU + ".json")
    write_json(config_path, config)
    row["effective_config"] = file_record(config_path)
    old = next((item for item in review.previous_record.get("coverage", [])
                if item["experiment"] == GPU and item["status"] == "ok"), None)
    if old:
        if old.get("config_sha256") != signature:
            raise ValueError("Resume GPU configuration differs; start a new output")
        summary = _verified(old["summary"])
        suite = verify_suite(summary, review.record["release"], expected_batches)
        if gpu_protocol.model_identity(config["model_path"])["sha256"] != suite["model_identity"]["sha256"]:
            raise ValueError("Resume GPU model files changed")
        row.update(status="ok", summary=old["summary"], reused_verified=True,
                   successful_jobs=len(cases), available_jobs=len(cases))
        review.save()
        return
    directory = review.output / "gpu" / review.attempt
    directory.mkdir(parents=True, exist_ok=True)
    cli = [review.python, str(Path(__file__).resolve().parents[1] / "runners/gpu_timing.py")]
    preflight = directory / "preflight.json"
    if not review.step(GPU + "-check", [*cli, "check", "--config", str(config_path),
                                       "--output", str(preflight)], 180):
        row.update(status="failed", reasons=[RESOURCE_HELP, "See the GPU preflight log."])
        if preflight.is_file():
            row["preflight"] = file_record(preflight)
        review.save()
        print(RESOURCE_HELP, flush=True)
        return
    if not preflight.is_file() or not _load(preflight).get("ok"):
        raise ValueError("GPU preflight did not produce a successful resource record. " + RESOURCE_HELP)
    row.update(status="running", available_jobs=len(cases), preflight=file_record(preflight))
    output = directory / "measurements"
    # Individual workers enforce per-case deadlines; include model hashing/loading margin.
    budget = sum(config["timeout_s"] for _ in cases) + 3600
    if not review.step(GPU + "-run", [*cli, "run", "--config", str(config_path), "--output", str(output)],
                       budget, termination_grace=120):
        row.update(status="failed", reasons=["GPU generation/training failed; inspect the GPU run log. " + RESOURCE_HELP])
        return
    summary = output / "summary.json"
    verify_suite(summary, review.record["release"], expected_batches)
    row.update(status="ok", successful_jobs=len(cases), summary=file_record(summary))
    review.save()


def finish_gpu(review, analysis_dir, analyzed):
    """Render GPU timings and derive (c) solely from this run's CPU/GPU results."""
    row = next((item for item in review.record["coverage"] if item["experiment"] == GPU), None)
    if row is None:
        return None
    output = review.output / "gpu" / review.attempt
    output.mkdir(parents=True, exist_ok=True)
    panels = []
    gpu_panel = dict(experiment=GPU, title="Figure 8(b)", status=row["status"],
                     reasons=list(row.get("reasons", [])), artifacts=[])
    panels.append(gpu_panel)
    # Reanalysis does not execute GPU work and retains the original measured identity.
    summary = None
    if row["status"] == "ok":
        try:
            summary = _verified(row["summary"])
            suite = _load(summary)
            verify_suite(summary, review.record["release"], suite["protocol"]["batches"])
            plot = output / "plots"
            cli = Path(__file__).resolve().parents[1] / "runners/gpu_timing.py"
            if review.step(GPU + "-plot", [review.python, str(cli), "plot", "--input", str(summary),
                                           "--output", str(plot)], 300):
                gpu_panel["artifacts"] = [file_record(plot / ("figure-08b." + ext)) for ext in ("png", "pdf")]
                gpu_panel["input"] = row["summary"]
            else:
                gpu_panel.update(status="failed", reasons=["GPU plotting failed; see the plot log."])
        except (ValueError, OSError, KeyError) as error:
            gpu_panel.update(status="failed", reasons=[str(error)])
            row.update(status="failed", reasons=[str(error)])
            raise ValueError("Cannot render invalid GPU artifacts: " + str(error)) from error
    selected = set(review.record["experiments"])
    # Figure 8(c) is derived for the full fan-out+GPU selection, not a GPU-only run.
    if (set(FANOUT).issubset(selected) and not review.limits
            and review.record.get("measurement_run_purpose") != "smoke"):
        theory = dict(experiment=THEORY, status="failed", planned_jobs=1, successful_jobs=0, reasons=[])
        review.record["coverage"] = [item for item in review.record["coverage"] if item["experiment"] != THEORY]
        review.record["coverage"].append(theory)
        panel = dict(experiment=THEORY, title="Figure 8(c)", status="failed", reasons=[], artifacts=[])
        panels.append(panel)
        ready = summary is not None and analyzed and all(
            any(item["experiment"] == name and item["status"] == "ok" for item in review.record["coverage"])
            for name in FANOUT)
        if not ready:
            reason = "Figure 8(c) requires successful GPU timings and all three CPU fan-out measurements."
            theory["reasons"] = panel["reasons"] = [reason]
        else:
            inputs = inputs_from_measurements(_load(summary), _load(analysis_dir / "summary.json"))
            if (set(inputs["backends"]) != {"deltabox", "cube", "e2b"} or inputs["source_kind"] != "fresh"
                    or any(item["estimated"] for item in inputs["sandbox_timings"])):
                raise ValueError("Figure 8(c) requires fresh measured fan-out for all three backends")
            destination = output / "theory"
            cli = Path(__file__).with_name("gpu_occupation.py")
            ok = review.step(THEORY, [review.python, str(cli), "--gpu-results", str(summary),
                                      "--fanout-summary", str(analysis_dir / "summary.json"),
                                      "--output", str(destination), "--plot"], 300)
            if ok:
                theory.update(status="ok", successful_jobs=1, available_jobs=1)
                panel.update(status="ok", input=file_record(destination / "occupation.json"),
                             artifacts=[file_record(destination / "plots" / ("figure-08c." + ext))
                                        for ext in ("png", "pdf")])
            else:
                theory["reasons"] = panel["reasons"] = ["Figure 8(c) calculation failed; see its log."]
    metadata = output / "comparison.json"
    write_json(metadata, dict(schema_version=1, release=review.record['release'], panels=panels))
    review.record["outputs"]["gpu"] = str(output)
    return metadata
