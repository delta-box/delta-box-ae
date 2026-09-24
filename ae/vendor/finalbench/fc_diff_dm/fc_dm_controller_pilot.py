#!/usr/bin/env python3
"""Controller-retained Firecracker diff + dm-thin pilot.

This version fixes the semantic problem in the initial whole-VM pilot: the MCTS
SearchTree is kept by the host controller and sent to the guest for each single
real Moatless iteration. VM restore only rolls back sandbox process/FS state;
the controller tree, including wrong branches, survives rollback.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fc_dm_pilot import (
    BASE,
    WORK_BASE,
    GUEST_IP,
    GUEST_PORT,
    HOST_IP,
    TAP,
    PAYLOAD,
    FirecrackerVM,
    DMThin,
    merge_mem,
    prepare_rootfs,
    run,
    setup_tap,
)
sys.path.insert(0, str(PAYLOAD))
from baseline_audit import flush_audit, message_policy  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from repro.memory_budget import check_capacity, GIB


def http_json(url: str, method: str = "GET", obj: dict | None = None, timeout: float = 30.0) -> dict:
    data = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Guest API {url} failed ({error.code}): {detail}') from error


def start_host_mock(instance: str, port: int, log_path: Path, audit_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PAYLOAD)
    env["PYTHONHASHSEED"] = "0"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            [str(Path(os.environ["MOATLESS_VENV"]) / "bin/python"),
             str(PAYLOAD / "mock_llm_server.py"), "--tcp-host", HOST_IP,
             "--tcp-port", str(port), "--traces-root", os.environ["MOCK_TRACES_ROOT"]],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True)
    except BaseException:
        logf.close()
        raise
    proc._finalbench_logf = logf  # type: ignore[attr-defined]
    base = f"http://{HOST_IP}:{port}"
    try:
        deadline = time.time() + 20.0
        while time.time() < deadline:
            try:
                h = http_json(f"{base}/admin/healthz", timeout=1.0)
                if h.get("ok"):
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise TimeoutError("host mock did not become healthy")
        load = http_json(f"{base}/admin/load", method="POST",
                         obj={"instance_id": instance, "variant": "ms"}, timeout=30.0)
        print(f"[mock] loaded {load}", flush=True)
        return proc
    except BaseException as error:
        try:
            flush_audit(base, audit_path, primary_error=error)
        finally:
            stop_host_mock(proc)
        raise


def stop_host_mock(proc: subprocess.Popen | None) -> None:
    if not proc:
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
        logf = getattr(proc, "_finalbench_logf", None)
        if logf:
            logf.close()


def wait_state(timeout_s: float = 180.0) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            return http_json(f"http://{GUEST_IP}:{GUEST_PORT}/state", timeout=2.0)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = repr(e)
            time.sleep(0.25)
    raise TimeoutError(f"guest state timeout; last={last}")


def parent_map_from_tree(tree: dict) -> dict[int, int | None]:
    out = {}
    def rec(n, p=None):
        if not isinstance(n, dict):
            return
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


def run_controller_pilot(
    instance: str,
    *,
    max_steps: int,
    mem_mib: int,
    vcpus: int,
    data_size: str,
    snapshot_mode: str = "diff",
    restore_on_rollback: bool = True,
    run_id_prefix: str = "ctrl",
    cleanup_large_artifacts: bool = False,
    mock_port: int = 22999,
) -> dict:
    if snapshot_mode not in {"diff", "full"}:
        raise ValueError(f"snapshot_mode must be 'diff' or 'full', got {snapshot_mode!r}")
    run_id = f"{run_id_prefix}_{instance}"
    results = BASE / "results" / run_id
    snaps = WORK_BASE / "snapshots" / run_id
    logs = WORK_BASE / "logs" / run_id
    dm_work = WORK_BASE / "dm_work" / run_id
    image = WORK_BASE / "images" / f"{run_id}.xfs"
    image.parent.mkdir(parents=True, exist_ok=True)
    for p in [results, snaps, logs, dm_work]:
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)

    memory_job = json.loads(os.environ.get('AE_MEMORY_JOB', '{}'))
    capacity_log = results / 'capacity.jsonl'
    def capacity(phase, allocation=0):
        if memory_job:
            return check_capacity(WORK_BASE, phase, allocation, capacity_log,
                                  node=memory_job['node'])

    print(f"[ctrl] prepare rootfs instance={instance}", flush=True)
    prepare_rootfs(instance, image, guest_driver="guest_controller_driver.py")
    print("[ctrl] setup tap", flush=True)
    setup_tap()
    print(f"[ctrl] setup dm-thin data_size={data_size}", flush=True)
    dm = DMThin(dm_work, image, data_size=data_size, meta_size="256M")
    vm = None
    mock_proc = None
    try:
        root_dev = dm.setup()
        if cleanup_large_artifacts:
            # dd has finished; the thin device owns its independent root copy.
            image.unlink()
        capacity('before-vm-boot', mem_mib * 1024 ** 2)
        mock_proc = start_host_mock(instance, mock_port, logs / "host_mock.log",
                                    results / 'mock_audit.json')
        vm = FirecrackerVM(
            api_sock=WORK_BASE / f"fc_{run_id}.socket",
            log_path=logs / "firecracker.log",
            root_dev=root_dev,
            mem_mib=mem_mib,
            vcpus=vcpus,
        )

        ckpts = []
        restore_events = []
        tree = None
        active_seq = None
        prev_node = None
        conditions = {
            "cpu_governor": Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip(),
            "cpu_min_freq": Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq").read_text().strip(),
            "cpu_max_freq": Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq").read_text().strip(),
            "numa_command_required": "run this script under numactl --cpunodebind=0 --membind=0",
            "semantics": "host-retained SearchTree; FC+dm restores sandbox only",
            "snapshot_mode": snapshot_mode,
            "message_policy": message_policy(),
        }

        vm.spawn()
        vm.configure_and_start()
        wait_state(timeout_s=240.0)

        init = http_json(
            f"http://{GUEST_IP}:{GUEST_PORT}/init",
            method="POST",
            obj={"instance": instance, "mock_url_base": f"http://{HOST_IP}:{mock_port}"},
            timeout=240.0,
        )
        if not init.get("ok"):
            raise RuntimeError(f"guest init failed: {init}")
        tree = init["tree"]
        s = init["state"]

        base_vmstate = snaps / "base.vmstate"
        base_mem = snaps / "base.mem"
        capacity('before-base-snapshot', mem_mib * 1024 ** 2)
        fc = vm.take_snapshot(snapshot_path=base_vmstate, mem_path=base_mem, snapshot_type="Full")
        capacity('after-base-snapshot')
        dmres = dm.snapshot("seq0")
        ckpts.append({
            "seq": 0,
            "state": s,
            "tree": tree,
            "vmstate": str(base_vmstate),
            "mem": str(base_mem),
            "diff_chain": [],
            **fc,
            **dmres,
        })
        active_seq = 0
        prev_node = 0
        print(f"[ckpt 0] root fc={fc['fc_total_ms']:.1f}ms dm={dmres['dm_snapshot_ms']:.1f}ms", flush=True)

        for seq in range(1, max_steps + 1):
            sel = http_json(
                f"http://{GUEST_IP}:{GUEST_PORT}/select",
                method="POST",
                obj={"tree": tree},
                timeout=60.0,
            )
            if not sel.get("ok"):
                raise RuntimeError(f"guest select failed: {sel}")
            selected_node_id = sel.get("selected_node_id")
            rollback_needed_pre = seq > 1 and selected_node_id != prev_node
            if rollback_needed_pre and restore_on_rollback:
                target_seq = node_checkpoint_seq(ckpts, selected_node_id)
                if target_seq is None:
                    raise RuntimeError(f"selected node {selected_node_id} has no checkpoint")
                target = next(c for c in ckpts if c["seq"] == target_seq)
                # load_snapshot also kills the old VM outside its timer.
                # Doing it here releases its previous mapped merge before making another.
                vm.kill()
                if snapshot_mode == "diff":
                    capacity('before-merge-' + str(seq), mem_mib * 1024 ** 2)
                    merged_mem = snaps / f"merged_pre_seq{seq}_target{target_seq}.mem"
                    merge = merge_mem(base_mem, [Path(p) for p in target["diff_chain"]], merged_mem)
                    restore_mem = merged_mem if target["diff_chain"] else Path(target["mem"])
                else:
                    merge = {
                        "merge_ms": 0.0,
                        "base_copy_ms": 0.0,
                        "diff_apply_ms": 0.0,
                        "n_diffs": 0,
                        "total_dirty_bytes": 0,
                        "snapshot_mode": "full",
                    }
                    restore_mem = Path(target["mem"])
                dm_restore = dm.restore_dev(f"seq{target['seq']}")
                load = vm.load_snapshot(
                    snapshot_path=Path(target["vmstate"]),
                    mem_path=restore_mem,
                    # This flag controls whether the resumed microVM keeps
                    # dirty-page tracking enabled for future Diff snapshots; it
                    # does not mean the mem_backend itself is a diff file. Diff
                    # mode must keep it enabled, otherwise the next Diff
                    # checkpoint is rejected by Firecracker.
                    enable_diff=(snapshot_mode == "diff"),
                )
                restored = wait_state(timeout_s=120.0)
                if cleanup_large_artifacts and snapshot_mode == "diff":
                    # Firecracker retains its open mapping until the next kill.
                    # No future restore uses this temporary reconstruction.
                    merged_mem.unlink()
                capacity('after-restore-' + str(seq))
                restore_events.append({
                    "before_seq": seq,
                    "selected_node_id": selected_node_id,
                    "target_seq": target["seq"],
                    "merge": merge,
                    "dm_restore": dm_restore,
                    "load": load,
                    "restored_state": restored,
                    "retained_tree_total_nodes": len(parent_map_from_tree(tree)),
                })
                active_seq = target["seq"]
                prev_node = selected_node_id
                print(f"[pre-restore] seq{seq}->target_seq{target['seq']} selected={selected_node_id}", flush=True)

            step = http_json(
                f"http://{GUEST_IP}:{GUEST_PORT}/step",
                method="POST",
                obj={"seq": seq, "tree": tree, "selected_node_id": selected_node_id},
                timeout=float(os.environ.get("DELTABOX_TEST_STEP_TIMEOUT", "240")),
            )
            if not step.get("ok"):
                raise RuntimeError(f"guest step failed: {step}")
            tree = step["tree"]
            event = step.get("event") or {}
            node_id = step.get("node_id")
            pm_after = parent_map_from_tree(tree)
            parent = pm_after.get(node_id)

            snap_prefix = "full" if snapshot_mode == "full" else "diff"
            snapshot_type = "Full" if snapshot_mode == "full" else "Diff"
            vmstate = snaps / f"{snap_prefix}_{seq}.vmstate"
            mem = snaps / f"{snap_prefix}_{seq}.mem"
            fc = vm.take_snapshot(snapshot_path=vmstate, mem_path=mem, snapshot_type=snapshot_type)
            dmres = dm.snapshot(f"seq{seq}")
            capacity('after-checkpoint-' + str(seq))
            active_ckpt = next(c for c in ckpts if c["seq"] == active_seq)
            if snapshot_mode == "diff":
                diff_chain = active_ckpt["diff_chain"] + [str(mem)]
            else:
                diff_chain = []
            s = step["state"]
            ckpts.append({
                "seq": seq,
                "state": s,
                "tree": tree,
                "event": event,
                "parent_node_id": parent,
                "selected_node_id": selected_node_id,
                "rollback_needed_pre": rollback_needed_pre,
                "vmstate": str(vmstate),
                "mem": str(mem),
                "snapshot_type": snapshot_type,
                "snapshot_mode": snapshot_mode,
                "parent_checkpoint_seq": active_seq,
                "diff_chain": diff_chain,
                **fc,
                **dmres,
            })
            print(
                f"[ckpt {seq}] node={node_id} parent={parent} pre_rollback={rollback_needed_pre} "
                f"cursor={(s.get('mock_stats') or {}).get('cursor')} fc={fc['fc_total_ms']:.1f}ms dm={dmres['dm_snapshot_ms']:.1f}ms",
                flush=True,
            )
            active_seq = seq
            prev_node = node_id

            if step.get("finished"):
                break

        out = {
            "ok": True,
            "instance": instance,
            "vm_config": {"mem_mib": mem_mib, "vcpus": vcpus},
            "dm_config": {"data_size": data_size, "meta_size": "256M"},
            "snapshot_mode": snapshot_mode,
            "conditions": conditions,
            "ckpts": ckpts,
            "restore_events": restore_events,
        }
        results.mkdir(parents=True, exist_ok=True)
        (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    finally:
        primary = sys.exc_info()[1]
        if memory_job:
            # Record allocation before teardown even if a snapshot write failed.
            try:
                check_capacity(WORK_BASE, 'before-cleanup', 0, capacity_log,
                               node=memory_job['node'], reserve=0)
            except Exception as diagnostic_error:
                print(f'Capacity evidence: {diagnostic_error}', file=sys.stderr)
        try:
            if vm is not None:
                vm.kill()
        finally:
            try:
                if mock_proc is not None:
                    flush_audit(f'http://{HOST_IP}:{mock_port}', results / 'mock_audit.json',
                                primary_error=primary or sys.exc_info()[1])
            finally:
                try:
                    stop_host_mock(mock_proc)
                finally:
                    dm.teardown()
                    run(["ip", "rule", "del", "to", f"{GUEST_IP}/32", "priority", "100", "lookup", "main"], check=False)
                    run(["ip", "route", "del", f"{GUEST_IP}/32"], check=False)
                    run(["ip", "link", "del", TAP], check=False)
                    if cleanup_large_artifacts:
                        shutil.rmtree(snaps, ignore_errors=True)
                        shutil.rmtree(dm_work, ignore_errors=True)
                        try:
                            image.unlink()
                        except OSError:
                            pass


def main() -> int:
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("benchmark interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="pytest-dev__pytest-8365")
    ap.add_argument("--max-steps", type=int, default=3)
    ap.add_argument("--mem-mib", type=int, default=8192)
    ap.add_argument("--vcpus", type=int, default=4)
    ap.add_argument("--data-size", default="24G")
    ap.add_argument("--snapshot-mode", choices=["diff", "full"], default="diff")
    ap.add_argument("--run-id-prefix", default="ctrl")
    ap.add_argument("--cleanup-large-artifacts", action="store_true")
    ap.add_argument("--mock-port", type=int, default=22999)
    args = ap.parse_args()
    out = run_controller_pilot(
        args.instance,
        max_steps=args.max_steps,
        mem_mib=args.mem_mib,
        vcpus=args.vcpus,
        data_size=args.data_size,
        snapshot_mode=args.snapshot_mode,
        run_id_prefix=args.run_id_prefix,
        cleanup_large_artifacts=args.cleanup_large_artifacts,
        mock_port=args.mock_port,
    )
    print(json.dumps({
        "ok": out["ok"],
        "n_ckpts": len(out["ckpts"]),
        "n_restore_events": len(out["restore_events"]),
        "restore_events": out["restore_events"],
    }, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
