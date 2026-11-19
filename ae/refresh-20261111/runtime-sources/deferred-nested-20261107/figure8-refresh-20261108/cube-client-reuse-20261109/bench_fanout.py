#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import signal
import statistics
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


FORK_DEFAULTS = [1, 4, 16, 64]
MEM_SERVER_PORT = 38765


MEM_SERVER_CODE = r"""
import os
import signal
import socket
import threading
import time

HOST = "127.0.0.1"
PORT = int(os.environ.get("OFFICIAL_FORK_MEM_PORT", "38765"))
MEM_MIB = int(os.environ.get("OFFICIAL_FORK_MEM_MIB", "64"))
TOKEN = os.environ["OFFICIAL_FORK_TOKEN"]
PAGE = 4096

buf = bytearray(MEM_MIB * 1024 * 1024)
for i in range(0, len(buf), PAGE):
    buf[i] = (i // PAGE) % 251

marker = f"official-fork-state token={TOKEN} bytes={len(buf)} pid={os.getpid()}\n"
open("/tmp/official_fork_state.txt", "w").write(marker)

def touch_buffer():
    total = 0
    # Read one byte per page to force the restored child to fault/touch the
    # memory-backed state rather than just returning from a control-plane API.
    for i in range(0, len(buf), PAGE):
        total = (total + buf[i]) & 0xFFFFFFFF
    return total

requests = 0
lock = threading.Lock()

def handle(conn):
    global requests
    with conn:
        _ = conn.recv(1024)
        checksum = touch_buffer()
        with lock:
            requests += 1
            n = requests
        conn.sendall(
            f"OK token={TOKEN} bytes={len(buf)} checksum={checksum} requests={n} pid={os.getpid()}\n".encode()
        )

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((HOST, PORT))
sock.listen(128)
open("/tmp/official_fork_mem_server.ready", "w").write(str(time.time()))
while True:
    conn, _ = sock.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
"""


@dataclass
class TimedStep:
    ok: bool
    ms: float
    error: str | None = None


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def getenv_any(names: list[str], default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def parse_forks(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        n = int(part)
        if n < 1:
            raise ValueError(f"fork count must be >=1, got {n}")
        out.append(n)
    if not out:
        raise ValueError("empty fork list")
    return out


def summarize(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0}
    ordered = sorted(vals)

    def pct(q: float) -> float:
        return ordered[int(q * (len(ordered) - 1))]

    return {
        "n": len(vals),
        "mean_ms": statistics.fmean(vals),
        "median_ms": statistics.median(vals),
        "p95_ms": pct(0.95),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def run_timed(fn) -> tuple[Any, TimedStep]:
    t0 = now_ms()
    try:
        return fn(), TimedStep(ok=True, ms=now_ms() - t0)
    except Exception as exc:
        return None, TimedStep(ok=False, ms=now_ms() - t0, error=f"{type(exc).__name__}: {exc}")


def check_command_result(result: Any) -> str:
    exit_code = getattr(result, "exit_code", 0)
    if exit_code not in (0, None):
        raise RuntimeError(
            f"command exit_code={exit_code} stderr={getattr(result, 'stderr', '')}"
        )
    return str(getattr(result, "stdout", "") or "")


def cube_run_shell(sb: Any, cmd: str, timeout: float) -> str:
    return check_command_result(sb.commands.run(cmd, timeout=timeout))


def e2b_run_shell(sb: Any, cmd: str, timeout: float) -> str:
    return check_command_result(sb.commands.run(cmd, timeout=timeout))


def cube_run_code(sb: Any, code: str, timeout: float) -> str:
    res = sb.run_code(code, timeout=timeout)
    text = getattr(res, "text", None)
    if text:
        return str(text)
    logs = getattr(res, "logs", None)
    stdout = getattr(logs, "stdout", None)
    if stdout:
        return "".join(str(x) for x in stdout)
    return ""


def e2b_run_code(sb: Any, code: str, timeout: float) -> str:
    cmd = getattr(sb, "commands")
    # E2B command API is the official data plane; use it to force the child
    # sandbox to be usable after creation from snapshot.
    result = cmd.run(f"python3 - <<'PY'\n{code}\nPY", timeout=timeout)
    if getattr(result, "exit_code", 0) not in (0, None):
        raise RuntimeError(f"command exit_code={result.exit_code} stderr={getattr(result, 'stderr', '')}")
    return str(getattr(result, "stdout", "") or "")


def start_mem_server_shell(*, mem_mib: int, token: str) -> str:
    assert mem_mib == 64
    return f"""
set -eu
chmod 755 /tmp/official_fork_verify
rm -f /tmp/official_fork_state.txt /tmp/official_fork_mem_server.pid
nohup /tmp/official_fork_verify serve {token} >/tmp/official_fork_mem_server.log 2>&1 </dev/null &
echo $! >/tmp/official_fork_mem_server.pid
python3 - <<'PYREADY'
import socket,time,subprocess
for i in range(3000):
    try:
        # A connect probe would be a verification request; only wait for the
        # inherited marker, then invoke the verifier once (requests=1).
        pid=open('/tmp/official_fork_mem_server.pid').read().strip()
        marker=open('/tmp/official_fork_state.txt').read()
        if 'pid='+pid not in marker or 'token={token}' not in marker:raise FileNotFoundError('marker pending')
        listening=any(line.split()[1].endswith(':976D') and line.split()[3]=='0A' for line in open('/proc/net/tcp').read().splitlines()[1:])
        if not listening:raise FileNotFoundError('listen pending')
        subprocess.run(['/tmp/official_fork_verify','verify',{token!r},'1'],check=True)
        break
    except FileNotFoundError:time.sleep(.01)
else:raise RuntimeError('native memory server not ready')
PYREADY
"""


def verify_mem_server_shell(*, token: str) -> str:
    return f"exec /tmp/official_fork_verify verify {token} 2"


def write_native_state(sb, *, mem_mib, token, timeout, runner):
    binary=(Path(__file__).resolve().parent/'verify_state').read_bytes()
    sb.files.write('/tmp/official_fork_verify',binary)
    return runner(sb,start_mem_server_shell(mem_mib=mem_mib,token=token),timeout)


def cube_write_state(sb: Any, *, mem_mib: int, token: str, timeout: float) -> str:
    return write_native_state(sb,mem_mib=mem_mib,token=token,timeout=timeout,runner=cube_run_shell)


def e2b_write_state(sb: Any, *, mem_mib: int, token: str, timeout: float) -> str:
    return write_native_state(sb,mem_mib=mem_mib,token=token,timeout=timeout,runner=e2b_run_shell)


def audit_identities(backend, children, timeout):
    runner = cube_run_shell if backend == "cube" else e2b_run_shell
    def one(sb):
        output = runner(sb, "getconf _NPROCESSORS_ONLN; awk '/^MemTotal:/{print $2}' /proc/meminfo", timeout)
        vals = output.strip().splitlines()
        assert int(vals[0]) == 4 and 7500*1024 < int(vals[1]) < 8500*1024, vals
        return {"sandbox_id": sb.sandbox_id, "cpus": int(vals[0]), "mem_total_kib": int(vals[1])}
    with cf.ThreadPoolExecutor(max_workers=min(64,len(children))) as pool:
        return list(pool.map(one,children))


def bench_cube(args: argparse.Namespace, forks: list[int]) -> list[dict[str, Any]]:
    from cubesandbox import Config, Sandbox

    api_url = args.cube_api_url or getenv_any(["CUBE_API_URL", "E2B_API_URL"], "http://127.0.0.1:3000")
    template = args.cube_template or getenv_any(["CUBE_TEMPLATE_ID", "E2B_TEMPLATE_ID"])
    proxy_node_ip = args.cube_proxy_node_ip or getenv_any(["CUBE_PROXY_NODE_IP"])
    cfg = Config(
        api_url=api_url,
        template_id=template,
        proxy_node_ip=proxy_node_ip,
        timeout=args.timeout,
        request_timeout=args.request_timeout,
    )
    rows: list[dict[str, Any]] = []

    for n in forks:
        run_id = f"cube-{int(time.time())}-{uuid.uuid4().hex[:8]}-n{n}"
        source = None
        clones: list[Any] = []
        row: dict[str, Any] = {
            "backend": "cube",
            "verification_workers_per_guest": 4,
            "verification_max_concurrent_guests": 4,
            "forks": n,
            "run_id": run_id,
            "api_url": api_url,
            "template": template,
            "mode": "official Sandbox.clone(n=N, concurrency=N)",
            "success": False,
        }
        t_total0 = now_ms()
        try:
            source, create_source = run_timed(
                lambda: Sandbox.create(
                    template=template,
                    timeout=args.timeout,
                    metadata={"official_fork_run": run_id, "role": "source"},
                    config=cfg,
                )
            )
            row["source_create"] = asdict(create_source)
            if not create_source.ok:
                raise RuntimeError(create_source.error)

            row["source_identity"] = cube_run_shell(source, "getconf _NPROCESSORS_ONLN; awk '/^MemTotal:/{print $2}' /proc/meminfo", args.exec_timeout)
            ident = row["source_identity"].strip().splitlines()
            assert int(ident[0]) == 4 and 7500 * 1024 < int(ident[1]) < 8500 * 1024, ident
            _, prep = run_timed(
                lambda: cube_write_state(
                    source,
                    mem_mib=args.mem_mib,
                    token=run_id,
                    timeout=args.exec_timeout,
                )
            )
            row["source_prepare"] = asdict(prep)
            if not prep.ok:
                raise RuntimeError(prep.error)

            # Cube's official RL fan-out wrapper includes creating and cleaning
            # an internal snapshot. Its wall time is the e2e fork time for the
            # public clone API.
            row["started_epoch"] = time.time()
            t_ready0 = now_ms()
            clones, clone_step = run_timed(lambda: source.clone(n=n, concurrency=n))
            row["official_clone"] = asdict(clone_step)
            row["freeze_ms"] = None
            row["fork_wall_ms"] = clone_step.ms
            row["e2e_ms"] = clone_step.ms
            if not clone_step.ok:
                raise RuntimeError(clone_step.error)

            per_child: list[dict[str, Any]] = []
            t_verify0 = now_ms()

            def verify_one(idx_sb: tuple[int, Any]) -> dict[str, Any]:
                idx, sb = idx_sb
                verified_text, step = run_timed(
                    lambda: cube_run_shell(
                        sb,
                        verify_mem_server_shell(token=run_id),
                        timeout=args.exec_timeout,
                    )
                )
                return {
                    "index": idx,
                    "sandbox_id": getattr(sb, "sandbox_id", None),
                    "verify": asdict(step),
                    "verification_stdout": verified_text,
                }

            with cf.ThreadPoolExecutor(max_workers=min(n, 4)) as pool:
                for child in pool.map(verify_one, list(enumerate(clones))):
                    per_child.append(child)
            row["verify_wall_ms"] = now_ms() - t_verify0
            row["ready_e2e_ms"] = now_ms() - t_ready0
            row["ready_finished_epoch"] = time.time()
            row["all_children_retained_until_ready"] = len(clones) == n
            row["children"] = per_child
            row["verify_summary"] = summarize([c["verify"]["ms"] for c in per_child if c["verify"]["ok"]])
            row["success_count"] = sum(1 for c in per_child if c["verify"]["ok"])
            row["success"] = row["success_count"] == n
            row["child_identity_after_timer"] = audit_identities("cube", clones, args.exec_timeout)
        except BaseException as exc:
            row["success"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            row["cleanup"] = []
            for sb in [*(clones or []), *([source] if source is not None else [])]:
                rec = {"sandbox_id": getattr(sb, "sandbox_id", None)}
                try:
                    value = sb.kill()
                    assert value is not False, "SDK kill returned False"
                    rec["status"] = "ok"
                except Exception as exc:
                    rec.update(status="failed", error=str(exc))
                    row["success"] = False
                row["cleanup"].append(rec)
            row["total_wall_ms"] = now_ms() - t_total0
            rows.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    return rows


def bench_e2b(args: argparse.Namespace, forks: list[int]) -> list[dict[str, Any]]:
    from e2b import Sandbox

    api_url = args.e2b_api_url or getenv_any(["E2B_API_URL"], "http://127.0.0.1:3000")
    sandbox_url = args.e2b_sandbox_url or getenv_any(["E2B_SANDBOX_URL"], "http://127.0.0.1:3002")
    api_key = args.e2b_api_key or getenv_any(["E2B_API_KEY"])
    template = args.e2b_template or getenv_any(["E2B_TEMPLATE_ID"], "base")
    base_opts: dict[str, Any] = {
        "api_url": api_url,
        "sandbox_url": sandbox_url,
        "request_timeout": args.request_timeout,
    }
    if api_key:
        base_opts["api_key"] = api_key

    rows: list[dict[str, Any]] = []
    for n in forks:
        run_id = f"e2b-{int(time.time())}-{uuid.uuid4().hex[:8]}-n{n}"
        source = None
        children: list[Any] = []
        snapshot_id: str | None = None
        cleanup: list[dict[str, Any]] = []

        def clean_resource(kind, resource_id, operation, *, phase="final"):
            record = {"resource": kind, "id": resource_id, "phase": phase}
            try:
                returned = operation()
                if returned is True or returned is False or returned is None:
                    record["returned"] = returned
                if returned is False:
                    raise RuntimeError("SDK cleanup returned False")
                record["status"] = "ok"
            except Exception as exc:
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            cleanup.append(record)
            return record["status"] == "ok"

        row: dict[str, Any] = {
            "backend": "e2b",
            "verification_workers_per_guest": 4,
            "verification_max_concurrent_guests": 4,
            "forks": n,
            "run_id": run_id,
            "api_url": api_url,
            "sandbox_url": sandbox_url,
            "template": template,
            "mode": "official create_snapshot + parallel Sandbox.create(snapshot_id)",
            "success": False,
        }
        t_total0 = now_ms()
        try:
            source, create_source = run_timed(
                lambda: Sandbox.create(
                    template=template,
                    timeout=args.timeout,
                    metadata={"official_fork_run": run_id, "role": "source"},
                    **base_opts,
                )
            )
            row["source_create"] = asdict(create_source)
            if not create_source.ok:
                raise RuntimeError(create_source.error)

            row["source_identity"] = e2b_run_shell(source, "getconf _NPROCESSORS_ONLN; awk '/^MemTotal:/{print $2}' /proc/meminfo", args.exec_timeout)
            ident = row["source_identity"].strip().splitlines()
            assert int(ident[0]) == 4 and 7500 * 1024 < int(ident[1]) < 8500 * 1024, ident
            _, prep = run_timed(
                lambda: e2b_write_state(
                    source,
                    mem_mib=args.mem_mib,
                    token=run_id,
                    timeout=args.exec_timeout,
                )
            )
            row["source_prepare"] = asdict(prep)
            if not prep.ok:
                raise RuntimeError(prep.error)

            # Named snapshots return namespace/name:tag; SDK 2.25.1 does not
            # escape that slash in DELETE. Keep the canonical ID instead.
            row["started_epoch"] = time.time()
            t_ready0 = now_ms()
            snapshot, freeze = run_timed(lambda: source.create_snapshot(**base_opts))
            row["freeze"] = asdict(freeze)
            row["freeze_ms"] = freeze.ms
            if not freeze.ok:
                raise RuntimeError(freeze.error)
            snapshot_id = getattr(snapshot, "snapshot_id", None) or getattr(snapshot, "snapshotId", None)
            row["snapshot_id"] = snapshot_id
            if not snapshot_id:
                raise RuntimeError(f"missing snapshot_id in {snapshot!r}")

            per_create: list[dict[str, Any]] = []
            row["child_creates"] = per_create  # Preserve failed-request details too.
            t_fork0 = now_ms()

            def create_child(idx: int) -> tuple[Any, dict[str, Any]]:
                child, step = run_timed(
                    lambda: Sandbox.create(
                        template=snapshot_id,
                        timeout=args.timeout,
                        metadata={"official_fork_run": run_id, "role": "child", "index": str(idx)},
                        **base_opts,
                    )
                )
                return child, {"index": idx, "create": asdict(step)}

            per_child: list[dict[str, Any]] = []
            batches = []
            batch_size = args.e2b_batch_size or n
            for offset in range(0, n, batch_size):
                indices = list(range(offset, min(n, offset + batch_size)))
                started = now_ms()
                batch_children = []
                with cf.ThreadPoolExecutor(max_workers=min(len(indices), args.max_workers)) as pool:
                    for child, meta in pool.map(create_child, indices):
                        per_create.append(meta)
                        if child is not None:
                            children.append(child)
                            batch_children.append((meta["index"], child))
                create_end = now_ms()
                if len(batch_children) != len(indices) or any(not m["create"]["ok"] for m in per_create):
                    raise RuntimeError("child create failure in measured batch")

                def verify_one(idx_sb):
                    idx, sb = idx_sb
                    verified_text, step = run_timed(lambda: e2b_run_shell(sb, verify_mem_server_shell(token=run_id), timeout=args.exec_timeout))
                    return {"index": idx, "sandbox_id": getattr(sb, "sandbox_id", None), "verify": asdict(step), "verification_stdout": verified_text}

                with cf.ThreadPoolExecutor(max_workers=min(len(indices), 4)) as pool:
                    verified = list(pool.map(verify_one, batch_children))
                ready = now_ms()
                per_child.extend(verified)
                batches.append({"offset": offset, "count": len(indices), "create_ms": create_end-started,
                                "verify_ms": ready-create_end, "ready_ms": ready-started,
                                "success_count": sum(c["verify"]["ok"] for c in verified)})
                if not all(c["verify"]["ok"] for c in verified):
                    raise RuntimeError("inherited-memory verification failed")
                # Free only this batch's owned children before launching the next.
                # Inter-batch cleanup is inside the end-to-end timer.
                if offset + batch_size < n:
                    for _, child in batch_children:
                        if not clean_resource("child", getattr(child, "sandbox_id", None),
                                              lambda: child.kill(**base_opts), phase="inter-batch"):
                            raise RuntimeError("inter-batch child cleanup failed")
                        children.remove(child)
            row["batch_size"] = batch_size
            row["batches"] = batches
            row["mode"] = "one snapshot + sequential measured create/verify batches; inter-batch cleanup included"
            row["fork_wall_ms"] = sum(b["create_ms"] for b in batches)
            row["verify_wall_ms"] = sum(b["verify_ms"] for b in batches)
            row["ready_e2e_ms"] = now_ms() - t_ready0
            row["ready_finished_epoch"] = time.time()
            row["all_children_retained_until_ready"] = len(children) == n
            assert batch_size == n and len(batches) == 1
            row["mode"] = "native N-way: one snapshot, concurrent create, verify all, retain all N until ready"
            row["e2e_ms"] = row["ready_e2e_ms"]
            row["child_creates"] = per_create
            row["children"] = per_child
            row["success_count"] = sum(c["verify"]["ok"] for c in per_child)
            row["success"] = row["success_count"] == n
            row["child_identity_after_timer"] = audit_identities("e2b", children, args.exec_timeout)
        except BaseException as exc:
            row["success"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            for sb in children:
                clean_resource("child", getattr(sb, "sandbox_id", None), lambda: sb.kill(**base_opts))
            if snapshot_id:
                clean_resource("snapshot", snapshot_id, lambda: Sandbox.delete_snapshot(snapshot_id, **base_opts))
            if source is not None:
                clean_resource("source", getattr(source, "sandbox_id", None), lambda: source.kill(**base_opts))
            row["cleanup"] = cleanup
            if any(record["status"] != "ok" for record in cleanup):
                row["success"] = False
            row["total_wall_ms"] = now_ms() - t_total0
            rows.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["cube", "e2b", "both"], default="both")
    ap.add_argument("--forks", default=",".join(str(x) for x in FORK_DEFAULTS))
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--request-timeout", type=float, default=180.0)
    ap.add_argument("--exec-timeout", type=float, default=120.0,
                    help="Guest command execution budget in seconds (hang protection, not a performance threshold)")
    ap.add_argument("--mem-mib", type=int, default=64)
    ap.add_argument("--max-workers", type=int, default=64)
    ap.add_argument("--e2b-batch-size", type=int, default=0, help="0 means one native N-way batch")
    ap.add_argument("--cube-api-url", default="")
    ap.add_argument("--cube-template", default="")
    ap.add_argument("--cube-proxy-node-ip", default="")
    ap.add_argument("--e2b-api-url", default="")
    ap.add_argument("--e2b-sandbox-url", default="")
    ap.add_argument("--e2b-api-key", default="")
    ap.add_argument("--e2b-template", default="")
    args = ap.parse_args()

    forks = parse_forks(args.forks)
    all_rows: list[dict[str, Any]] = []
    if args.backend in ("cube", "both"):
        all_rows.extend(bench_cube(args, forks))
    if args.backend in ("e2b", "both"):
        all_rows.extend(bench_e2b(args, forks))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(all_rows, indent=2, ensure_ascii=True) + "\n")
    return 0 if all(r.get("success") for r in all_rows) else 1


def _ae_interrupted(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt(f"Experiment interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _ae_interrupted)
    raise SystemExit(main())
