#!/usr/bin/env python3
"""Controller-retained CRIU incremental dump + copytree baseline.

Semantics:
  * Host controller retains the SearchTree and mock LLM cursor.
  * A worker process runs real Moatless iteration logic against a live
    repository copy. CRIU checkpoints this real worker process. The worker is
    controlled by a file mailbox, so no HTTP control socket is part of the
    checkpointed state.
  * Filesystem checkpoints are complete directory trees made with
    `rsync --link-dest=<parent>`: unchanged files are hardlinked to the parent
    snapshot, changed files get new inodes. Restores copy the chosen snapshot
    back to a mutable live repo without hardlinking, so later edits cannot
    corrupt snapshots.
  * The historical default runtime is None; local-pytest explicitly enables tests. Test-result
    messages can differ from the trace and must remain visible in the audit.
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
import time
import urllib.request
from pathlib import Path


BASE = Path(os.environ.get("AE_BASE", str(Path(__file__).resolve().parent)))
PAYLOAD = Path(os.environ["SPR_PAYLOAD"])
VENV_PY = Path(os.environ["MOATLESS_VENV"]) / "bin/python"
PYTHON = VENV_PY if VENV_PY.exists() else Path(sys.executable)
SOURCE_REPOS = PAYLOAD / "repos"
INDEX_STORE = PAYLOAD / "index_store"
TRACES_ROOT = Path(os.environ["MOCK_TRACES_ROOT"])
CRIU = os.environ.get("FINALBENCH_CRIU") or shutil.which("criu") or "/usr/sbin/criu"
sys.path.insert(0, str(PAYLOAD))
from baseline_audit import flush_audit, message_policy  # noqa: E402


def run(cmd: list[str], *, timeout: float = 120.0, check: bool = True,
        cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        check=check,
        timeout=timeout,
        text=True,
        capture_output=True,
    )


def http_json(url: str, method: str = "GET", obj: dict | None = None, timeout: float = 30.0) -> dict:
    data = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_mock(instance: str, port: int, log_path: Path, audit_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PAYLOAD)
    env["PYTHONHASHSEED"] = "0"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [
            str(PYTHON),
            str(PAYLOAD / "mock_llm_server.py"),
            "--tcp-port",
            str(port),
            "--traces-root",
            str(TRACES_ROOT),
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
                h = http_json(f"{base}/admin/healthz", timeout=1.0)
                if h.get("ok"):
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise TimeoutError("mock did not become healthy")
        load = http_json(
            f"{base}/admin/load",
            method="POST",
            obj={"instance_id": instance, "variant": "ms"},
            timeout=30.0,
        )
        print(f"[mock] loaded {load}", flush=True)
        return proc
    except BaseException as error:
        try:
            flush_audit(f'http://127.0.0.1:{port}', audit_path, primary_error=error)
        finally:
            stop_proc(proc)
        raise


def stop_proc(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    logf = getattr(proc, "_finalbench_logf", None)
    if logf:
        logf.close()


def worker_env(repo_base: Path, mailbox: Path, mock_port: int) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{BASE}:{PAYLOAD}:{env.get('PYTHONPATH', '')}"
    env["PYTHONHASHSEED"] = "0"
    env["OPENAI_API_KEY"] = "dummy"
    # CRIU 3.16 on this SPR host cannot dump Python processes after NumPy/Faiss
    # has used wide AVX state ("Can't set FPU registers ... Bad address").
    # Keep the worker on generic/scalar-ish code paths so the process remains
    # checkpointable. This changes only local index-search implementation
    # performance, not LLM/mock semantics or Moatless action behavior.
    env["FAISS_OPT_LEVEL"] = "generic"
    env["FAISS_DISABLE_CPU_FEATURES"] = "AVX512_SPR,AVX512,AVX2"
    env["NPY_DISABLE_CPU_FEATURES"] = (
        "AVX512F AVX512CD AVX512ER AVX512PF AVX5124FMAPS AVX5124VNNIW "
        "AVX512VPOPCNTDQ AVX512VL AVX512BW AVX512DQ AVX512VNNI "
        "AVX512IFMA AVX512VBMI AVX512VBMI2 AVX512BITALG AVX512FP16 "
        "AVX512_KNL AVX512_KNM AVX512_SKX AVX512_CLX AVX512_CNL "
        "AVX512_ICL AVX512_SPR AVX2 FMA3 F16C AVX"
    )
    env["RAPIDFUZZ_IMPLEMENTATION"] = "python"
    env["GLIBC_TUNABLES"] = (
        "glibc.cpu.hwcaps=-x86-64-v4,-x86-64-v3,"
        "-AVX512F,-AVX512VL,-AVX512BW,-AVX512DQ,-AVX512CD,"
        "-AVX512_VNNI,-AVX512_IFMA,-AVX512_VBMI,-AVX512_VBMI2,"
        "-AVX512_BITALG,-AVX512_VPOPCNTDQ,-AVX512_FP16,-AVX2,-AVX,-FMA"
    )
    env["OPENBLAS_CORETYPE"] = "Haswell"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["OMP_THREAD_LIMIT"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    env["NUMEXPR_MAX_THREADS"] = "1"
    # PyArrow can otherwise load its bundled jemalloc pool and start a
    # jemalloc background thread after CodeIndex init. CRIU 3.16 is fragile
    # dumping that multi-threaded Python process on this SPR host.
    env["ARROW_DEFAULT_MEMORY_POOL"] = "system"
    # Treat PyArrow as an unavailable optional dependency inside the CRIU
    # worker. Pandas only probes it for optional Arrow dtype/parquet support,
    # while importing it starts a jemalloc background thread that CRIU 3.16
    # cannot dump on this host.
    env["CRIU_BLOCK_PYARROW"] = "1"
    env["MALLOC_CONF"] = "background_thread:false"
    env["CRIU_WORKER_MAILBOX"] = str(mailbox)
    env["CRIU_REPO_BASE"] = str(repo_base)
    env["CRIU_INDEX_STORE"] = str(INDEX_STORE)
    env["CRIU_TRACES_ROOT"] = str(TRACES_ROOT)
    env["CRIU_MOCK_URL_BASE"] = f"http://127.0.0.1:{mock_port}"
    return env


def wait_worker_ready(mailbox: Path, timeout_s: float = 60.0) -> dict:
    ready = mailbox / "worker_ready.json"
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            if ready.exists():
                return json.loads(ready.read_text(encoding="utf-8"))
        except Exception as e:
            last = repr(e)
        time.sleep(0.05)
    raise TimeoutError(f"worker ready timeout; last={last}")


def worker_rpc(mailbox: Path, obj: dict, timeout_s: float = 300.0) -> dict:
    cmd = mailbox / "cmd.json"
    resp = mailbox / "resp.json"
    tmp = mailbox / "cmd.json.tmp"
    resp.unlink(missing_ok=True)
    cmd.unlink(missing_ok=True)
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(cmd)
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            if resp.exists():
                out = json.loads(resp.read_text(encoding="utf-8"))
                resp.unlink(missing_ok=True)
                return out
        except Exception as e:
            last = repr(e)
        time.sleep(0.02)
    raise TimeoutError(f"worker rpc {obj.get('op')} timeout; last={last}")


def start_worker(instance: str, repo_base: Path, mailbox: Path, mock_port: int, log_path: Path) -> subprocess.Popen:
    env = worker_env(repo_base, mailbox, mock_port)
    if mailbox.exists():
        shutil.rmtree(mailbox)
    mailbox.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [str(PYTHON), str(BASE / "criu_worker.py")],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=str(BASE),
        preexec_fn=os.setsid,
    )
    proc._finalbench_logf = logf  # type: ignore[attr-defined]
    try:
        wait_worker_ready(mailbox, timeout_s=60.0)
        init = worker_rpc(
            mailbox,
            {"op": "init", "instance": instance, "mock_url_base": f"http://127.0.0.1:{mock_port}"},
            timeout_s=240.0,
        )
        if not init.get("ok"):
            raise RuntimeError(f"worker init failed: {init}")
        proc._finalbench_init = init  # type: ignore[attr-defined]
        return proc
    except BaseException:
        stop_proc(proc)
        raise


def parent_map_from_tree(tree: dict) -> dict[int, int | None]:
    out: dict[int, int | None] = {}

    def rec(n: dict, p: int | None) -> None:
        nid = n.get("node_id")
        out[nid] = p
        for ch in n.get("children") or []:
            rec(ch, nid)

    rec(tree["root"], None)
    return out


def node_checkpoint_seq(ckpts: list[dict], node_id: int | None) -> int | None:
    for c in ckpts:
        if c["state"].get("node_id") == node_id:
            return c["seq"]
    return None


def compact_checkpoint(c: dict) -> dict:
    fs = c.get("fs") or {}
    criu_dump = c.get("criu_dump") or {}
    state = c.get("state") or {}
    event = c.get("event") or {}
    node_id = state.get("node_id")
    if node_id is None:
        node_id = event.get("new_node_id")
    fs_ms = float(fs.get("rsync_ms", 0.0) or 0.0)
    criu_ms = float(criu_dump.get("criu_dump_ms", 0.0) or 0.0)
    return {
        "seq": c.get("seq"),
        "node_id": node_id,
        "parent_node_id": c.get("parent_node_id"),
        "selected_node_id": c.get("selected_node_id"),
        "rollback_needed_pre": c.get("rollback_needed_pre"),
        "parent_checkpoint_seq": c.get("parent_checkpoint_seq"),
        "fs_snapshot": c.get("fs_snapshot"),
        "criu_images": c.get("criu_images"),
        "fs_checkpoint_ms": fs_ms,
        "criu_dump_ms": criu_ms,
        "checkpoint_total_ms": fs_ms + criu_ms,
        "image_bytes": int(criu_dump.get("size_bytes", 0) or 0),
        "mock_stats": state.get("mock_stats"),
        "step_wall_ms": event.get("step_wall_ms"),
        "test_runtime_records": event.get("test_runtime_records", []),
    }


def compact_restore(r: dict) -> dict:
    fs = r.get("fs_restore") or {}
    criu_restore = r.get("criu_restore") or {}
    return {
        "before_seq": r.get("before_seq"),
        "selected_node_id": r.get("selected_node_id"),
        "target_seq": r.get("target_seq"),
        "restore_total_ms": float(r.get("restore_total_ms", 0.0) or 0.0),
        "fs_restore_ms": float(fs.get("rsync_ms", 0.0) or 0.0),
        "criu_restore_ms": float(criu_restore.get("criu_restore_ms", 0.0) or 0.0),
        "restored_pid": criu_restore.get("restored_pid"),
        "retained_tree_total_nodes": r.get("retained_tree_total_nodes"),
        "mock_stats": ((r.get("restored_state") or {}).get("state") or {}).get("mock_stats"),
    }


def make_result(
    *,
    ok: bool,
    status: str,
    instance: str,
    conditions: dict,
    setup: dict,
    ckpts: list[dict],
    restore_events: list[dict],
    tail_error: dict | None = None,
) -> dict:
    checkpoints = [compact_checkpoint(c) for c in ckpts]
    restores = [compact_restore(r) for r in restore_events]
    out = {
        "ok": ok,
        "status": status,
        "instance": instance,
        "conditions": conditions,
        "setup": {"initial_rsync": setup},
        "checkpoints": checkpoints,
        "restores": restores,
        "n_ckpts": len(checkpoints),
        "n_restores": len(restores),
    }
    out["ckpts"] = checkpoints
    out["restore_events"] = restores
    if tail_error is not None:
        out["tail_error"] = tail_error
    return out


def rsync_copy(src: Path, dst: Path, *, link_dest: Path | None = None,
               delete: bool = True) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if link_dest is None and dst.exists():
        shutil.rmtree(dst)
    cmd = ["rsync", "-a"]
    if delete:
        cmd.append("--delete")
    if link_dest is not None:
        cmd.append(f"--link-dest={link_dest.resolve()}")
    cmd += [f"{src}/", f"{dst}/"]
    t0 = time.perf_counter()
    cp = run(cmd, timeout=600.0, check=False)
    ms = (time.perf_counter() - t0) * 1000.0
    out = {"rsync_ms": ms, "rc": cp.returncode}
    if cp.returncode != 0:
        out["stderr"] = cp.stderr[-1000:]
    return out


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() or p.is_symlink():
                total += p.lstat().st_size
        except OSError:
            pass
    return total


def criu_dump(pid: int, image_dir: Path, *, prev_dir: Path | None, log_file: str) -> dict:
    if image_dir.exists():
        shutil.rmtree(image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        CRIU,
        "dump",
        "--leave-running",
        "-t", str(pid),
        "-D", str(image_dir),
        "--shell-job",
        "--tcp-close",
        "--file-locks",
        "--ext-unix-sk",
        "--track-mem",
        "--log-file", log_file,
        "-v1",
    ]
    if prev_dir is not None:
        cmd += ["--prev-images-dir", os.path.relpath(prev_dir, image_dir)]
    t0 = time.perf_counter()
    cp = run(cmd, timeout=120.0, check=False)
    ms = (time.perf_counter() - t0) * 1000.0
    out = {
        "criu_dump_ms": ms,
        "rc": cp.returncode,
        "size_bytes": dir_size_bytes(image_dir),
        "prev_images_dir": str(prev_dir) if prev_dir else None,
    }
    if cp.returncode != 0:
        out["stderr"] = cp.stderr[-1000:]
    return out


def criu_restore(image_dir: Path, *, log_file: str) -> dict:
    cmd = [
        CRIU,
        "restore",
        "-d",
        "-D", str(image_dir),
        "--shell-job",
        "--tcp-close",
        "--file-locks",
        "--ext-unix-sk",
        "--log-file", log_file,
        "-v1",
    ]
    t0 = time.perf_counter()
    cp = run(cmd, timeout=120.0, check=False)
    ms = (time.perf_counter() - t0) * 1000.0
    out = {"criu_restore_ms": ms, "rc": cp.returncode}
    if cp.returncode != 0:
        out["stderr"] = cp.stderr[-1000:]
    return out


def quiesce_and_dump(
    *,
    mailbox: Path,
    worker_pid: int,
    image_dir: Path,
    prev_dir: Path | None,
    log_file: str,
) -> dict:
    """Ask the worker to quiesce, then dump it while it is idle in the mailbox loop."""
    q = worker_rpc(mailbox, {"op": "quiesce"}, timeout_s=60.0)
    if not q.get("ok"):
        raise RuntimeError(f"worker quiesce failed: {q}")
    crres = criu_dump(worker_pid, image_dir, prev_dir=prev_dir, log_file=log_file)
    crres["worker_quiesce"] = q.get("quiesce")
    return crres


def kill_worker(proc: subprocess.Popen | None) -> None:
    stop_proc(proc)


def kill_pid(pid: int | None) -> None:
    if not pid or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    for _ in range(30):
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def run_pilot(
    instance: str,
    *,
    max_steps: int,
    run_id_prefix: str,
    cleanup_large_artifacts: bool,
    numa_node: int | None,
) -> dict:
    run_id = f"{run_id_prefix}_{instance}"
    results = BASE / "results" / run_id
    work = BASE / "work" / run_id
    repo_base = work / "repo_base"
    live_repo = repo_base / f"swe-bench_{instance}"
    fs_snaps = work / "fs_snaps"
    criu_imgs = work / "criu_images"
    logs = work / "logs"
    for p in [results, work]:
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    for p in [repo_base, fs_snaps, criu_imgs, logs]:
        p.mkdir(parents=True, exist_ok=True)
    mailbox = work / "mailbox"

    affinity = sorted(os.sched_getaffinity(0))
    policies = {str(cpu): {name: (Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq") / name).read_text().strip()
                for name in ("scaling_governor", "scaling_min_freq", "scaling_max_freq")} for cpu in affinity}
    conditions = {
        "cpu_affinity": affinity,
        "cpu_policies": policies,
        "cpu_governor": policies[str(affinity[0])]["scaling_governor"],
        "cpu_min_freq": policies[str(affinity[0])]["scaling_min_freq"],
        "cpu_max_freq": policies[str(affinity[0])]["scaling_max_freq"],
        "numa_node": numa_node,
        "semantics": "host-retained SearchTree and mock; CRIU restores mailbox worker process; rsync link-dest restores FS",
        "fs_snapshot": "rsync -a --delete --link-dest=<parent>",
        "criu": CRIU,
        "criu_incremental": "dump --track-mem --prev-images-dir=<parent>",
        "message_policy": message_policy(),
    }

    print(f"[criu-copytree] setup live repo {instance}", flush=True)
    setup = rsync_copy(SOURCE_REPOS / f"swe-bench_{instance}", live_repo, delete=True)
    if setup["rc"] != 0:
        raise RuntimeError(f"initial rsync failed: {setup}")

    mock_port = free_tcp_port()
    audit_path = results / 'mock_audit.json'
    mock_proc = start_mock(instance, mock_port, logs / "mock.log", audit_path)
    worker_proc = None
    worker_pid: int | None = None
    ckpts: list[dict] = []
    restore_events: list[dict] = []
    tree = None
    active_seq = None
    prev_node = None

    try:
        worker_proc = start_worker(
            instance, repo_base, mailbox, mock_port, logs / "worker.log"
        )
        worker_pid = worker_proc.pid
        init = getattr(worker_proc, "_finalbench_init")
        tree = init["tree"]
        s = init["state"]
        root_fs = fs_snaps / "seq0"
        fsres = rsync_copy(live_repo, root_fs, delete=True)
        crdir = criu_imgs / "seq0"
        crres = quiesce_and_dump(
            mailbox=mailbox,
            worker_pid=worker_pid,
            image_dir=crdir,
            prev_dir=None,
            log_file="dump_seq0.log",
        )
        if fsres["rc"] != 0 or crres["rc"] != 0:
            raise RuntimeError(f"root checkpoint failed fs={fsres} criu={crres}")
        ckpts.append({
            "seq": 0,
            "state": s,
            "tree": tree,
            "fs_snapshot": str(root_fs),
            "criu_images": str(crdir),
            "parent_checkpoint_seq": None,
            **{"fs": fsres, "criu_dump": crres},
        })
        active_seq = 0
        prev_node = 0
        print(f"[ckpt 0] root fs={fsres['rsync_ms']:.1f}ms criu={crres['criu_dump_ms']:.1f}ms", flush=True)

        for seq in range(1, max_steps + 1):
            sel = worker_rpc(mailbox, {"op": "select", "tree": tree}, timeout_s=60.0)
            if not sel.get("ok"):
                raise RuntimeError(f"worker select failed: {sel}")
            selected_node_id = sel.get("selected_node_id")
            rollback_needed_pre = seq > 1 and selected_node_id != prev_node
            if rollback_needed_pre:
                target_seq = node_checkpoint_seq(ckpts, selected_node_id)
                if target_seq is None:
                    raise RuntimeError(f"selected node {selected_node_id} has no checkpoint")
                target = next(c for c in ckpts if c["seq"] == target_seq)
                t0 = time.perf_counter()
                if worker_proc is not None and worker_proc.poll() is None:
                    kill_worker(worker_proc)
                else:
                    kill_pid(worker_pid)
                worker_proc = None
                worker_pid = None
                fs_restore = rsync_copy(Path(target["fs_snapshot"]), live_repo, delete=True)
                if fs_restore['rc'] != 0:
                    raise RuntimeError(f'filesystem restore failed: {fs_restore}')
                cr_restore = criu_restore(Path(target["criu_images"]), log_file=f"restore_pre_seq{seq}_target{target_seq}.log")
                if cr_restore["rc"] != 0:
                    raise RuntimeError(f"criu restore failed: {cr_restore}")
                restored = worker_rpc(mailbox, {"op": "state"}, timeout_s=60.0)
                if not restored.get('ok'):
                    raise RuntimeError(f'restored worker state failed: {restored}')
                worker_pid = int(((restored.get("state") or {}).get("pid")) or -1)
                restore_events.append({
                    "before_seq": seq,
                    "selected_node_id": selected_node_id,
                    "target_seq": target_seq,
                    "fs_restore": fs_restore,
                    "criu_restore": cr_restore,
                    "restore_total_ms": (time.perf_counter() - t0) * 1000.0,
                    "restored_state": restored,
                    "retained_tree_total_nodes": len(parent_map_from_tree(tree)),
                })
                active_seq = target_seq
                prev_node = selected_node_id
                print(f"[pre-restore] seq{seq}->target_seq{target_seq} selected={selected_node_id}", flush=True)

            step = worker_rpc(
                mailbox,
                {"op": "step", "seq": seq, "tree": tree, "selected_node_id": selected_node_id},
                timeout_s=float(os.environ.get("DELTABOX_TEST_STEP_TIMEOUT", "240")),
            )
            if not step.get("ok"):
                raise RuntimeError(f"worker step failed: {step}")
            tree = step["tree"]
            event = step.get("event") or {}
            node_id = step.get("node_id")
            parent = parent_map_from_tree(tree).get(node_id)

            fs_snap = fs_snaps / f"seq{seq}"
            active_ckpt = next(c for c in ckpts if c["seq"] == active_seq)
            fsres = rsync_copy(live_repo, fs_snap, link_dest=Path(active_ckpt["fs_snapshot"]), delete=True)
            crdir = criu_imgs / f"seq{seq}"
            worker_pid = int((step.get("state") or {}).get("pid") or worker_pid or -1)
            crres = quiesce_and_dump(
                mailbox=mailbox,
                worker_pid=worker_pid,
                image_dir=crdir,
                prev_dir=Path(active_ckpt["criu_images"]),
                log_file=f"dump_seq{seq}.log",
            )
            if fsres["rc"] != 0 or crres["rc"] != 0:
                raise RuntimeError(f"checkpoint {seq} failed fs={fsres} criu={crres}")
            s = step["state"]
            ckpts.append({
                "seq": seq,
                "state": s,
                "tree": tree,
                "event": event,
                "parent_node_id": parent,
                "selected_node_id": selected_node_id,
                "rollback_needed_pre": rollback_needed_pre,
                "parent_checkpoint_seq": active_seq,
                "fs_snapshot": str(fs_snap),
                "criu_images": str(crdir),
                "fs": fsres,
                "criu_dump": crres,
            })
            print(
                f"[ckpt {seq}] node={node_id} parent={parent} pre_rollback={rollback_needed_pre} "
                f"cursor={(s.get('mock_stats') or {}).get('cursor')} "
                f"fs={fsres['rsync_ms']:.1f}ms criu={crres['criu_dump_ms']:.1f}ms",
                flush=True,
            )
            active_seq = seq
            prev_node = node_id
            if step.get("finished"):
                break

        out = make_result(
            ok=True,
            status="OK",
            instance=instance,
            conditions=conditions,
            setup=setup,
            ckpts=ckpts,
            restore_events=restore_events,
        )
        results.mkdir(parents=True, exist_ok=True)
        (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    finally:
        primary = sys.exc_info()[1]
        try:
            if worker_proc is not None and worker_proc.poll() is None:
                stop_proc(worker_proc)
            else:
                kill_pid(worker_pid)
        finally:
            try:
                # Every checkpoint/restore timer has ended; the stopped
                # worker cannot produce more requests while we flush.
                flush_audit(f'http://127.0.0.1:{mock_port}', audit_path,
                            primary_error=primary or sys.exc_info()[1])
            finally:
                try:
                    stop_proc(mock_proc)
                finally:
                    # Preserve logs even when large images are removed.
                    diagnostics = results / 'diagnostics'
                    diagnostics.mkdir(exist_ok=True)
                    for log in work.rglob('*.log'):
                        destination = diagnostics / log.relative_to(work)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(log, destination)
                    if cleanup_large_artifacts and (results / 'pilot_result.json').exists():
                        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True)
    ap.add_argument("--max-steps", type=int, default=29)
    ap.add_argument("--run-id-prefix", default="pilot")
    ap.add_argument("--cleanup-large-artifacts", action="store_true")
    ap.add_argument("--numa-node", type=int, default=None)
    args = ap.parse_args()
    out = run_pilot(
        args.instance,
        max_steps=args.max_steps,
        run_id_prefix=args.run_id_prefix,
        cleanup_large_artifacts=args.cleanup_large_artifacts,
        numa_node=args.numa_node,
    )
    print(json.dumps({
        "ok": out["ok"],
        "instance": out["instance"],
        "n_ckpts": len(out["ckpts"]),
        "n_restores": len(out["restore_events"]),
    }, indent=2))
    return 0 if out.get("ok") else 1


def _ae_interrupted(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt(f"Experiment interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _ae_interrupted)
    sys.exit(main())
