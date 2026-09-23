"""Shared host process lifecycle and error reporting for Table 4."""

from __future__ import annotations

import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def report_stderr(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def record_host_error(output: Path, phase: str, error: BaseException) -> None:
    message = str(error) or type(error).__name__
    report_stderr(f"[host] {phase}: {message}")
    try:
        path = output / "host_errors.json"
        rows = json.loads(path.read_text()) if path.exists() else []
        rows.append({"phase": phase, "type": type(error).__name__, "error": message})
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(rows, indent=2) + "\n")
        temporary.replace(path)
    except Exception as reporting_error:
        report_stderr(f"[host] cannot record {phase}: {reporting_error}")


def cleanup_actions(output: Path, actions: list[tuple[str, Callable, bool]],
                    primary: BaseException | None = None) -> None:
    failure = None
    for phase, action, required in actions:
        try:
            action()
        except Exception as error:
            record_host_error(output, phase, error)
            if required and failure is None:
                failure = error
    if primary is None and failure is not None:
        raise failure


@dataclass(frozen=True)
class Lane:
    index: int
    cpus: str | None = None
    memory_node: int | None = None

    @property
    def binding(self) -> dict | None:
        if self.cpus is None:
            return None
        return {"cpus": self.cpus, "memory_node": self.memory_node}


@dataclass
class InstanceRun:
    config_path: Path
    config: dict

    @property
    def output_dir(self) -> Path:
        return self.config_path.parent

    @property
    def schedule_path(self) -> Path:
        return Path(self.config["schedule"])

    @property
    def results_path(self) -> Path:
        return self.output_dir / f"{self.config['instance']}.results.jsonl"

    def save(self) -> None:
        write_json(self.config_path, self.config)

    def mark(self, status: str, error: BaseException | None = None) -> None:
        self.config["status"] = status
        if error is not None:
            self.config["error"] = str(error) or type(error).__name__
        self.save()


@dataclass
class RunningJob:
    spec: InstanceRun
    lane: Lane
    process: subprocess.Popen
    logfile: TextIO


def build_instance_command(spec: InstanceRun, lane: Lane) -> list[str]:
    runner = Path(__file__).with_name("run_instance.py")
    command = ["unshare", "--mount", "--net", "--propagation", "private",
               sys.executable, str(runner), "--_run-config", str(spec.config_path)]
    if lane.cpus is not None:
        command = ["numactl", f"--physcpubind={lane.cpus}",
                   f"--membind={lane.memory_node}"] + command
    return command


def launch_on_lane(spec: InstanceRun, lane: Lane, resources=None) -> RunningJob:
    logfile = None
    try:
        spec.config.update(status="preparing", lane=lane.index, host_binding=lane.binding)
        if resources is not None:
            resources.apply(spec.config, lane.index)
        command = build_instance_command(spec, lane)
        spec.config["host_command"] = command
        spec.mark("running")
        print(f"[{spec.config['mode']}] lane={lane.index} {spec.config['instance']}", flush=True)
        logfile = (spec.output_dir / "runner.log").open("w")
        process = subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        return RunningJob(spec, lane, process, logfile)
    except BaseException as error:
        actions = [("record launch failure", lambda: spec.mark("failed", error), True)]
        if logfile is not None:
            actions.insert(0, ("close runner log", logfile.close, True))
        cleanup_actions(spec.output_dir, actions, error)
        raise


def kill_group(job: RunningJob) -> None:
    try:
        os.killpg(job.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    job.process.wait()


def finish_job(job: RunningJob) -> bool:
    returncode = job.process.poll()
    if returncode is None:
        return False
    error = None
    if returncode:
        error = RuntimeError(
            f"{job.spec.config['instance']} {job.spec.config['mode']} failed "
            f"(rc={returncode}); see {job.spec.output_dir / 'runner.log'}")
    job.spec.config["returncode"] = returncode
    try:
        cleanup_actions(job.spec.output_dir, [
            ("reap instance process group", lambda: kill_group(job), True),
            ("close runner log", job.logfile.close, True),
        ], error)
        if error is not None:
            raise error
        raw = job.spec.results_path.read_bytes()
        job.spec.config["artifacts"] = [{"path": job.spec.results_path.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}]
        if job.spec.config.get("schedule_artifact"):
            local = job.spec.output_dir / job.spec.config['schedule_artifact']
            raw = local.read_bytes()
            job.spec.config['artifacts'].append({'path':local.name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
        storage = job.spec.output_dir / "storage.json"
        if storage.is_file():
            raw = storage.read_bytes()
            job.spec.config['artifacts'].append({'path': storage.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
        job.spec.mark("ok")
    except BaseException as failure:
        cleanup_actions(job.spec.output_dir, [
            ("record instance failure", lambda: job.spec.mark("failed", failure), True),
        ], failure)
        raise
    return True


def cancel_jobs(jobs: list[RunningJob], primary: BaseException) -> None:
    for job in jobs:
        def terminate(job=job):
            if job.process.poll() is None:
                job.process.terminate()
        cleanup_actions(job.spec.output_dir, [("terminate instance", terminate, True)], primary)
    deadline = time.monotonic() + 120
    try:
        while any(job.process.poll() is None for job in jobs):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    except Exception as error:
        for job in jobs:
            record_host_error(job.spec.output_dir, "wait for cancelled instances", error)
    finally:
        for job in jobs:
            actions = [
                ("reap instance process group", lambda job=job: kill_group(job), True),
                ("close runner log", job.logfile.close, True),
            ]
            if job.spec.config.get("status") not in ("ok", "failed"):
                actions.append(("record cancellation",
                                lambda job=job: job.spec.mark("cancelled", primary), True))
            cleanup_actions(job.spec.output_dir, actions, primary)


def execute_instance(spec: InstanceRun) -> None:
    job = launch_on_lane(spec, Lane(0))
    try:
        job.process.wait()
        finish_job(job)
    except BaseException as error:
        cancel_jobs([job], error)
        raise
