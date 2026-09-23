#!/usr/bin/env python3
"""Profile worker-only soft-dirty pages for a slim SWE-search work runtime.

This is not a full moatless run.  It replays recorded P-EAGLE worker_ops against
real repositories, with CodeIndex/search kept in a sidecar process.  The only
PID measured is the tiny checkpoint-target worker process.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
TRACE_ROOT = REPO_ROOT / "traces/swe-search/qwen3-coder-30b-p-eagle-ms/mcts-iter30"
PAYLOAD_REPOS = Path(os.environ.get("SPR_PAYLOAD_REPOS", "/mnt/disk2/dyp/spr_payload/repos"))
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
SOFT_DIRTY_BIT = 1 << 55
PRESENT_BIT = 1 << 63

sys.path.insert(0, str(REPO_ROOT / "experiments/end2end_real_replay"))
from qwen3_tree_to_schedule import convert  # noqa: E402


def mean(vals: list[float]) -> float | None:
    return statistics.fmean(vals) if vals else None


def percentile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    return vals[int(q * (len(vals) - 1))]


def stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "p95": percentile(vals, 0.95),
        "min": min(vals),
        "max": max(vals),
    }


def clear_soft_dirty(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/clear_refs", "w", encoding="ascii") as f:
            f.write("4\n")
        return True
    except OSError:
        return False


def read_smaps_rollup(pid: int) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open(f"/proc/{pid}/smaps_rollup", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                parts = rest.strip().split()
                if parts and parts[0].isdigit():
                    out[key] = int(parts[0])
    except OSError:
        pass
    return out


def iter_maps(pid: int, mode: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    with open(f"/proc/{pid}/maps", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 2:
                continue
            perms = parts[1]
            path = parts[5] if len(parts) >= 6 else ""
            if "r" not in perms or "w" not in perms:
                continue
            private = "p" in perms
            if not private:
                continue
            if mode == "anon_private":
                if path and not path.startswith("["):
                    continue
            elif mode == "all_private":
                pass
            else:
                raise ValueError(mode)
            lo_s, hi_s = parts[0].split("-", 1)
            lo = int(lo_s, 16)
            hi = int(hi_s, 16)
            if hi > lo:
                ranges.append((lo, hi))
    return ranges


def count_soft_dirty(pid: int, mode: str) -> dict:
    pages = 0
    present = 0
    sampled = 0
    ranges = iter_maps(pid, mode)
    try:
        with open(f"/proc/{pid}/pagemap", "rb", buffering=0) as pm:
            for lo, hi in ranges:
                start = lo // PAGE_SIZE
                end = (hi + PAGE_SIZE - 1) // PAGE_SIZE
                for page in range(start, end):
                    pm.seek(page * 8)
                    data = pm.read(8)
                    if len(data) != 8:
                        continue
                    sampled += 1
                    val = struct.unpack("Q", data)[0]
                    if val & PRESENT_BIT:
                        present += 1
                    if (val & PRESENT_BIT) and (val & SOFT_DIRTY_BIT):
                        pages += 1
    except OSError as e:
        return {"ok": False, "error": str(e), "mode": mode}
    return {
        "ok": True,
        "mode": mode,
        "vmas": len(ranges),
        "sampled_pages": sampled,
        "present_pages": present,
        "soft_dirty_pages": pages,
        "soft_dirty_bytes": pages * PAGE_SIZE,
        "soft_dirty_mb": pages * PAGE_SIZE / (1024 * 1024),
    }


def du_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if ".git" in p.parts:
            continue
        try:
            if p.is_file() or p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def repo_source_for(instance: str) -> Path | None:
    p = PAYLOAD_REPOS / f"swe-bench_{instance}"
    if p.is_dir():
        return p
    # Some previous runs kept cloned repos under a profile root.
    for root in (
        REPO_ROOT / "traces/swe-search/qwen3-coder-30b-vllm18086/profile50-native-mcts30-20260609_015241/repos",
        REPO_ROOT / "traces/swe-search/qwen3-coder-30b-vllm18086/profile5-tree-rss-20260609_052851/repos",
    ):
        p = root / f"swe-bench_{instance}"
        if p.is_dir():
            return p
    return None


def call_sidecar(url: str, method: str, **kwargs) -> dict:
    body = json.dumps(
        {"method": method, "kwargs": kwargs},
        separators=(",", ":"),
    ).encode()
    req = Request(
        url.rstrip("/") + "/call",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"sidecar {method} failed")
    return payload["result"]


class Worker:
    def __init__(self, repo: Path, sidecar_url: str, numa: int | None):
        env = os.environ.copy()
        env.update({
            "SLIM_WORKRT_INDEX_URL": sidecar_url,
            "SLIM_WORKRT_EXTERNALIZE_RUN_TESTS": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONHASHSEED": "0",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        })
        cmd = [sys.executable, str(REPO_ROOT / "scripts/slim_workrt_worker.py")]
        if numa is not None and shutil.which("numactl"):
            cmd = ["numactl", f"--cpunodebind={numa}", f"--membind={numa}", *cmd]
        self.proc = subprocess.Popen(
            cmd,
            cwd=repo,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert self.proc.stdout is not None
        ready = json.loads(self.proc.stdout.readline())
        self.pid = int(ready["pid"])

    def exec_ops(self, repo: Path, ops: list[dict], ckpt_id: str, ev_i: int) -> dict:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        req = {"op": "exec", "root": str(repo), "ops": ops, "ckpt_id": ckpt_id, "ev_i": ev_i}
        self.proc.stdin.write(json.dumps(req, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = ""
            try:
                if self.proc.stderr is not None:
                    err = self.proc.stderr.read()
            except Exception:
                pass
            raise RuntimeError(f"worker exited rc={self.proc.poll()} stderr={err[-4000:]}")
        return json.loads(line)

    def stop(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write('{"op":"stop"}\n')
                self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass


def snapshot_repo(repo: Path, snap_root: Path, ckpt_id: str) -> Path:
    dst = snap_root / ckpt_id
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".mypy_cache")
    shutil.copytree(repo, dst, symlinks=True, ignore=ignore)
    return dst


def restore_repo(repo: Path, snap: Path) -> None:
    for p in repo.iterdir():
        if p.name == ".git":
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)
    for p in snap.iterdir():
        dst = repo / p.name
        if p.is_dir() and not p.is_symlink():
            shutil.copytree(p, dst, symlinks=True)
        else:
            if p.is_symlink():
                os.symlink(os.readlink(p), dst)
            else:
                shutil.copy2(p, dst)


def start_sidecar(port: int, numa: int | None) -> tuple[subprocess.Popen, str]:
    cmd = [
        sys.executable,
        "-u",
        str(REPO_ROOT / "guest/index_sidecar.py"),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if numa is not None and shutil.which("numactl"):
        cmd = ["numactl", f"--cpunodebind={numa}", f"--membind={numa}", *cmd]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"sidecar exited rc={proc.returncode}: {err[-4000:]}")
        try:
            with urlopen(url + "/healthz", timeout=0.5) as resp:
                if resp.status == 200:
                    return proc, url
        except Exception as e:
            last = str(e)
        time.sleep(0.05)
    raise TimeoutError(f"sidecar boot timeout: {last}")


def profile_instance(args_tuple: tuple) -> dict:
    instance, trace_dir, out_dir, lane, numa = args_tuple
    t0 = time.time()
    repo_src = repo_source_for(instance)
    if repo_src is None:
        return {"instance_id": instance, "ok": False, "error": "missing_repo"}
    inst_out = out_dir / instance
    if inst_out.exists():
        shutil.rmtree(inst_out)
    inst_out.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix=f"slim-workrt-{instance}-", dir=str(out_dir / "tmp")))
    sidecar = None
    worker = None
    try:
        repo = work_root / "repo"
        snap_root = work_root / "snaps"
        snap_root.mkdir()
        ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".mypy_cache")
        shutil.copytree(repo_src, repo, symlinks=True, ignore=ignore)
        repo_size = du_bytes(repo)
        schedule, _ = convert(trace_dir / "trajectory.json", instance)

        port = 19080 + lane
        sidecar, sidecar_url = start_sidecar(port, numa)
        initial_index = call_sidecar(sidecar_url, "build", root=str(repo))
        worker = Worker(repo, sidecar_url, numa)

        ckpt_snaps: dict[str, Path] = {}
        sidecar_snaps: set[str] = set()
        rows: list[dict] = []
        ckpt_count = 0
        restore_count = 0
        failures = 0
        for ev_i, ev in enumerate(schedule):
            kind = ev.get("type")
            if kind == "restore":
                target = ev.get("restore_to_ckpt_id")
                snap = ckpt_snaps.get(target)
                if snap is None:
                    failures += 1
                    rows.append({"ev_i": ev_i, "kind": kind, "ok": False, "error": "missing_snapshot", "target": target})
                    continue
                restore_repo(repo, snap)
                if target in sidecar_snaps:
                    call_sidecar(sidecar_url, "restore_snapshot", snapshot_id=target)
                else:
                    call_sidecar(sidecar_url, "build", root=str(repo))
                restore_count += 1
                rows.append({"ev_i": ev_i, "kind": kind, "ok": True, "target": target})
                continue
            if kind != "ckpt":
                continue
            ops = ev.get("worker_ops") or []
            ckpt_id = ev.get("ckpt_id") or f"ckpt_{ev_i}"
            clear_ok = clear_soft_dirty(worker.pid)
            before = read_smaps_rollup(worker.pid)
            exec_t0 = time.time()
            result = worker.exec_ops(repo, ops, ckpt_id, ev_i)
            exec_wall_ms = (time.time() - exec_t0) * 1000.0
            after = read_smaps_rollup(worker.pid)
            dirty_anon = count_soft_dirty(worker.pid, "anon_private")
            dirty_all = count_soft_dirty(worker.pid, "all_private")
            snap = snapshot_repo(repo, snap_root, ckpt_id)
            ckpt_snaps[ckpt_id] = snap
            try:
                call_sidecar(sidecar_url, "snapshot", snapshot_id=ckpt_id)
                sidecar_snaps.add(ckpt_id)
            except Exception:
                pass
            ckpt_count += 1
            if not result.get("ok"):
                failures += 1
            rows.append({
                "ev_i": ev_i,
                "iter": ev.get("iter"),
                "kind": kind,
                "ok": bool(result.get("ok")),
                "ckpt_id": ckpt_id,
                "strategy": ev.get("strategy"),
                "n_ops": len(ops),
                "op_types": [op.get("type") for op in ops if isinstance(op, dict)],
                "worker_pid": worker.pid,
                "clear_soft_dirty_ok": clear_ok,
                "exec_wall_ms": exec_wall_ms,
                "worker_result": {
                    "n_ops": result.get("n_ops"),
                    "n_failed": result.get("n_failed"),
                    "results": result.get("results"),
                },
                "rss_mb_after": (after.get("Rss") or 0) / 1024.0,
                "private_dirty_mb_after": (after.get("Private_Dirty") or 0) / 1024.0,
                "rss_mb_before": (before.get("Rss") or 0) / 1024.0,
                "soft_dirty_anon_private": dirty_anon,
                "soft_dirty_all_private": dirty_all,
                "soft_dirty_anon_private_mb": dirty_anon.get("soft_dirty_mb"),
                "soft_dirty_all_private_mb": dirty_all.get("soft_dirty_mb"),
            })
        step_rows = [r for r in rows if r.get("kind") == "ckpt"]
        anon = [float(r["soft_dirty_anon_private_mb"]) for r in step_rows if isinstance(r.get("soft_dirty_anon_private_mb"), (int, float))]
        allp = [float(r["soft_dirty_all_private_mb"]) for r in step_rows if isinstance(r.get("soft_dirty_all_private_mb"), (int, float))]
        # Exclude root/zero-op ckpt for per-step action mean. This is a declared
        # warm/root boundary, not an MCTS worker action.
        action_rows = [r for r in step_rows if int(r.get("n_ops") or 0) > 0]
        anon_action = [float(r["soft_dirty_anon_private_mb"]) for r in action_rows if isinstance(r.get("soft_dirty_anon_private_mb"), (int, float))]
        allp_action = [float(r["soft_dirty_all_private_mb"]) for r in action_rows if isinstance(r.get("soft_dirty_all_private_mb"), (int, float))]
        rss_vals = [float(r["rss_mb_after"]) for r in step_rows if isinstance(r.get("rss_mb_after"), (int, float))]
        payload = {
            "instance_id": instance,
            "ok": True,
            "measurement_ok": True,
            "worker_semantic_ok": failures == 0,
            "duration_s": time.time() - t0,
            "lane": lane,
            "numa": numa,
            "repo_source": str(repo_src),
            "repo_size_bytes": repo_size,
            "initial_index": initial_index,
            "n_events": len(schedule),
            "n_ckpt": ckpt_count,
            "n_restore": restore_count,
            "n_worker_failed_ckpt": failures,
            "worker_pid": worker.pid,
            "worker_rss_mean_mb": mean(rss_vals),
            "worker_rss_max_mb": max(rss_vals) if rss_vals else None,
            "soft_dirty_anon_private_mb": stats(anon),
            "soft_dirty_all_private_mb": stats(allp),
            "soft_dirty_anon_private_action_mb": stats(anon_action),
            "soft_dirty_all_private_action_mb": stats(allp_action),
            "rows_path": str(inst_out / "rows.jsonl"),
        }
        (inst_out / "rows.jsonl").write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n",
            encoding="utf-8",
        )
        (inst_out / "summary.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload
    except Exception as e:  # noqa: BLE001
        return {
            "instance_id": instance,
            "ok": False,
            "measurement_ok": False,
            "worker_semantic_ok": False,
            "duration_s": time.time() - t0,
            "lane": lane,
            "numa": numa,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if worker is not None:
            worker.stop()
        if sidecar is not None:
            try:
                os.killpg(sidecar.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                sidecar.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(sidecar.pid, signal.SIGKILL)
                except OSError:
                    pass
        if os.environ.get("SLIM_WORKRT_KEEP_TMP") != "1":
            shutil.rmtree(work_root, ignore_errors=True)


def pick_instances(limit: int) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for d in sorted(TRACE_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "trajectory.json").exists():
            continue
        if repo_source_for(d.name) is None:
            continue
        rows.append((d.name, d))
    return rows[:limit] if limit else rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=24)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--numa-nodes", default="0,1,2")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else (
        REPO_ROOT / "traces/swe-search/qwen3-coder-30b-p-eagle-ms"
        / f"slim-workrt-dirty-{time.strftime('%Y%m%d_%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tmp").mkdir(exist_ok=True)

    numa_nodes = [int(x) for x in args.numa_nodes.replace(",", " ").split() if x.strip()]
    instances = pick_instances(args.limit)
    tasks = []
    for i, (inst, trace_dir) in enumerate(instances):
        numa = numa_nodes[i % len(numa_nodes)] if numa_nodes else None
        tasks.append((inst, trace_dir, out_dir, i, numa))

    print(f"[slim-prof] out={out_dir}", flush=True)
    print(f"[slim-prof] instances={len(tasks)} jobs={args.jobs} numa={numa_nodes}", flush=True)
    started = time.time()
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(profile_instance, task): task[0] for task in tasks}
        for fut in cf.as_completed(futs):
            inst = futs[fut]
            row = fut.result()
            results.append(row)
            print(
                f"[slim-prof] done {inst} ok={row.get('ok')} "
                f"action_dirty_mean={((row.get('soft_dirty_anon_private_action_mb') or {}).get('mean'))} "
                f"rss_mean={row.get('worker_rss_mean_mb')} "
                f"err={row.get('error', '')}",
                flush=True,
            )
            (out_dir / "partial_summary.json").write_text(
                json.dumps({"instances": results}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    ok_rows = [r for r in results if r.get("measurement_ok") or r.get("ok")]
    def gather(key: str, subkey: str = "mean") -> list[float]:
        vals = []
        for r in ok_rows:
            d = r.get(key) or {}
            v = d.get(subkey)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        return vals

    rss = [float(r["worker_rss_mean_mb"]) for r in ok_rows if isinstance(r.get("worker_rss_mean_mb"), (int, float))]
    repo_mb = [float(r["repo_size_bytes"]) / (1024 * 1024) for r in ok_rows if isinstance(r.get("repo_size_bytes"), int)]
    summary = {
        "run_root": str(out_dir),
        "started_at": started,
        "finished_at": time.time(),
        "duration_s": time.time() - started,
        "n_requested": len(tasks),
        "n_ok": len(ok_rows),
        "n_measurement_failed": len(results) - len(ok_rows),
        "n_worker_semantic_ok": sum(1 for r in ok_rows if r.get("worker_semantic_ok")),
        "n_worker_semantic_with_failures": sum(1 for r in ok_rows if not r.get("worker_semantic_ok")),
        "mouthful": (
            "worker-only soft-dirty for slim workrt; CodeIndex/search in sidecar; "
            "run_tests externalized to verifier and not counted in checkpointed worker"
        ),
        "worker_rss_mean_mb": stats(rss),
        "repo_source_size_mb": stats(repo_mb),
        "instance_mean_soft_dirty_anon_private_action_mb": stats(gather("soft_dirty_anon_private_action_mb")),
        "instance_mean_soft_dirty_all_private_action_mb": stats(gather("soft_dirty_all_private_action_mb")),
        "instance_mean_soft_dirty_anon_private_all_ckpt_mb": stats(gather("soft_dirty_anon_private_mb")),
        "instance_mean_soft_dirty_all_private_all_ckpt_mb": stats(gather("soft_dirty_all_private_mb")),
        "instances": sorted(results, key=lambda r: r.get("instance_id", "")),
    }
    (out_dir / "slim_workrt_dirty_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"[slim-prof] summary={out_dir / 'slim_workrt_dirty_summary.json'}", flush=True)
    print(json.dumps({
        "n_ok": summary["n_ok"],
        "worker_rss_mean_mb": summary["worker_rss_mean_mb"],
        "anon_private_action_mb": summary["instance_mean_soft_dirty_anon_private_action_mb"],
        "all_private_action_mb": summary["instance_mean_soft_dirty_all_private_action_mb"],
    }, indent=2), flush=True)
    return 0 if len(ok_rows) >= min(20, len(tasks)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
