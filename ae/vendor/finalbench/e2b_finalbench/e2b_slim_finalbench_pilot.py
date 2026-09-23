#!/usr/bin/env python3
"""E2B slim same-trajectory smoke/pilot.

This is the E2B counterpart of the DeltaBox slim-worker Table 2 harness:

  * controller owns the Moatless SearchTree and build_action LLM calls;
  * E2B sandbox owns only the live repository/action execution state;
  * mock LLM server and CodeIndex sidecar run outside the sandbox;
  * each real action is executed inside E2B, then persisted as an E2B build.

Only the recorded LLM server is mocked. No mock server, trace, or index data is
loaded inside the E2B sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any


if os.environ.get("PYTHONHASHSEED") != "0":
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)


BASE = Path(os.environ.get("E2B_FINALBENCH_BASE", "/mnt/disk2/dyp/finalbench/e2b_finalbench"))
DELTABOX_STD = Path(os.environ.get("DELTABOX_STD_BASE", "/mnt/disk2/dyp/finalbench/deltabox_std"))
E2B = Path(os.environ.get("E2B_INFRA", "/mnt/disk2/dyp/e2b-infra"))
PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", "/mnt/disk2/dyp/spr_payload"))
VENV = Path(os.environ.get("MOATLESS_VENV", "/mnt/disk2/dyp/moatless_det_venv"))
PYTHON = VENV / "bin/python" if (VENV / "bin/python").exists() else Path(sys.executable)
SOURCE_REPOS = PAYLOAD / "repos"
INDEX_STORE = PAYLOAD / "index_store"
SOURCE_TRACES = PAYLOAD / "det_traces"
MOCK_TRACES = BASE / "mock_traces"
DEFAULT_TRACES_ROOT = Path(os.environ.get(
    "E2B_TRACES_ROOT",
    "/mnt/disk2/dyp/d-overlayfs/traces/swe-search",
))
EXECUTION = os.environ.get("E2B_EXECUTION", "ssh")
if EXECUTION not in ("ssh", "local"):
    raise ValueError("E2B_EXECUTION must be ssh or local")
L1_KEY = Path(os.environ["E2B_L1_KEY"]) if EXECUTION == "ssh" else None

SIDE_CAR_IP_FOR_SANDBOX = os.environ.get("E2B_SIDECAR_IP_FOR_SANDBOX", "10.0.2.2")

for p in (str(PAYLOAD), str(PAYLOAD / "moatless-det-src"), str(BASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from replay_driver import rewrite_model_base_url, strip_recorded_tree  # noqa: E402
from baseline_audit import flush_audit, message_policy  # noqa: E402


def run(
    cmd: list[str],
    *,
    timeout: float = 120.0,
    check: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        input=input_text,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def ssh_cmd(port: int, remote: str, *, timeout: float = 300.0, check: bool = True) -> subprocess.CompletedProcess:
    return run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            "-i",
            str(L1_KEY),
            "-p",
            str(port),
            os.environ.get("E2B_L1_HOST", "ubuntu@127.0.0.1"),
            remote,
        ],
        timeout=timeout,
        check=check,
    )


def l1_remote(cmd: str, *, timeout: float = 600.0, check: bool = True) -> subprocess.CompletedProcess:
    if EXECUTION == "local":
        # The Go process owns FC children and network/NBD cleanup. Give it a
        # bounded graceful exit, then reap our entire group on interruption.
        argv = ["bash", "-c", cmd]
        proc = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        previous = signal.getsignal(signal.SIGTERM)
        def terminated(signum, frame):
            raise KeyboardInterrupt("E2B local execution terminated")
        signal.signal(signal.SIGTERM, terminated)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:
            try:
                # resume-build's normal mode installs an os.Interrupt handler;
                # SIGTERM exits immediately without its deferred resource cleanup.
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)
        result = subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    port = int(os.environ.get("E2B_L1_SSH_PORT", "56555"))
    return ssh_cmd(port, cmd, timeout=timeout, check=check)


def q(s: str) -> str:
    import shlex

    return shlex.quote(s)


def launch_background_worker(setup: str, worker: str, wait_ready: str) -> str:
    # Without the braces, '&' backgrounds the complete setup && worker list.
    # Its extra shell retains envd's output pipe for the worker's lifetime.
    return setup + " && { " + worker + " & }; " + wait_ready


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_json(url: str, method: str = "GET", obj: dict | None = None, timeout: float = 30.0) -> dict:
    import urllib.request

    data = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def resolve_trajectory_path(traces_root: Path, instance: str, variant: str) -> Path:
    if variant == "ms":
        return traces_root / "qwen3-coder-30b-ms" / instance / "trajectory.json"
    if variant == "p-eagle-ms":
        return (traces_root / "qwen3-coder-30b-p-eagle-ms" / "mcts-iter30"
                / instance / "trajectory.json")
    raise ValueError(f"unknown trace variant: {variant!r}")


def start_shared_mock(instance: str, variant: str, traces_root: Path,
                      port: int, log_path: Path, audit_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PAYLOAD)
    env["PYTHONHASHSEED"] = "0"
    env["MOCK_MISMATCH_DIR"] = str(log_path.parent)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [
            str(PYTHON),
            str(PAYLOAD / "mock_llm_server.py"),
            "--tcp-host",
            "0.0.0.0",
            "--tcp-port",
            str(port),
            "--traces-root",
            str(traces_root),
        ],
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    proc._finalbench_logf = logf  # type: ignore[attr-defined]
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                if http_json(f"{base}/admin/healthz", timeout=1.0).get("ok"):
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise TimeoutError("shared mock did not become healthy")
        http_json(
            f"{base}/admin/load",
            method="POST",
            obj={"instance_id": instance, "variant": variant},
            timeout=30.0,
        )
        return proc
    except BaseException as error:
        try:
            flush_audit(base, audit_path, primary_error=error)
        finally:
            stop_proc(proc)
        raise


def stop_proc(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    logf = getattr(proc, "_finalbench_logf", None)
    if logf:
        logf.close()


def e2b_env_prefix() -> str:
    # Explicit remote toolchain; do not inherit a host-specific developer path.
    path = os.environ["E2B_REMOTE_PATH"]
    cache = os.environ.get("E2B_GOCACHE", "/var/tmp/go-cache")
    modules = os.environ.get("E2B_GOMODCACHE", "/var/tmp/go-mod")
    return f"export PATH={q(path)}; export GOCACHE={q(cache)}; export GOMODCACHE={q(modules)}; "


def start_external_index_sidecar(instance: str, repo_path: Path, port: int, log_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    inherited_pythonpath = [
        p for p in env.get("PYTHONPATH", "").split(":")
        if p and Path(p) != DELTABOX_STD / "slim_shims"
    ]
    env["PYTHONPATH"] = ":".join(
        [str(DELTABOX_STD), str(PAYLOAD), str(PAYLOAD / "moatless-det-src")]
        + inherited_pythonpath
    )
    env.pop("DELTABOX_SLIM_SHIMS", None)
    env.pop("DELTABOX_SLIM_INDEX_URL", None)
    env["PYTHONHASHSEED"] = "0"
    env["FAISS_OPT_LEVEL"] = "generic"
    env["FAISS_DISABLE_CPU_FEATURES"] = "AVX512_SPR,AVX512,AVX2"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["OMP_THREAD_LIMIT"] = "1"
    env["MALLOC_ARENA_MAX"] = "1"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            str(PYTHON),
            str(DELTABOX_STD / "index_sidecar.py"),
            "--instance",
            instance,
            "--repo-path",
            str(repo_path),
            "--index-store-dir",
            str(INDEX_STORE),
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=str(DELTABOX_STD),
        preexec_fn=os.setsid,
    )
    proc._finalbench_logf = logf  # type: ignore[attr-defined]
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + float(os.environ.get("E2B_INDEX_SIDECAR_START_TIMEOUT", "300"))
        while time.time() < deadline:
            try:
                if http_json(f"{base}/healthz", timeout=1.0).get("ok"):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            stop_proc(proc)
            raise TimeoutError("external index sidecar did not become healthy")
        return proc
    except BaseException:
        stop_proc(proc)
        raise


def external_mock_json(port: int, path: str, method: str = "GET", obj: dict | None = None) -> dict:
    return http_json(f"http://127.0.0.1:{port}{path}", method=method, obj=obj, timeout=30.0)


def make_payload_tar(instance: str, out: Path) -> None:
    repo = SOURCE_REPOS / f"swe-bench_{instance}"
    if not repo.exists():
        raise FileNotFoundError(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    with tarfile.open(tmp, "w") as tar:
        tar.add(BASE / "e2b_slim_action_runner.py", arcname="finalbench/e2b_slim_action_runner.py")
        tar.add(BASE / "e2b_slim_action_worker.py", arcname="finalbench/e2b_slim_action_worker.py")
        tar.add(DELTABOX_STD / "slim_index_proxy.py", arcname="finalbench/slim_index_proxy.py")
        tar.add(DELTABOX_STD / "slim_shims", arcname="finalbench/slim_shims")
        tar.add(VENV, arcname="moatless_det_venv")
        if os.environ.get("NLTK_DATA"):
            tar.add(Path(os.environ["NLTK_DATA"]), arcname="nltk_data")
        tar.add((PAYLOAD / "moatless-det-src").resolve(), arcname="spr_payload/moatless-det-src")
        for name in ("protocol.py", "__init__.py"):
            tar.add(PAYLOAD / name, arcname=f"spr_payload/{name}")
        tar.add(repo.resolve(), arcname="repo")
    tmp.replace(out)


def e2b_resume_build_cmd(
    *,
    from_build: str,
    to_build: str,
    storage: str,
    command: str,
    finalbench_json: Path,
    uploads: list[tuple[Path, str]],
    downloads: list[tuple[str, Path]],
) -> str:
    parts = [
        "sudo -E env",
        'PATH="$PATH"',
        'GOCACHE="$GOCACHE"',
        'GOMODCACHE="$GOMODCACHE"',
        f"ALLOW_SANDBOX_INTERNAL_CIDRS={q(SIDE_CAR_IP_FOR_SANDBOX + '/32')}",
        q(os.environ["E2B_RESUME_BINARY"]) if os.environ.get("E2B_RESUME_BINARY") else "go run ./cmd/resume-build",
        "-from-build",
        q(from_build),
        "-to-build",
        q(to_build),
        "-storage",
        q(storage),
        "-cmd-pause",
        q(command),
        "-finalbench-json",
        q(str(finalbench_json)),
        "-no-prefetch",
    ]
    if os.environ.get("E2B_SANDBOX_DIR"):
        parts.extend(["-sandbox-dir", q(os.environ["E2B_SANDBOX_DIR"])])
    for local, remote in uploads:
        parts.extend(["-upload-file", q(f"{local}:{remote}")])
    for remote, local in downloads:
        parts.extend(["-download-file", q(f"{remote}:{local}")])
    return (
        "set -euo pipefail; "
        f"cd {q(str(E2B / 'packages/orchestrator'))}; "
        f"{e2b_env_prefix()} "
        + " ".join(parts)
    )


def e2b_step(
    *,
    from_build: str,
    to_build: str,
    storage: str,
    command: str,
    timings_path: Path,
    uploads: list[tuple[Path, str]],
    downloads: list[tuple[str, Path]],
    timeout: float = 1200.0,
) -> dict:
    for p in [timings_path, *[local for _, local in downloads]]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    remote = e2b_resume_build_cmd(
        from_build=from_build,
        to_build=to_build,
        storage=storage,
        command=command,
        finalbench_json=timings_path,
        uploads=uploads,
        downloads=downloads,
    )
    cp = l1_remote(remote, timeout=timeout, check=False)
    timing = json.loads(timings_path.read_text(encoding="utf-8")) if timings_path.exists() else None
    out: dict[str, Any] = dict(timing or {})
    # A timing file cannot override a failed process or manufacture success.
    out.update({
        "ok": cp.returncode == 0 and isinstance(timing, dict) and timing.get("ok") is True,
        "host_rc": cp.returncode,
        "timing_present": timing is not None,
        "stdout_tail": (cp.stdout or "")[-2000:],
        "stderr_tail": (cp.stderr or "")[-4000:],
    })
    if not out["ok"]:
        out.setdefault("error", "resume-build process failed or successful timing evidence is missing")
    return out


def create_base_build(*, storage: str, build_id: str, mem_mib: int, disk_mb: int, fc_version: str) -> dict:
    remote = f"""
set -euo pipefail
cd {q(str(E2B / "packages/orchestrator"))}
{e2b_env_prefix()}
sudo modprobe nbd nbds_max=64 || true
sudo -E env PATH="$PATH" GOCACHE="$GOCACHE" GOMODCACHE="$GOMODCACHE" \\
  DEFAULT_FIRECRACKER_VERSION={q(fc_version)} \\
  ALLOW_SANDBOX_INTERNAL_CIDRS={q(SIDE_CAR_IP_FOR_SANDBOX + '/32')} \\
  go run ./cmd/create-build \\
    -to-build {q(build_id)} \\
    -storage {q(storage)} \\
    -firecracker {q(fc_version)} \\
    -memory {mem_mib} -vcpu 1 -disk {disk_mb} -hugepages=false -timeout 12
"""
    cp = l1_remote(remote, timeout=1800, check=False)
    return {
        "ok": cp.returncode == 0,
        "build_id": build_id,
        "rc": cp.returncode,
        "stdout_tail": (cp.stdout or "")[-3000:],
        "stderr_tail": (cp.stderr or "")[-4000:],
    }


def load_initial_tree(instance: str, mock_port: int,
                      traces_root: Path, variant: str) -> dict:
    traj_path = resolve_trajectory_path(traces_root, instance, variant)
    data = json.loads(traj_path.read_text(encoding="utf-8"))
    data = strip_recorded_tree(data)
    rewrite_model_base_url(data, f"http://127.0.0.1:{mock_port}/v1")
    return data


def controller_tree_from_dict(tree_dict: dict, repo_path: Path, index_store_dir: Path, instance: str):
    from moatless.index import CodeIndex
    from moatless.repository.file import FileRepository
    from moatless.search_tree import SearchTree

    repo = FileRepository(repo_path=str(repo_path))
    code_index = CodeIndex.from_index_name(instance, file_repo=repo, index_store_dir=str(index_store_dir))
    return SearchTree.from_dict(tree_dict, repository=repo, code_index=code_index)


def controller_build_action_only(tree, node) -> None:
    from moatless.completion.model import Completion
    from moatless.node import ActionStep

    if node.action:
        node.reset()
    node.possible_actions = [action.name for action in tree.agent.actions]
    system_prompt = tree.agent.generate_system_prompt()
    action_args = [action.args_schema for action in tree.agent.actions]
    messages = tree.agent.message_generator.generate(node)
    try:
        completion_response = tree.agent._completion.create_completion(
            messages,
            system_prompt=system_prompt,
            response_model=action_args,
        )
        if completion_response.structured_outputs:
            node.action_steps = [ActionStep(action=action) for action in completion_response.structured_outputs]
        node.assistant_message = completion_response.text_response
        node.completions["build_action"] = completion_response.completion
    except Exception as e:
        node.terminal = True
        node.error = __import__("traceback").format_exc()
        if hasattr(e, "messages") and hasattr(e, "last_completion"):
            node.completions["build_action"] = Completion.from_llm_completion(
                input_messages=e.messages,
                completion_response=e.last_completion,
                model=tree.agent.completion.model,
            )
            return
        raise
    if node.action is None:
        return
    duplicate_node = node.find_duplicate()
    if duplicate_node:
        node.is_duplicate = True


def build_node_build_action_cursor(instance: str, traces_root: Path,
                                   variant: str) -> dict[int, int]:
    """node_id -> cursor index of that node's build_action in the recorded
    sequence the mock serves.

    The mock advances ONE monotonic cursor and hash-asserts each request; MCTS
    re-selection (UCT) issues build_action calls out of recorded node_id order,
    so before each expansion we rewind the shared mock to the selected node's
    recorded build_action position (the mock's per-node /admin/rewind, designed
    "for MCTS restore events").  load_trajectory returns the same ordered list
    the mock loads, so the enumerate index == mock cursor.
    """
    from trajectory_index import load_trajectory
    traj_path = resolve_trajectory_path(traces_root, instance, variant)
    seq = load_trajectory(str(traj_path))
    node_cursor: dict[int, int] = {}
    for i, c in enumerate(seq):
        if c.purpose == "build_action" and c.node_id not in node_cursor:
            node_cursor[c.node_id] = i
    return node_cursor


def run_one_e2b_iteration(
    *,
    tree,
    instance: str,
    seq: int,
    build_by_node: dict[int, str],
    node_build_cursor: dict[int, int],
    completions: list,
    storage: str,
    work: Path,
    shared_mock_port: int,
    index_port: int,
    materialize_file_context: bool,
    warm_action_worker: bool,
) -> tuple[dict, dict[int, str]]:
    from moatless.actions.model import Observation
    from moatless.file_context import FileContext

    tree.assert_runnable()
    if tree.is_finished():
        return {"ok": True, "finished": True, "node_id": None, "event": None, "e2b_steps": []}, build_by_node
    selected = tree._select(tree.root)
    if selected is None:
        return {"ok": True, "finished": True, "node_id": None, "event": {"event_type": "no_expandable_nodes"}, "e2b_steps": []}, build_by_node
    selected_node_id = selected.node_id
    if selected_node_id not in build_by_node:
        return {"ok": False, "error": f"no E2B build recorded for selected node {selected_node_id}", "node_id": selected_node_id, "e2b_steps": []}, build_by_node
    selected_build = build_by_node[selected_node_id]
    new_node = tree._expand(selected) or selected
    # Pin the shared mock to this node's recorded build_action position before
    # the controller issues build_action; the action's exec.* completions then
    # advance contiguously from there (recorded right after, same node). Without
    # this, the first UCT re-selection issues build_action out of recorded order
    # and audit mode must never mask an incorrect response cursor.
    _bcur = node_build_cursor.get(new_node.node_id)
    if type(_bcur) is not int or _bcur < 0:
        raise RuntimeError(f'missing or invalid recorded build_action cursor for node {new_node.node_id}: {_bcur!r}')
    rewound = http_json(
        f"http://127.0.0.1:{shared_mock_port}/admin/rewind",
        method="POST", obj={"cursor": _bcur}, timeout=2.0,
    )
    if (not isinstance(rewound, dict) or rewound.get('ok') is not True
            or type(rewound.get('cursor')) is not int or rewound['cursor'] != _bcur):
        raise RuntimeError(f'mock rewind did not confirm cursor {_bcur} for node {new_node.node_id}: {rewound!r}')
    before_build = http_json(f"http://127.0.0.1:{shared_mock_port}/admin/stats", timeout=2.0)
    controller_build_action_only(tree, new_node)
    after_build = http_json(f"http://127.0.0.1:{shared_mock_port}/admin/stats", timeout=2.0)
    end_cursor = after_build["cursor"]
    matched = completions[_bcur:end_cursor]
    if (before_build["cursor"] != _bcur or end_cursor < _bcur or
            end_cursor > len(completions) or
            after_build["n_served"]-before_build["n_served"] != len(matched) or
            any(c.node_id != new_node.node_id or c.purpose != "build_action" for c in matched)):
        raise RuntimeError('Cannot bind controller LLM floor to served build_action completions')
    llm_floor = {"protocol": "served-controller-build-action-v1", "start_cursor": _bcur,
                 "end_cursor": end_cursor, "node_id": new_node.node_id,
                 "recorded_ms": 1000*sum(c.dur_s for c in matched),
                 "served": len(matched), "before_stats": before_build, "after_stats": after_build}
    action_events: list[dict] = []
    e2b_steps: list[dict] = []
    child_build = str(uuid.uuid4())

    if not new_node.is_duplicate and new_node.action_steps:
        for idx, action_step in enumerate(new_node.action_steps):
            if action_step.observation is not None:
                continue
            step_to_build = child_build if idx == len(new_node.action_steps) - 1 else str(uuid.uuid4())
            action_payload = action_step.action.model_dump()
            action_payload["action_args_class"] = (
                f"{action_step.action.__class__.__module__}.{action_step.action.__class__.__name__}"
            )
            action_model = tree.agent._action_map[type(action_step.action)].model_dump()

            req = {
                "instance": instance,
                "repo_path": "/workspace/repo",
                "index_url": f"http://{SIDE_CAR_IP_FOR_SANDBOX}:{index_port}",
                "mock_base_url": f"http://{SIDE_CAR_IP_FOR_SANDBOX}:{shared_mock_port}",
                "seq": seq,
                "node_id": new_node.node_id,
                "action": action_payload,
                "action_model": action_model,
                "file_context": new_node.file_context.model_dump(),
                "materialize_file_context": materialize_file_context,
            }
            req_path = work / f"seq{seq}_action{idx}.req.json"
            resp_path = work / f"seq{seq}_action{idx}.resp.json"
            timing_path = work / f"seq{seq}_action{idx}.timing.json"
            req_path.write_text(json.dumps(req, separators=(",", ":")), encoding="utf-8")
            common_env = (
                "PYTHONPATH=/opt/finalbench/slim_shims:/opt/finalbench:/opt/spr_payload:/opt/spr_payload/moatless-det-src "
                "DELTABOX_SLIM_SHIMS=1 "
                "E2B_FINALBENCH_BASE=/opt/finalbench "
                "SPR_PAYLOAD=/opt/spr_payload "
                "PYTHONHASHSEED=0 OPENAI_API_KEY=dummy CUSTOM_LLM_API_KEY=dummy LITELLM_LOG=ERROR "
                "NLTK_DATA=/opt/nltk_data LITELLM_LOCAL_MODEL_COST_MAP=True "
                f"E2B_MATERIALIZE_FILE_CONTEXT={'1' if materialize_file_context else '0'} "
                "FAISS_OPT_LEVEL=generic FAISS_DISABLE_CPU_FEATURES=AVX512_SPR,AVX512,AVX2 "
                "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MALLOC_ARENA_MAX=1 "
            )
            if warm_action_worker:
                command = (
                    "mkdir -p /tmp/finalbench && "
                    "rm -f /tmp/finalbench/action.resp.json /tmp/finalbench/action.resp.json.tmp && "
                    "test -p /tmp/finalbench/action_worker.in || "
                    "{ cat /tmp/finalbench/action_worker.log >&2 || true; exit 4; }; "
                    "timeout 30 sh -c 'cat /tmp/finalbench/action.req.json > /tmp/finalbench/action_worker.in' || "
                    "{ cat /tmp/finalbench/action_worker.log >&2 || true; exit 5; }; "
                    "for i in $(seq 1 30000); do "
                    "[ -s /tmp/finalbench/action.resp.json ] && exit 0; "
                    "sleep 0.01; "
                    "done; "
                    "cat /tmp/finalbench/action_worker.log >&2 || true; "
                    "exit 6"
                )
            else:
                command = (
                    "mkdir -p /tmp/finalbench && "
                    f"{common_env} "
                    "/opt/moatless_det_venv/bin/python "
                    "/opt/finalbench/e2b_slim_action_runner.py "
                    "/tmp/finalbench/action.req.json /tmp/finalbench/action.resp.json; "
                    "RC=$?; exit $RC"
                )
            e2b = e2b_step(
                from_build=selected_build,
                to_build=step_to_build,
                storage=storage,
                command=command,
                timings_path=timing_path,
                uploads=[(req_path, "/tmp/finalbench/action.req.json")],
                downloads=[("/tmp/finalbench/action.resp.json", resp_path)],
            )
            e2b_steps.append(e2b)
            if not e2b.get("ok"):
                return {"ok": False, "error": "e2b step failed", "e2b": e2b, "node_id": new_node.node_id, "e2b_steps": e2b_steps}, build_by_node
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            if not resp.get("ok"):
                return {"ok": False, "error": "action runner failed", "response": resp, "e2b": e2b, "node_id": new_node.node_id, "e2b_steps": e2b_steps}, build_by_node
            action_step.observation = Observation.model_validate(resp["observation"])
            if action_step.observation.execution_completion:
                action_step.completion = action_step.observation.execution_completion
            new_node.file_context = FileContext.from_dict(repo=tree.repository, runtime=None, data=resp["file_context"])
            new_node.terminal = action_step.observation.terminal
            ev = dict(resp.get("event") or {})
            ev["action_args_class"] = action_payload["action_args_class"]
            action_events.append(ev)
            selected_build = step_to_build
    else:
        child_build = selected_build

    if new_node.observation:
        new_node.terminal = new_node.observation.terminal
    tree._backpropagate(new_node)
    build_by_node[new_node.node_id] = child_build
    best = tree.get_best_trajectory()
    return {
        "ok": True,
        "finished": tree.is_finished(),
        "node_id": new_node.node_id,
        "build_id": child_build,
        "selected_build_id": build_by_node[selected_node_id],
        "event": {
            "event_type": "e2b_slim_tree_iteration",
            "selected_node_id": selected_node_id,
            "new_node_id": new_node.node_id,
            "total_nodes": len(tree.root.get_all_nodes()),
            "finished_nodes": len(tree.get_finished_nodes()),
            "best_node_id": best.node_id if best else None,
            "controller_llm_floor": llm_floor,
            "n_worker_actions": len(action_events),
            "action_events": action_events,
            "is_duplicate": bool(new_node.is_duplicate),
        },
        "e2b_steps": e2b_steps,
    }, build_by_node


def run_pilot(args: argparse.Namespace) -> dict:
    instance = args.instance
    traces_root = Path(args.traces_root)
    traj_path = resolve_trajectory_path(traces_root, instance, args.trace_variant)
    if not traj_path.exists():
        raise FileNotFoundError(traj_path)
    run_id = f"{args.run_id_prefix}_{instance}_{uuid.uuid4().hex[:6]}"
    print(f"[pilot] run_id={run_id}", flush=True)
    work = BASE / "work" / run_id
    results = BASE / "results" / run_id
    logs = work / "logs"
    for p in (work, results):
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    shared_mock_port = args.worker_mock_port or free_tcp_port()
    index_port = args.index_port or free_tcp_port()
    shared_mock_proc = start_shared_mock(
        instance, args.trace_variant, traces_root, shared_mock_port,
        logs / "shared_mock.log", results / 'mock_audit.json',
    )
    index_proc: subprocess.Popen | None = None

    try:
        print(
            f"[pilot] starting external sidecars shared_mock={shared_mock_port} index={index_port}",
            flush=True,
        )
        index_proc = start_external_index_sidecar(
            instance,
            SOURCE_REPOS / f"swe-bench_{instance}",
            index_port,
            logs / "external_index_sidecar.log",
        )

        if args.clean_storage:
            l1_remote(f"sudo rm -rf {q(args.storage)} && mkdir -p {q(args.storage)}", timeout=300, check=True)

        base_build = args.from_build or str(uuid.uuid4())
        if not args.from_build:
            print(f"[pilot] create base build={base_build}", flush=True)
            create = create_base_build(
                storage=args.storage,
                build_id=base_build,
                mem_mib=args.mem_mib,
                disk_mb=args.disk_mb,
                fc_version=args.fc_version,
            )
            if not create["ok"]:
                out = {"ok": False, "stage": "create_base_build", "create": create}
                (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
                return out
        else:
            create = {"ok": True, "build_id": base_build, "reused": True}

        if args.root_build:
            root_build = args.root_build
            root = {"ok": True, "to_build": root_build, "reused": True}
            print(f"[pilot] reuse root build={root_build}", flush=True)
        else:
            payload_tar = work / f"{instance}.slim_payload.tar"
            print("[pilot] building slim payload tar", flush=True)
            make_payload_tar(instance, payload_tar)
            root_build = str(uuid.uuid4())
            root_timing = work / "root_setup.timing.json"
            root_command = (
                "mkdir -p /opt /workspace /mnt/disk2/dyp && "
                "tar -xf /tmp/finalbench_payload.tar -C /opt && "
                "rm -f /tmp/finalbench_payload.tar && "
                "mv /opt/repo /workspace/repo && "
                "rm -rf /workspace/repo/.git && "
                "ln -sfn /opt/spr_payload /mnt/disk2/dyp/spr_payload && "
                "ln -sfn /opt/finalbench /mnt/disk2/dyp/finalbench && "
                "ln -sfn /opt/moatless_det_venv /mnt/disk2/dyp/moatless_det_venv && "
                "ln -sfn /opt/spr_payload/moatless-det-src /mnt/disk2/dyp/spr_payload/moatless-det-src && "
                "test -x /opt/moatless_det_venv/bin/python && "
                "test -f /opt/finalbench/e2b_slim_action_runner.py && "
                "test -d /workspace/repo"
            )
            if args.warm_action_worker:
                worker_env = (
                    "PYTHONPATH=/opt/finalbench/slim_shims:/opt/finalbench:/opt/spr_payload:/opt/spr_payload/moatless-det-src "
                    "DELTABOX_SLIM_SHIMS=1 "
                    "E2B_FINALBENCH_BASE=/opt/finalbench "
                    "SPR_PAYLOAD=/opt/spr_payload "
                    "PYTHONHASHSEED=0 OPENAI_API_KEY=dummy CUSTOM_LLM_API_KEY=dummy LITELLM_LOG=ERROR "
                    "NLTK_DATA=/opt/nltk_data LITELLM_LOCAL_MODEL_COST_MAP=True "
                    f"E2B_MATERIALIZE_FILE_CONTEXT={'1' if args.materialize_file_context else '0'} "
                    "FAISS_OPT_LEVEL=generic FAISS_DISABLE_CPU_FEATURES=AVX512_SPR,AVX512,AVX2 "
                    "OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 MALLOC_ARENA_MAX=1 "
                )
                root_command = launch_background_worker(
                    root_command + " && mkdir -p /tmp/finalbench",
                    f"nohup env {worker_env} /opt/moatless_det_venv/bin/python "
                    "/opt/finalbench/e2b_slim_action_worker.py "
                    "--mode fifo "
                    "--fifo /tmp/finalbench/action_worker.in "
                    "--response /tmp/finalbench/action.resp.json "
                    "--ready /tmp/finalbench/action_worker.ready "
                    "> /tmp/finalbench/action_worker.log 2>&1 < /dev/null",
                    "for i in $(seq 1 300); do "
                    "[ -p /tmp/finalbench/action_worker.in ] && "
                    "[ -f /tmp/finalbench/action_worker.ready ] && exit 0; "
                    "sleep 0.1; "
                    "done; "
                    "cat /tmp/finalbench/action_worker.log >&2 || true; "
                    "exit 3",
                )
            root = e2b_step(
                from_build=base_build,
                to_build=root_build,
                storage=args.storage,
                command=root_command,
                timings_path=root_timing,
                uploads=[(payload_tar, "/tmp/finalbench_payload.tar")],
                downloads=[],
                timeout=2400.0,
            )
            if not root.get("ok"):
                out = {"ok": False, "stage": "root_setup", "create": create, "root_setup": root}
                (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
                return out
            print(f"[pilot] root setup ok build={root_build}", flush=True)

        print("[pilot] loading controller tree", flush=True)
        tree_dict = load_initial_tree(
            instance, shared_mock_port, traces_root, args.trace_variant)
        controller_repo = SOURCE_REPOS / f"swe-bench_{instance}"
        tree = controller_tree_from_dict(tree_dict, controller_repo, INDEX_STORE, instance)
        build_by_node = {tree.root.node_id: root_build}
        node_build_cursor = build_node_build_action_cursor(
            instance, traces_root, args.trace_variant)
        from trajectory_index import load_trajectory
        completions = load_trajectory(str(traj_path))
        iterations = []
        for seq in range(1, args.max_steps + 1):
            it, build_by_node = run_one_e2b_iteration(
                tree=tree,
                instance=instance,
                seq=seq,
                build_by_node=build_by_node,
                node_build_cursor=node_build_cursor,
                completions=completions,
                storage=args.storage,
                work=work,
                shared_mock_port=shared_mock_port,
                index_port=index_port,
                materialize_file_context=bool(args.materialize_file_context),
                warm_action_worker=bool(args.warm_action_worker),
            )
            iterations.append(it)
            print(f"[{seq}] ok={it.get('ok')} node={it.get('node_id')} build={it.get('build_id')}", flush=True)
            if not it.get("ok") or it.get("finished"):
                break

        e2b_steps = [step for it in iterations for step in (it.get("e2b_steps") or [])]
        ck = [float(s.get("checkpoint_persist_ms", 0.0)) for s in e2b_steps if s.get("ok")]
        rs = [float(s.get("resume_ms", 0.0)) for s in e2b_steps if s.get("ok")]
        shared_mock_stats = http_json(f"http://127.0.0.1:{shared_mock_port}/admin/stats", timeout=2.0)
        out = {
            "ok": all(i.get("ok") for i in iterations),
            "instance": instance,
            "run_id": run_id,
            "trace_variant": args.trace_variant,
            "trace_path": str(traj_path),
            "semantics": (
                "same recorded Moatless trajectory; controller/SearchTree and the shared mock cursor stay outside E2B; "
                "the L1 mock and index sidecars are external, while actions execute inside E2B and each node is persisted as an E2B build"
            ),
            "measurement_scope": "Table2 ck/rs use inner E2B Go API timings from resume-build finalbench-json, not CLI wall time",
            "checkpoint_metric": "checkpoint_persist_ms = Pause() + local snapshot upload",
            "restore_metric": "resume_ms = Factory.ResumeSandbox() return latency",
            "sidecar_note": (
                "The external CodeIndex sidecar serves the same static base index as DeltaBox slim. "
                "Moatless search hits contain locations, while code text is materialized inside the sandbox from the live repo."
            ),
            "create": create,
            "root_setup": root,
            "iterations": iterations,
            "n_node_builds": len(build_by_node),
            "n_e2b_steps": len(e2b_steps),
            "ck_mean_ms": sum(ck) / len(ck) if ck else None,
            "rs_mean_ms": sum(rs) / len(rs) if rs else None,
            "mock_stats": shared_mock_stats,
            "message_policy": message_policy(),
            "ports": {"shared_mock_l1": shared_mock_port, "index_l1": index_port},
        }
        (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    finally:
        try:
            flush_audit(f'http://127.0.0.1:{shared_mock_port}', results / 'mock_audit.json',
                        primary_error=sys.exc_info()[1])
        finally:
            try:
                stop_proc(shared_mock_proc)
            finally:
                stop_proc(index_proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="pytest-dev__pytest-8365")
    ap.add_argument("--max-steps", type=int, default=4)
    ap.add_argument("--materialize-file-context", action="store_true",
                    help="Persist Moatless FileContext edits to the live repo for physical-FS workload runs.")
    ap.add_argument("--warm-action-worker", action="store_true",
                    help="Start a persistent in-sandbox Python action worker at root setup and send actions through a shell/FIFO hot path.")
    ap.add_argument("--trace-variant", default="ms", choices=("ms", "p-eagle-ms"))
    ap.add_argument("--traces-root", default=str(DEFAULT_TRACES_ROOT))
    ap.add_argument("--run-id-prefix", default="e2b_slim_same_trace")
    ap.add_argument("--storage", default="/var/tmp/e2b-slim-finalbench")
    ap.add_argument("--clean-storage", action="store_true")
    ap.add_argument("--from-build", default="")
    ap.add_argument("--root-build", default="")
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--disk-mb", type=int, default=4096)
    ap.add_argument("--fc-version", default="v1.14.1_458ca91")
    ap.add_argument("--worker-mock-port", type=int, default=0)
    ap.add_argument("--index-port", type=int, default=0)
    args = ap.parse_args()
    out = run_pilot(args)
    print(json.dumps({k: out.get(k) for k in ("ok", "instance", "run_id", "n_e2b_steps", "ck_mean_ms", "rs_mean_ms")}, indent=2))
    return 0 if out.get("ok") else 1


def _ae_interrupted(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt(f"Experiment interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _ae_interrupted)
    raise SystemExit(main())
