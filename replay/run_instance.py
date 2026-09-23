#!/usr/bin/env python3
"""Run recorded Table 4 workloads in disposable Firecracker VMs."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace

from make_schedule import make_schedule, validate_schedule
from legacy_schedule import convert_legacy
from provenance import build_guest_archive, cached_digest, file_digest, signature
from summarize import read_results, summarize
from host_execution import (
    InstanceRun, Lane, build_instance_command, cleanup_actions, execute_instance, write_json,
)

TABLE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TABLE_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ae/runners"))
sys.path.insert(0, str(REPO_ROOT / "ae"))
from common.runtime_profile import PROFILES, checkpoint_environment
import vm


def guest_environment(mode: str, instance: str, commit: str, profile: str = "runtime-default") -> dict[str, str]:
    return {
        **checkpoint_environment(profile, mode),
        "DELTABOX_INSTANCE_ID": instance,
        "DELTABOX_REPLAY_REPOSITORY_COMMIT": commit,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
        "PYTHONPATH": "/app",
        "DELTABOX_KEEP_AGENT_CHANNEL_AFTER_RESTORE": "0",
        "DELTABOX_REOPEN_AGENT_FIFOS_ON_EPOCH": "1",
        "DELTABOX_REPLAY_STRICT_EPOCH": "1",
        "DELTABOX_ALLOW_COLD_RESTORE_POOL_CLEAR": "1",
        "DELTABOX_REQUIRE_REAL_AGENT": "1",
        "DELTABOX_REPLAY_AGENT_MODE": "real",
        "DELTABOX_REPLAY_WORKER_EXEC": "1",
        "DELTABOX_REPLAY_ACTIVE_WORKER": "0",
        "DELTABOX_WORKER_INDEX_SIDECAR": "0",
        "DELTABOX_WORKER_EXEC_TIMEOUT": "180",
        "AGENT_WORKER_CMD_TIMEOUT": "180",
        "DELTABOX_WORKER_INDEX_TIMEOUT": "300",
        "AGENT_WORKER_INDEX_MAX_TOTAL_BYTES": str(512 << 20),
        "DELTABOX_CRIU_DUMP_BIN": "criu",
        "DELTABOX_CRIU_RESTORE_BIN": "criu",
    }


def validate_results(path: Path, schedule: Path) -> None:
    rows = read_results(path)
    events = [json.loads(line) for line in schedule.read_text().splitlines() if line.strip()]
    measured = [row for row in rows if row.get("kind") in ("ckpt", "restore")]
    if len(measured) != len(events):
        raise ValueError(f"incomplete run: {len(measured)} results for {len(events)} scheduled events")
    for index, (event, row) in enumerate(zip(events, measured)):
        if row.get("ev_i") != index:
            raise ValueError("result event index does not match schedule")
        if event["type"] != row["kind"]:
            raise ValueError("result event order does not match schedule")
        if row.get("agent_mode") != "real" or row.get("require_real_agent") is not True:
            raise ValueError("results are not from the required real worker")
        if event["type"] == "ckpt" and row.get("schedule_ckpt_id") != event["ckpt_id"]:
            raise ValueError("checkpoint result does not match the scheduled ID")
        if event["type"] == "restore" and row.get("schedule_target_id") != event["restore_to_ckpt_id"]:
            raise ValueError("restore result does not match the scheduled target")
        if row["kind"] == "ckpt":
            worker = row.get("worker_exec") or {}
            index_status = row.get("worker_index_status") or {}
            if (not worker.get("ok") and not row.get("worker_exec_test_timeout")) or not index_status.get("ok"):
                raise ValueError("checkpoint is missing successful real-worker evidence")
        elif not (row.get("worker_index_status_after_restore") or {}).get("matches_target_ckpt"):
            raise ValueError("restore is missing matching worker-index evidence")
        fields = ("ckpt_wall_ms", "checkpoint_api_wall_ms", "checkpoint_sync_no_dump_ms") if row["kind"] == "ckpt" else ("restore_critical_ms", "restore_api_wall_ms")
        for field in fields:
            value = row.get(field)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"invalid measured latency: {field}={value!r}")
    summary = rows[-1]
    if not summary.get("worker_index_loaded") or not summary.get("worker_exec_required"):
        raise ValueError("worker did not load and execute the real code index")


def load_instance_run(config_path: Path) -> InstanceRun:
    return InstanceRun(config_path, json.loads(config_path.read_text()))


def run_guest(config_path: Path) -> int:
    from release.lock import from_environment
    from_environment()
    spec = load_instance_run(config_path)
    if file_digest(spec.schedule_path)["sha256"] != spec.config["schedule_sha256"]:
        raise ValueError("schedule changed after preparation")
    for key, recorded in spec.config["images"].items():
        if signature(Path(spec.config[key])) != {k: v for k, v in recorded.items() if k != "sha256"}:
            raise ValueError(f"image changed after preparation: {key}")
    with managed_vm(spec) as machine:
        with collect_results_on_exit(machine, spec):
            upload_inputs(machine, spec)
            execute_replay(machine, spec)
    validate_results(spec.results_path, spec.schedule_path)
    return 0


def vm_options(spec: InstanceRun, runtime: Path) -> argparse.Namespace:
    config = spec.config
    return argparse.Namespace(
        kernel=Path(config["kernel"]), base_xfs=Path(config["base_xfs"]),
        data_xfs=Path(config["data_xfs"]), run_rootfs=runtime / "rootfs.xfs",
        socket=runtime / "firecracker.socket",
        log=spec.output_dir / "firecracker.log", ssh_pubkey=None,
        tap=f"db{runtime.name[-10:]}", guest_ip=vm.GUEST_IP,
        vcpus=config["vcpus"], mem_mib=config["mem_mib"],
        ssh_timeout=config["ssh_timeout"], reuse_rootfs=False, no_nat=True,
        inherit_process_group=True,
    )


def capture_binding(machine, spec: InstanceRun) -> None:
    process = machine.process
    write_json(spec.output_dir / "host_binding.json", {
        "runner_affinity": sorted(os.sched_getaffinity(0)),
        "firecracker_pid": process.pid,
        "firecracker_affinity": sorted(os.sched_getaffinity(process.pid)),
        "status": Path(f"/proc/{process.pid}/status").read_text(),
        "numa_maps_at_boot": Path(f"/proc/{process.pid}/numa_maps").read_text(),
    })


def capture_exit_maps(machine, spec: InstanceRun) -> None:
    process = machine.process
    if process is not None and process.poll() is None:
        maps = Path(f"/proc/{process.pid}/numa_maps")
        (spec.output_dir / "numa_maps_at_exit.txt").write_text(maps.read_text())


@contextmanager
def managed_vm(spec: InstanceRun):
    work_parent = Path(spec.config["work_dir"]) if spec.config.get("work_dir") else None
    if work_parent:
        work_parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="db-paper-", dir=work_parent)
    machine = None
    primary = None
    storage = ExitStack()
    try:
        runtime = Path(temporary.name)
        args = vm_options(spec, runtime)
        if spec.config.get("storage_mode") == "tmpfs":
            from memory_storage import memory_images
            base, data = storage.enter_context(memory_images(
                runtime, spec.config, spec.output_dir / "storage.json"))
            args.base_xfs, args.data_xfs = base, data
            args.consume_staged_base = True
        machine = SimpleNamespace(
            args=args, runtime=runtime, process=None, ssh_ready=False,
            ssh=["ssh", *vm.ssh_opts(), "-o", "ConnectTimeout=10",
                 "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                 f"root@{args.guest_ip}"],
            scp=["scp", "-O", *vm.ssh_opts(), "-o", "ConnectTimeout=10"],
        )
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
        machine.process = vm.start_vm(args)
        machine.ssh_ready = True
        cleanup_actions(spec.output_dir, [
            ("capture binding", lambda: capture_binding(machine, spec), False),
        ])
        yield machine
    except BaseException as error:
        primary = error
        raise
    finally:
        actions = []
        if machine is not None:
            actions.extend([
                ("capture exit NUMA maps", lambda: capture_exit_maps(machine, spec), False),
                ("stop VM", lambda: vm.stop_vm(machine.args, machine.process), True),
            ])
        actions.append(("unmount RAM disks", storage.close, True))
        actions.append(("remove runtime directory", temporary.cleanup, True))
        cleanup_actions(spec.output_dir, actions, primary)


def upload_inputs(machine, spec: InstanceRun) -> None:
    archive = Path(spec.config["guest_archive"])
    if file_digest(archive)["sha256"] != spec.config["source_provenance"]["archive"]["sha256"]:
        raise ValueError("guest archive changed after preparation")
    with archive.open("rb") as source:
        subprocess.run(machine.ssh + ["mkdir -p /app && find /app -maxdepth 1 -name '*.py' -delete && rm -rf /app/pycriu /app/__pycache__ && tar -xf - -C /app"],
                       stdin=source, check=True, timeout=120)
    binary = spec.config.get("criu_dump_binary")
    if binary is not None:
        path = Path(binary["path"])
        if file_digest(path)["sha256"] != binary["sha256"]:
            raise ValueError("pinned CRIU changed after preparation")
        with path.open("rb") as source:
            subprocess.run(machine.ssh + ["mkdir -p /app/bin && cat > /app/bin/criu-pinned && chmod 755 /app/bin/criu-pinned"],
                           stdin=source, check=True, timeout=120)
    for source, destination in (
        (spec.config_path, "/tmp/table4-config.json"),
        (spec.schedule_path, "/tmp/replay_schedule.jsonl"),
    ):
        subprocess.run(machine.scp + [str(source), f"root@{machine.args.guest_ip}:{destination}"],
                       check=True, timeout=60)


def execute_replay(machine, spec: InstanceRun) -> None:
    with (spec.output_dir / "guest.log").open("w") as logfile:
        subprocess.run(machine.ssh + ["python3 -u /app/entry.py"], check=True,
                       timeout=spec.config["timeout"], stdout=logfile, stderr=subprocess.STDOUT)


def collect_jsonl(machine, spec: InstanceRun) -> None:
    subprocess.run(machine.scp + [
        f"root@{machine.args.guest_ip}:/tmp/replay_results.jsonl", str(spec.results_path),
    ], check=True, timeout=60)


def collect_dmesg(machine, spec: InstanceRun) -> None:
    with (spec.output_dir / "dmesg.log").open("w") as logfile:
        subprocess.run(machine.ssh + ["dmesg"], stdout=logfile, stderr=subprocess.STDOUT,
                       timeout=15, check=True)


def collect_diagnostics(machine, spec: InstanceRun) -> None:
    script = """import json,pathlib,sys,tarfile
# This runs after execute_replay, outside every timed C/R action.
journal = pathlib.Path('/tmp/prewarm.events')
if journal.exists():
    sys.path.insert(0, '/app')
    from cooperative_prewarm import journal_records
    with open('/tmp/prewarm.jsonl', 'w') as stream:
        for record in journal_records(journal):
            stream.write(json.dumps(record, separators=(',', ':')) + '\\n')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    for name in ('agent_trace.jsonl', 'agent_trace.log', 'template_fork.log', 'dump_lifecycle.jsonl', 'replay-agent.log', 'async-checkpoints.json', 'criu-diagnostics', 'api_profile.jsonl', 'prewarm.jsonl', 'prewarm.events', 'restore-diagnostics.json', 'restore-kernel-trace.txt', 'workload-environment.json', 'workload-environment.build.log'):
        path = pathlib.Path('/tmp') / name
        if path.exists(): archive.add(path, arcname=name)
    for path in pathlib.Path('/var/lib/replay/snapshots').glob('*/dump.log'):
        archive.add(path, arcname='criu-diagnostics/' + path.parent.name + '/dump.log')
"""
    if spec.config.get("prewarm_execution") == "agent-cooperative":
        script += """
if not journal.exists():
    raise RuntimeError('Requested cooperative prewarm has no diagnostic journal')
failed = [row for row in journal_records(journal) if row.get('state') == 'failed']
if failed:
    raise RuntimeError('Cooperative prewarm failed: ' + repr(failed))
"""
    with (spec.output_dir / 'diagnostics.tar.gz').open('wb') as out:
        subprocess.run(machine.ssh + ['python3 -'], input=script.encode(), stdout=out,
                       timeout=30, check=True)


@contextmanager
def collect_results_on_exit(machine, spec: InstanceRun):
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        if machine.ssh_ready:
            cleanup_actions(spec.output_dir, [
                ("collect results JSONL", lambda: collect_jsonl(machine, spec), True),
                ("collect dmesg", lambda: collect_dmesg(machine, spec), False),
                ("collect lifecycle diagnostics", lambda: collect_diagnostics(machine, spec),
                 spec.config.get("prewarm_execution") == "agent-cooperative"),
            ], primary)


def add_run_options(parser: argparse.ArgumentParser, modes=("fast", "slow")) -> None:
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--base-xfs", type=Path, required=True)
    parser.add_argument("--mode", choices=modes, default="fast")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--storage-mode", choices=("disk", "tmpfs"), default="disk",
                        help="tmpfs stages both VM disks in non-swappable RAM before boot")
    parser.add_argument("--testbed")
    parser.add_argument("--vcpus", type=int, default=4)
    parser.add_argument("--mem-mib", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--ssh-timeout", type=int, default=120)
    parser.add_argument("--image-hash-cache", type=Path)
    parser.add_argument("--adaptive", action="store_true")
    parser.add_argument("--experiment-id")
    parser.add_argument("--memory-policy", choices=("none", "skip", "gc", "warm"))
    parser.add_argument("--prewarm-policy", choices=("historical", "off", "read"), default="off",
                        help="off (default), safe read-only prefetch, or legacy flags with an explicit safe mode override")
    parser.add_argument("--trajectory-timing", action="store_true", help="Legacy transition input adapter (timing provenance recorded)")
    parser.add_argument("--legacy-timing-policy", choices=("recorded-wall", "paper-zero"), default="recorded-wall",
                        help="Legacy adapter timing: recorded transition intervals or explicit historical paper zero-wait policy")
    parser.add_argument("--guest-env-json", type=Path, help="Recorded policy overrides; required worker/source/mode settings are protected")
    parser.add_argument("--checkpoint-profile", choices=PROFILES,
                        default="runtime-default")
    parser.add_argument("--criu-dump-binary", type=Path,
                        help="Pinned guest-compatible CRIU; async-incremental uses it for dump and restore")
    parser.add_argument("--max-events", type=int, help="Explicit prefix quick-check only; omitted runs the full trace")
    parser.add_argument("--dry-run", action="store_true")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a trace using the current checkout's runtime in a disposable VM.")
    parser.add_argument("--instance", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--schedule", type=Path)
    source.add_argument("--trace-dir", type=Path)
    parser.add_argument("--data-xfs", type=Path, required=True)
    add_run_options(parser)
    args = parser.parse_args(argv)
    validate_options(args)
    return args


def validate_options(args) -> None:
    if min(args.vcpus, args.mem_mib, args.timeout, args.ssh_timeout) <= 0:
        raise ValueError("CPU, memory and timeout values must be positive")
    if args.max_events is not None and args.max_events <= 0:
        raise ValueError("--max-events must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+", args.instance):
        raise ValueError("invalid instance ID")
    if args.testbed and (Path(args.testbed).name != args.testbed or args.testbed in (".", "..")):
        raise ValueError("testbed must be one directory name")
    binary = getattr(args, "criu_dump_binary", None)
    if args.checkpoint_profile in ("async-incremental", "async-incremental-lazy") and binary is None:
        raise ValueError("async-incremental requires --criu-dump-binary")
    if binary is not None and not binary.is_file():
        raise FileNotFoundError(binary)
    if args.checkpoint_profile in ("async-incremental", "async-incremental-lazy") and (args.adaptive or args.memory_policy):
        raise ValueError("async-incremental currently supports standard checkpoints only")
    for path in (args.kernel, args.base_xfs, args.data_xfs):
        if not path.is_file():
            raise FileNotFoundError(path)


def prepare_single_run(args, *, existing_output: bool = False) -> InstanceRun:
    validate_options(args)
    if not args.dry_run:
        vm.require_root()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=existing_output)
    schedules = output / "schedules"
    schedules.mkdir(exist_ok=existing_output)
    schedule = schedules / f"{args.instance}.jsonl"
    if args.trace_dir:
        legacy = getattr(args, "trajectory_timing", False)
        input_paths = [args.trace_dir / name for name in (("trajectory.json",) if legacy else ("trajectory.json", "ms_trace.jsonl"))]
        inputs = [file_digest(path) for path in input_paths]
        converter = convert_legacy if legacy else make_schedule
        timing_options = {"timing_policy": args.legacy_timing_policy} if legacy else {}
        metadata = converter(args.trace_dir, args.instance, schedule, adaptive=args.adaptive, **timing_options)
    else:
        metadata_path = args.schedule.with_suffix(".meta.json")
        input_paths = [args.schedule, metadata_path]
        inputs = [file_digest(path) for path in input_paths]
        metadata = json.loads(metadata_path.read_text())
        shutil.copyfile(args.schedule, schedule)
    for path, digest in zip(input_paths, inputs):
        if signature(path) != {k: v for k, v in digest.items() if k != "sha256"}:
            raise RuntimeError(f"input changed while preparing schedule: {path}")
    if metadata.get("instance") != args.instance:
        raise ValueError("schedule instance does not match --instance")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", metadata.get("repository_commit", "")):
        raise ValueError("schedule metadata requires a full repository commit")
    events = [json.loads(line) for line in schedule.read_text().splitlines() if line.strip()]
    validate_schedule(events, adaptive=args.adaptive)
    original_n = len(events)
    if args.max_events is not None:
        events = events[:args.max_events]
        schedule.write_text("".join(json.dumps(event) + "\n" for event in events))
    metadata.update(n_ckpt=sum(e["type"] == "ckpt" for e in events),
                    n_restore=sum(e["type"] == "restore" for e in events),
                    original_events=original_n, max_events=args.max_events,
                    run_purpose="quick-check" if args.max_events is not None else "full-trace")
    write_json(schedule.with_suffix(".meta.json"), metadata)
    run_dir = output / args.mode / args.instance
    run_dir.mkdir(parents=True)
    local_schedule = run_dir / "schedule.jsonl"
    shutil.copyfile(schedule, local_schedule)
    from repro.common import run_purpose, host_state
    metadata['run_purpose'] = run_purpose(metadata['run_purpose'])
    archive = run_dir / "guest.tar"
    sources = build_guest_archive(REPO_ROOT, TABLE_ROOT / "guest", archive)
    from release.lock import from_environment
    sources["release"] = from_environment()
    images = {key: cached_digest(getattr(args, key), args.image_hash_cache)
              for key in ("kernel", "base_xfs", "data_xfs")}
    environment = guest_environment(args.mode, args.instance, metadata["repository_commit"], args.checkpoint_profile)
    binary = getattr(args, "criu_dump_binary", None)
    dump_binary = file_digest(binary) if binary is not None else None
    if dump_binary is not None:
        environment["DELTABOX_CRIU_DUMP_BIN"] = "/app/bin/criu-pinned"
        if args.checkpoint_profile in ("async-incremental", "async-incremental-lazy"):
            # Different build-time KDAT magic values thrash /run/criu.kdat
            # when stock restore alternates with patched dump. The patch is
            # dump-only; use the same pinned ELF for both command paths.
            environment["DELTABOX_CRIU_RESTORE_BIN"] = "/app/bin/criu-pinned"
    if args.guest_env_json:
        policy_digest = file_digest(args.guest_env_json)
        overrides = json.loads(args.guest_env_json.read_text())
        if signature(args.guest_env_json) != {k: v for k, v in policy_digest.items() if k != "sha256"}:
            raise RuntimeError("guest policy changed while reading")
        if not isinstance(overrides, dict):
            raise ValueError("guest env JSON must contain an object")
        for key, value in overrides.items():
            if (not re.fullmatch(r"(?:DELTABOX|AGENT)_[A-Z0-9_]+", key)
                    or key in environment or key.startswith(("DELTABOX_MEMCURVE", "DELTABOX_PAPER_", "DELTABOX_FORK_ONLY", "DELTABOX_GC_KILL"))
                    or not isinstance(value, str)
                    or key.startswith(("AGENT_", "DELTABOX_ACTIVE_", "DELTABOX_REPLAY_", "DELTABOX_DATA_",
                                       "DELTABOX_SPR_", "DELTABOX_MOATLESS_", "DELTABOX_TRAJECTORY_",
                                       "DELTABOX_INDEX_", "DELTABOX_WORKER_"))):
                raise ValueError(f"unsupported or protected guest environment override: {key}")
        environment.update(overrides)
        inputs.append(policy_digest)
    memory_policy = getattr(args, "memory_policy", None)
    prewarm_policy = getattr(args, "prewarm_policy", "off")
    if memory_policy and prewarm_policy not in ("historical", "off"):
        raise ValueError("Figure 6 memory policy already selects prewarm; do not override it")
    if memory_policy:
        if args.mode != "fast":
            raise ValueError("Memory policies only use fork restore")
        environment.update(DELTABOX_PAPER_MEMORY_POLICY=memory_policy, DELTABOX_MEMCURVE="1",
                           DELTABOX_FORK_ONLY_MEMCURVE="1", DELTABOX_RESTAMP_PARENT_INVENTORY="0",
                           DELTABOX_ASYNC_TEMPLATE_FULL_DUMP="0")
        if memory_policy == "skip": environment["DELTABOX_MEMCURVE_SKIP"] = "1"
        if memory_policy == "gc": environment["DELTABOX_MEMCURVE_GC"] = "1"
        if memory_policy == "warm":
            if environment.get("DELTABOX_DISABLE_PREWARM") == "1":
                raise ValueError("Requested warm memory policy conflicts with DELTABOX_DISABLE_PREWARM=1")
            environment["DELTABOX_PAPER_COOPERATIVE_PREWARM"] = "1"
    guest_flags = ["--enable-adaptive" if args.adaptive else "--no-adaptive", "--warm-template", "--prewarm",
                   "--agent-mode", "real", "--require-real-agent", "--worker-exec"]
    if memory_policy or prewarm_policy == "off":
        guest_flags[guest_flags.index("--prewarm")] = "--no-prewarm"
    environment.setdefault("DELTABOX_PREWARM_MODE", "write" if prewarm_policy == "historical" else "read")
    prewarm_mode = "off"
    if memory_policy == "warm":
        # GSD's historical external write-back stays disabled. The fork-only
        # agent checks support before events and performs self madvise after
        # restore, joining before the next template fork.
        environment["DELTABOX_PREWARM_MODE"] = "read"
        prewarm_mode = "populate-write-self"
    if "--prewarm" in guest_flags:
        if environment.get("DELTABOX_DISABLE_PREWARM") == "1":
            raise ValueError("Requested prewarm conflicts with DELTABOX_DISABLE_PREWARM=1; use --prewarm-policy off")
        if environment["DELTABOX_PREWARM_MODE"] not in {"read", "readonly", "read-only"}:
            raise ValueError("Unsafe write-prewarm: use --prewarm-policy off or read; historical CoW warming is unavailable")
        prewarm_mode = "read"
    config = {
        "analysis_mode": "fresh-measurement",
        "prewarm_mode": prewarm_mode,
        "host": host_state(),
        "memory_policy": memory_policy,
        "prewarm_policy": prewarm_policy,
        "prewarm_requested": memory_policy == "warm" or "--prewarm" in guest_flags,
        "prewarm_execution": "agent-cooperative" if memory_policy == "warm" else "gsd-external" if "--prewarm" in guest_flags else "off",
        "experiment": getattr(args, "experiment_id", None) or ("figure-06-memory" if memory_policy else "table-03-slow" if args.mode == "slow" else "table-02-deltabox"),
        **metadata, "mode": args.mode, "adaptive": args.adaptive,
        **{key: str(getattr(args, key).resolve()) for key in images},
        "images": images, "inputs": inputs,
        "source_provenance": sources, "guest_archive": str(archive),
        "schedule": str(schedule), "schedule_sha256": file_digest(schedule)["sha256"],
        "schedule_artifact": local_schedule.name,
        "testbed": args.testbed,
        "work_dir": str(args.work_dir.resolve()) if args.work_dir else str(REPO_ROOT / "ae/work/vm"),
        "storage_mode": getattr(args, "storage_mode", "disk"),
        "vcpus": args.vcpus, "mem_mib": args.mem_mib,
        "timeout": args.timeout, "ssh_timeout": args.ssh_timeout,
        "checkpoint_profile": args.checkpoint_profile,
        "incremental_dump_enabled": args.checkpoint_profile != "historical-async-full" and not memory_policy,
        "durable_dump_enabled": not bool(memory_policy),
        "checkpoint_profile_note": ("Historical harness uses asynchronous FULL dumps; does not measure the paper's incremental dump mechanism."
                                    if args.checkpoint_profile == "historical-async-full"
                                    else "Detached exact-parent CRIU page comparison; background reads all candidate pages before first image write."
                                    if args.checkpoint_profile in ("async-incremental", "async-incremental-lazy")
                                    else "Current runtime default incremental dump configuration; no runtime code overrides."),
        "criu_dump_binary": dump_binary,
        "guest_env": environment,
        "guest_flags": guest_flags,
        "status": "prepared",
    }
    config_path = run_dir / "run.json"
    write_json(config_path, config)
    return InstanceRun(config_path, config)


def run_single(options) -> int:
    spec = prepare_single_run(options)
    if options.dry_run:
        print(shlex.join(build_instance_command(spec, Lane(0))), flush=True)
        return 0
    execute_instance(spec)
    summarize(options.out.resolve())
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if len(sys.argv) == 3 and sys.argv[1] == "--_run-config":
        return run_guest(Path(sys.argv[2]))
    return run_single(parse_args())


def interrupted(signum, frame) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
