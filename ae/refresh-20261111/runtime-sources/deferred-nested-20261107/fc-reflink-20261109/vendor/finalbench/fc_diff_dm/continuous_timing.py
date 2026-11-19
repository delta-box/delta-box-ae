"""Continuous FC restore timing; cache preparation belongs to the caller."""
import time
from pathlib import Path


def restore_operation(vm, dm, target, base_mem, snaps, snapshot_mode, before_seq,
                      merge_mem, wait_state, cleanup_large_artifacts,
                      clock_ns=time.perf_counter_ns):
    intervals = {}

    def stage(name, fn):
        begin = clock_ns()
        value = fn()
        end = clock_ns()
        intervals[name] = {"start_ns": begin, "end_ns": end,
                           "ms": (end - begin) / 1e6}
        return value

    start = clock_ns()
    stage("vm_kill", vm.kill)
    merged_mem = snaps / f"merged_pre_seq{before_seq}_target{target['seq']}.mem"
    if snapshot_mode == "diff" and target["diff_chain"]:
        merge = stage("merge", lambda: merge_mem(
            base_mem, [Path(p) for p in target["diff_chain"]], merged_mem))
        restore_mem = merged_mem if target["diff_chain"] else Path(target["mem"])
    else:
        merge = {"merge_ms": 0.0, "base_copy_ms": 0.0, "diff_apply_ms": 0.0,
                 "n_diffs": 0, "total_dirty_bytes": 0, "snapshot_mode": snapshot_mode, "method": "direct-base-no-diffs"}
        restore_mem = Path(target["mem"])
    dm_restore = stage("dm_restore", lambda: dm.restore_dev(f"seq{target['seq']}"))
    load = stage("load", lambda: vm.load_snapshot(
        snapshot_path=Path(target["vmstate"]), mem_path=restore_mem,
        enable_diff=(snapshot_mode == "diff")))
    restored = stage("first_rpc", lambda: wait_state(timeout_s=120.0))
    ready_end = intervals["first_rpc"]["end_ns"]
    if not restored.get("ok"):
        raise RuntimeError(f"restored guest is not ready: {restored}")

    def finish():
        if cleanup_large_artifacts and snapshot_mode == "diff" and target["diff_chain"]:
            # FC owns its open mapping; no future restore uses this reconstruction.
            merged_mem.unlink()
    stage("post_ready", finish)
    end = clock_ns()
    total = (end - start) / 1e6
    return {
        "merge": merge, "dm_restore": dm_restore, "load": load,
        "restored_state": restored,
        "vm_kill_ms": intervals["vm_kill"]["ms"],
        "fc_spawn_ms": load.get("fc_spawn_ms"),
        "first_rpc_ms": intervals["first_rpc"]["ms"],
        "restore_ready_ms": (ready_end - start) / 1e6,
        "restore_total_ms": total,
        "post_ready_ms": (end - ready_end) / 1e6,
        "restore_start_ns": start, "restore_ready_end_ns": ready_end,
        "restore_end_ns": end, "continuous_intervals": intervals,
        "unattributed_ms": total - sum(v["ms"] for v in intervals.values()),
        "timing_method": "continuous-perf-counter-ns",
    }
