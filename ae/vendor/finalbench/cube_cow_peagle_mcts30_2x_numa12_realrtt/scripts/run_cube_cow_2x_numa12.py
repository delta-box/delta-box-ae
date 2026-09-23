#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path


EXP_ROOT = Path(__file__).resolve().parents[1]

WORKERS = [
    {"lane": 0, "vm_index": 20, "numa": 0, "cpus": "0-3"},
    {"lane": 1, "vm_index": 21, "numa": 1, "cpus": "24-27"},
    {"lane": 2, "vm_index": 22, "numa": 2, "cpus": "48-51"},
]

BACKEND = "cube-cow"


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def result_ok(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("ok"))
    except Exception:
        return False


def completed_instances(prefix: str) -> set[str]:
    done: set[str] = set()
    for result in (EXP_ROOT / "results").glob(f"{prefix}_*/pilot_result.json"):
        if not result_ok(result):
            continue
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("instance"):
            done.add(str(data["instance"]))
    return done


def start(worker: dict, item: dict[str, str], args: argparse.Namespace) -> subprocess.Popen:
    inst = item["instance"]
    log_dir = EXP_ROOT / "runner_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{inst}.{BACKEND}.lane{worker['lane']}.log"
    cmd = [
        "numactl",
        f"--cpunodebind={worker['numa']}",
        f"--membind={worker['numa']}",
        sys.executable,
        str(EXP_ROOT / "scripts" / "cube_cow_schedule_replay.py"),
        "--manifest",
        str(Path(args.manifest).resolve()),
        "--instances",
        inst,
        "--template",
        args.template,
        "--sandbox-timeout",
        str(args.sandbox_timeout),
        "--worker-timeout",
        str(args.worker_timeout),
        "--run-id-prefix",
        args.run_id_prefix,
        "--upload-chunk-mb",
        str(args.upload_chunk_mb),
        "--host-cpus",
        worker["cpus"],
        "--fail-fast",
    ]
    if args.no_llm_sleep:
        cmd.append("--no-llm-sleep")
    if args.no_warm_action_worker:
        cmd.append("--no-warm-action-worker")
    if args.materialize_file_context:
        cmd.append("--materialize-file-context")
    if args.keep_snapshots:
        cmd.append("--keep-snapshots")
    if args.max_events > 0:
        cmd += ["--max-events", str(args.max_events)]
    if args.max_ckpts > 0:
        cmd += ["--max-ckpts", str(args.max_ckpts)]
    print(
        f"[cube-run2x] start lane={worker['lane']} numa={worker['numa']} "
        f"cpus={worker['cpus']} inst={inst}",
        flush=True,
    )
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(EXP_ROOT),
        env=os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc._cube_log = log  # type: ignore[attr-defined]
    proc._cube_log_path = str(log_path)  # type: ignore[attr-defined]
    return proc


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", default=str(EXP_ROOT / "manifest_peagle12.tsv"))
    p.add_argument("--template", default=os.environ.get("CUBE_FINALBENCH_TEMPLATE", "cube-finalbench-python-4vcpu-4096m-20260604-cow"))
    p.add_argument("--sandbox-timeout", type=int, default=7200)
    p.add_argument("--worker-timeout", type=float, default=300.0)
    p.add_argument("--run-id-prefix", default=f"cube_cow_full12_realrtt_{time.strftime('%Y%m%d_%H%M%S')}")
    p.add_argument("--upload-chunk-mb", type=int, default=8)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--instances", nargs="*")
    p.add_argument("--max-events", type=int, default=0)
    p.add_argument("--max-ckpts", type=int, default=0)
    p.add_argument("--no-llm-sleep", action="store_true")
    p.add_argument("--no-warm-action-worker", action="store_true")
    p.add_argument("--materialize-file-context", action="store_true")
    p.add_argument("--keep-snapshots", action="store_true")
    p.add_argument("--rerun-completed", action="store_true")
    args = p.parse_args()

    manifest = load_manifest(Path(args.manifest))
    if args.instances:
        wanted = set(args.instances)
        manifest = [row for row in manifest if row["instance"] in wanted]
    done = set() if args.rerun_completed else completed_instances(args.run_id_prefix)
    pending = [row for row in manifest if row["instance"] not in done]
    if args.limit:
        pending = pending[: args.limit]

    metadata = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend": BACKEND,
        "template": args.template,
        "workers": WORKERS,
        "manifest": str(Path(args.manifest).resolve()),
        "pending_instances": [row["instance"] for row in pending],
        "run_id_prefix": args.run_id_prefix,
        "llm_sleep": not args.no_llm_sleep,
        "warm_action_worker": not args.no_warm_action_worker,
    }
    (EXP_ROOT / "run_config.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    queue = deque(pending)
    running: dict[int, tuple[dict, dict[str, str], subprocess.Popen]] = {}
    failures: list[dict[str, object]] = []
    started = 0
    print(f"[cube-run2x] manifest={len(manifest)} done={len(done)} pending={len(pending)}")

    while queue or running:
        for worker in WORKERS:
            if worker["lane"] in running or not queue:
                continue
            item = queue.popleft()
            proc = start(worker, item, args)
            running[worker["lane"]] = (worker, item, proc)
            started += 1

        time.sleep(3)
        for lane, (worker, item, proc) in list(running.items()):
            rc = proc.poll()
            if rc is None:
                continue
            log = getattr(proc, "_cube_log", None)
            if log is not None:
                log.close()
            status = "ok" if rc == 0 else f"rc={rc}"
            print(
                f"[cube-run2x] done lane={lane} inst={item['instance']} {status} "
                f"log={getattr(proc, '_cube_log_path', '')}",
                flush=True,
            )
            if rc != 0:
                failures.append({
                    "instance": item["instance"],
                    "rc": rc,
                    "lane": lane,
                    "log": getattr(proc, "_cube_log_path", ""),
                })
            del running[lane]

    summary = {
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "started": started,
        "failures": failures,
        "run_id_prefix": args.run_id_prefix,
    }
    (EXP_ROOT / "batch_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"[cube-run2x] complete started={started} failures={len(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
