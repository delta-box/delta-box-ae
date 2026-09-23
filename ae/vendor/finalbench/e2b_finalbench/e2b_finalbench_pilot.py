#!/usr/bin/env python3
"""E2B same-trajectory finalbench pilot.

This harness reuses the recorded Moatless MCTS trajectories and mock LLM
server. The controller owns the SearchTree and mock cursor; E2B owns only the
sandboxed repository/action execution state. Each completed node is persisted
as an E2B build, so rollback is E2B ResumeSandbox(from_build=<node build>).

Only the recorded LLM server is mocked. Moatless action execution inside E2B is
real: the action runner imports moatless, FileRepository, and CodeIndex inside
the sandbox and executes the selected action against the sandbox repo.
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


BASE = Path(os.environ.get("E2B_FINALBENCH_BASE", "/mnt/disk2/dyp/finalbench/e2b_finalbench"))
E2B = Path(os.environ.get("E2B_INFRA", "/mnt/disk2/dyp/e2b-infra"))
PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", "/mnt/disk2/dyp/spr_payload"))
VENV = Path(os.environ.get("MOATLESS_VENV", "/mnt/disk2/dyp/moatless_det_venv"))
VENV_PY = VENV / "bin/python"
PYTHON = VENV_PY if VENV_PY.exists() else Path(sys.executable)
SOURCE_REPOS = PAYLOAD / "repos"
INDEX_STORE = PAYLOAD / "index_store"
SOURCE_TRACES = PAYLOAD / "det_traces"
MOCK_TRACES = BASE / "mock_traces"
RESUME_BUILD = E2B / "packages/orchestrator/cmd/resume-build"
CREATE_BUILD = E2B / "packages/orchestrator/cmd/create-build"
L1_KEY = Path(os.environ["E2B_L1_KEY"])

for p in (str(PAYLOAD), str(PAYLOAD / "moatless-det-src"), str(BASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from replay_driver import rewrite_model_base_url, strip_recorded_tree  # noqa: E402


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
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=5",
            "-i", str(L1_KEY),
            "-p", str(port),
            os.environ.get("E2B_L1_HOST", "ubuntu@127.0.0.1"),
            remote,
        ],
        timeout=timeout,
        check=check,
    )


def l1_remote(cmd: str, *, timeout: float = 600.0, check: bool = True) -> subprocess.CompletedProcess:
    port = int(os.environ.get("E2B_L1_SSH_PORT", "56555"))
    return ssh_cmd(port, cmd, timeout=timeout, check=check)


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


def ensure_mock_trace_layout() -> None:
    target = MOCK_TRACES / "qwen3-coder-30b-ms"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(SOURCE_TRACES / "ms", target, target_is_directory=True)


def start_mock(instance: str, port: int, log_path: Path) -> subprocess.Popen:
    ensure_mock_trace_layout()
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
            "--tcp-port", str(port),
            "--traces-root", str(MOCK_TRACES),
        ],
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    proc._finalbench_logf = logf  # type: ignore[attr-defined]
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            if http_json(f"{base}/admin/healthz", timeout=1.0).get("ok"):
                break
        except Exception:
            time.sleep(0.2)
    else:
        stop_proc(proc)
        raise TimeoutError("mock did not become healthy")
    http_json(
        f"{base}/admin/load",
        method="POST",
        obj={"instance_id": instance, "variant": "ms"},
        timeout=30.0,
    )
    return proc


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


def make_payload_tar(instance: str, out: Path) -> None:
    repo = SOURCE_REPOS / f"swe-bench_{instance}"
    index = INDEX_STORE / instance
    if not repo.exists():
        raise FileNotFoundError(repo)
    if not index.exists():
        raise FileNotFoundError(index)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    with tarfile.open(tmp, "w") as tar:
        tar.add(BASE / "e2b_action_runner.py", arcname="finalbench/e2b_action_runner.py")
        tar.add(VENV, arcname="moatless_det_venv")
        tar.add((PAYLOAD / "moatless-det-src").resolve(), arcname="spr_payload/moatless-det-src")
        for name in ("mock_llm_server.py", "protocol.py", "trajectory_index.py", "__init__.py"):
            tar.add(PAYLOAD / name, arcname=f"spr_payload/{name}")
        tar.add(SOURCE_TRACES / "ms" / instance, arcname=f"det_traces/qwen3-coder-30b-ms/{instance}")
        tar.add(index, arcname=f"spr_payload/index_store/{instance}")
        tar.add(repo.resolve(), arcname="repo")
    tmp.replace(out)


def e2b_env_prefix() -> str:
    # Explicit remote toolchain; do not inherit a host-specific developer path.
    path = os.environ["E2B_REMOTE_PATH"]
    return f"export PATH={q(path)}; export GOCACHE=/var/tmp/go-cache; export GOMODCACHE=/var/tmp/go-mod; "


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
    import shlex

    parts = [
        "sudo -E env PATH=\"$PATH\" GOCACHE=\"$GOCACHE\" GOMODCACHE=\"$GOMODCACHE\" go run ./cmd/resume-build",
        "-from-build", shlex.quote(from_build),
        "-to-build", shlex.quote(to_build),
        "-storage", shlex.quote(storage),
        "-cmd-pause", shlex.quote(command),
        "-finalbench-json", shlex.quote(str(finalbench_json)),
        "-no-prefetch",
    ]
    for local, remote in uploads:
        parts.extend(["-upload-file", shlex.quote(f"{local}:{remote}")])
    for remote, local in downloads:
        parts.extend(["-download-file", shlex.quote(f"{remote}:{local}")])
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
    out = {
        "host_rc": cp.returncode,
        "stdout_tail": (cp.stdout or "")[-2000:],
        "stderr_tail": (cp.stderr or "")[-4000:],
    }
    if timings_path.exists():
        out.update(json.loads(timings_path.read_text(encoding="utf-8")))
    if cp.returncode != 0:
        out["ok"] = False
        out.setdefault("error", "resume-build command failed")
    return out


def create_base_build(*, storage: str, build_id: str, mem_mib: int, disk_mb: int, fc_version: str) -> dict:
    import shlex

    remote = f"""
set -euo pipefail
cd {q(str(E2B / "packages/orchestrator"))}
{e2b_env_prefix()}
sudo modprobe nbd nbds_max=64 || true
sudo -E env PATH="$PATH" GOCACHE="$GOCACHE" GOMODCACHE="$GOMODCACHE" DEFAULT_FIRECRACKER_VERSION={shlex.quote(fc_version)} \\
  go run ./cmd/create-build \\
    -to-build {shlex.quote(build_id)} \\
    -storage {shlex.quote(storage)} \\
    -firecracker {shlex.quote(fc_version)} \\
    -memory {mem_mib} -vcpu 1 -disk {disk_mb} -hugepages=false -timeout 12
"""
    cp = l1_remote(remote, timeout=1800, check=False)
    return {"ok": cp.returncode == 0, "build_id": build_id, "rc": cp.returncode, "stdout_tail": cp.stdout[-3000:], "stderr_tail": cp.stderr[-4000:]}


def load_initial_tree(instance: str, mock_port: int) -> dict:
    traj_path = SOURCE_TRACES / "ms" / instance / "trajectory.json"
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


def run_one_e2b_iteration(
    *,
    tree,
    instance: str,
    seq: int,
    build_by_node: dict[int, str],
    storage: str,
    work: Path,
) -> tuple[dict, dict[int, str]]:
    from moatless.actions.model import Observation
    from moatless.file_context import FileContext

    tree.assert_runnable()
    if tree.is_finished():
        return {"ok": True, "finished": True, "node_id": None, "event": None, "e2b": None}, build_by_node
    selected = tree._select(tree.root)
    if selected is None:
        return {"ok": True, "finished": True, "node_id": None, "event": {"event_type": "no_expandable_nodes"}, "e2b": None}, build_by_node
    selected_node_id = selected.node_id
    if selected_node_id not in build_by_node:
        return {"ok": False, "error": f"no E2B build recorded for selected node {selected_node_id}", "node_id": selected_node_id}, build_by_node
    selected_build = build_by_node[selected_node_id]
    new_node = tree._expand(selected) or selected
    controller_build_action_only(tree, new_node)
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
            mock_stats_before = http_json(
                f"{tree.agent.completion.model_base_url.rsplit('/v1', 1)[0]}/admin/stats",
                timeout=2.0,
            )
            cursor_before_action = int(mock_stats_before["cursor"])
            req = {
                "instance": instance,
                "repo_path": "/workspace/repo",
                "index_store_dir": "/opt/spr_payload/index_store",
                "seq": seq,
                "node_id": new_node.node_id,
                "action": action_payload,
                "action_model": action_model,
                "file_context": new_node.file_context.model_dump(),
            }
            req_path = work / f"seq{seq}_action{idx}.req.json"
            resp_path = work / f"seq{seq}_action{idx}.resp.json"
            timing_path = work / f"seq{seq}_action{idx}.timing.json"
            runner_path = BASE / "e2b_action_runner.py"
            req_path.write_text(json.dumps(req, separators=(",", ":")), encoding="utf-8")
            command = (
                "mkdir -p /tmp/finalbench /opt/finalbench && "
                "PYTHONPATH=/opt/spr_payload PYTHONHASHSEED=0 "
                "/opt/moatless_det_venv/bin/python /opt/spr_payload/mock_llm_server.py "
                "--tcp-host 127.0.0.1 --tcp-port 19999 --traces-root /opt/det_traces "
                "> /tmp/finalbench/mock.log 2>&1 & "
                "MOCK_PID=$!; "
                "for i in $(seq 1 50); do curl -fsS http://127.0.0.1:19999/admin/healthz >/dev/null && break || sleep 0.1; done; "
                f"curl -fsS -X POST -H 'Content-Type: application/json' --data '{{\"instance_id\":\"{instance}\",\"variant\":\"ms\"}}' "
                "http://127.0.0.1:19999/admin/load >/tmp/finalbench/mock_load.json; "
                f"curl -fsS -X POST -H 'Content-Type: application/json' --data '{{\"cursor\":{cursor_before_action}}}' "
                "http://127.0.0.1:19999/admin/rewind >/tmp/finalbench/mock_rewind.json; "
                "PYTHONPATH=/opt/spr_payload:/opt/spr_payload/moatless-det-src "
                "PYTHONHASHSEED=0 OPENAI_API_KEY=dummy CUSTOM_LLM_API_KEY=dummy "
                "LITELLM_LOG=ERROR "
                "/opt/moatless_det_venv/bin/python "
                "/opt/finalbench/e2b_action_runner.py "
                "/tmp/finalbench/action.req.json /tmp/finalbench/action.resp.json; "
                "RC=$?; curl -fsS http://127.0.0.1:19999/admin/stats >/tmp/finalbench/mock_stats.json || true; "
                "kill $MOCK_PID 2>/dev/null || true; wait $MOCK_PID 2>/dev/null || true; exit $RC"
            )
            e2b = e2b_step(
                from_build=selected_build,
                to_build=step_to_build,
                storage=storage,
                command=command,
                timings_path=timing_path,
                uploads=[
                    (runner_path, "/opt/finalbench/e2b_action_runner.py"),
                    (req_path, "/tmp/finalbench/action.req.json"),
                ],
                downloads=[
                    ("/tmp/finalbench/action.resp.json", resp_path),
                    ("/tmp/finalbench/mock_stats.json", work / f"seq{seq}_action{idx}.mock_stats.json"),
                    ("/tmp/finalbench/mock.log", work / f"seq{seq}_action{idx}.mock.log"),
                ],
            )
            e2b_steps.append(e2b)
            if not e2b.get("ok"):
                return {"ok": False, "error": "e2b step failed", "e2b": e2b, "node_id": new_node.node_id}, build_by_node
            resp = json.loads(resp_path.read_text(encoding="utf-8"))
            inner_mock_stats_path = work / f"seq{seq}_action{idx}.mock_stats.json"
            if inner_mock_stats_path.exists():
                inner_mock_stats = json.loads(inner_mock_stats_path.read_text(encoding="utf-8"))
                served_delta = int(inner_mock_stats.get("cursor", cursor_before_action)) - cursor_before_action
                if served_delta > 0:
                    http_json(
                        f"{tree.agent.completion.model_base_url.rsplit('/v1', 1)[0]}/admin/rewind",
                        method="POST",
                        obj={"cursor": cursor_before_action + served_delta},
                        timeout=2.0,
                    )
            if not resp.get("ok"):
                return {"ok": False, "error": "action runner failed", "response": resp, "e2b": e2b, "node_id": new_node.node_id}, build_by_node
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
        # Duplicate/empty-action nodes have no sandbox delta. Reuse the parent build.
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
            "event_type": "e2b_tree_iteration",
            "selected_node_id": selected_node_id,
            "new_node_id": new_node.node_id,
            "total_nodes": len(tree.root.get_all_nodes()),
            "finished_nodes": len(tree.get_finished_nodes()),
            "best_node_id": best.node_id if best else None,
            "n_worker_actions": len(action_events),
            "action_events": action_events,
            "is_duplicate": bool(new_node.is_duplicate),
        },
        "e2b_steps": e2b_steps,
    }, build_by_node


def run_pilot(args: argparse.Namespace) -> dict:
    instance = args.instance
    run_id = f"{args.run_id_prefix}_{instance}_{uuid.uuid4().hex[:6]}"
    work = BASE / "work" / run_id
    results = BASE / "results" / run_id
    logs = work / "logs"
    for p in (work, results):
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    mock_port = free_tcp_port()
    mock_proc = start_mock(instance, mock_port, logs / "mock.log")

    try:
        base_build = args.from_build or str(uuid.uuid4())
        if not args.from_build:
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
        else:
            payload_tar = work / f"{instance}.payload.tar"
            make_payload_tar(instance, payload_tar)
            root_build = str(uuid.uuid4())
            root_timing = work / "root_setup.timing.json"
            root_command = (
                "mkdir -p /opt /workspace /mnt/disk2/dyp && "
                "tar -xf /tmp/finalbench_payload.tar -C /opt && "
                "rm -f /tmp/finalbench_payload.tar && "
                "mv /opt/repo /workspace/repo && "
                "ln -sfn /opt/spr_payload /mnt/disk2/dyp/spr_payload && "
                "ln -sfn /opt/finalbench /mnt/disk2/dyp/finalbench && "
                "ln -sfn /opt/moatless_det_venv /mnt/disk2/dyp/moatless_det_venv && "
                "ln -sfn /opt/spr_payload/moatless-det-src /mnt/disk2/dyp/spr_payload/moatless-det-src && "
                "ln -sfn /opt/spr_payload/index_store /mnt/disk2/dyp/spr_payload/index_store && "
                "test -x /opt/moatless_det_venv/bin/python && "
                "test -d /workspace/repo"
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

        tree_dict = load_initial_tree(instance, mock_port)
        controller_repo = SOURCE_REPOS / f"swe-bench_{instance}"
        tree = controller_tree_from_dict(tree_dict, controller_repo, INDEX_STORE, instance)
        build_by_node = {tree.root.node_id: root_build}
        iterations = []
        for seq in range(1, args.max_steps + 1):
            it, build_by_node = run_one_e2b_iteration(
                tree=tree,
                instance=instance,
                seq=seq,
                build_by_node=build_by_node,
                storage=args.storage,
                work=work,
            )
            iterations.append(it)
            print(f"[{seq}] ok={it.get('ok')} node={it.get('node_id')} build={it.get('build_id')}", flush=True)
            if not it.get("ok") or it.get("finished"):
                break

        e2b_steps = [step for it in iterations for step in (it.get("e2b_steps") or [])]
        ck = [float(s.get("checkpoint_persist_ms", 0.0)) for s in e2b_steps if s.get("ok")]
        rs = [float(s.get("resume_ms", 0.0)) for s in e2b_steps if s.get("ok")]
        mock_stats = http_json(f"http://127.0.0.1:{mock_port}/admin/stats", timeout=2.0)
        out = {
            "ok": all(i.get("ok") for i in iterations),
            "instance": instance,
            "run_id": run_id,
            "semantics": "same recorded Moatless trajectory; controller/SearchTree/mock outside E2B; actions execute inside E2B; each node persisted as an E2B build",
            "measurement_scope": "Table2 ck/rs use inner E2B Go API timings from resume-build finalbench-json, not CLI elapsed time",
            "checkpoint_metric": "checkpoint_persist_ms = Pause() + local snapshot upload",
            "restore_metric": "resume_ms = Factory.ResumeSandbox() return latency",
            "root_setup": root,
            "create": create,
            "iterations": iterations,
            "n_node_builds": len(build_by_node),
            "n_e2b_steps": len(e2b_steps),
            "ck_mean_ms": sum(ck) / len(ck) if ck else None,
            "rs_mean_ms": sum(rs) / len(rs) if rs else None,
            "mock_stats": mock_stats,
        }
        (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    finally:
        stop_proc(mock_proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="django__django-10914")
    ap.add_argument("--max-steps", type=int, default=1)
    ap.add_argument("--run-id-prefix", default="e2b_same_trace")
    ap.add_argument("--storage", default="/var/tmp/e2b-l1-storage")
    ap.add_argument("--from-build", default="")
    ap.add_argument("--root-build", default="")
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--disk-mb", type=int, default=4096)
    ap.add_argument("--fc-version", default="v1.14.1_458ca91")
    args = ap.parse_args()
    out = run_pilot(args)
    print(json.dumps({k: out.get(k) for k in ("ok", "instance", "run_id", "n_e2b_steps", "ck_mean_ms", "rs_mean_ms")}, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
