#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/rl")
from batched_fork import BatchedTemplatePool


DONOR_SRC = r"""
from __future__ import annotations

import ctypes
import json
import mmap
import os
from pathlib import Path
import signal
import sys
import time
import traceback

try:
    mem_mib = int(sys.argv[1])
    ctrl_in = sys.argv[2]
    ctrl_out = sys.argv[3]
    ready_path = sys.argv[4]
    result_dir = Path(sys.argv[5])
    touch_mode = sys.argv[6]

    page_size = 4096
    size = mem_mib * 1024 * 1024
    mem = mmap.mmap(-1, size, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                    mmap.PROT_READ | mmap.PROT_WRITE)
    buf = (ctypes.c_ubyte * size).from_buffer(mem)

    t0 = time.monotonic_ns()
    checksum = 0
    pages = 0
    for off in range(0, size, page_size):
        val = ((off // page_size) + 17) & 0xFF
        buf[off] = val
        checksum = (checksum + val) & 0xFFFFFFFF
        pages += 1
    t1 = time.monotonic_ns()

    os.environ["TEMPLATE_CTRL_IN"] = ctrl_in
    os.environ["TEMPLATE_CTRL_OUT"] = ctrl_out
    sys.path.insert(0, "/app")
    sys.path.insert(0, "/app/rl")
    import batched_fork
    read_fd, write_path = batched_fork.install_template_endpoint()

    ready = {
        "pid": os.getpid(),
        "mem_mib": mem_mib,
        "pages": pages,
        "checksum": checksum,
        "touch_ms": (t1 - t0) / 1e6,
    }
    Path(ready_path).write_text(json.dumps(ready) + "\n")

    def child_touch() -> None:
        pid = os.getpid()
        trigger = Path(f"/tmp/deltabox_touch_trigger_{pid}.go")
        deadline = time.time() + 30.0
        while time.time() < deadline and not trigger.exists():
            time.sleep(0.0002)
        if not trigger.exists():
            result = {
                "pid": pid,
                "ok": False,
                "error": "trigger timeout",
            }
            (result_dir / f"child_{pid}.json").write_text(json.dumps(result) + "\n")
            os._exit(4)

        t_child0 = time.monotonic_ns()
        errors = 0
        checksum_read = 0
        checksum_after = None
        for off in range(0, size, page_size):
            expected = ((off // page_size) + 17) & 0xFF
            got = int(buf[off])
            checksum_read = (checksum_read + got) & 0xFFFFFFFF
            if got != expected:
                errors += 1
            if touch_mode == "write":
                new_val = got ^ 0xA5
                buf[off] = new_val
                if checksum_after is None:
                    checksum_after = 0
                checksum_after = (checksum_after + int(buf[off])) & 0xFFFFFFFF
        t_child1 = time.monotonic_ns()
        result = {
            "pid": pid,
            "ok": errors == 0,
            "errors": errors,
            "pages": pages,
            "touch_mode": touch_mode,
            "checksum_read": checksum_read,
            "checksum_after_write": checksum_after,
            "touch_ms": (t_child1 - t_child0) / 1e6,
        }
        (result_dir / f"child_{pid}.json").write_text(json.dumps(result) + "\n")
        os._exit(0 if errors == 0 else 5)

    import select
    while True:
        r, _, _ = select.select([read_fd], [], [], 1.0)
        if not r:
            continue
        role = batched_fork.handle_batched_message(read_fd, write_path)
        if role == "child":
            child_touch()

except Exception:
    traceback.print_exc()
    raise
"""


def _state(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return line.split(None, 2)[1]
    except FileNotFoundError:
        return "gone"
    return "?"


def wait_until_stopped(pid: int, timeout_s: float) -> tuple[bool, float]:
    deadline = time.time() + timeout_s
    t0 = time.monotonic_ns()
    while time.time() < deadline:
        if _state(pid) == "T":
            return True, (time.monotonic_ns() - t0) / 1e6
        time.sleep(0.0005)
    return False, (time.monotonic_ns() - t0) / 1e6


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    return {
        "n": len(s),
        "mean_ms": statistics.fmean(s),
        "median_ms": statistics.median(s),
        "p95_ms": s[min(len(s) - 1, int(0.95 * (len(s) - 1)))],
        "min_ms": s[0],
        "max_ms": s[-1],
    }


def spawn_donor(workdir: Path, mem_mib: int, touch_mode: str) -> tuple[subprocess.Popen, dict]:
    ctrl_in = workdir / "tmpl_in.fifo"
    ctrl_out = workdir / "tmpl_out.fifo"
    ready = workdir / "donor.ready.json"
    results = workdir / "results"
    results.mkdir(parents=True, exist_ok=True)

    donor_py = workdir / "donor.py"
    donor_py.write_text(DONOR_SRC)
    cmd = [
        sys.executable, "-u", str(donor_py),
        str(mem_mib), str(ctrl_in), str(ctrl_out), str(ready), str(results),
        touch_mode,
    ]
    stderr = open(workdir / "donor.stderr", "w")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        cwd="/tmp",
    )
    deadline = time.time() + 60.0
    while time.time() < deadline:
        if proc.poll() is not None:
            try:
                err = (workdir / "donor.stderr").read_text()
            except OSError:
                err = ""
            raise RuntimeError(f"donor exited early rc={proc.returncode}: {err[-2000:]}")
        if ready.exists():
            return proc, json.loads(ready.read_text())
        time.sleep(0.05)
    raise RuntimeError("donor ready timeout")


def cleanup_round_files(children: list[int]) -> None:
    for pid in children:
        for path in [
            Path(f"/tmp/deltabox_touch_trigger_{pid}.go"),
        ]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def run_one(pool: BatchedTemplatePool, donor_pid: int, result_dir: Path,
            n: int, timeout_s: float, touch_mode: str) -> dict:
    for old in result_dir.glob("child_*.json"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    t0 = time.monotonic_ns()
    td0 = time.monotonic_ns()
    if not pool.dispatch_fork_batch(donor_pid, n):
        raise RuntimeError(f"dispatch_fork_batch failed n={n}")
    td1 = time.monotonic_ns()
    pairs = pool.await_fork_batch(donor_pid, n, timeout=timeout_s)
    ta1 = time.monotonic_ns()
    if len(pairs) != n:
        raise RuntimeError(f"await_fork_batch got {len(pairs)}/{n} n={n}")
    children = [int(child) for _, child in pairs]
    stopped, wait_stop_ms = wait_until_stopped(donor_pid, timeout_s=5.0)
    tfork1 = time.monotonic_ns()
    if not stopped:
        raise RuntimeError(f"donor {donor_pid} did not stop after fork_n={n}")

    tverify0 = time.monotonic_ns()
    for pid in children:
        Path(f"/tmp/deltabox_touch_trigger_{pid}.go").write_text("go\n")

    child_results: list[dict] = []
    deadline = time.time() + timeout_s
    seen: set[int] = set()
    while time.time() < deadline and len(child_results) < n:
        for p in result_dir.glob("child_*.json"):
            try:
                row = json.loads(p.read_text())
                pid = int(row["pid"])
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if pid not in seen:
                seen.add(pid)
                child_results.append(row)
        if len(child_results) < n:
            time.sleep(0.001)
    tverify1 = time.monotonic_ns()
    cleanup_round_files(children)

    ok = len(child_results) == n and all(r.get("ok") for r in child_results)
    return {
        "backend": "deltabox",
        "mode": "guest rl batched_fork fork_n + child inherited-memory read"
                + ("+write" if touch_mode == "write" else ""),
        "forks": n,
        "success": ok,
        "success_count": sum(1 for r in child_results if r.get("ok")),
        "child_count": len(child_results),
        "children": children,
        "fork_wall_ms": (tfork1 - t0) / 1e6,
        "batch_dispatch_ms": (td1 - td0) / 1e6,
        "batch_await_ms": (ta1 - td1) / 1e6,
        "wait_stopped_ms": wait_stop_ms,
        "verify_wall_ms": (tverify1 - tverify0) / 1e6,
        "ready_e2e_ms": (tverify1 - t0) / 1e6,
        "child_touch_summary": summarize([
            float(r.get("touch_ms", 0.0)) for r in child_results
        ]),
        "child_results": child_results,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forks", type=int, nargs="+", default=[1, 4, 16, 64])
    ap.add_argument("--mem-mib", type=int, default=64)
    ap.add_argument("--touch-mode", choices=["read", "write"], default="read")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="deltabox_fork_touch_"))
    donor = None
    try:
        tprep0 = time.monotonic_ns()
        donor, donor_ready = spawn_donor(workdir, args.mem_mib, args.touch_mode)
        tprep1 = time.monotonic_ns()
        pool = BatchedTemplatePool(
            ctrl_in=str(workdir / "tmpl_in.fifo"),
            ctrl_out=str(workdir / "tmpl_out.fifo"),
        )
        rows = []
        for n in args.forks:
            row = run_one(
                pool, donor.pid, workdir / "results",
                n, args.timeout, args.touch_mode,
            )
            row["source_prepare"] = {
                "ok": True,
                "ms": (tprep1 - tprep0) / 1e6,
                "donor_ready": donor_ready,
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
        Path(args.out).write_text(json.dumps(rows, indent=2) + "\n")
        return 0 if all(r["success"] for r in rows) else 1
    finally:
        if donor is not None and donor.poll() is None:
            try:
                os.kill(donor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
