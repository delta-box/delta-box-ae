"""Figure 6's explicit no-CRIU memory-policy adapter.

Only this experiment imports the subclass.
The checkpoint override removes durable dump and is guarded against changes to
the core method. Restore delegates to the current core with cold fallback
explicitly prohibited, so exit barriers and other restore fixes cannot drift.
This adapter does not represent a durable checkpoint.
"""
from __future__ import annotations
import ast
import hashlib
import inspect
import os
import shutil
import signal
import textwrap
import time
import uuid
from typing import Optional, List
import sandbox_controller as core
from sandbox_controller import SandboxController, _read_rss_mb, InstructionSemanticParser, classify_vmas

_EXPECTED_CHECKPOINT_ACTION = '78c508d8d660148485d0ef7dbade155ce6046cf160cb9240223c1600c58090ca'


def verify_runtime_compatibility() -> None:
    """Fail before acquiring guest resources if the remaining copy drifted."""
    name = "checkpoint_action"
    source = textwrap.dedent(inspect.getsource(getattr(core.SandboxController, name)))
    node = ast.parse(source).body[0]
    actual = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    if actual != _EXPECTED_CHECKPOINT_ACTION:
        raise RuntimeError(
            f"Current runtime method changed: review Figure 6 adapter {name}; "
            f"expected={_EXPECTED_CHECKPOINT_ACTION}, actual={actual}")
    parameter = inspect.signature(core.SandboxController.restore_action).parameters.get(
        "allow_cold_fallback")
    if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise RuntimeError("Figure 6 requires core restore's explicit allow_cold_fallback policy")


verify_runtime_compatibility()


class ForkOnlyController(SandboxController):
    def __init__(self, *args, **kwargs):
        if os.environ.get("DELTABOX_ASYNC_INCREMENTAL_DUMP") == "1":
            raise ValueError("Figure 6 fork-only policy cannot use async-incremental dumps")
        super().__init__(*args, **kwargs)
        if not self.enable_warm_template or self.template_pool is None:
            raise ValueError("Figure 6 needs a real warm template pool")
        self.async_template_full_dump = False
        self.prefork_template_dump = False
        self.enable_incremental_dump = False
        self.restore_fastfork_dump_pid = False

    def _need_fixed_dump_clone(self, strategy):
        return False

    def checkpoint_action(self, parent_ckpt_id: str, tag: str,
                          raw_command: str = "",
                          replay_worker_ops: Optional[List[dict]] = None) -> dict:
        checkpoint_wall_t0 = time.time()
        if not self.async_template_full_dump:
            self._drain_pending_template_cleanup()
        pending_dump_join_ms = 0.0
        if self.prefork_template_dump and parent_ckpt_id:
            pending = self.registry.get(parent_ckpt_id, {}).get("dump_future")
            if pending is not None and not pending.done():
                t_pending_0 = time.time()
                pending.result(timeout=30.0)
                pending_dump_join_ms = (time.time() - t_pending_0) * 1000
                print(f"[PERF] checkpoint-pending-dump-join="
                      f"{pending_dump_join_ms:.2f}ms parent={parent_ckpt_id}")
        new_id = str(uuid.uuid4())[:8]
        curr_dir = os.path.join(self.snapshot_store, f"{tag}_{new_id}_mem")
        os.makedirs(curr_dir, exist_ok=True)

        # ── Strategy selection ──
        # Trace replay schedules may pass an explicit strategy tag. Honor that
        # even when semantic adaptive parsing is disabled (e.g. the no-adapt
        # Table 2 backend), otherwise LW schedules silently collapse to all-
        # standard checkpoints.
        strategy = "standard"
        if raw_command in ("lightweight", "standard", "predump"):
            strategy = raw_command
        elif self.enable_adaptive and raw_command:
            strategy = InstructionSemanticParser.parse_strategy(raw_command)
            # Runtime hot-region detection: upgrade standard → predump
            if (strategy == "standard"
                    and os.environ.get("DELTABOX_DISABLE_HOT_PREDUMP") != "1"):
                try:
                    result = classify_vmas(self.agent_pid)
                    if result["hot"]:
                        strategy = "predump"
                except Exception:
                    pass
            print(f"[ADAPTIVE] Command='{raw_command}' → Strategy='{strategy}'")

        print(f"[Controller] Checkpointing {new_id} (strategy={strategy})...", flush=True)

        predump_ms = 0.0
        template_pid = None
        fork_ms = 0.0
        bootstrap_attempted_before_dump = False
        if strategy != "lightweight":
            template_pid, fork_ms, bootstrap_attempted_before_dump = \
                self._bootstrap_active_before_dump(new_id)
        dump_tree_pid = self._current_dump_tree_pid()

        if strategy == "predump":
            raise ValueError("Figure 6 fork-only policy cannot pre-dump")

        # Phase 1: CRIU dump (skip for lightweight file-only ops)
        criu_time = 0.0
        dump_future = None
        dump_started_at = None
        dump_completed_ms = None
        dump_size_bytes = 0
        dump_stats = {}
        validation_full_twin = None
        validation_pre_live_mem = None
        validation_post_live_mem = None
        validation_production_join_ms = 0.0
        prev_ckpt_id = None
        prev_rel_parent = None
        parent_mem_path = None
        prev_cross_pid = False
        prev_pid_mode = None
        validation_needed = False
        pre_dump_template_pid = None
        pre_dump_fork_ms = 0.0
        pre_template_dump_join_ms = 0.0
        async_template_dump_pid = None
        async_template_warm_pid = None
        fixed_dump_clone_pid = None
        fixed_dump_clone_ms = 0.0
        if strategy == "lightweight":
            with open(os.path.join(curr_dir, "lightweight_marker.txt"), "w") as stream:
                stream.write(f"parent={parent_ckpt_id}\n")
        else:
            with open(os.path.join(curr_dir, "fork_only_marker.txt"), "w") as stream:
                stream.write("Figure 6: live template only; no durable CRIU image\n")
        dump_stats = {"fork_only": True, "criu_ms": 0.0, "dump_size_bytes": 0}

        # Phase 2: OverlayFS layer sink (always, regardless of strategy)
        if parent_ckpt_id and parent_ckpt_id in self.registry:
            parent_layers = self.registry[parent_ckpt_id]['layers']
        else:
            parent_layers = [self.base_layer]

        is_upper_dirty = False
        try:
            # Check if upper directory tree has any files/directories
            for root, dirs, files in os.walk(self.current_upper):
                if dirs or files:
                    is_upper_dirty = True
                    break
        except OSError as e:
            print(f"[FS Warning] Failed to check upper dir {self.current_upper}: {e}")
            is_upper_dirty = True  # Assume dirty on error to be safe

        new_layers_list = []
        ovl_time = 0.0

        if is_upper_dirty:
            print(f"[FS] Upper is dirty. Sinking to new layer.")
            sunk_layer_path = os.path.join(self.layers_root, f"layer_{new_id}")
            os.rename(self.current_upper, sunk_layer_path)
            next_upper = os.path.join(self.layers_root, f"upper_{new_id}_next")
            next_work = os.path.join(self.layers_root, f"work_{new_id}_next")
            os.makedirs(next_upper, exist_ok=True)
            os.makedirs(next_work, exist_ok=True)
            new_layers_list = [sunk_layer_path] + parent_layers
            # New overlayfs checkpoint ioctl auto-injects the old upper as
            # lower[0]. Keep it in the registry for future restores, but do
            # not pass it in lowerdir here or the kernel sees an alias.
            ioctl_layers = parent_layers
            t2 = time.time()
            self._apply_overlay_switch(ioctl_layers, next_upper, next_work, kind="ckpt")
            t3 = time.time()
            ovl_time = (t3 - t2) * 1000
            print(f"[PERF] Checkpoint ID {new_id}: OverlayFS={ovl_time:.2f}ms")
            self.current_upper = next_upper
            self.current_work = next_work

        else:
            print(f"[FS] Upper is clean. Skipping sink (Optimization Triggered).")

            new_layers_list = parent_layers

        # Root-coverage overlays checkpoint in lock-step with /testbed so a later
        # restore to this id rolls back system-root changes (apt installs, etc.).
        if self.root_overlays is not None:
            self.root_overlays.checkpoint(new_id)

        # Phase 3: checkpoint-side warm template (if enabled). Once the active
        # agent has moved off pid-ns init, the default stashes a detached
        # template for this snapshot while keeping active PID stable, preserving
        # CRIU's incremental dump chain. The first checkpoint after boot/slow
        # restore does that fork-replace before the dump, so the first durable
        # image already belongs to the stable active worker PID.
        # DELTABOX_CHECKPOINT_STASH_TEMPLATE=0 keeps the old always-fork mode
        # for debugging only.
        # NOTE: warm-template fork requires the agent to be runnable to read
        # CTRL_IN_FIFO and call fork(). The async CRIU dump holds it
        # SIGSTOPped via --leave-running's pre-thaw window, so we must join
        # the dump future here before issuing the fork request. This collapses
        # the parallel window for warm-template runs — by design (warm-template
        # is off-by-default for MCTS per project memory; no checkpoint-LLM
        # overlap to be had on the BoN path that uses it).
        if (self.enable_warm_template
                and strategy != "lightweight"
                and not bootstrap_attempted_before_dump
                and not pre_dump_template_pid
                and not async_template_dump_pid):
            skip_warm_template = False
            if skip_warm_template:
                template_pid = None
                fork_ms = 0.0
            else:
                if fixed_dump_clone_pid is not None:
                    self._dispose_fixed_dump_clone(
                        fixed_dump_clone_pid, new_id)
                    fixed_dump_clone_pid = None
            # Re-SIGSTOP every known template before issuing the fork command.
            # CRIU dump with --leave-running may thaw previously SIGSTOPped
            # templates, leaving multiple runnable readers on CTRL_IN_FIFO.
            # When that happens, a template (instead of the current active)
            # wins the "fork" race, forks from stale memory, and the old
            # active becomes an orphan that keeps reading NPD_NOTIFY_FIFO and
            # stealing notify bytes destined for the new active. See
            # template_fork.py L21-37 for the "≤1 runnable reader" invariant.
                for tpid in list(self.template_pool.templates.values()):
                    if tpid == self.agent_pid:
                        continue
                    try:
                        os.kill(tpid, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass

                rss_mb = _read_rss_mb(self.agent_pid)
                print(f"[PERF] Agent RSS at fork: {rss_mb:.1f} MB "
                      f"(pid={self.agent_pid})")
                t_f0 = time.time()
                if self.checkpoint_stash_template:
                    template_pid = self.template_pool.request_stash_template(
                        self.agent_pid, new_id, timeout=2.0,
                        setsid_template=self.fixed_active_pid is not None,
                        clear_soft_dirty_template=(
                            self.fixed_active_pid is not None))
                    fork_ms = (time.time() - t_f0) * 1000
                    if template_pid is not None:
                        print(f"[PERF] Warm-template stash={fork_ms:.2f}ms "
                              f"(template={template_pid}, active={self.agent_pid}, "
                              f"rss={rss_mb:.1f}MB)")
                    else:
                        print(f"[Controller] Warm-template stash timed out; "
                              f"snapshot {new_id} will use slow restore path")
                else:
                    parent_pid, child_pid = self.template_pool.request_fork(
                        self.agent_pid, new_id, timeout=2.0)
                    fork_ms = (time.time() - t_f0) * 1000
                    if parent_pid is not None and child_pid is not None:
                        template_pid = parent_pid
                        self.agent_pid = child_pid
                        self._clear_soft_dirty_after_fork(child_pid)
                        print(f"[PERF] Warm-template fork={fork_ms:.2f}ms "
                              f"(template={template_pid}, active={child_pid}, "
                              f"rss={rss_mb:.1f}MB)")
                    else:
                        print(f"[Controller] Warm-template fork timed out; "
                              f"snapshot {new_id} will use slow restore path")

        # If checkpoint-side template stash joined the async dump above, the
        # completion callback has already populated dump_stats. Report those
        # completed values instead of the initial placeholders.
        reported_criu_ms = dump_stats.get("criu_ms", criu_time)
        reported_dump_completed_ms = dump_stats.get(
            "dump_completed_ms", dump_completed_ms)
        reported_dump_size_bytes = dump_stats.get(
            "dump_size_bytes", dump_size_bytes)
        reported_dump_error = dump_stats.get("dump_error")

        if strategy != "lightweight" and template_pid is None:
            raise RuntimeError("Figure 6 checkpoint failed to create a live template")
        info = {
            "fork_only": True,
            "id": new_id,
            "mem_path": curr_dir,
            "parent_id": parent_ckpt_id,
            "layers": new_layers_list,
            "strategy": strategy,
            "criu_ms": reported_criu_ms,
            "predump_ms": predump_ms,
            "template_pid": template_pid,
            "async_dump_template_pid": async_template_dump_pid,
            "async_warm_template_pid": async_template_warm_pid,
            "fixed_dump_clone_ms": fixed_dump_clone_ms,
            "restore_fastfork_dump_pid": self.restore_fastfork_dump_pid,
            "fork_ms": fork_ms,
            "dump_future": dump_future,  # None for lightweight; Future otherwise
            "dump_started_at": dump_started_at,
            "dump_completed_ms": reported_dump_completed_ms,
            "dump_size_bytes": reported_dump_size_bytes,
            "dump_stats": dump_stats,
            "dump_error": reported_dump_error,
            "overlay_ms": ovl_time,
            "pre_template_dump_join_ms": pre_template_dump_join_ms,
            "validation_production_join_ms": validation_production_join_ms,
            "pending_dump_join_ms": pending_dump_join_ms,
            "ckpt_wall_ms": (time.time() - checkpoint_wall_t0) * 1000,
            "checkpoint_sync_no_dump_ms": (
                fork_ms + ovl_time + pre_template_dump_join_ms * 0.0
            ),
            "upper_dirty": is_upper_dirty,
            "dump_tree_pid": dump_tree_pid,
            "ns_init_pid": self.ns_init_pid,
            "external_pidns": self._is_pidns_init(dump_tree_pid) is False,
            "fixed_active_pid": self.fixed_active_pid,
            "validation_needed": validation_needed,
            "validation_full_id": (
                validation_full_twin.get("id") if validation_full_twin else None),
            "validation_full_mem_path": (
                validation_full_twin.get("mem_path") if validation_full_twin else None),
            "validation_pre_live_mem": validation_pre_live_mem,
            "validation_post_live_mem": validation_post_live_mem,
            "prev_ckpt_id": prev_ckpt_id,
            "prev_cross_pid": prev_cross_pid,
            "prev_pid_mode": prev_pid_mode,
            "action": None,  # Transition record attached by runner post-decision.
        }
        # For lightweight checkpoints, record the effective restore target
        if strategy == "lightweight":
            parent = self.registry.get(parent_ckpt_id, {})
            info["effective_restore_id"] = parent.get("effective_restore_id", parent_ckpt_id)
            replay_item = {
                "action": tag,
                "command": raw_command,
                "worker_ops": replay_worker_ops or [],
            }
            info["replay_cmds"] = parent.get("replay_cmds", []) + [replay_item]

        self.registry[new_id] = info
        if validation_full_twin is not None:
            full_id = validation_full_twin["id"]
            self.registry[full_id] = {
                "id": full_id,
                "mem_path": validation_full_twin["mem_path"],
                "parent_id": None,
                "layers": new_layers_list,
                "strategy": "validation_full_twin",
                "criu_ms": validation_full_twin["criu_ms"],
                "predump_ms": 0.0,
                "template_pid": None,
                "fork_ms": 0.0,
                "dump_future": None,
                "dump_started_at": None,
                "dump_completed_ms": validation_full_twin["criu_ms"],
                "dump_size_bytes": validation_full_twin["dump_size_bytes"],
                "dump_stats": {
                    "criu_ms": validation_full_twin["criu_ms"],
                    "dump_completed_ms": validation_full_twin["criu_ms"],
                    "dump_size_bytes": validation_full_twin["dump_size_bytes"],
                },
                "dump_error": None,
                "overlay_ms": ovl_time,
                "pre_template_dump_join_ms": 0.0,
                "pending_dump_join_ms": 0.0,
                "checkpoint_sync_no_dump_ms": 0.0,
                "upper_dirty": is_upper_dirty,
                "dump_tree_pid": dump_tree_pid,
                "ns_init_pid": self.ns_init_pid,
                "external_pidns": self._is_pidns_init(dump_tree_pid) is False,
                "fixed_active_pid": self.fixed_active_pid,
                "validation_needed": False,
                "validation_full_twin_for": new_id,
                "prev_ckpt_id": None,
                "prev_cross_pid": False,
                "prev_pid_mode": None,
                "action": None,
            }
        if (strategy != "lightweight"
                and not pre_dump_template_pid
                and not async_template_dump_pid):
            self._active_dump_owner_id = new_id
        return info

    def restore_action(self, target_ckpt_id: str):
        # Share the actual warm transaction, including exit/reap barriers,
        # overlapped workspace preparation, and post-bookkeeping prewarm.
        # A missing/dying template must never turn this no-CRIU experiment
        # into a durable restore (there are no images to restore).
        return super().restore_action(target_ckpt_id, allow_cold_fallback=False)

    def gc_obsolete_snapshots(self, keep_n=30, keep_ids=None):
        ids = list(self.registry)
        keep = set(keep_ids) & set(ids) if keep_ids is not None else set(ids[-keep_n:])
        for rid in list(keep):
            parent = self.registry[rid].get("parent_id")
            while parent in self.registry and parent not in keep:
                keep.add(parent)
                parent = self.registry[parent].get("parent_id")
        for rid in set(ids) - keep:
            pid = self.template_pool.templates.get(rid)
            if pid is not None:
                if pid == self.agent_pid or pid == self.ns_init_pid:
                    raise RuntimeError("Refusing to GC active or namespace-init PID")
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 2
                while not self._pid_gone_or_zombie(pid):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Template {pid} did not exit during GC")
                    time.sleep(.002)
            self.template_pool.templates.pop(rid, None)
            entry = self.registry.pop(rid)
            if os.path.isdir(entry["mem_path"]):
                shutil.rmtree(entry["mem_path"])
