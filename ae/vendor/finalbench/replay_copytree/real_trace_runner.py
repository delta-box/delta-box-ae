#!/usr/bin/env python3
"""Run one trace for the real copytree+replay baseline.

For each inferred restore event:

  1. rmtree the per-event work repo, if present,
  2. copytree the pristine source repo into a temporary repo-base,
  3. start a dedicated mock_llm_server with the selected recorded or zero latency,
  4. run real_replay_driver.py to replay moatless from root to the restore
     target expansion count.

This intentionally measures the expensive "naive replay" behavior. No Find*,
ViewCode, edit, or mock-LLM step is stubbed by this runner. The historical default runtime=None
does not run tests; test-result messages can differ from the original trace
and are retained as audit evidence, without claiming workload equivalence.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
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
TRACES_ROOT = Path(os.environ["MOCK_TRACES_ROOT"])
INDEX_STORE = PAYLOAD / "index_store"
MOCK_LATENCY_POLICY = os.environ.get("MOCK_LATENCY_POLICY", "zero")
if MOCK_LATENCY_POLICY not in ("zero", "recorded"):
    raise ValueError("Unknown replay mock latency policy")
REPLAY_TIMING_METHOD = ("zero-latency-wall" if MOCK_LATENCY_POLICY == "zero"
                        else "recorded-sleep-subtracted")

sys.path.insert(0, str(BASE))
sys.path.insert(0, str(PAYLOAD))
from walker import parse_trajectory  # noqa: E402
from baseline_audit import flush_audit, message_policy  # noqa: E402


def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: float = 10.0) -> dict:
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_healthz(port: int, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            if http_json(f"http://127.0.0.1:{port}/admin/healthz", timeout=1.0).get("ok"):
                return
        except Exception as e:
            last = e
        time.sleep(0.1)
    raise TimeoutError(f"mock port {port} not healthy: {last}")


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_mock(port: int, log_path: Path, audit_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PAYLOAD)
    env["PYTHONHASHSEED"] = "0"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            [str(PYTHON), str(PAYLOAD / "mock_llm_server.py"),
             "--tcp-port", str(port), "--traces-root", str(TRACES_ROOT),
             "--latency-policy", MOCK_LATENCY_POLICY],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
    except BaseException:
        logf.close()
        raise
    proc._finalbench_logf = logf
    try:
        wait_healthz(port)
        return proc
    except BaseException as error:
        try:
            flush_audit(f'http://127.0.0.1:{port}', audit_path, primary_error=error)
        finally:
            stop_proc(proc)
        raise


def stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    finally:
        logf = getattr(proc, '_finalbench_logf', None)
        if logf:
            logf.close()


def restore_events_for_trace(instance_id: str) -> list[dict]:
    traj = PAYLOAD / "det_traces" / "ms" / instance_id / "trajectory.json"
    parsed = parse_trajectory(traj)
    events = [e for e in parsed["events"] if e["event"] == "restore"]
    # Convert target cursor to expansion count. The build_action cursor i creates
    # the i-th expansion, so target_cursor_inclusive N means replay N+1 completed
    # expansions. Root target (-1) needs zero replay.
    for e in events:
        e["target_expansions"] = max(0, int(e["target_cursor_inclusive"]) + 1)
    return events



def completion_wait_ms(instance_id, stats, target_expansions):
    """Paper Table 2: recorded RTT prefix through the mock's served cursor.

    This is the historical accounting rule, not the scheduler's measured sleep.
    Preserve sleep_wall_s separately so the two quantities can be inspected.
    """
    if not target_expansions or MOCK_LATENCY_POLICY == "zero":
        return 0.0
    from trajectory_index import load_trajectory
    sequence = load_trajectory(PAYLOAD / "det_traces" / "ms" / instance_id / "trajectory.json")
    cursor = stats.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= len(sequence):
        raise ValueError("Recorded replay requires a valid served completion cursor")
    if stats.get("n_served") != cursor:
        raise ValueError("Paper RTT accounting requires one forward replay from cursor zero")
    durations = [float(item.dur_s) for item in sequence[:cursor]]
    if any(not math.isfinite(value) or value < 0 for value in durations):
        raise ValueError("Recorded completion RTT must be finite and nonnegative")
    return math.fsum(durations) * 1000.0


def run_one_restore(
    instance_id: str,
    event: dict,
    event_idx: int,
    out_dir: Path,
    keep_workdir: bool,
) -> dict:
    src_repo = SOURCE_REPOS / f"swe-bench_{instance_id}"
    repo_base = BASE / "workdir" / "real" / instance_id / f"restore_{event_idx:03d}" / "repo_base"
    work_repo = repo_base / f"swe-bench_{instance_id}"
    summary_path = out_dir / f"restore_{event_idx:03d}.driver.json"
    driver_log = out_dir / f"restore_{event_idx:03d}.driver.log"
    mock_log = out_dir / f"restore_{event_idx:03d}.mock.log"
    audit_path = out_dir / f"restore_{event_idx:03d}.mock_audit.json"

    if repo_base.parent.exists():
        shutil.rmtree(repo_base.parent)
    repo_base.mkdir(parents=True, exist_ok=True)

    # Each restore starts from an existing, dirty-able working tree. The setup
    # copy is intentionally not charged to the restore event; it represents the
    # state left behind by the previous expansion. The measured restore begins
    # at rmtree(work_repo).
    shutil.copytree(src_repo, work_repo, symlinks=True)

    t0 = time.perf_counter()
    shutil.rmtree(work_repo)
    rmtree_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    shutil.copytree(src_repo, work_repo, symlinks=True)
    copytree_s = time.perf_counter() - t0

    target_expansions = int(event["target_expansions"])
    replay_s = 0.0
    rc = 0
    driver_summary = {
        "ok": True,
        "status": "ROOT",
        "target_expansions": target_expansions,
        "completed_expansions": 0,
        "mock_stats": None,
    }
    port = None
    mock_proc = None
    if target_expansions > 0:
        port = free_tcp_port()
        mock_proc = start_mock(port, mock_log, audit_path)
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{BASE}:{PAYLOAD}:{env.get('PYTHONPATH', '')}"
        env["PYTHONHASHSEED"] = "0"
        env["OPENAI_API_KEY"] = "dummy"

        cmd = [
            str(PYTHON),
            str(BASE / "real_replay_driver.py"),
            "--manifest-line",
            f"{instance_id}__ms",
            "--traces-root",
            str(TRACES_ROOT),
            "--mock-port",
            str(port),
            "--repo-base",
            str(repo_base),
            "--index-store-dir",
            str(INDEX_STORE),
            "--target-expansions",
            str(target_expansions),
            "--summary-json",
            str(summary_path),
        ]
        try:
            t0 = time.perf_counter()
            try:
                with open(driver_log, "w") as logf:
                    proc = subprocess.run(cmd, env=env, stdout=logf,
                                          stderr=subprocess.STDOUT, timeout=900)
            finally:
                # The entire subprocess remains measured, including its logs
                # and summary serialization. Audit flush begins only below.
                replay_s = time.perf_counter() - t0
            rc = proc.returncode
            if summary_path.exists():
                driver_summary = json.loads(summary_path.read_text())
            else:
                driver_summary = {"ok": False, "status": "NO_SUMMARY"}
        finally:
            primary = sys.exc_info()[1]
            try:
                flush_audit(f'http://127.0.0.1:{port}', audit_path, primary_error=primary)
            finally:
                stop_proc(mock_proc)
                mock_proc = None
                if primary is not None and not keep_workdir:
                    shutil.rmtree(repo_base.parent, ignore_errors=True)

    if not keep_workdir:
        shutil.rmtree(repo_base.parent, ignore_errors=True)

    ok = bool(driver_summary.get("ok")) and rc == 0
    stats = driver_summary.get('mock_stats') or {}
    if target_expansions and stats.get('latency_policy') != MOCK_LATENCY_POLICY:
        raise ValueError('Replay mock latency policy differs from the requested policy')
    if target_expansions and MOCK_LATENCY_POLICY == 'zero' and stats.get('sleep_wall_s') != 0.0:
        raise ValueError('Zero-latency mock injected sleep')
    mock_sleep_wall_ms = float(stats.get('sleep_wall_s', 0.0)) * 1000.0
    mock_sleep_ms = completion_wait_ms(instance_id, stats, target_expansions)
    restore_ms = (rmtree_s + copytree_s + replay_s) * 1000.0
    if mock_sleep_ms < 0 or mock_sleep_ms > replay_s * 1000.0:
        raise ValueError('Recorded completion RTT prefix is outside the replay interval')
    return {
        "instance": instance_id,
        "restore_index": event_idx,
        "iter": event["iter"],
        "target_node": event["target_node"],
        "target_cursor_inclusive": event["target_cursor_inclusive"],
        "target_expansions": target_expansions,
        "ok": ok,
        "rc": rc,
        "status": driver_summary.get("status"),
        "rmtree_ms": rmtree_s * 1000.0,
        "copytree_ms": copytree_s * 1000.0,
        "replay_ms": replay_s * 1000.0,
        "driver_run_search_ms": float(driver_summary.get("wall_s") or 0.0) * 1000.0,
        "restore_ms": restore_ms,
        "mock_sleep_ms": mock_sleep_ms,
        "mock_completion_wait_ms": mock_sleep_ms,
        "mock_sleep_wall_ms": mock_sleep_wall_ms,
        "llm_accounting": "served-completion-recorded-rtt-prefix" if MOCK_LATENCY_POLICY == "recorded" else "zero-injected-delay",
        # Preserve both measured wall time and the paper sleep-subtracted view.
        "restore_zero_llm_ms": restore_ms - mock_sleep_ms,
        "replay_zero_llm_ms": replay_s * 1000.0 - mock_sleep_ms,
        "mock_latency_policy": MOCK_LATENCY_POLICY,
        "replay_timing_method": REPLAY_TIMING_METHOD,
        "mock_cursor": (driver_summary.get("mock_stats") or {}).get("cursor"),
        "mock_total": (driver_summary.get("mock_stats") or {}).get("total"),
        "mock_mismatch": (driver_summary.get("mock_stats") or {}).get("n_mismatch"),
        "mock_protocol_errors": stats.get('n_protocol_errors'),
        "message_policy": message_policy(),
        "mock_audit": str(audit_path) if target_expansions else None,
        "driver_summary": str(summary_path),
        "driver_log": str(driver_log),
        "mock_log": str(mock_log),
    }


def run_trace(instance_id: str, max_restores: int | None, keep_workdir: bool) -> dict:
    out_dir = BASE / "results" / "real_per_restore" / instance_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events = restore_events_for_trace(instance_id)
    if max_restores is not None:
        events = events[:max_restores]

    rows = []
    t0 = time.perf_counter()
    for idx, ev in enumerate(events):
        row = run_one_restore(instance_id, ev, idx, out_dir, keep_workdir)
        rows.append(row)
        print(
            f"{instance_id} restore {idx + 1}/{len(events)} "
            f"target_exp={row['target_expansions']} ok={row['ok']} "
            f"copytree={row['copytree_ms']:.0f}ms replay={row['replay_ms']:.0f}ms "
            f"total={row['restore_ms']:.0f}ms",
            flush=True,
        )
        if not row["ok"]:
            break

    csv_path = out_dir / "restores.csv"
    fields = [
        "instance",
        "restore_index",
        "iter",
        "target_node",
        "target_cursor_inclusive",
        "target_expansions",
        "ok",
        "rc",
        "status",
        "rmtree_ms",
        "copytree_ms",
        "replay_ms",
        "driver_run_search_ms",
        "restore_ms",
        "mock_sleep_ms",
        "mock_completion_wait_ms",
        "mock_sleep_wall_ms",
        "llm_accounting",
        "mock_latency_policy",
        "replay_timing_method",
        "restore_zero_llm_ms",
        "replay_zero_llm_ms",
        "mock_cursor",
        "mock_total",
        "mock_mismatch",
        "mock_protocol_errors",
        "message_policy",
        "mock_audit",
        "driver_summary",
        "driver_log",
        "mock_log",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})

    ok_rows = [r for r in rows if r["ok"]]
    summary = {
        "mock_latency_policy": MOCK_LATENCY_POLICY,
        "replay_timing_method": REPLAY_TIMING_METHOD,
        "instance": instance_id,
        "requested_restores": len(events),
        "completed_restores": len(ok_rows),
        "ok": len(ok_rows) == len(events),
        "wall_s": time.perf_counter() - t0,
        "csv_path": str(csv_path),
        "mean_restore_ms": sum(r["restore_ms"] for r in ok_rows) / len(ok_rows)
        if ok_rows
        else None,
        "mean_replay_ms": sum(r["replay_ms"] for r in ok_rows) / len(ok_rows)
        if ok_rows
        else None,
        "mean_copytree_ms": sum(r["copytree_ms"] for r in ok_rows) / len(ok_rows)
        if ok_rows
        else None,
    }
    with open(out_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main() -> int:
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('benchmark interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    ap = argparse.ArgumentParser()
    ap.add_argument("instance_id")
    ap.add_argument("--max-restores", type=int, default=None)
    ap.add_argument("--keep-workdir", action="store_true")
    args = ap.parse_args()

    s = run_trace(args.instance_id, args.max_restores, args.keep_workdir)
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
