#!/usr/bin/env python3
"""Run the DeltaBox RL batched fork benchmark inside one DeltaBox VM.

Host side:
  * boot one DeltaBox Firecracker VM using the repo's runner_vm.boot_vm()
  * scp a small guest benchmark into the VM
  * run fork_n fan-out for N=1,4,16,64
  * pull back JSON and tear the VM down

Guest side:
  * spawn one donor process
  * donor allocates and touches a real mmap heap
  * harness asks the donor to fork N children via guest/rl/batched_fork.py
  * each child verifies inherited page values; optionally writes one byte per page
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time


DEFAULT_REPO = Path("/mnt/disk2/dyp/d-overlayfs")
DEFAULT_OUT_DIR = Path("/mnt/disk2/dyp/finalbench/official_sandbox_fork")


GUEST_BENCH = r'''
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
'''


def run(cmd: list[str], *, timeout: float | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    print("$ " + " ".join(shlex.quote(x) for x in cmd), flush=True)
    return subprocess.run(
        cmd,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=str(DEFAULT_REPO))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--forks", type=int, nargs="+", default=[1, 4, 16, 64])
    ap.add_argument("--mem-mib", type=int, default=64)
    ap.add_argument("--touch-mode", choices=["read", "write"], default="read")
    ap.add_argument("--vm-index", type=int, default=91)
    ap.add_argument("--vcpus", type=int, default=4)
    ap.add_argument("--vm-mem-mib", type=int, default=4096)
    ap.add_argument("--instance-id", default="django__django-12143")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out).resolve() if args.out else (
        out_dir / f"deltabox_official_fork_touch_{ts}.json"
    )
    log_path = out_path.with_suffix(".guest.log")

    sys.path.insert(0, str(repo_root / "benchmarks" / "trace_replay"))
    sys.path.insert(0, str(repo_root))
    import runner_vm  # type: ignore
    from main import SSH_OPTS  # type: ignore

    run_id = f"official-fork-{ts}"
    fc = rootfs = api_socket = guest_ip = None
    status = "unknown"
    boot_ms = None

    local_guest = out_dir / f"_deltabox_guest_fork_touch_{ts}.py"
    local_guest.write_text(textwrap.dedent(GUEST_BENCH).lstrip())
    try:
        tboot0 = time.monotonic_ns()
        fc, rootfs, api_socket, guest_ip = runner_vm.boot_vm(
            run_id=run_id,
            vcpus=args.vcpus,
            mem_mib=args.vm_mem_mib,
            instance_id=args.instance_id,
            vm_index=args.vm_index,
        )
        boot_ms = (time.monotonic_ns() - tboot0) / 1e6

        run(["scp", *SSH_OPTS, str(local_guest),
             f"root@{guest_ip}:/tmp/deltabox_fork_touch.py"],
            timeout=60, capture=True).check_returncode()

        remote_json = "/tmp/deltabox_fork_touch.json"
        remote_cmd = (
            "python3 -u /tmp/deltabox_fork_touch.py "
            + "--forks " + " ".join(str(n) for n in args.forks)
            + f" --mem-mib {int(args.mem_mib)}"
            + f" --touch-mode {shlex.quote(args.touch_mode)}"
            + f" --timeout {int(args.timeout)}"
            + f" --out {shlex.quote(remote_json)}"
        )
        started = time.monotonic_ns()
        cp = run(
            ["ssh", *SSH_OPTS, f"root@{guest_ip}", remote_cmd],
            timeout=args.timeout + 120,
            capture=True,
        )
        elapsed_ms = (time.monotonic_ns() - started) / 1e6
        log_path.write_text((cp.stdout or "") + (cp.stderr or ""))
        if cp.returncode != 0:
            status = f"guest rc={cp.returncode}"
            print(cp.stdout)
            print(cp.stderr, file=sys.stderr)
            return cp.returncode

        run(["scp", *SSH_OPTS, f"root@{guest_ip}:{remote_json}", str(out_path)],
            timeout=60, capture=True).check_returncode()

        rows = json.loads(out_path.read_text())
        wrapped = {
            "backend": "deltabox",
            "run_id": run_id,
            "instance_id": args.instance_id,
            "vm_index": args.vm_index,
            "vm_boot_ms": boot_ms,
            "guest_command_wall_ms": elapsed_ms,
            "forks": args.forks,
            "mem_mib": args.mem_mib,
            "touch_mode": args.touch_mode,
            "rows": rows,
        }
        out_path.write_text(json.dumps(wrapped, indent=2) + "\n")
        status = "ok"
        print(json.dumps(wrapped, indent=2), flush=True)
        return 0
    finally:
        if fc is not None:
            fc.kill()
        if guest_ip is not None:
            runner_vm._cleanup_parallel_tap(args.vm_index, guest_ip)
        for p in (rootfs, api_socket):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        try:
            local_guest.unlink()
        except FileNotFoundError:
            pass
        meta = {
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": status,
            "out": str(out_path),
            "guest_log": str(log_path),
            "vm_boot_ms": boot_ms,
        }
        meta_path = out_path.with_suffix(".meta.json")
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        uid = os.environ.get("SUDO_UID")
        gid = os.environ.get("SUDO_GID")
        if uid and gid:
            for p in (out_path, log_path, meta_path):
                try:
                    os.chown(p, int(uid), int(gid))
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
