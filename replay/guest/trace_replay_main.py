#!/usr/bin/env python3
"""trace_replay_main.py — in-VM driver: feeds a recorded schedule to the
production SandboxController.

Runs INSIDE the Firecracker VM (booted via host-side main.py path), where:
  - /testbed_original_data is the read-only data disk (mounted from
    data-{group}.xfs).
  - The guest kernel includes the patched OverlayFS implementation.
  - guest/ code lives at /app.

For each event in the schedule it either:
  - "step":    sleep(latency_ms), then dirty `dirty_mb` MB to /testbed
  - "ckpt":    controller.checkpoint_action(parent_id, tag)
  - "restore": controller.restore_action(target_id)

Records per-event timing to /tmp/replay_results.jsonl.

The DeltaBox feature flags (warm-template, adaptive, prewarm) are passed
through to SandboxController so the same script produces both the full
DeltaBox numbers and the ablation rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sandbox_controller import SandboxController, DumpUnavailableError  # noqa: E402
from measurements import timed_api_call, settle_dumps
from control_channel import AgentProbe, restore_with_ready
from lifecycle import ReplayResources

if os.environ.get('DELTABOX_RESTORE_DIAGNOSTICS') == '1':
    from restore_diagnostics import install_controller
    install_controller()

if os.environ.get('DELTABOX_DUMP_DIAGNOSTICS') == '1':
    from dump_diagnostics import install_controller
    install_controller()

# Reuse the production testbed staging + overlay bring-up.  trace replay must
# stage the real repo before worker-exec indexing; otherwise the checkpointed
# agent only sees an empty /testbed and the index "passes" vacuously.
from bringup import (
    init_testbed_overlay,
    load_instance_data,
    setup_git_environment,
    select_testbed,
    stage_testbed_from_data,
)
from workload_environment import workload_for, prepare_workload


SCHEDULE_PATH = "/tmp/replay_schedule.jsonl"
RESULTS_PATH = "/tmp/replay_results.jsonl"
SANDBOX_ROOT = "/"
OVERLAY_MOUNT = "/testbed"
SNAPSHOT_STORE = "/var/lib/replay/snapshots"
DATASET_PATH = "/app/swe_bench_verified.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--schedule", default=SCHEDULE_PATH)
    p.add_argument("--results", default=RESULTS_PATH)
    p.add_argument("--enable-adaptive", dest="adaptive",
                   action="store_true", default=False)
    p.add_argument("--no-adaptive", dest="adaptive", action="store_false")
    p.add_argument("--warm-template", dest="warm_template",
                   action="store_true", default=True,
                   help="Enable warm-template fork (standard default: on)")
    p.add_argument("--no-warm-template", dest="warm_template",
                   action="store_false")
    p.add_argument("--prewarm", dest="prewarm",
                   action="store_true", default=True)
    p.add_argument("--no-prewarm", dest="prewarm", action="store_false")
    p.add_argument("--sync-dump", action="store_true",
                   help="Block on each dump_future immediately after submit "
                        "(disables the async overlap optimization)")
    p.add_argument("--agent-mode", choices=("dummy", "real"), default="dummy",
                   help="Process under checkpoint: dummy heap agent or the "
                        "socket-free production agent.py with mock NPD")
    p.add_argument("--agent-probe", action="store_true",
                   help="In real-agent mode, send one FIFO/NPD request on "
                        "each step event to prove rollback preserves agent IO")
    p.add_argument("--active-worker-load", action="store_true",
                   help="In real-agent mode, fail unless the checkpointed "
                        "agent process loads and retains real moatless "
                        "FileRepository/CodeIndex/SearchTree state")
    p.add_argument("--worker-exec", action="store_true",
                   help="In real-agent mode, require schedule worker_ops and "
                        "execute them inside the checkpointed agent process")
    p.add_argument("--require-real-agent", action="store_true",
                   help="Fail unless --agent-mode=real. Use for report/final "
                        "runs; the dummy heap agent is development-only.")
    return p.parse_args()


def load_schedule(path: str) -> list[dict]:
    events: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _short_repo_from_instance(instance_id: str) -> str:
    tail = instance_id.split("__", 1)[1] if "__" in instance_id else instance_id
    return tail.rsplit("-", 1)[0]


def _git_has_commit(repo: str, commit: str) -> bool:
    if not commit:
        return False
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", repo, "cat-file", "-e", f"{commit}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _git_head_commit(repo: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={repo}", "-C", repo, "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None


def _copy_testbed(src: str, dest: str = "/testbed_original_data") -> None:
    if os.path.exists(dest):
        subprocess.check_call(["rm", "-rf", dest])
    os.makedirs(dest, exist_ok=True)
    print(f"[stage] {src}  ->  {dest}", flush=True)
    t0 = time.time()
    subprocess.check_call(["cp", "-a", f"{src}/.", f"{dest}/"])
    print(f"[stage] done in {time.time() - t0:.2f}s", flush=True)


def stage_testbed_from_commit(instance_id: str, base_commit: str,
                              data_root: str = "/mnt/data") -> dict:
    """Stage the testbed by finding the data.xfs repo that contains commit.

    P-EAGLE traces include repository.commit for instances that are absent from
    the compact SWE-bench metadata shipped in the VM.  For worker-exec runs we
    must still stage the real repo, otherwise /testbed is empty and the agent
    builds a vacuous code index.
    """
    testbeds_root = os.path.join(data_root, "testbeds")
    short_repo = _short_repo_from_instance(instance_id)
    if not os.path.isdir(testbeds_root):
        raise RuntimeError(
            f"data testbeds root not mounted/found: {testbeds_root}")
    candidates = [
        os.path.join(testbeds_root, name)
        for name in sorted(os.listdir(testbeds_root))
        if name.startswith(f"{short_repo}__")
    ]
    explicit_testbed = os.environ.get("DELTABOX_DATA_TESTBED")
    workload = workload_for(instance_id, base_commit)
    if workload:
        candidates = [os.path.join(testbeds_root, f"{short_repo}__{workload['version']}")]
    if explicit_testbed:
        if workload and explicit_testbed != f"{short_repo}__{workload['version']}":
            raise ValueError("explicit testbed conflicts with verified workload version")
        candidates = [os.path.join(testbeds_root, explicit_testbed)]
    src = select_testbed(candidates, base_commit)
    version = os.path.basename(src).split("__", 1)[1]
    print(f"[replay] staging real testbed by commit instance={instance_id} "
          f"version={version} commit={base_commit}", flush=True)
    _copy_testbed(src)
    return {
        "instance_id": instance_id,
        "version": version,
        "base_commit": base_commit,
        "stage_source": "trajectory_commit",
        "stage_src": src,
        "workload_profile": workload,
    }


def stage_testbed_from_payload_repo(instance_id: str, repo_path: str,
                                    base_commit: str) -> dict:
    if not repo_path or not os.path.isdir(repo_path):
        raise RuntimeError(
            f"active worker repo path missing for {instance_id}: {repo_path!r}")
    head = _git_head_commit(repo_path)
    if head is not None and base_commit and head != base_commit:
        raise RuntimeError(
            f"active worker repo HEAD mismatch for {instance_id}: "
            f"repo={repo_path} head={head} expected={base_commit}")
    print(f"[replay] staging real testbed from active payload repo "
          f"instance={instance_id} repo={repo_path} "
          f"commit={head or base_commit or 'unknown'}",
          flush=True)
    _copy_testbed(repo_path)
    return {
        "instance_id": instance_id,
        "version": "p-eagle-payload",
        "base_commit": base_commit or head,
        "stage_source": "active_payload_repo",
        "stage_src": repo_path,
        "git_available": head is not None,
    }


def stage_replay_testbed(instance_id: str,
                         dataset_path: str = DATASET_PATH) -> dict:
    if not instance_id:
        raise RuntimeError(
            "DELTABOX_INSTANCE_ID is required when staging the real testbed")
    commit_from_trace = os.environ.get("DELTABOX_REPLAY_REPOSITORY_COMMIT", "")
    payload_repo = os.environ.get("DELTABOX_ACTIVE_REPO_PATH", "")
    if payload_repo:
        return stage_testbed_from_payload_repo(
            instance_id, payload_repo, commit_from_trace)
    if commit_from_trace:
        return stage_testbed_from_commit(instance_id, commit_from_trace)
    data = load_instance_data(dataset_path, instance_id)
    if not data:
        if commit_from_trace:
            return stage_testbed_from_commit(instance_id, commit_from_trace)
        raise RuntimeError(
            f"no dataset row for {instance_id!r} in {dataset_path}; "
            "set DELTABOX_REPLAY_REPOSITORY_COMMIT from the trajectory")
    version = data.get("version")
    base_commit = commit_from_trace or data.get("base_commit")
    if not version:
        raise RuntimeError(
            f"dataset row for {instance_id!r} has no version")
    if not base_commit:
        raise RuntimeError(
            f"dataset row for {instance_id!r} has no base_commit")

    print(f"[replay] staging real testbed instance={instance_id} "
          f"version={version}", flush=True)
    stage_testbed_from_data(instance_id, version)
    return {
        "instance_id": instance_id,
        "version": version,
        "base_commit": base_commit,
        "stage_source": "dataset",
    }


def require_nonempty_worker_index(status: dict, context: str) -> None:
    n_files = int(status.get("n_files") or 0)
    n_symbols = int(status.get("n_classes") or 0) + int(
        status.get("n_functions") or 0)
    if (not status.get("ok")
            or not status.get("loaded")
            or not status.get("fingerprint")
            or n_files <= 0
            or int(status.get("total_bytes") or 0) <= 0
            or n_symbols <= 0):
        raise RuntimeError(
            f"worker code index invalid/nonempty gate failed "
            f"({context}): {status}")


def _unlink_many(paths: list[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def spawn_checkpoint_agent(mode: str, resources: ReplayResources) -> tuple[int, int | None, list[subprocess.Popen]]:
    """Launch the checkpoint target under namespace_launcher.

    `dummy` preserves the historical Path-B timing harness. `real` starts the
    production socket-free agent.py plus mock_npd.py: the checkpointed process
    owns only FIFOs/files, while the mock NPD owns the response delay and any
    future socket-like state outside the CRIU dump tree.
    """
    if mode == "dummy":
        agent_pid, ns_init_pid = _spawn_dummy_agent(resources)
        return agent_pid, ns_init_pid, []
    if mode == "real":
        return _spawn_real_agent(resources)
    raise ValueError(f"unknown agent mode {mode!r}")


def _spawn_dummy_agent(resources: ReplayResources) -> tuple[int, int | None]:
    """Launch the dummy agent under namespace_launcher (PID-ns isolation,
    same as guest/main.py does for guest/agent.py).

    Returns the host PID of the ns-init child (NOT the launcher parent).
    The ns-init host PID is written by namespace_launcher to AGENT_PID_FILE
    after unshare()+fork(); CRIU `--tree` MUST target this PID, otherwise
    it walks /proc and finds the ns-init as a "nested pid namespace" child
    of the launcher and aborts with "Can't dump nested pid namespace".
    """
    pidfile = "/tmp/agent_ns_pid"
    ns_init_pidfile = "/tmp/agent_ns_init_pid"
    fifo = "/tmp/template_ctrl.in"
    _unlink_many([pidfile, ns_init_pidfile, fifo, "/tmp/template_ctrl.out"])
    env = os.environ.copy()
    env["AGENT_NS_INIT_PID_FILE"] = ns_init_pidfile
    cmd = [
        "/bin/bash", "-c",
        "exec python3 /app/namespace_launcher.py "
        "python3 /app/trace_replay_dummy_agent.py",
    ]
    proc = resources.popen_namespace(cmd, stdout=sys.stdout, stderr=sys.stderr, env=env)
    # Wait for both: pidfile (proves launcher unshared+forked) and FIFO
    # (proves dummy agent installed its template endpoint).
    deadline = time.time() + 5.0
    agent_pid: int | None = None
    ns_init_pid: int | None = None
    need_ns_init_pid = bool(env.get("DELTABOX_FIXED_ACTIVE_PID"))
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"dummy agent exited rc={proc.returncode} during boot")
        if agent_pid is None and os.path.exists(pidfile):
            try:
                agent_pid = int(open(pidfile).read().strip())
            except (OSError, ValueError):
                pass
        if ns_init_pid is None and os.path.exists(ns_init_pidfile):
            try:
                candidate = int(open(ns_init_pidfile).read().strip())
                resources.own_namespace(candidate)
                ns_init_pid = candidate
            except (OSError, ValueError):
                pass
        if (agent_pid is not None and os.path.exists(fifo)
                and (ns_init_pid is not None or not need_ns_init_pid)):
            resources.own_namespace(ns_init_pid or agent_pid)
            return agent_pid, ns_init_pid
        time.sleep(0.05)
    raise TimeoutError(
        f"dummy agent boot timeout: agent_pid={agent_pid} "
        f"fifo_exists={os.path.exists(fifo)}")


def _spawn_real_agent(resources: ReplayResources) -> tuple[int, int | None, list[subprocess.Popen]]:
    pidfile = "/tmp/agent_ns_pid"
    ns_init_pidfile = "/tmp/agent_ns_init_pid"
    template_fifo = "/tmp/template_ctrl.in"
    agent_in = "/tmp/agent.in"
    agent_out = "/tmp/agent.out"
    npd_req = "/tmp/npd_req.fifo"
    npd_notify = "/tmp/npd_notify.fifo"
    _unlink_many([
        pidfile, ns_init_pidfile, template_fifo, "/tmp/template_ctrl.out",
        agent_in, agent_out, npd_req, npd_notify,
    ])
    for d in ("/tmp/npd_requests", "/tmp/npd_responses"):
        try:
            for name in os.listdir(d):
                _unlink_many([os.path.join(d, name)])
        except OSError:
            os.makedirs(d, exist_ok=True)
    with open("/tmp/npd_current_epoch", "w") as f:
        f.write("0\n")

    npd_env = os.environ.copy()
    npd_env.setdefault("BENCH_LLM_RTT_MS", "5")
    npd_proc = resources.popen(
        ["python3", "/app/mock_npd.py"],
        stdout=sys.stdout, stderr=sys.stderr, env=npd_env,
    )
    agent_env = os.environ.copy()
    agent_env["AGENT_WARM_TEMPLATE"] = "1"
    agent_env["AGENT_NS_INIT_PID_FILE"] = ns_init_pidfile
    agent_env.setdefault("MODEL_NAME", "mock-npd")
    agent_command = ["/bin/bash", "-c", "exec python3 /app/namespace_launcher.py python3 /app/agent.py"]
    if agent_env.get("DELTABOX_ASYNC_INCREMENTAL_DUMP") == "1":
        # Diagnostics are an external append-only stream, not a snapshot of
        # the SSH pipe. This also avoids capturing mutable anonymous pipe data.
        with open("/tmp/replay-agent.log", "ab", buffering=0) as agent_log:
            agent_proc = resources.popen_namespace(agent_command, stdin=subprocess.DEVNULL,
                stdout=agent_log, stderr=agent_log, env=agent_env)
    else:
        agent_proc = resources.popen_namespace(agent_command,
            stdout=sys.stdout, stderr=sys.stderr, env=agent_env)

    deadline = time.time() + 10.0
    agent_pid: int | None = None
    ns_init_pid: int | None = None
    need_ns_init_pid = bool(agent_env.get("DELTABOX_FIXED_ACTIVE_PID"))
    while time.time() < deadline:
        for name, proc in (("mock_npd", npd_proc), ("agent", agent_proc)):
            if proc.poll() is not None:
                raise RuntimeError(f"{name} exited rc={proc.returncode} during boot")
        if agent_pid is None and os.path.exists(pidfile):
            try:
                agent_pid = int(open(pidfile).read().strip())
            except (OSError, ValueError):
                pass
        if ns_init_pid is None and os.path.exists(ns_init_pidfile):
            try:
                candidate = int(open(ns_init_pidfile).read().strip())
                resources.own_namespace(candidate)
                ns_init_pid = candidate
            except (OSError, ValueError):
                pass
        ready = (
            agent_pid is not None
            and (ns_init_pid is not None or not need_ns_init_pid)
            and os.path.exists(template_fifo)
            and os.path.exists(agent_in)
            and os.path.exists(agent_out)
            and os.path.exists(npd_req)
            and os.path.exists(npd_notify)
        )
        if ready:
            return agent_pid, ns_init_pid, [npd_proc, agent_proc]
        time.sleep(0.05)
    raise TimeoutError(
        f"real agent boot timeout: agent_pid={agent_pid} "
        f"template={os.path.exists(template_fifo)} "
        f"agent_in={os.path.exists(agent_in)} agent_out={os.path.exists(agent_out)} "
        f"npd_req={os.path.exists(npd_req)} npd_notify={os.path.exists(npd_notify)}")


def _sidecar_enabled() -> bool:
    return os.environ.get("DELTABOX_WORKER_INDEX_SIDECAR") == "1"


def _start_index_sidecar(resources: ReplayResources) -> tuple[subprocess.Popen | None, str | None]:
    if not _sidecar_enabled():
        return None, None
    host = os.environ.get("DELTABOX_WORKER_INDEX_SIDECAR_HOST", "127.0.0.1")
    port = int(os.environ.get("DELTABOX_WORKER_INDEX_SIDECAR_PORT", "18765"))
    url = f"http://{host}:{port}"
    env = os.environ.copy()
    proc = resources.popen(
        ["python3", "-u", "/app/index_sidecar.py",
         "--host", host, "--port", str(port)],
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=env,
    )
    deadline = time.time() + float(os.environ.get(
        "DELTABOX_WORKER_INDEX_SIDECAR_BOOT_TIMEOUT", "10"))
    last_err = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"index sidecar exited during boot rc={proc.returncode}")
        try:
            with urlopen(url + "/healthz", timeout=0.5) as resp:
                if resp.status == 200:
                    os.environ["AGENT_WORKER_INDEX_SIDECAR_URL"] = url
                    print(f"[replay] worker index sidecar ready: {url}",
                          flush=True)
                    return proc, url
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(0.05)
    proc.terminate()
    raise TimeoutError(f"index sidecar boot timeout url={url}: {last_err}")


def _rebuild_sidecar_index_after_restore(
    agent_channel: "AgentProbe | None",
) -> dict | None:
    if not _sidecar_enabled() or agent_channel is None:
        return None
    agent_channel.reset()
    status = agent_channel.control({
        "ctrl": "worker_index_build",
        "root": OVERLAY_MOUNT,
    }, timeout=float(os.environ.get(
        "DELTABOX_WORKER_INDEX_TIMEOUT", "300")))
    require_nonempty_worker_index(status, "sidecar rebuild after restore")
    return status


def _snapshot_sidecar_index(
    agent_channel: "AgentProbe | None",
    snapshot_id: str,
) -> dict | None:
    if not _sidecar_enabled() or agent_channel is None:
        return None
    status = agent_channel.control({
        "ctrl": "worker_index_snapshot",
        "snapshot_id": snapshot_id,
    }, timeout=float(os.environ.get(
        "DELTABOX_WORKER_INDEX_TIMEOUT", "300")))
    require_nonempty_worker_index(status, f"sidecar snapshot {snapshot_id}")
    return status


def _restore_sidecar_index_snapshot(
    agent_channel: "AgentProbe | None",
    snapshot_id: str,
) -> dict | None:
    if not _sidecar_enabled() or agent_channel is None:
        return None
    agent_channel.reset()
    status = agent_channel.control({
        "ctrl": "worker_index_restore_snapshot",
        "snapshot_id": snapshot_id,
    }, timeout=float(os.environ.get(
        "DELTABOX_WORKER_INDEX_TIMEOUT", "300")))
    if status.get("ok") and not status.get("missing_snapshot"):
        require_nonempty_worker_index(
            status, f"sidecar restore snapshot {snapshot_id}")
        return status
    rebuilt = _rebuild_sidecar_index_after_restore(agent_channel)
    if rebuilt is not None:
        rebuilt["snapshot_restore_fallback"] = status
    return rebuilt


def process_footprint(pid: int) -> dict:
    out = {
        "pid": pid,
        "rss_kb": None,
        "pss_kb": None,
        "private_dirty_kb": None,
        "private_clean_kb": None,
        "vm_size_kb": None,
        "threads": None,
        "n_maps": None,
    }
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                out["rss_kb"] = int(line.split()[1])
            elif line.startswith("VmSize:"):
                out["vm_size_kb"] = int(line.split()[1])
            elif line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
    except OSError as e:
        out["err"] = f"status: {e}"
        return out
    try:
        n_maps = 0
        pss = private_dirty = private_clean = 0
        for line in open(f"/proc/{pid}/smaps"):
            if "-" in line.split(None, 1)[0]:
                n_maps += 1
                continue
            if line.startswith("Pss:"):
                pss += int(line.split()[1])
            elif line.startswith("Private_Dirty:"):
                private_dirty += int(line.split()[1])
            elif line.startswith("Private_Clean:"):
                private_clean += int(line.split()[1])
        out.update({
            "pss_kb": pss,
            "private_dirty_kb": private_dirty,
            "private_clean_kb": private_clean,
            "n_maps": n_maps,
        })
    except OSError as e:
        out["smaps_err"] = str(e)
    return out


def worker_exec_is_test_timeout(result: dict | None) -> bool:
    """Return True only for run_tests commands that reached their timeout.

    A slow generated test is not a C/R failure. Protocol timeouts, file/edit
    failures, index failures, and restore failures still fail the replay.
    """
    if not isinstance(result, dict) or result.get("ok"):
        return False
    failed = [
        r for r in (result.get("results") or [])
        if isinstance(r, dict) and not r.get("ok")
    ]
    if not failed:
        return False
    return all(
        r.get("type") == "run_tests" and r.get("err") == "TimeoutExpired"
        for r in failed
    )


def replay_lightweight_worker_ops(agent_channel: AgentControlChannel,
                                  replay_cmds: list[dict],
                                  *,
                                  root: str,
                                  timeout_s: float,
                                  timeout_margin_s: float) -> dict:
    """Replay worker ops accumulated by lightweight checkpoints.

    A lightweight checkpoint is only a logical node: restore lands on the
    nearest physical ancestor and then replays the skipped read-only actions.
    The replay cost is part of the restore critical path for the LW row.
    """
    t0 = time.time()
    out: dict = {
        "ok": True,
        "n_cmds": len(replay_cmds),
        "n_ops": 0,
        "results": [],
        "wall_ms": 0.0,
    }
    for i, item in enumerate(replay_cmds):
        ops = item.get("worker_ops") or []
        if not isinstance(ops, list):
            ops = []
        out["n_ops"] += len(ops)
        if not ops:
            continue
        res = agent_channel.control({
            "ctrl": "worker_exec",
            "ops": ops,
            "root": root,
            "replay_index": i,
            "replay_action": item.get("action"),
        }, timeout=timeout_s + timeout_margin_s)
        row = {
            "index": i,
            "action": item.get("action"),
            "command": item.get("command"),
            "n_ops": len(ops),
            "worker_exec": res,
        }
        out["results"].append(row)
        if not res.get("ok"):
            out["ok"] = False
            out["first_bad"] = row
            out["wall_ms"] = (time.time() - t0) * 1000
            return out
    out["wall_ms"] = (time.time() - t0) * 1000
    return out


def write_dirty(dirty_mb: int, step_idx: int) -> None:
    """Write `dirty_mb` MB of incompressible data to /testbed/step_{i}.dat
    so the overlay upper layer has realistic dirt before each checkpoint."""
    if dirty_mb <= 0:
        return
    path = f"/testbed/replay_step_{step_idx}.dat"
    with open(path, "wb") as f:
        # 1 MB chunks of os.urandom (incompressible — defeats any layer
        # compression and forces real bytes into the upper inode).
        chunk_mb = 1
        for _ in range(dirty_mb // chunk_mb):
            f.write(os.urandom(chunk_mb * 1024 * 1024))
        rem = dirty_mb % chunk_mb
        if rem:
            f.write(os.urandom(rem * 1024 * 1024))
        f.flush()
        os.fsync(f.fileno())


def replay_file_digest() -> dict:
    digest = hashlib.sha256()
    count = 0
    total = 0
    try:
        names = sorted(n for n in os.listdir(OVERLAY_MOUNT)
                       if n.startswith("replay_step_") and n.endswith(".dat"))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(OVERLAY_MOUNT, name)
        try:
            st = os.stat(path)
            digest.update(name.encode())
            digest.update(str(st.st_size).encode())
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            count += 1
            total += st.st_size
        except OSError as e:
            digest.update(f"{name}:ERR:{e}".encode())
    return {
        "replay_file_sha256": digest.hexdigest(),
        "replay_file_count": count,
        "replay_file_bytes": total,
    }


def _tree_footprint(root: str) -> dict:
    digest = hashlib.sha256()
    out = {
        "path": root,
        "exists": os.path.exists(root),
        "file_count": 0,
        "dir_count": 0,
        "symlink_count": 0,
        "special_count": 0,
        "bytes": 0,
        "disk_bytes": 0,
        "sha256": None,
        "errors": [],
    }
    if not out["exists"]:
        out["sha256"] = digest.hexdigest()
        return out

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        try:
            out["disk_bytes"] += os.lstat(dirpath).st_blocks * 512
        except OSError:
            pass
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        for dirname in dirnames:
            rel = os.path.join(rel_dir, dirname)
            digest.update(b"D\0")
            digest.update(rel.encode(errors="surrogateescape"))
            digest.update(b"\0")
            out["dir_count"] += 1
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            rel = os.path.join(rel_dir, filename)
            try:
                st = os.lstat(path)
                out["disk_bytes"] += st.st_blocks * 512
                digest.update(b"F\0")
                digest.update(rel.encode(errors="surrogateescape"))
                digest.update(b"\0")
                digest.update(str(st.st_mode).encode())
                digest.update(b"\0")
                if stat.S_ISREG(st.st_mode):
                    digest.update(str(st.st_size).encode())
                    digest.update(b"\0")
                    with open(path, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            digest.update(chunk)
                    out["file_count"] += 1
                    out["bytes"] += st.st_size
                elif stat.S_ISLNK(st.st_mode):
                    target = os.readlink(path)
                    digest.update(target.encode(errors="surrogateescape"))
                    out["symlink_count"] += 1
                else:
                    # Overlay whiteouts and opaque markers may appear as
                    # special inodes. Count them, but never try to read them.
                    digest.update(str(st.st_rdev).encode())
                    out["special_count"] += 1
            except OSError as e:
                digest.update(f"{rel}:ERR:{e}".encode(errors="replace"))
                if len(out["errors"]) < 8:
                    out["errors"].append({"path": rel, "err": str(e)})
    out["sha256"] = digest.hexdigest()
    return out


def overlay_delta_footprint(controller: SandboxController,
                            snapshot_layers: list[str] | None = None,
                            include_current_upper: bool = True) -> dict:
    """Measure the replay-visible filesystem delta, not the base repo.

    The old metric only counted synthetic replay_step_*.dat files. Worker-exec
    changes live in overlay upper/layer directories, so Table 2 needs this
    full delta side of the checkpoint footprint.
    """
    out = replay_file_digest()
    base = os.path.abspath(getattr(controller, "base_layer",
                                   "/testbed_original_data"))
    seen: set[str] = set()
    components = []

    def add_component(kind: str, path: str | None) -> None:
        if not path:
            return
        abs_path = os.path.abspath(path)
        if abs_path == base or abs_path in seen:
            return
        seen.add(abs_path)
        fp = _tree_footprint(abs_path)
        fp["kind"] = kind
        components.append(fp)

    if include_current_upper:
        add_component("current_upper", getattr(controller, "current_upper", None))
    for layer in snapshot_layers or []:
        add_component("snapshot_layer", layer)

    digest = hashlib.sha256()
    total_bytes = 0
    total_disk_bytes = 0
    total_files = 0
    total_dirs = 0
    total_symlinks = 0
    total_special = 0
    for comp in components:
        # Logical digest: physical layer paths differ across checkpoint and
        # restore. Combine non-empty component content only, so the digest is
        # stable for the same logical overlay delta.
        if (int(comp.get("bytes") or 0)
                or int(comp.get("file_count") or 0)
                or int(comp.get("special_count") or 0)):
            digest.update(str(comp.get("sha256")).encode())
            digest.update(b"\0")
        total_bytes += int(comp.get("bytes") or 0)
        total_disk_bytes += int(comp.get("disk_bytes") or 0)
        total_files += int(comp.get("file_count") or 0)
        total_dirs += int(comp.get("dir_count") or 0)
        total_symlinks += int(comp.get("symlink_count") or 0)
        total_special += int(comp.get("special_count") or 0)

    out.update({
        "overlay_delta_bytes": total_bytes,
        "overlay_delta_disk_bytes": total_disk_bytes,
        "overlay_delta_file_count": total_files,
        "overlay_delta_dir_count": total_dirs,
        "overlay_delta_symlink_count": total_symlinks,
        "overlay_delta_special_count": total_special,
        "overlay_delta_sha256": digest.hexdigest(),
        "overlay_components": components,
    })
    return out


def current_overlay_footprint(controller: SandboxController,
                              parent_ckpt_id: str | None) -> dict:
    if parent_ckpt_id and parent_ckpt_id in controller.registry:
        layers = controller.registry[parent_ckpt_id].get("layers", [])
    else:
        layers = [getattr(controller, "base_layer", "/testbed_original_data")]
    return overlay_delta_footprint(
        controller, snapshot_layers=layers, include_current_upper=True)


def restored_overlay_footprint(controller: SandboxController,
                               target_ckpt_id: str) -> dict:
    layers = controller.registry.get(target_ckpt_id, {}).get("layers", [])
    return overlay_delta_footprint(
        controller, snapshot_layers=layers, include_current_upper=True)


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    vals = sorted(vals)
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": vals[len(vals) // 2],
        "min": vals[0],
        "max": vals[-1],
        "p95": vals[int(0.95 * (len(vals) - 1))],
    }


def build_run_summary(results: list[dict],
                      agent_mode: str,
                      require_real: bool,
                      active_worker_required: bool,
                      worker_exec_required: bool,
                      worker_index_initial: dict | None,
                      active_worker_initial: dict | None) -> dict:
    probe_rows = [
        r for r in results
        if r.get("kind") in ("ckpt", "restore")
        and r.get("agent_probe") is not None
    ]
    probe_bad = [
        r for r in probe_rows
        if (not r.get("agent_probe", {}).get("ok")
                or r.get("agent_probe", {}).get("mismatch"))
    ]
    ck_probe_rows = [
        r for r in probe_rows
        if r.get("kind") == "ckpt"
    ]
    restore_probe_rows = [
        r for r in probe_rows
        if r.get("kind") == "restore"
    ]
    rss_vals: list[float] = []
    pss_vals: list[float] = []
    vm_vals: list[float] = []
    private_dirty_vals: list[float] = []
    fs_bytes: list[float] = []
    fs_overlay_bytes: list[float] = []
    fs_overlay_disk_bytes: list[float] = []
    worker_exec_rows = [
        r for r in results
        if r.get("kind") == "ckpt" and r.get("worker_exec") is not None
    ]
    worker_exec_test_timeout = [
        r for r in worker_exec_rows
        if worker_exec_is_test_timeout(r.get("worker_exec"))
    ]
    worker_exec_bad = [
        r for r in worker_exec_rows
        if (not r.get("worker_exec", {}).get("ok")
                and not worker_exec_is_test_timeout(r.get("worker_exec")))
    ]
    error_rows = [
        r for r in results
        if r.get("ok") is False or r.get("err")
    ]
    worker_exec_ops = sum(
        int(r.get("worker_exec", {}).get("n_ops") or 0)
        for r in worker_exec_rows
    )

    for row in results:
        fp = (row.get("agent_footprint")
              or row.get("agent_footprint_after_restore"))
        if isinstance(fp, dict):
            for key, out in (
                ("rss_kb", rss_vals),
                ("pss_kb", pss_vals),
                ("vm_size_kb", vm_vals),
                ("private_dirty_kb", private_dirty_vals),
            ):
                val = fp.get(key)
                if val is not None:
                    out.append(float(val))
        fs = row.get("fs_footprint") or row.get("fs_footprint_after_restore")
        if isinstance(fs, dict) and fs.get("replay_file_bytes") is not None:
            fs_bytes.append(float(fs["replay_file_bytes"]))
        if isinstance(fs, dict) and fs.get("overlay_delta_bytes") is not None:
            fs_overlay_bytes.append(float(fs["overlay_delta_bytes"]))
        if isinstance(fs, dict) and fs.get("overlay_delta_disk_bytes") is not None:
            fs_overlay_disk_bytes.append(float(fs["overlay_delta_disk_bytes"]))

    return {
        "kind": "run_summary",
        "agent_mode": agent_mode,
        "require_real_agent": require_real,
        "correctness_mode": (
            "worker_exec_action"
            if worker_exec_required else
            "active_worker_state"
            if active_worker_required else
            ("liveness_probe" if probe_rows else "none")
        ),
        "worker_exec_required": worker_exec_required,
        "worker_index_initial": worker_index_initial,
        "worker_index_loaded": bool(
            worker_index_initial and worker_index_initial.get("loaded")),
        "worker_exec_ckpt_n": len(worker_exec_rows),
        "worker_exec_ops_n": worker_exec_ops,
        "worker_exec_test_timeout_n": len(worker_exec_test_timeout),
        "worker_exec_test_timeouts": [
            {
                "ev_i": r.get("ev_i"),
                "schedule_ckpt_id": r.get("schedule_ckpt_id"),
                "worker_exec": r.get("worker_exec"),
            }
            for r in worker_exec_test_timeout[:8]
        ],
        "worker_exec_bad_n": len(worker_exec_bad),
        "worker_exec_failures": [
            {
                "ev_i": r.get("ev_i"),
                "schedule_ckpt_id": r.get("schedule_ckpt_id"),
                "worker_exec": r.get("worker_exec"),
            }
            for r in worker_exec_bad[:8]
        ],
        "error_n": len(error_rows),
        "first_error": (
            {
                "ev_i": error_rows[0].get("ev_i"),
                "kind": error_rows[0].get("kind"),
                "err": error_rows[0].get("err"),
                "msg": error_rows[0].get("msg"),
            }
            if error_rows else None
        ),
        "active_worker_required": active_worker_required,
        "active_worker_loaded": (
            bool(active_worker_initial and active_worker_initial.get("loaded"))
        ),
        "active_worker_initial": active_worker_initial,
        "agent_probe_enabled": bool(probe_rows),
        "agent_probe_n": len(probe_rows),
        "agent_probe_ckpt_n": len(ck_probe_rows),
        "agent_probe_restore_n": len(restore_probe_rows),
        "agent_probe_bad_n": len(probe_bad),
        "agent_probe_mismatch_n": sum(
            1 for r in probe_rows
            if r.get("agent_probe", {}).get("mismatch")),
        "agent_probe_failures": [
            {
                "ev_i": r.get("ev_i"),
                "kind": r.get("kind"),
                "probe": r.get("agent_probe"),
            }
            for r in probe_bad[:8]
        ],
        "agent_rss_kb": _stats(rss_vals),
        "agent_pss_kb": _stats(pss_vals),
        "agent_vm_size_kb": _stats(vm_vals),
        "agent_private_dirty_kb": _stats(private_dirty_vals),
        "fs_replay_file_bytes": _stats(fs_bytes),
        "fs_overlay_delta_bytes": _stats(fs_overlay_bytes),
        "fs_overlay_delta_disk_bytes": _stats(fs_overlay_disk_bytes),
    }


def process_memory_digest(pid: int, mode: str = "large_anon") -> dict:
    digest = hashlib.sha256()
    ranges: list[dict] = []
    try:
        maps = open(f"/proc/{pid}/maps").read().splitlines()
    except OSError as e:
        return {"ok": False, "err": f"maps: {e}", "pid": pid}
    smaps_meta: dict[str, dict] = {}
    try:
        current_key = None
        for line in open(f"/proc/{pid}/smaps").read().splitlines():
            fields = line.split(None, 5)
            if fields and "-" in fields[0]:
                current_key = fields[0]
                smaps_meta[current_key] = {}
                continue
            if current_key is None or ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key in (
                "Size", "Rss", "Pss", "Shared_Clean",
                "Shared_Dirty", "Private_Clean", "Private_Dirty",
                "Referenced", "Anonymous", "AnonHugePages", "VmFlags",
            ):
                smaps_meta[current_key][key] = value.strip()
    except OSError:
        smaps_meta = {}
    try:
        mem_fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    except OSError as e:
        return {"ok": False, "err": f"mem: {e}", "pid": pid}
    try:
        for line in maps:
            parts = line.split(None, 5)
            if len(parts) < 5:
                continue
            addr, perms = parts[0], parts[1]
            path = parts[5] if len(parts) > 5 else ""
            if "r" not in perms or "w" not in perms or "p" not in perms:
                continue
            start_s, end_s = addr.split("-")
            start = int(start_s, 16)
            end = int(end_s, 16)
            size = end - start
            if mode == "large_anon":
                # The replay dummy agent's heap is a large anonymous/private
                # RW allocation. Avoid noisy small interpreter arenas unless
                # the real-agent validator explicitly requests them.
                if path not in ("", "[heap]") or size < 4 * 1024 * 1024:
                    continue
            elif mode == "all_rw_private":
                # Real-agent validation must include small writable private
                # mappings such as python's data/bss.  Those are exactly where
                # parent-hole bugs can otherwise hide.
                pass
            else:
                return {"ok": False, "err": f"unknown digest mode {mode}",
                        "pid": pid}
            rd = 0
            range_digest = hashlib.sha256()
            try:
                os.lseek(mem_fd, start, os.SEEK_SET)
                remaining = size
                while remaining > 0:
                    chunk = os.read(mem_fd, min(1 << 20, remaining))
                    if not chunk:
                        break
                    digest.update(chunk)
                    range_digest.update(chunk)
                    rd += len(chunk)
                    remaining -= len(chunk)
            except OSError as e:
                ranges.append({
                    "start": start_s, "end": end_s, "path": path,
                    "perms": perms, "size": size, "read": rd,
                    "smaps": smaps_meta.get(addr, {}),
                    "err": str(e),
                })
                continue
            ranges.append({
                "start": start_s, "end": end_s, "path": path,
                "perms": perms, "size": size, "read": rd,
                "smaps": smaps_meta.get(addr, {}),
                "sha256": range_digest.hexdigest(),
            })
    finally:
        os.close(mem_fd)
    total = sum(r.get("read", 0) for r in ranges)
    return {
        "ok": total > 0,
        "pid": pid,
        "mode": mode,
        "mem_sha256": digest.hexdigest(),
        "mem_bytes": total,
        "ranges": ranges,
    }


def memory_diff_summary(left: dict, right: dict, limit: int = 12) -> dict:
    def _key(r: dict) -> tuple:
        return (r.get("start"), r.get("end"), r.get("path"))

    lmap = {_key(r): r for r in left.get("ranges", []) if r.get("sha256")}
    rmap = {_key(r): r for r in right.get("ranges", []) if r.get("sha256")}
    common = sorted(set(lmap) & set(rmap),
                    key=lambda k: int(k[0], 16) if k[0] else -1)
    diffs = []
    for key in common:
        l = lmap[key]
        r = rmap[key]
        if l.get("sha256") == r.get("sha256"):
            continue
        diffs.append({
            "start": key[0],
            "end": key[1],
            "path": key[2],
            "read": l.get("read"),
            "left_sha256": l.get("sha256"),
            "right_sha256": r.get("sha256"),
        })
        if len(diffs) >= limit:
            break
    only_left = sorted(set(lmap) - set(rmap),
                       key=lambda k: int(k[0], 16) if k[0] else -1)
    only_right = sorted(set(rmap) - set(lmap),
                        key=lambda k: int(k[0], 16) if k[0] else -1)
    return {
        "left_ranges": len(lmap),
        "right_ranges": len(rmap),
        "common_ranges": len(common),
        "diff_count_limited": len(diffs),
        "diffs": diffs,
        "only_left": [
            {"start": k[0], "end": k[1], "path": k[2]}
            for k in only_left[:limit]
        ],
        "only_right": [
            {"start": k[0], "end": k[1], "path": k[2]}
            for k in only_right[:limit]
        ],
    }


def validate_incremental_equivalence(controller: SandboxController,
                                     ckpt_ids: list[str], resources=None) -> list[dict]:
    """Restore production incremental images and compare to full twins.

    This is the final durable-image oracle: both sides are CRIU images captured
    from the same stopped checkpoint state.  Warm-template/live-process state is
    deliberately not used because the template runs Python/FIFO control logic
    after the dump and drifts at byte granularity.
    """
    out: list[dict] = []
    old_force = os.environ.get("DELTABOX_FORCE_CRIU_RESTORE")
    old_stopped = os.environ.get("DELTABOX_CRIU_RESTORE_LEAVE_STOPPED")
    os.environ["DELTABOX_FORCE_CRIU_RESTORE"] = "1"
    os.environ["DELTABOX_CRIU_RESTORE_LEAVE_STOPPED"] = "1"
    try:
        for ckpt_id in ckpt_ids:
            target = controller.registry.get(ckpt_id)
            if not target:
                continue
            full_id = target.get("validation_full_id")
            if not full_id or full_id not in controller.registry:
                out.append({
                    "kind": "validation",
                    "runtime_ckpt_id": ckpt_id,
                    "ok": False,
                    "err": "missing_full_twin",
                    "validation_full_id": full_id,
                })
                continue

            def _restore_observe(which: str, restore_id: str) -> dict:
                t0 = time.time()
                try:
                    r = restore_with_ready(controller, restore_id, None, resources)
                    return {
                        "which": which,
                        "ok": True,
                        "restore": r,
                        "mem": process_memory_digest(
                            controller.agent_pid,
                            mode=os.environ.get(
                                "DELTABOX_VALIDATION_MEM_MODE",
                                "all_rw_private")),
                        "files": replay_file_digest(),
                        "wall_ms": (time.time() - t0) * 1000,
                    }
                except Exception as e:  # noqa: BLE001
                    return {
                        "which": which,
                        "ok": False,
                        "err": type(e).__name__,
                        "msg": str(e),
                        "wall_ms": (time.time() - t0) * 1000,
                    }

            inc = _restore_observe("incremental", ckpt_id)
            full = _restore_observe("full_twin", full_id)
            mem_diff = memory_diff_summary(
                inc.get("mem", {}), full.get("mem", {}))
            ok = (
                inc.get("ok")
                and full.get("ok")
                and inc.get("mem", {}).get("ok")
                and full.get("mem", {}).get("ok")
                and inc.get("mem", {}).get("mem_sha256")
                    == full.get("mem", {}).get("mem_sha256")
                and inc.get("mem", {}).get("mem_bytes")
                    == full.get("mem", {}).get("mem_bytes")
                and inc.get("files", {}).get("replay_file_sha256")
                    == full.get("files", {}).get("replay_file_sha256")
                and inc.get("files", {}).get("replay_file_bytes")
                    == full.get("files", {}).get("replay_file_bytes")
            )
            print(f"[PROBE] incremental/full-twin restore equivalence "
                  f"ckpt={ckpt_id} ok={ok} "
                  f"inc_mem={inc.get('mem', {}).get('mem_sha256')} "
                  f"full_mem={full.get('mem', {}).get('mem_sha256')} "
                  f"inc_files={inc.get('files', {}).get('replay_file_sha256')} "
                  f"full_files={full.get('files', {}).get('replay_file_sha256')}",
                  flush=True)
            row = {
                "kind": "validation",
                "runtime_ckpt_id": ckpt_id,
                "validation_full_id": full_id,
                "ok": bool(ok),
                "incremental": inc,
                "full_twin": full,
                "pre_live_mem": target.get("validation_pre_live_mem"),
                "post_live_mem": target.get("validation_post_live_mem"),
                "mem_diff": mem_diff,
            }
            if (not ok and os.environ.get(
                    "DELTABOX_VALIDATE_FULL_FULL_ON_FAIL") == "1"):
                full2 = _restore_observe("full_twin_second", full_id)
                full_full_ok = (
                    full.get("ok")
                    and full2.get("ok")
                    and full.get("mem", {}).get("ok")
                    and full2.get("mem", {}).get("ok")
                    and full.get("mem", {}).get("mem_sha256")
                        == full2.get("mem", {}).get("mem_sha256")
                    and full.get("mem", {}).get("mem_bytes")
                        == full2.get("mem", {}).get("mem_bytes")
                    and full.get("files", {}).get("replay_file_sha256")
                        == full2.get("files", {}).get("replay_file_sha256")
                    and full.get("files", {}).get("replay_file_bytes")
                        == full2.get("files", {}).get("replay_file_bytes")
                )
                print(f"[PROBE] full/full restore self-check "
                      f"ckpt={ckpt_id} ok={full_full_ok} "
                      f"full1_mem={full.get('mem', {}).get('mem_sha256')} "
                      f"full2_mem={full2.get('mem', {}).get('mem_sha256')}",
                      flush=True)
                row["full_twin_second"] = full2
                row["full_full_ok"] = bool(full_full_ok)
                row["full_full_mem_diff"] = memory_diff_summary(
                    full.get("mem", {}), full2.get("mem", {}))
            out.append(row)
    finally:
        if old_force is None:
            os.environ.pop("DELTABOX_FORCE_CRIU_RESTORE", None)
        else:
            os.environ["DELTABOX_FORCE_CRIU_RESTORE"] = old_force
        if old_stopped is None:
            os.environ.pop("DELTABOX_CRIU_RESTORE_LEAVE_STOPPED", None)
        else:
            os.environ["DELTABOX_CRIU_RESTORE_LEAVE_STOPPED"] = old_stopped
    return out


_MEMCURVE_READONLY_OPS = {
    "find_symbol", "grep", "view_file", "noop", "list_files",
    "view_code", "find_files", "semantic_search",
}


def _memcurve_ops_read_only(worker_ops) -> bool:
    """True iff every replayed worker op is read-only (skip-eligible)."""
    if not isinstance(worker_ops, list) or not worker_ops:
        return False
    for op in worker_ops:
        name = (op or {}).get("type") or ""
        if name not in _MEMCURVE_READONLY_OPS:
            return False
    return True


def _pss_kb(pid) -> int | None:
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError, TypeError):
        return None
    return None


def _meminfo_subset() -> dict:
    out = {}
    try:
        for line in open("/proc/meminfo"):
            key = line.split(":")[0]
            if key in ("MemTotal", "MemFree", "MemAvailable", "Cached",
                       "Shmem", "AnonPages"):
                out[key.lower() + "_kb"] = int(line.split()[1])
    except OSError:
        pass
    return out


def _memcurve_sample(controller, ev_i: int, kind: str) -> dict:
    """Per-event memory sample: tmpfs images + template-pool PSS + active PSS.

    PSS (not RSS) is the honest metric for CoW-shared template pools."""
    row = {"kind": "memcurve", "ev_i": ev_i, "after": kind,
           "ts": time.time()}
    try:
        st = os.statvfs(controller.snapshot_store)
        row["snapshot_tmpfs_bytes"] = (st.f_blocks - st.f_bfree) * st.f_frsize
    except (OSError, AttributeError):
        row["snapshot_tmpfs_bytes"] = None
    n_alive = 0
    pss_total = 0
    for rid, e in list(getattr(controller, "registry", {}).items()):
        pid = e.get("template_pid")
        if not pid:
            continue
        pss = _pss_kb(pid)
        if pss is None:
            raise RuntimeError(f"Missing template PSS for pid={pid}")
        n_alive += 1
        pss_total += pss
    row["n_templates_alive"] = n_alive
    row["templates_pss_kb"] = pss_total
    active_pss = _pss_kb(getattr(controller, "agent_pid", None))
    if active_pss is None or row["snapshot_tmpfs_bytes"] is None:
        raise RuntimeError("Missing active PSS or snapshot tmpfs measurement")
    row["active_pss_kb"] = active_pss
    row["registry_n"] = len(getattr(controller, "registry", {}))
    row["meminfo"] = _meminfo_subset()
    return row


def _memcurve_reachability_gc(controller, schedule, ev_i,
                              schedule_to_runtime, current_checkpoint_id) -> None:
    """Reachability-aware GC arm: keep only checkpoints the REMAINING
    schedule can still restore to (gc itself preserves ancestor chains)."""
    future = set()
    for ev in schedule[ev_i + 1:]:
        t = ev.get("restore_to_ckpt_id") or ev.get("target_ckpt_id")
        if t:
            future.add(t)
    keep = {schedule_to_runtime[t] for t in future
            if t in schedule_to_runtime}
    reg = getattr(controller, "registry", {})
    if current_checkpoint_id in reg:
        keep.add(current_checkpoint_id)  # restore may return to an older insertion
    controller.gc_obsolete_snapshots(keep_ids=keep)


def run_replay(args: argparse.Namespace) -> None:
    # Covers guard failures, worker initialization, validation and final output.
    with ReplayResources() as resources:
        _run_replay(args, resources)


def _run_replay(args: argparse.Namespace, resources: ReplayResources) -> None:
    require_real = (args.require_real_agent
                    or os.environ.get("DELTABOX_REQUIRE_REAL_AGENT") == "1")
    active_worker_required = (
        args.active_worker_load
        or os.environ.get("DELTABOX_REPLAY_ACTIVE_WORKER") == "1"
    )
    worker_exec_required = (
        args.worker_exec
        or os.environ.get("DELTABOX_REPLAY_WORKER_EXEC") == "1"
    )
    if require_real and args.agent_mode != "real":
        raise SystemExit(
            "report/final replay requires --agent-mode=real; dummy is only a "
            "development/regression stand-in and must not be used as the "
            "reported DeltaBox worker path"
        )
    if active_worker_required and args.agent_mode != "real":
        raise SystemExit(
            "active worker replay requires --agent-mode=real; dummy/slim "
            "development paths cannot satisfy the active-worker gate"
        )
    if worker_exec_required and args.agent_mode != "real":
        raise SystemExit(
            "worker-exec replay requires --agent-mode=real; dummy and driver "
            "side dirtying are not accepted for report/final runs"
        )
    controller_class = SandboxController
    if os.environ.get("DELTABOX_PAPER_MEMORY_POLICY"):
        from paper_memory_policy import ForkOnlyController, verify_runtime_compatibility
        verify_runtime_compatibility()
        controller_class = ForkOnlyController
    # SSH starts bash with cwd == /root which may not exist in the VM rootfs;
    # an invalid cwd propagates to the dummy agent and breaks CRIU's parasite
    # mkdir('proc.crtools') with ENOENT, blocking every standard ckpt.
    try:
        os.chdir("/tmp")
    except OSError as e:
        print(f"[replay] WARN chdir /tmp failed: {e}", flush=True)
    schedule = load_schedule(args.schedule)
    print(f"[replay] {len(schedule)} events from {args.schedule}", flush=True)

    staged_testbed = None
    if args.agent_mode == "real":
        instance_id = os.environ.get("DELTABOX_INSTANCE_ID", "")
        staged_testbed = stage_replay_testbed(instance_id)
    fs_config = init_testbed_overlay(SANDBOX_ROOT)
    if staged_testbed is not None:
        if staged_testbed.get("git_available", True):
            setup_git_environment(staged_testbed["base_commit"])
            if staged_testbed.get("workload_profile"):
                prepared = prepare_workload(staged_testbed["workload_profile"])
                print(f"[replay] workload environment ready: {prepared['environment']['executable']}", flush=True)
        else:
            print("[replay] payload repo has no .git metadata after staging; "
                  "using staged tree as-is and trusting trajectory commit "
                  f"{staged_testbed.get('base_commit')}", flush=True)
    print(f"[replay] overlay ready: {fs_config}", flush=True)

    # Mount tmpfs at SNAPSHOT_STORE so CRIU dumps don't exhaust the 3 GB
    # rootfs. A full no-adapt replay produces 80-95 dumps × ~86 MB each =
    # 7-8 GB of CRIU image data; rootfs cannot hold that.
    os.makedirs(SNAPSHOT_STORE, exist_ok=True)
    if not os.path.ismount(SNAPSHOT_STORE):
        rc = subprocess.call(["mount", "-t", "tmpfs", "-o", "size=14G,noswap",
                              "tmpfs", SNAPSHOT_STORE])
        if rc == 0:
            print(f"[replay] tmpfs mounted at {SNAPSHOT_STORE} (14G cap)", flush=True)
        else:
            raise RuntimeError(f"snapshot tmpfs mount failed rc={rc}; refusing disk fallback")

    sidecar_proc, sidecar_url = _start_index_sidecar(resources)
    agent_pid, ns_init_pid, agent_procs = spawn_checkpoint_agent(args.agent_mode, resources)
    print(f"[replay] {args.agent_mode} agent host pid={agent_pid}", flush=True)
    # Give CRIU a stable target: wait for /proc/<pid>/status accessible.
    for _ in range(40):
        if os.path.isdir(f"/proc/{agent_pid}"):
            break
        time.sleep(0.05)

    controller = resources.create_controller(controller_class,
        agent_pid=agent_pid,
        snapshot_store=SNAPSHOT_STORE,
        layers_root=fs_config["layers"],
        initial_upper=fs_config["upper"],
        initial_work=fs_config["work"],
        overlay_mount_point=OVERLAY_MOUNT,
        enable_adaptive=args.adaptive,
        enable_warm_template=args.warm_template,
        enable_prewarm=args.prewarm,
        ns_init_pid=ns_init_pid,
    )
    print(f"[replay] controller up. flags adaptive={args.adaptive} "
          f"warm_template={args.warm_template} prewarm={args.prewarm} "
          f"sync_dump={args.sync_dump}", flush=True)

    results: list[dict] = []
    pending_dumps = []
    last_ckpt_id: str | None = None
    # Map schedule's deterministic ckpt_id (from codescout_to_schedule) →
    # controller's runtime ckpt_id (uuid prefix). restore events reference
    # the schedule id; we look up the runtime id here.
    schedule_to_runtime: dict[str, str] = {}
    runtime_index_status: dict[str, dict] = {}
    validation_ckpts: list[str] = []
    agent_channel = AgentProbe() if args.agent_mode == "real" else None
    if agent_channel is not None:
        resources.callback(agent_channel.reset)
        agent_channel.connect()
    agent_probe = agent_channel if args.agent_mode == "real" and args.agent_probe else None
    worker_index_initial = None
    if worker_exec_required:
        assert agent_channel is not None
        worker_index_initial = agent_channel.control({
            "ctrl": "worker_index_build",
            "root": OVERLAY_MOUNT,
        }, timeout=float(os.environ.get(
            "DELTABOX_WORKER_INDEX_TIMEOUT", "300")))
        require_nonempty_worker_index(worker_index_initial, "initial build")
        print(f"[replay] worker code index loaded: "
              f"files={worker_index_initial.get('n_files')} "
              f"classes={worker_index_initial.get('n_classes')} "
              f"functions={worker_index_initial.get('n_functions')} "
              f"bytes={worker_index_initial.get('total_bytes')} "
              f"sidecar={worker_index_initial.get('sidecar')} "
              f"rss={worker_index_initial.get('footprint', {}).get('rss_kb')}KB "
              f"build_ms={worker_index_initial.get('build_ms')}",
              flush=True)
    active_worker_initial = None
    if active_worker_required:
        assert agent_channel is not None
        payload_root = os.environ.get("DELTABOX_SPR_PAYLOAD_ROOT",
                                      "/tmp/spr_payload")
        active_worker_initial = agent_channel.control({
            "ctrl": "active_worker_load",
            "instance_id": os.environ.get("DELTABOX_INSTANCE_ID", ""),
            "spr_payload_root": payload_root,
            "moatless_src": os.environ.get(
                "DELTABOX_MOATLESS_SRC",
                os.path.join(payload_root, "moatless-det-src")),
            "trajectory_path": os.environ.get(
                "DELTABOX_TRAJECTORY_PATH",
                os.path.join(payload_root, "trajectory.json")),
            "repo_path": os.environ.get(
                "DELTABOX_ACTIVE_REPO_PATH",
                os.path.join(payload_root, "repos",
                             f"swe-bench_{os.environ.get('DELTABOX_INSTANCE_ID', '')}")),
            "index_store_dir": os.environ.get(
                "DELTABOX_INDEX_STORE_DIR",
                os.path.join(payload_root, "index_store")),
            "mock_url": os.environ.get(
                "DELTABOX_ACTIVE_MOCK_URL",
                "http://127.0.0.1:9/v1"),
        }, timeout=float(os.environ.get(
            "DELTABOX_ACTIVE_WORKER_LOAD_TIMEOUT", "120")))
        if not active_worker_initial.get("ok"):
            raise RuntimeError(
                f"active worker load failed: {active_worker_initial}")
        if not active_worker_initial.get("loaded"):
            raise RuntimeError(
                f"active worker did not report loaded state: "
                f"{active_worker_initial}")
        print(f"[replay] active worker loaded: "
              f"rss={active_worker_initial.get('footprint', {}).get('rss_kb')}KB "
              f"pss={active_worker_initial.get('footprint', {}).get('pss_kb')}KB "
              f"classes={active_worker_initial.get('index_classes')} "
              f"functions={active_worker_initial.get('index_functions')} "
              f"load_ms={active_worker_initial.get('load_wall_ms')}",
              flush=True)
    step_idx = 0
    had_error = False

    for ev_i, ev in enumerate(schedule):
        kind = ev.get("type") or ev.get("kind")  # accept both
        t0 = time.time()
        try:
            if kind == "ckpt":
                lat_ms = float(ev.get("latency_ms", 0.0))
                dirty_mb = int(ev.get("dirty_mb", 0))
                if lat_ms > 0:
                    time.sleep(lat_ms / 1000.0)
                probe_result = None
                if agent_probe is not None:
                    probe_result = agent_probe.request(step_idx, phase="ckpt")
                    if not probe_result.get("ok"):
                        raise RuntimeError(f"agent probe failed: {probe_result}")
                    if probe_result.get("mismatch"):
                        raise RuntimeError(f"agent probe mismatch: {probe_result}")
                worker_exec_result = None
                worker_exec_test_timeout = False
                worker_ops = ev.get("worker_ops")
                worker_ops_required = bool(ev.get(
                    "worker_ops_required", worker_exec_required))
                if worker_exec_required:
                    if not isinstance(worker_ops, list):
                        raise RuntimeError(
                            f"worker-exec mode requires worker_ops on ckpt "
                            f"event ev_i={ev_i} ckpt={ev.get('ckpt_id')}")
                    if worker_ops_required and not worker_ops:
                        raise RuntimeError(
                            f"worker-exec mode got empty worker_ops for "
                            f"required ckpt ev_i={ev_i} ckpt={ev.get('ckpt_id')}")
                    assert agent_channel is not None
                    worker_exec_timeout = float(os.environ.get(
                        "DELTABOX_WORKER_EXEC_TIMEOUT", "180"))
                    worker_exec_margin = float(os.environ.get(
                        "DELTABOX_WORKER_EXEC_TIMEOUT_MARGIN", "30"))
                    worker_exec_result = agent_channel.control({
                        "ctrl": "worker_exec",
                        "ops": worker_ops,
                        "root": OVERLAY_MOUNT,
                        "step_idx": step_idx,
                        "ckpt_id": ev.get("ckpt_id"),
                    }, timeout=worker_exec_timeout + worker_exec_margin)
                    worker_exec_test_timeout = worker_exec_is_test_timeout(
                        worker_exec_result)
                    if (not worker_exec_result.get("ok")
                            and not worker_exec_test_timeout):
                        raise RuntimeError(
                            f"worker exec failed: {worker_exec_result}")
                else:
                    write_dirty(dirty_mb, step_idx)
                fs_before_ckpt = current_overlay_footprint(
                    controller, last_ckpt_id)
                agent_footprint_before_ckpt = process_footprint(controller.agent_pid)
                active_worker_status = None
                if active_worker_required:
                    assert agent_channel is not None
                    active_worker_status = agent_channel.control_fresh(
                        {"ctrl": "active_worker_status"}, timeout=10.0)
                    if (not active_worker_status.get("ok")
                            or not active_worker_status.get("loaded")):
                        raise RuntimeError(
                            f"active worker status invalid before ckpt: "
                            f"{active_worker_status}")
                worker_index_status = None
                if worker_exec_required:
                    assert agent_channel is not None
                    worker_index_status = agent_channel.control_fresh(
                        {"ctrl": "worker_index_status"}, timeout=10.0)
                    require_nonempty_worker_index(
                        worker_index_status, f"before ckpt ev={ev_i}")
                ckpt_t0 = time.time()
                tag = ev.get("strategy", "standard") or "standard"
                if (os.environ.get("DELTABOX_MEMCURVE_SKIP") == "1"
                        and tag == "standard"
                        and _memcurve_ops_read_only(worker_ops)):
                    tag = "lightweight"
                info, checkpoint_api_wall_ms = timed_api_call(
                    controller.checkpoint_action,
                    last_ckpt_id, tag, raw_command=tag,
                    replay_worker_ops=worker_ops if tag == "lightweight" else None)
                if info.get("dump_future") is not None:
                    pending_dumps.append((info["id"], info["dump_future"], info.get("dump_stats", {})))
                if args.sync_dump and info.get("dump_future") is not None:
                    info["dump_future"].result(timeout=60)
                if info.get("validation_needed"):
                    validation_ckpts.append(info["id"])
                    print(f"[PROBE] full-twin oracle registered "
                          f"ckpt={info['id']} "
                          f"full_id={info.get('validation_full_id')}",
                          flush=True)
                last_ckpt_id = info["id"]
                sched_id = ev.get("ckpt_id")
                if sched_id:
                    schedule_to_runtime[sched_id] = info["id"]
                sidecar_snapshot_status = None
                if worker_index_status is not None:
                    sidecar_snapshot_status = _snapshot_sidecar_index(
                        agent_channel, info["id"])
                    if sidecar_snapshot_status is not None:
                        worker_index_status = sidecar_snapshot_status
                    runtime_index_status[info["id"]] = worker_index_status
                results.append({
                    "ev_i": ev_i, "kind": "ckpt",
                    "agent_mode": args.agent_mode,
                    "require_real_agent": require_real,
                    "step_idx": step_idx,
                    "schedule_ckpt_id": sched_id,
                    "bootstrap": bool(ev.get("bootstrap")),
                    "runtime_ckpt_id": info["id"],
                    "strategy": info["strategy"],
                    "latency_ms": lat_ms, "dirty_mb": dirty_mb,
                    "agent_probe": probe_result,
                    "agent_probe_ok": (
                        probe_result.get("ok") if probe_result is not None
                        else None),
                    "agent_probe_mismatch": (
                        probe_result.get("mismatch") if probe_result is not None
                        else None),
                    "worker_exec": worker_exec_result,
                    "worker_exec_test_timeout": worker_exec_test_timeout,
                    "worker_ops_required": worker_ops_required,
                    "worker_ops_n": (
                        len(worker_ops) if isinstance(worker_ops, list)
                        else None),
                    "worker_index_status": worker_index_status,
                    "sidecar_snapshot_status": sidecar_snapshot_status,
                    "agent_footprint": agent_footprint_before_ckpt,
                    "active_worker_status": active_worker_status,
                    "fs_footprint": fs_before_ckpt,
                    "ckpt_wall_ms": (time.time() - ckpt_t0) * 1000,
                    "checkpoint_api_wall_ms": checkpoint_api_wall_ms,
                    "checkpoint_sync_no_dump_ms": info.get(
                        "checkpoint_sync_no_dump_ms"),
                    "memory_protocol": info.get("memory_protocol"),
                    "async_admission_wait_ms": info.get("async_admission_wait_ms"),
                    "checkpoint_state_at_return": info.get("state"),
                    "checkpoint_fork_ms": info.get("fork_ms"),
                    "checkpoint_overlay_ms": info.get("overlay_ms"),
                    "checkpoint_overlay_preparation_ms": info.get("overlay_preparation_ms"),
                    "checkpoint_pre_template_dump_join_ms": info.get(
                        "pre_template_dump_join_ms"),
                    "validation_full_id": info.get("validation_full_id"),
                    "validation_production_join_ms": info.get(
                        "validation_production_join_ms"),
                    "wall_ms": (time.time() - t0) * 1000,
                })
                step_idx += 1
            elif kind == "restore":
                sched_target = (ev.get("restore_to_ckpt_id")
                                or ev.get("target_ckpt_id"))
                target_id = schedule_to_runtime.get(sched_target)
                if target_id is None:
                    raise KeyError(
                        f"restore target schedule_id={sched_target!r} not yet "
                        f"checkpointed (have {list(schedule_to_runtime)[:5]}…)")
                r, restore_api_wall_ms = timed_api_call(
                    restore_with_ready, controller, target_id, agent_channel, resources)
                last_ckpt_id = target_id
                sidecar_restore_after_restore = (
                    _restore_sidecar_index_snapshot(agent_channel, target_id)
                    if worker_exec_required else None
                )
                replay_result = None
                replay_ms = 0.0
                replay_cmds = r.get("replay_cmds") or []
                if worker_exec_required and replay_cmds:
                    assert agent_channel is not None
                    agent_channel.reset()
                    worker_exec_timeout = float(os.environ.get(
                        "DELTABOX_WORKER_EXEC_TIMEOUT", "180"))
                    worker_exec_margin = float(os.environ.get(
                        "DELTABOX_WORKER_EXEC_TIMEOUT_MARGIN", "30"))
                    replay_result = replay_lightweight_worker_ops(
                        agent_channel, replay_cmds,
                        root=OVERLAY_MOUNT,
                        timeout_s=worker_exec_timeout,
                        timeout_margin_s=worker_exec_margin)
                    replay_ms = float(replay_result.get("wall_ms") or 0.0)
                    if (not replay_result.get("ok")
                            and not worker_exec_is_test_timeout(
                                replay_result.get("first_bad", {}).get(
                                    "worker_exec"))):
                        raise RuntimeError(
                            f"lightweight replay worker_exec failed: "
                            f"{replay_result}")
                post_restore_probe = None
                if agent_probe is not None:
                    # Restore may replace the active process; reopen FIFOs so
                    # this is a real post-restore liveness request.
                    agent_probe.reset()
                    post_restore_probe = agent_probe.request(
                        step_idx, phase=f"restore-{ev_i}")
                    if not post_restore_probe.get("ok"):
                        raise RuntimeError(
                            f"post-restore agent probe failed: "
                            f"{post_restore_probe}")
                    if post_restore_probe.get("mismatch"):
                        raise RuntimeError(
                            f"post-restore agent probe mismatch: "
                            f"{post_restore_probe}")
                post_restore_footprint = process_footprint(controller.agent_pid)
                keep_agent_channel_after_restore = (
                    os.environ.get("DELTABOX_KEEP_AGENT_CHANNEL_AFTER_RESTORE") == "1"
                )
                active_worker_after_restore = None
                if active_worker_required:
                    assert agent_channel is not None
                    if not keep_agent_channel_after_restore:
                        agent_channel.reset()
                    active_worker_after_restore = agent_channel.control_fresh(
                        {"ctrl": "active_worker_status"},
                        timeout=float(os.environ.get(
                            "DELTABOX_WORKER_INDEX_TIMEOUT", "300")))
                    if (not active_worker_after_restore.get("ok")
                            or not active_worker_after_restore.get("loaded")):
                        raise RuntimeError(
                            f"active worker state missing after restore: "
                            f"{active_worker_after_restore}")
                worker_index_after_restore = None
                if worker_exec_required:
                    assert agent_channel is not None
                    if not keep_agent_channel_after_restore:
                        agent_channel.reset()
                    worker_index_after_restore = agent_channel.control_fresh(
                        {"ctrl": "worker_index_status"},
                        timeout=float(os.environ.get(
                            "DELTABOX_WORKER_INDEX_TIMEOUT", "300")))
                    require_nonempty_worker_index(
                        worker_index_after_restore,
                        f"after restore ev={ev_i}")
                    expected_index = runtime_index_status.get(target_id)
                    if not expected_index:
                        raise RuntimeError(
                            f"missing expected worker index status for "
                            f"target runtime ckpt {target_id}")
                    for key in ("fingerprint", "n_files", "n_classes",
                                "n_functions", "total_bytes",
                                "sample_query"):
                        if worker_index_after_restore.get(key) != expected_index.get(key):
                            raise RuntimeError(
                                f"worker code index mismatch after restore "
                                f"target={target_id} key={key} "
                                f"expected={expected_index.get(key)!r} "
                                f"actual={worker_index_after_restore.get(key)!r}")
                    worker_index_after_restore["matches_target_ckpt"] = True
                results.append({
                    "ev_i": ev_i, "kind": "restore",
                    "agent_mode": args.agent_mode,
                    "require_real_agent": require_real,
                    "schedule_target_id": sched_target,
                    "runtime_target_id": target_id,
                    "path": r.get("path", "criu"),
                    "restore_wall_ms": restore_api_wall_ms + replay_ms,
                    "restore_table3_total_ms": r.get("restore_table3_total_ms"),
                    "restore_fast_coordination_ms": r.get("restore_fast_coordination_ms"),
                    "restore_slow_coordination_ms": r.get("restore_slow_coordination_ms"),
                    "restore_critical_ms": (
                        (r["restore_critical_ms"] if r.get("restore_critical_ms") is not None else r.get("restore_ms"))
                        + replay_ms
                        if (r.get("restore_critical_ms") is not None
                            or r.get("restore_ms") is not None)
                        else None),
                    "restore_api_wall_ms": restore_api_wall_ms,
                    "restore_runtime_reported_api_wall_ms": r.get("restore_api_wall_ms"),
                    "restore_cleanup_ms": r.get("restore_cleanup_ms"),
                    "restore_dump_join_ms": r.get("restore_dump_join_ms"),
                    "restore_kill_active_ms": r.get(
                        "restore_kill_active_ms"),
                    "restore_prepare_overlapped_ms": r.get(
                        "restore_prepare_overlapped_ms"),
                    "restore_fast_dispatch_ms": r.get(
                        "restore_fast_dispatch_ms"),
                    "restore_fast_ioctl_ms": r.get("restore_fast_ioctl_ms"),
                    "restore_fast_fork_wait_ms": r.get(
                        "restore_fast_fork_wait_ms"),
                    "restore_fast_fork_total_ms": r.get(
                        "restore_fast_fork_total_ms"),
                    "restore_fast_fork_agent_ms": r.get(
                        "restore_fast_fork_agent_ms"),
                    "restore_fast_fork_timing": r.get(
                        "restore_fast_fork_timing"),
                    "restore_replay_ready_ms": r.get("restore_replay_ready_ms"),
                    "restore_replay_ready_retries": r.get("restore_replay_ready_retries"),
                    "restore_slow_ioctl_ms": r.get(
                        "restore_slow_ioctl_ms"),
                    "restore_slow_criu_ms": r.get("restore_slow_criu_ms"),
                    "restore_slow_total_ms": r.get(
                        "restore_slow_total_ms"),
                    "restore_slow_pre_criu_ms": r.get(
                        "restore_slow_pre_criu_ms"),
                    "restore_slow_lazy_daemon_ms": r.get(
                        "restore_slow_lazy_daemon_ms"),
                    "restore_slow_post_criu_ms": r.get(
                        "restore_slow_post_criu_ms"),
                    "restore_slow_lazy": r.get("restore_slow_lazy"),
                    "restore_slow_parallel_lazy": r.get(
                        "restore_slow_parallel_lazy"),
                    "lightweight_replay": replay_result,
                    "lightweight_replay_ms": replay_ms,
                    "lightweight_replay_cmds": len(replay_cmds),
                    "sidecar_rebuild_after_restore":
                        sidecar_restore_after_restore,
                    "sidecar_restore_after_restore":
                        sidecar_restore_after_restore,
                    "agent_probe": post_restore_probe,
                    "agent_footprint_after_restore": post_restore_footprint,
                    "active_worker_status_after_restore":
                        active_worker_after_restore,
                    "worker_index_status_after_restore":
                        worker_index_after_restore,
                    "fs_footprint_after_restore": restored_overlay_footprint(
                        controller, target_id),
                    "wall_ms": (time.time() - t0) * 1000,
                })
            else:
                results.append({"ev_i": ev_i, "kind": "unknown",
                                "agent_mode": args.agent_mode,
                                "require_real_agent": require_real,
                                "raw": ev,
                                "wall_ms": (time.time() - t0) * 1000})
        except DumpUnavailableError as e:
            results.append({"ev_i": ev_i, "kind": kind, "ok": False,
                            "agent_mode": args.agent_mode,
                            "require_real_agent": require_real,
                            "err": "DumpUnavailable", "msg": str(e),
                            "wall_ms": (time.time() - t0) * 1000})
            had_error = True
        except Exception as e:  # noqa: BLE001 — record + continue
            results.append({"ev_i": ev_i, "kind": kind, "ok": False,
                            "agent_mode": args.agent_mode,
                            "require_real_agent": require_real,
                            "err": type(e).__name__, "msg": str(e),
                            "wall_ms": (time.time() - t0) * 1000})
            had_error = True

        if os.environ.get("DELTABOX_MEMCURVE") == "1":
            try:
                results.append(_memcurve_sample(controller, ev_i, kind))
            except Exception as e:  # noqa: BLE001 - sampling must not kill the run
                results.append({"kind": "memcurve", "ev_i": ev_i,
                                "ok": False, "err": str(e)})
            if (os.environ.get("DELTABOX_MEMCURVE_GC") == "1"
                    and kind in ("ckpt", "restore")):
                try:
                    _memcurve_reachability_gc(
                        controller, schedule, ev_i, schedule_to_runtime, last_ckpt_id)
                except Exception as e:  # noqa: BLE001
                    results.append({"kind": "memcurve_gc", "ok": False, "err": str(e)})
                    had_error = True
        # Stream-write so a crash mid-replay still leaves partial data.
        with open(args.results, "w") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
        if had_error:
            break

    if had_error:
        # Event exceptions are recorded and caught above. They still require
        # the failure path before any future.result()/unbounded normal join.
        resources.abort_dumps(controller)
    else:
        controller._dump_pool.shutdown(wait=True)
    if getattr(controller, "_async_incremental", None) is not None:
        # Export diagnostic metadata only after the measured loop and writers.
        snapshots = [{k: v for k, v in entry.items() if k != "dump_future"}
                     for entry in controller.registry.values()]
        with open("/tmp/async-checkpoints.json", "w") as stream:
            json.dump(snapshots, stream, indent=2)
    dump_rows = settle_dumps(pending_dumps)
    results.extend(dump_rows)
    had_error = had_error or any(row.get("ok") is False for row in dump_rows)
    if not had_error and os.environ.get("DELTABOX_VALIDATE_INCREMENTAL_EQUIV") == "1":
        validation_rows = validate_incremental_equivalence(
            controller, validation_ckpts, resources)
        for row in validation_rows:
            row.setdefault("agent_mode", args.agent_mode)
            row.setdefault("require_real_agent", require_real)
        results.extend(validation_rows)
    results.append(build_run_summary(
        results, args.agent_mode, require_real,
        active_worker_required, worker_exec_required,
        worker_index_initial,
        active_worker_initial))
    with open(args.results, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"[replay] done. {len(results)} events recorded -> {args.results}",
          flush=True)
    if had_error:
        raise SystemExit(1)


if __name__ == "__main__":
    run_replay(parse_args())
