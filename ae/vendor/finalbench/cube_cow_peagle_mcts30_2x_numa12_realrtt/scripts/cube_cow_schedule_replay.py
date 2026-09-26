#!/usr/bin/env python3
"""Schedule-driven P-EAGLE replay on CubeSandbox cube-cow rollback.

This mirrors the DeltaBox 62 P-EAGLE real-RTT experiment at the controller
level: it consumes the same manifest and schedules, sleeps the recorded LLM
RTT, runs the recorded worker_ops against the live repository, and replaces
DeltaBox checkpoint/restore with CubeSandbox create_snapshot/rollback.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import signal
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
E2B_REF = BASE.parent / "e2b_peagle_mcts30_2x_numa12_lw_realrtt"
PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", E2B_REF / "payload"))
SOURCE_REPOS = PAYLOAD / "repos"
SOURCE_TRACES = PAYLOAD / "det_traces" / "ms"
CUBE_SDK = Path(os.environ.get("CUBE_SDK_PATH", "/mnt/disk2/dyp/cubesandbox/sdk/python"))

if str(CUBE_SDK) not in sys.path:
    sys.path.insert(0, str(CUBE_SDK))

from cubesandbox import Sandbox  # noqa: E402


RETRYABLE_ERROR_MARKERS = (
    "invalid connection",
    "connection reset",
    "connection refused",
    "connection aborted",
    "broken pipe",
    "server disconnected",
    "already in progress",
    "timed out",
    "timeout",
    "already in progress",
    "active snapshot operation",
    "duplicate entry",
)


def q(s: str) -> str:
    import shlex

    return shlex.quote(str(s))


def run(cmd: list[str], *, timeout: float = 120.0, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(path)


def _skip_git_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if ".git" in info.name.split("/"):
        return None
    return info


def add_tree(tar: tarfile.TarFile, path: Path, arcname: str, *, skip_git: bool = False) -> None:
    src = path.resolve() if path.is_symlink() else path
    tar.add(src, arcname=arcname, filter=_skip_git_filter if skip_git else None)


def make_repo_payload_tar(instance: str, out: Path) -> None:
    repo = SOURCE_REPOS / f"swe-bench_{instance}"
    if not repo.exists():
        raise FileNotFoundError(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    with tarfile.open(tmp, "w") as tar:
        add_tree(tar, repo, "repo", skip_git=True)
    tmp.replace(out)


def make_shared_payload_tar(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    with tarfile.open(tmp, "w") as tar:
        tar.add(BASE / "e2b_slim_action_runner.py", arcname="finalbench/e2b_slim_action_runner.py")
        tar.add(BASE / "e2b_slim_action_runner_worker_ops.py", arcname="finalbench/e2b_slim_action_runner_worker_ops.py")
        tar.add(BASE / "e2b_slim_action_worker.py", arcname="finalbench/e2b_slim_action_worker.py")
        add_tree(tar, PAYLOAD / "moatless-det-src", arcname="spr_payload/moatless-det-src")
    tmp.replace(out)


def upload_file_chunked(sb: Sandbox, local: Path, remote: str, *, chunk_mb: int = 8) -> dict[str, Any]:
    size = local.stat().st_size
    h = hashlib.sha256()
    chunks = 0
    t0 = time.perf_counter()
    sb.commands.run(f"rm -f {q(remote)} {q(remote)}.part.* && : > {q(remote)}", timeout=60)
    with local.open("rb") as f:
        while True:
            data = f.read(chunk_mb << 20)
            if not data:
                break
            h.update(data)
            part = f"{remote}.part.{chunks:04d}"
            sb.files.write(part, data)
            r = sb.commands.run(f"cat {q(part)} >> {q(remote)} && rm -f {q(part)}", timeout=120)
            if r.exit_code != 0:
                raise RuntimeError(f"append remote chunk failed rc={r.exit_code}: {r.stderr[-1000:]}")
            chunks += 1
    digest = h.hexdigest()
    r = sb.commands.run(f"sha256sum {q(remote)} | cut -d' ' -f1 && wc -c < {q(remote)}", timeout=120)
    got = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    if r.exit_code != 0 or len(got) < 2 or got[0] != digest or int(got[1]) != size:
        raise RuntimeError(
            f"remote upload verification failed rc={r.exit_code} got={got!r} "
            f"want_sha={digest} want_size={size} stderr={r.stderr[-1000:]}"
        )
    return {
        "local": str(local),
        "remote": remote,
        "size_bytes": size,
        "sha256": digest,
        "chunks": chunks,
        "upload_wall_ms": (time.perf_counter() - t0) * 1000.0,
    }


def cube_run(sb: Sandbox, cmd: str, *, timeout: float = 300.0, cwd: str | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        res = sb.commands.run(cmd, timeout=timeout, cwd=cwd)
        return {
            "ok": res.exit_code == 0,
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "command_wall_ms": (time.perf_counter() - t0) * 1000.0,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": f"{type(e).__name__}: {e}",
            "command_wall_ms": (time.perf_counter() - t0) * 1000.0,
        }


def load_schedule(schedule: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in schedule.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_recorded_action_outputs(instance: str) -> dict[tuple[int, int], dict[str, Any]]:
    data = read_json(SOURCE_TRACES / instance / "trajectory.json")
    out: dict[tuple[int, int], dict[str, Any]] = {}
    stack = [data.get("root") or {}]
    while stack:
        node = stack.pop()
        node_id = node.get("node_id")
        if isinstance(node_id, int):
            file_context = node.get("file_context")
            for idx, step in enumerate(node.get("action_steps") or []):
                action = step.get("action")
                observation = step.get("observation")
                if observation is None:
                    observation = node.get("output")
                if observation is None and action is None:
                    continue
                out[(node_id, idx)] = {
                    "action": action,
                    "observation": observation,
                    "file_context": file_context,
                    "completion": step.get("completion"),
                }
        stack.extend(reversed(node.get("children") or []))
    return out


def default_file_context() -> dict[str, Any]:
    return {"max_tokens": 8000, "files": [], "test_files": []}


def make_schedule_action_request(
    *,
    instance: str,
    seq: int,
    node_id: int,
    action_step_idx: int,
    recorded: dict[str, Any] | None,
    worker_ops: list[dict[str, Any]],
    materialize_file_context: bool,
) -> dict[str, Any]:
    if not recorded or not recorded.get("action"):
        raise RuntimeError(f"missing recorded action for node={node_id} action_step_idx={action_step_idx}")
    req = {
        "instance": instance,
        "repo_path": "/workspace/repo",
        "index_url": "cube-cow-no-sidecar",
        "mock_base_url": "schedule-driven-no-llm-calls",
        "seq": seq,
        "node_id": node_id,
        "action": recorded["action"],
        "file_context": recorded.get("file_context") or default_file_context(),
        "materialize_file_context": materialize_file_context,
        "worker_ops": worker_ops,
    }
    if recorded.get("observation") is not None:
        req["recorded_observation"] = recorded["observation"]
    if recorded.get("file_context") is not None:
        req["recorded_file_context"] = recorded["file_context"]
    if recorded.get("completion") is not None:
        req["recorded_completion"] = recorded["completion"]
    return req


def prepare_sandbox(
    *,
    sb: Sandbox,
    instance: str,
    work: Path,
    warm_action_worker: bool,
    materialize_file_context: bool,
    chunk_mb: int,
) -> dict[str, Any]:
    shared_tar = BASE / "work" / "shared_payload.tar"
    if not shared_tar.exists():
        make_shared_payload_tar(shared_tar)
    repo_tar = work / f"{instance}.repo_payload.tar"
    if not repo_tar.exists():
        make_repo_payload_tar(instance, repo_tar)

    upload_shared = upload_file_chunked(sb, shared_tar, "/tmp/finalbench_payload.tar", chunk_mb=chunk_mb)
    upload_repo = upload_file_chunked(sb, repo_tar, "/tmp/finalbench_repo.tar", chunk_mb=chunk_mb)
    setup_cmd = (
        "set -euo pipefail; "
        "rm -rf /opt/finalbench /opt/spr_payload /workspace/repo /tmp/finalbench; "
        "mkdir -p /opt /workspace /mnt/disk2/dyp /tmp/finalbench; "
        "tar -xf /tmp/finalbench_payload.tar -C /opt; "
        "tar -xf /tmp/finalbench_repo.tar -C /workspace; "
        "rm -f /tmp/finalbench_payload.tar /tmp/finalbench_repo.tar; "
        "ln -sfn /opt/finalbench /mnt/disk2/dyp/finalbench; "
        "test -f /opt/finalbench/e2b_slim_action_runner.py; "
        "test -f /opt/finalbench/e2b_slim_action_worker.py; "
        "test -d /workspace/repo; "
        "python3 --version"
    )
    setup = cube_run(sb, setup_cmd, timeout=600)
    if not setup["ok"]:
        raise RuntimeError(f"sandbox setup failed: {setup['stderr'][-2000:]}")
    worker = None
    if warm_action_worker:
        env = action_runner_env(materialize_file_context)
        worker_cmd = (
            "exec env "
            + env
            + " python3 /opt/finalbench/e2b_slim_action_worker.py "
            "--mode fifo "
            "--fifo /tmp/finalbench/action_worker.in "
            "--response /tmp/finalbench/action.resp.json "
            "--ready /tmp/finalbench/action_worker.ready "
            "> /tmp/finalbench/action_worker.log 2>&1 < /dev/null"
        )
        start_cmd = (
            "set -euo pipefail; "
            "rm -f /tmp/finalbench/action_worker.in /tmp/finalbench/action_worker.ready "
            "/tmp/finalbench/action.resp.json /tmp/finalbench/action.resp.json.tmp; "
            "setsid sh -c "
            + q(worker_cmd)
            + " & "
            "for i in $(seq 1 300); do "
            "[ -p /tmp/finalbench/action_worker.in ] && [ -f /tmp/finalbench/action_worker.ready ] && exit 0; "
            "sleep 0.1; done; "
            "cat /tmp/finalbench/action_worker.log >&2 || true; exit 3"
        )
        worker = cube_run(sb, start_cmd, timeout=120)
        if not worker["ok"]:
            raise RuntimeError(f"warm worker setup failed: {worker['stderr'][-4000:]}")
    return {
        "shared_upload": upload_shared,
        "repo_upload": upload_repo,
        "setup": tail_record(setup),
        "warm_worker": tail_record(worker) if worker else None,
    }


def action_runner_env(materialize_file_context: bool) -> str:
    return (
        "PYTHONPATH=/opt/finalbench:/opt/spr_payload:/opt/spr_payload/moatless-det-src "
        "DELTABOX_SLIM_SHIMS=1 "
        "E2B_FINALBENCH_BASE=/opt/finalbench "
        "SPR_PAYLOAD=/opt/spr_payload "
        "PYTHONHASHSEED=0 OPENAI_API_KEY=dummy CUSTOM_LLM_API_KEY=dummy LITELLM_LOG=ERROR "
        f"E2B_MATERIALIZE_FILE_CONTEXT={'1' if materialize_file_context else '0'} "
        "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MALLOC_ARENA_MAX=1 "
    )


def schedule_action_command(materialize_file_context: bool, warm_action_worker: bool) -> str:
    prepare = "mkdir -p /tmp/finalbench && mv /tmp/action.req.json /tmp/finalbench/action.req.json"
    if warm_action_worker:
        run_action = (
            "rm -f /tmp/finalbench/action.resp.json /tmp/finalbench/action.resp.json.tmp && "
            "test -p /tmp/finalbench/action_worker.in || "
            "{ echo '[warm-client] missing worker fifo' >&2; "
            "cat /tmp/finalbench/action_worker.log >&2 || true; exit 4; }; "
            "timeout 30 sh -c 'cat /tmp/finalbench/action.req.json > /tmp/finalbench/action_worker.in' || "
            "{ echo '[warm-client] failed to write request fifo' >&2; "
            "cat /tmp/finalbench/action_worker.log >&2 || true; exit 5; }; "
            "for i in $(seq 1 30000); do "
            "[ -s /tmp/finalbench/action.resp.json ] && break; "
            "sleep 0.01; done; "
            "[ -s /tmp/finalbench/action.resp.json ] || "
            "{ echo '[warm-client] timed out waiting for worker response' >&2; "
            "cat /tmp/finalbench/action_worker.log >&2 || true; exit 6; }"
        )
    else:
        run_action = (
            action_runner_env(materialize_file_context)
            + " python3 /opt/finalbench/e2b_slim_action_runner.py "
            "/tmp/finalbench/action.req.json /tmp/finalbench/action.resp.json"
        )
    return "set -euo pipefail; " + prepare + "; " + run_action


def tail_record(obj: dict[str, Any] | None, *, limit: int = 4000) -> dict[str, Any] | None:
    if obj is None:
        return None
    out = dict(obj)
    for key in ("stdout", "stderr"):
        if isinstance(out.get(key), str) and len(out[key]) > limit:
            out[key] = out[key][-limit:]
    return out


def is_retryable_connection_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in RETRYABLE_ERROR_MARKERS)


def retry_delay(exc: BaseException, attempt: int) -> float:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "already in progress" in text or "active snapshot operation" in text:
        return min(10.0, 1.0 + attempt * 0.75)
    if "duplicate entry" in text:
        return min(10.0, 2.0 + attempt)
    return 1.0


def reconnect_sandbox(sb: Sandbox) -> Sandbox:
    sandbox_id = sb.sandbox_id
    try:
        sb.close()
    except Exception:
        pass
    time.sleep(1.0)
    return Sandbox.connect(sandbox_id)


def post_api_settle() -> None:
    settle_ms = float(os.environ.get("CUBE_POST_API_SETTLE_MS", "200"))
    if settle_ms > 0:
        time.sleep(settle_ms / 1000.0)


def cube_run_retry(sb: Sandbox, cmd: str, *, timeout: float = 300.0, cwd: str | None = None) -> tuple[Sandbox, dict[str, Any]]:
    res = cube_run(sb, cmd, timeout=timeout, cwd=cwd)
    if res["ok"] or not any(marker in str(res.get("stderr", "")).lower() for marker in RETRYABLE_ERROR_MARKERS):
        return sb, res
    sb = reconnect_sandbox(sb)
    res = cube_run(sb, cmd, timeout=timeout, cwd=cwd)
    return sb, res


def encode_action_request(request: dict[str, Any]) -> str:
    """Frame one FIFO request as JSONL without relying on the writer closing."""
    return json.dumps(request, separators=(",", ":")) + "\n"


def capture_action_failure(sb: Sandbox, result_dir: Path, ev_i: int,
                           request: str, action: dict[str, Any],
                           response: dict[str, Any] | None) -> dict[str, Any]:
    """Collect bounded, read-only diagnostics before teardown; never retry an action."""
    directory = result_dir / "action-failures" / f"event-{ev_i:04d}"
    record: dict[str, Any] = {"event": ev_i, "directory": str(directory)}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "request.json").write_text(request, encoding="utf-8")
        write_json(directory / "failure.json", {"action": action, "response": response})
        env = dict(os.environ, CUBE_SDK_PATH=str(CUBE_SDK))
        completed = subprocess.run(
            [sys.executable, str(BASE / "scripts" / "cube_action_failure_probe.py"),
             "--sandbox-id", sb.sandbox_id, "--output", str(directory / "probe.json")],
            env=env, text=True, capture_output=True, timeout=20, check=False,
        )
        record.update(probe_exit_code=completed.returncode,
                      probe_stderr=completed.stderr[-4000:])
    except subprocess.TimeoutExpired:
        record["probe_error"] = "diagnostic subprocess exceeded 20 seconds; partial evidence retained"
    except Exception as error:  # diagnostics must not replace the action failure
        record["probe_error"] = f"{type(error).__name__}: {error}"
    return record


def write_action_request(sb: Sandbox, content: str) -> tuple[Sandbox, dict[str, Any]]:
    """Recover a stale file connection without ever re-executing an action.

    A rollback can invalidate the transport while the sandbox itself is ready.
    Replacing this request file is idempotent; the worker consumes it only in the
    later command. Verify the complete upload before allowing that command.
    """
    errors = []
    for attempt in range(3):
        try:
            sb.files.write("/tmp/action.req.json", content)
            if sb.files.read("/tmp/action.req.json") != content:
                raise ValueError("Action request read-back differs from uploaded bytes")
            return sb, {"attempts": attempt + 1, "retry_errors": errors,
                        "verified_sha256": hashlib.sha256(content.encode()).hexdigest()}
        except Exception as exc:
            text = str(exc).lower()
            transient = is_retryable_connection_error(exc) or "504 gateway time-out" in text or "502 bad gateway" in text
            if not transient or attempt == 2:
                raise
            errors.append(f"{type(exc).__name__}: {exc}")
            sb = reconnect_sandbox(sb)
    raise AssertionError("unreachable upload retry state")


def snapshot_create(sb: Sandbox, name: str) -> tuple[Sandbox, dict[str, Any]]:
    api_start = time.time_ns()
    t0 = time.perf_counter()
    last_error = ""
    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            # Let CubeMaster allocate a fresh snapshot/template id. Reusing a
            # deterministic name across retries can collide with a previous
            # active template attempt and abort otherwise valid runs.
            snap = sb.create_snapshot()
            wall_ms = (time.perf_counter() - t0) * 1000.0
            api_end = time.time_ns()
            post_api_settle()
            return sb, {
                "snapshot_id": snap.snapshot_id,
                "template_id": getattr(snap, "template_id", ""),
                "name": name,
                "sandbox_id": sb.sandbox_id,
                "api_start_unix_ns": api_start,
                "api_end_unix_ns": api_end,
                "checkpoint_wall_ms": wall_ms,
                "api_retries": attempt,
                "last_retry_error": last_error,
            }
        except Exception as e:  # noqa: BLE001
            if attempt >= max_attempts - 1 or not is_retryable_connection_error(e):
                raise
            last_error = f"{type(e).__name__}: {e}"
            if not ("already in progress" in str(e).lower() or "active snapshot operation" in str(e).lower() or "duplicate entry" in str(e).lower()):
                sb = reconnect_sandbox(sb)
            time.sleep(retry_delay(e, attempt))
    raise RuntimeError("unreachable snapshot retry state")


def snapshot_rollback(sb: Sandbox, snapshot_id: str) -> tuple[Sandbox, dict[str, Any]]:
    api_start = time.time_ns()
    t0 = time.perf_counter()
    last_error = ""
    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            resp = sb.rollback(snapshot_id)
            wall_ms = (time.perf_counter() - t0) * 1000.0
            api_end = time.time_ns()
            post_api_settle()
            return sb, {
                "ok": True,
                "snapshot_id": snapshot_id,
                "rollback_response": resp,
                "sandbox_id": sb.sandbox_id,
                "api_start_unix_ns": api_start,
                "api_end_unix_ns": api_end,
                "restore_wall_ms": wall_ms,
                "api_retries": attempt,
                "last_retry_error": last_error,
            }
        except Exception as e:  # noqa: BLE001
            if attempt >= max_attempts - 1 or not is_retryable_connection_error(e):
                raise
            last_error = f"{type(e).__name__}: {e}"
            if not ("already in progress" in str(e).lower() or "active snapshot operation" in str(e).lower() or "duplicate entry" in str(e).lower()):
                sb = reconnect_sandbox(sb)
            time.sleep(retry_delay(e, attempt))
    raise RuntimeError("unreachable rollback retry state")


def delete_snapshots(snapshots: list[str]) -> None:
    for sid in snapshots:
        try:
            Sandbox.delete_snapshot(sid)
        except Exception as e:  # noqa: BLE001
            print(f"[cleanup] WARN delete_snapshot {sid}: {type(e).__name__}: {e}", flush=True)


def run_instance(args: argparse.Namespace, row: dict[str, str]) -> dict[str, Any]:
    instance = row["instance"]
    group = row.get("group", "")
    run_id = f"{args.run_id_prefix}_{instance}_{uuid.uuid4().hex[:6]}"
    work = BASE / "work" / run_id
    result_dir = BASE / "results" / run_id
    for p in (work, result_dir):
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)

    schedule_path = Path(row["schedule"])
    if not schedule_path.exists():
        schedule_path = BASE / "schedules" / schedule_path.name
    schedule = load_schedule(schedule_path)
    if args.max_events > 0:
        schedule = schedule[: args.max_events]
    recorded_action_outputs = build_recorded_action_outputs(instance)

    start_wall = time.time()
    sb: Sandbox | None = None
    snapshots: dict[str, str] = {}
    snapshot_ids: list[str] = []
    iterations: list[dict[str, Any]] = []
    llm_sleep_ms_total = 0.0
    setup_info: dict[str, Any] | None = None
    create_ms = None
    status = "unknown"

    try:
        t0 = time.perf_counter()
        sb = Sandbox.create(template=args.template, timeout=args.sandbox_timeout)
        create_ms = (time.perf_counter() - t0) * 1000.0
        setup_info = prepare_sandbox(
            sb=sb,
            instance=instance,
            work=work,
            warm_action_worker=args.warm_action_worker,
            materialize_file_context=args.materialize_file_context,
            chunk_mb=args.upload_chunk_mb,
        )

        ckpt_seen = 0
        for ev_i, ev in enumerate(schedule):
            kind = ev.get("type") or ev.get("kind")
            if kind == "ckpt":
                ckpt_seen += 1
                latency_ms = float(ev.get("latency_ms") or 0.0)
                if latency_ms > 0 and not args.no_llm_sleep:
                    time.sleep(latency_ms / 1000.0)
                    llm_sleep_ms_total += latency_ms
                node_id = ev.get("node_id")
                worker_ops = ev.get("worker_ops") or []
                action_step_idx: int | None = None
                response: dict[str, Any] | None = None
                action: dict[str, Any] | None = None
                if worker_ops:
                    if not isinstance(node_id, int):
                        raise RuntimeError(f"worker_ops present but node_id missing at ev_i={ev_i}")
                    by_idx: dict[int, list[dict[str, Any]]] = {}
                    for op in worker_ops:
                        by_idx.setdefault(int(op.get("action_step_idx", 0)), []).append(op)
                    if len(by_idx) > 1:
                        raise RuntimeError(f"multiple action_step_idx at ev_i={ev_i}: {sorted(by_idx)}")
                    action_step_idx = next(iter(by_idx))
                    req = make_schedule_action_request(
                        instance=instance,
                        seq=ev_i,
                        node_id=node_id,
                        action_step_idx=action_step_idx,
                        recorded=recorded_action_outputs.get((node_id, action_step_idx)),
                        worker_ops=by_idx[action_step_idx],
                        materialize_file_context=args.materialize_file_context,
                    )
                    req_json = encode_action_request(req)
                    t_upload = time.perf_counter()
                    sb, request_upload = write_action_request(sb, req_json)
                    request_upload_ms = (time.perf_counter() - t_upload) * 1000.0
                    # A transport timeout does not prove that the action never ran.
                    # Dispatch once; preserve an uncertain outcome instead of replaying it.
                    action = cube_run(
                        sb,
                        schedule_action_command(args.materialize_file_context, args.warm_action_worker),
                        timeout=args.worker_timeout,
                    )
                    response_text = ""
                    if action["ok"]:
                        try:
                            response_text = sb.files.read("/tmp/finalbench/action.resp.json")
                            response = json.loads(response_text)
                        except Exception as e:  # noqa: BLE001
                            response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    action["request_upload_ms"] = request_upload_ms
                    action["request_upload"] = request_upload
                    action["response_tail"] = response_text[-4000:] if response_text else ""
                action_ok = (action is None or bool(action.get("ok"))) and (response is None or bool(response.get("ok")))
                if not action_ok:
                    diagnostics = capture_action_failure(sb, result_dir, ev_i, req_json, action, response)
                    iterations.append({
                        "ok": False, "kind": "ckpt", "ev_i": ev_i,
                        "iter": ev.get("iter"), "ckpt_id": ev.get("ckpt_id"),
                        "node_id": node_id, "parent_node_id": ev.get("parent_node_id"),
                        "table2_role": ev.get("table2_role"),
                        "table2_step_idx": ev.get("table2_step_idx"),
                        "action_step_idx": action_step_idx, "worker_ops_n": len(worker_ops),
                        "latency_ms": latency_ms, "latency_source": ev.get("latency_source"),
                        "cube_steps": [tail_record(action)] if action else [],
                        "action_response": response, "action_failure_diagnostics": diagnostics,
                        "checkpoint_not_started": True, "agent_mode": "real",
                        "require_real_agent": True, "path": "cube-cow",
                    })
                    print(f"[cube {instance}] ev={ev_i} action failed; checkpoint not started", flush=True)
                    break
                sb, snap = snapshot_create(sb, f"{instance}-{ev.get('ckpt_id') or ev_i}")
                ckpt_id = ev.get("ckpt_id")
                if isinstance(ckpt_id, str) and ckpt_id:
                    snapshots[ckpt_id] = snap["snapshot_id"]
                    snapshot_ids.append(snap["snapshot_id"])
                ok = (action is None or bool(action.get("ok"))) and (response is None or bool(response.get("ok")))
                iterations.append({
                    "ok": ok,
                    "kind": "ckpt",
                    "ev_i": ev_i,
                    "iter": ev.get("iter"),
                    "ckpt_id": ckpt_id,
                    "node_id": node_id,
                    "parent_node_id": ev.get("parent_node_id"),
                    "table2_role": ev.get("table2_role"),
                    "table2_step_idx": ev.get("table2_step_idx"),
                    "action_step_idx": action_step_idx,
                    "worker_ops_n": len(worker_ops),
                    "latency_ms": latency_ms,
                    "latency_source": ev.get("latency_source"),
                    "cube_steps": [tail_record(action)] if action else [],
                    "action_response": response,
                    "snapshot": snap,
                    "checkpoint_wall_ms": snap["checkpoint_wall_ms"],
                    "snapshot_id": snap["snapshot_id"],
                    "agent_mode": "real",
                    "require_real_agent": True,
                    "path": "cube-cow",
                })
                print(
                    f"[cube {instance}] ev={ev_i} ckpt ok={ok} ops={len(worker_ops)} "
                    f"ckpt_ms={snap['checkpoint_wall_ms']:.1f}",
                    flush=True,
                )
                if not ok:
                    break
                if args.max_ckpts > 0 and ckpt_seen >= args.max_ckpts:
                    break
            elif kind == "restore":
                target = ev.get("restore_to_ckpt_id")
                if target not in snapshots:
                    raise RuntimeError(f"restore target {target!r} not built at ev_i={ev_i}")
                sb, restore = snapshot_rollback(sb, snapshots[target])
                iterations.append({
                    "ok": True,
                    "kind": "restore",
                    "ev_i": ev_i,
                    "iter": ev.get("iter"),
                    "restore_to_ckpt_id": target,
                    "node_id": ev.get("node_id"),
                    "parent_node_id": ev.get("parent_node_id"),
                    "table2_role": ev.get("table2_role"),
                    "table2_step_idx": ev.get("table2_step_idx"),
                    "snapshot_id": snapshots[target],
                    "restore_wall_ms": restore["restore_wall_ms"],
                    "restore_critical_ms": restore["restore_wall_ms"],
                    "cube_steps": [restore],
                    "agent_mode": "real",
                    "require_real_agent": True,
                    "path": "cube-cow",
                })
                print(
                    f"[cube {instance}] ev={ev_i} restore target={target} "
                    f"restore_ms={restore['restore_wall_ms']:.1f}",
                    flush=True,
                )
            else:
                raise RuntimeError(f"unknown schedule event kind={kind!r} at ev_i={ev_i}")
        status = "ok" if all(it.get("ok") for it in iterations) else "fail"
    except Exception as e:  # noqa: BLE001
        status = "error"
        iterations.append({
            "ok": False,
            "kind": "error",
            "error": f"{type(e).__name__}: {e}",
        })
    finally:
        if sb is not None:
            try:
                if sys.exc_info()[0] not in (KeyboardInterrupt, SystemExit):
                    tail = cube_run(
                        sb,
                        "cat /tmp/finalbench/action_worker.log 2>/dev/null || true",
                        timeout=5,
                    )
                    (result_dir / "action_worker.log").write_text(tail.get("stdout", ""), encoding="utf-8")
            except Exception:
                pass
            try:
                sb.kill()
            except Exception as e:  # noqa: BLE001
                print(f"[cleanup] WARN sandbox kill failed: {type(e).__name__}: {e}", flush=True)
        if args.delete_snapshots:
            delete_snapshots(snapshot_ids)

    ck = [float(it["checkpoint_wall_ms"]) for it in iterations if it.get("kind") == "ckpt" and it.get("checkpoint_wall_ms") is not None and it.get("ok")]
    rs = [float(it["restore_wall_ms"]) for it in iterations if it.get("kind") == "restore" and it.get("restore_wall_ms") is not None and it.get("ok")]
    out = {
        "ok": status == "ok",
        "status": status,
        "instance": instance,
        "group": group,
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(start_wall)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_wall_s": time.time() - start_wall,
        "semantics": "DeltaBox P-EAGLE allstd real-RTT replay, backend swapped to CubeSandbox cube-cow snapshot/rollback",
        "backend": "cube-cow",
        "template": args.template,
        "resources": {
            "template_cpu_millicores": args.template_cpu_millicores,
            "template_memory_mb": args.template_memory_mb,
            "writable_layer_size": args.writable_layer_size,
            "host_cpus": args.host_cpus,
        },
        "schedule_path": str(schedule_path),
        "schedule_n_events_loaded": len(schedule),
        "schedule_sha256": sha256_file(schedule_path),
        "llm_replay_mode": "schedule_latency_sleep",
        "llm_sleep_ms_total": llm_sleep_ms_total,
        "warm_action_worker": bool(args.warm_action_worker),
        "measurement_scope": "host-side CubeSandbox SDK API elapsed time",
        "create_ms": create_ms,
        "setup": setup_info,
        "iterations": iterations,
        "snapshot_by_ckpt": snapshots,
        "n_schedule_events": len([it for it in iterations if it.get("kind") in ("ckpt", "restore")]),
        "n_ckpt_events": len(ck),
        "n_restore_events": len(rs),
        "ck_mean_ms": statistics.mean(ck) if ck else None,
        "ck_median_ms": statistics.median(ck) if ck else None,
        "ck_p95_ms": percentile(ck, 95) if ck else None,
        "ck_max_ms": max(ck) if ck else None,
        "rs_mean_ms": statistics.mean(rs) if rs else None,
        "rs_median_ms": statistics.median(rs) if rs else None,
        "rs_p95_ms": percentile(rs, 95) if rs else None,
        "rs_max_ms": max(rs) if rs else None,
    }
    write_json(result_dir / "pilot_result.json", out)
    return out


def percentile(values: list[float], pct: float) -> float:
    if not values:
        raise ValueError("empty")
    if len(values) == 1:
        return values[0]
    vals = sorted(values)
    k = (len(vals) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    frac = k - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def parse_cpu_set(spec: str) -> set[int]:
    cpus: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.update(range(int(lo), int(hi) + 1))
        else:
            cpus.add(int(part))
    return cpus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=BASE / "manifest_peagle12.tsv")
    ap.add_argument("--template", default=os.environ.get("CUBE_FINALBENCH_TEMPLATE", "cube-finalbench-python-4vcpu-4096m-20260604-cow"))
    ap.add_argument("--sandbox-timeout", type=int, default=7200)
    ap.add_argument("--worker-timeout", type=float, default=300.0)
    ap.add_argument("--run-id-prefix", default="cube_cow_peagle_mcts30_realrtt")
    ap.add_argument("--instances", default="", help="comma-separated instance filter")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-events", type=int, default=0)
    ap.add_argument("--max-ckpts", type=int, default=0)
    ap.add_argument("--upload-chunk-mb", type=int, default=8)
    ap.add_argument("--materialize-file-context", action="store_true")
    ap.add_argument("--warm-action-worker", action="store_true", default=True)
    ap.add_argument("--no-warm-action-worker", dest="warm_action_worker", action="store_false")
    ap.add_argument("--no-llm-sleep", action="store_true")
    ap.add_argument("--delete-snapshots", action="store_true", default=True)
    ap.add_argument("--keep-snapshots", dest="delete_snapshots", action="store_false")
    ap.add_argument("--host-cpus", default="")
    ap.add_argument("--template-cpu-millicores", type=int, default=4000)
    ap.add_argument("--template-memory-mb", type=int, default=4096)
    ap.add_argument("--writable-layer-size", default="8Gi")
    ap.add_argument("--fail-fast", action="store_true")
    args = ap.parse_args()

    if args.host_cpus:
        os.sched_setaffinity(0, parse_cpu_set(args.host_cpus))
        print(f"[cube-batch] host affinity set to {args.host_cpus}", flush=True)

    wanted = {x for x in args.instances.split(",") if x}
    rows = [r for r in read_manifest(args.manifest) if not wanted or r["instance"] in wanted]
    if args.limit > 0:
        rows = rows[: args.limit]

    status_path = BASE / "summaries" / f"run_status_{args.run_id_prefix}_{time.strftime('%Y%m%d_%H%M%S')}.tsv"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    with status_path.open("w", encoding="utf-8") as f:
        f.write("group\tinstance\tstatus\tstarted\tended\tresult\n")

    failures = 0
    for row in rows:
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        out = run_instance(args, row)
        if not out.get("ok"):
            failures += 1
        result = BASE / "results" / str(out.get("run_id", "")) / "pilot_result.json"
        ended = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with status_path.open("a", encoding="utf-8") as f:
            f.write(
                f"{row.get('group','')}\t{row['instance']}\t"
                f"{'ok' if out.get('ok') else 'fail'}\t{started}\t{ended}\t{result if result.exists() else ''}\n"
            )
        if args.fail_fast and not out.get("ok"):
            break

    print(f"[cube-batch] status={status_path}", flush=True)
    return 1 if failures else 0


def _ae_interrupted(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt(f"Experiment interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _ae_interrupted)
    raise SystemExit(main())
