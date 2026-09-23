"""Detached exact-page incremental dumps with bounded, owned lifetimes.

The warm checkpoint is published before its disposable CRIU writer finishes.
Only the writer waits for physical ancestors. No soft-dirty/starttime inference
is used to reuse another task's pages; the pinned CRIU compares page bytes.
"""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeout, wait
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import threading
import time
import uuid

try:
    from .criu_page_stats import collect_page_stats
except ImportError:
    from criu_page_stats import collect_page_stats

PROTOCOL = "exact-parent-v1"


def open_pidfd(pid):
    try:
        from .process_wait import _pidfd_opener
    except ImportError:
        from process_wait import _pidfd_opener
    opener = _pidfd_opener()
    if opener is None:
        raise RuntimeError("detached dumps require Linux pidfd support")
    return opener(pid)


def kill_pidfd(fd):
    return send_pidfd_signal(fd, signal.SIGKILL)


def send_pidfd_signal(fd, sig):
    native = getattr(signal, "pidfd_send_signal", None)
    if native is not None:
        return native(fd, sig)
    if os.uname().machine not in ("x86_64", "aarch64"):
        raise RuntimeError("unsupported pidfd_send_signal syscall ABI")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(ctypes.c_long(424), ctypes.c_int(fd), ctypes.c_int(sig),
                    ctypes.c_void_p(), ctypes.c_uint(0)) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))



def retained_ids(registry, requested):
    """Keep both ancestry graphs and every pending writer's dependency closure."""
    keep = set(requested)
    keep.update(k for k, v in registry.items()
                if v.get("dump_future") is not None and not v["dump_future"].done())
    pending = list(keep)
    while pending:
        entry = registry.get(pending.pop(), {})
        for key in ("parent_id", "prev_ckpt_id", "effective_restore_id"):
            parent = entry.get(key)
            if parent in registry and parent not in keep:
                keep.add(parent)
                pending.append(parent)
    return keep


class AsyncIncrementalCheckpoint:
    def __init__(self, controller):
        self.controller = controller
        # Load resource inspection dependencies before the measured loop.
        try:
            from . import async_resources
        except ImportError:
            import async_resources
        if (not controller.enable_warm_template or controller.template_pool is None
                or controller.fixed_active_pid is not None
                or controller.prefork_template_dump or controller.async_template_full_dump):
            raise ValueError("async-incremental requires warm templates, dynamic active PID and no legacy dump modes")
        if (not controller.checkpoint_stash_template
                or os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1"):
            raise ValueError("async-incremental requires stash checkpoints and a non-init active task")
        if os.environ.get("DELTABOX_RESTAMP_PARENT_INVENTORY", "0") != "0":
            raise ValueError("async-incremental prohibits parent inventory restamping")
        binary = shutil.which(controller.criu_dump_bin)
        if binary is None:
            raise FileNotFoundError(controller.criu_dump_bin)
        capabilities = subprocess.check_output(
            [binary, "--version"], env={**os.environ, "DELTABOX_CRIU_CAPABILITIES": "1"},
            text=True, timeout=10)
        if PROTOCOL not in capabilities:
            raise RuntimeError("async-incremental requires the pinned CRIU exact-parent-v1 capability")
        if controller.enable_criu_lazy_restore:
            if "exact-parent-lazy-v1" not in capabilities:
                raise RuntimeError("async-incremental lazy restore requires exact-parent-lazy-v1")
            restore_binary = shutil.which(controller.criu_restore_bin)
            if restore_binary is None:
                raise FileNotFoundError(controller.criu_restore_bin)
            restore_caps = capabilities if restore_binary == binary else subprocess.check_output(
                [restore_binary, "--version"],
                env={**os.environ, "DELTABOX_CRIU_CAPABILITIES": "1"}, text=True, timeout=10)
            if "exact-parent-lazy-v1" not in restore_caps:
                raise RuntimeError("lazy parent-page reader requires exact-parent-lazy-v1 restore binary")
        self.binary_identity = {"path": binary, "sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
                                "capabilities": capabilities.strip()}
        maximum = int(os.environ.get("DELTABOX_ASYNC_MAX_PENDING", "4"))
        if maximum < 1 or maximum > 64:
            raise ValueError("DELTABOX_ASYNC_MAX_PENDING must be between 1 and 64")
        self.slots = threading.BoundedSemaphore(maximum)
        self.max_pending = maximum
        self.failed = None
        self.timeout = float(os.environ.get("DELTABOX_ASYNC_DUMP_TIMEOUT", "300"))
        if self.timeout <= 0:
            raise ValueError("invalid async dump timeout")

    def _parent(self, parent_id):
        c = self.controller
        if not parent_id:
            return None
        if parent_id not in c.registry:
            raise ValueError(f"unknown checkpoint parent: {parent_id}")
        entry = c.registry[parent_id]
        effective = entry.get("effective_restore_id", parent_id)
        entry = c.registry[effective]
        if entry.get("memory_protocol") != PROTOCOL:
            raise ValueError("cannot attach exact-parent dump to a different checkpoint protocol")
        return entry

    def _sink(self, checkpoint_id, parent):
        c = self.controller
        layers = list(parent["layers"] if parent else [c.base_layer])
        if not os.path.isdir(c.current_upper):
            raise FileNotFoundError(c.current_upper)
        def propagate(error):
            raise error
        dirty = any(dirs or files for _, dirs, files in os.walk(c.current_upper, onerror=propagate))
        started = time.perf_counter()
        switch_ms = 0.0
        if dirty:
            frozen = os.path.join(c.layers_root, f"layer_{checkpoint_id}")
            upper = os.path.join(c.layers_root, f"upper_{checkpoint_id}_next")
            work = os.path.join(c.layers_root, f"work_{checkpoint_id}_next")
            os.makedirs(upper)
            os.makedirs(work)
            os.rename(c.current_upper, frozen)
            switch_started = time.perf_counter()
            c._apply_overlay_switch(layers, upper, work, kind="ckpt")
            switch_ms = (time.perf_counter() - switch_started) * 1000
            c.current_upper, c.current_work = upper, work
            layers = [frozen, *layers]
        if c.root_overlays is not None:
            c.root_overlays.checkpoint(checkpoint_id)
        preparation_ms = (time.perf_counter() - started) * 1000 - switch_ms
        return layers, dirty, switch_ms, preparation_ms

    def checkpoint(self, parent_id, tag, raw_command="", replay_worker_ops=None):
        c = self.controller
        started = time.perf_counter()
        if self.failed:
            raise RuntimeError(f"async checkpoint transaction previously failed: {self.failed}")
        if raw_command in ("lightweight", "predump"):
            raise ValueError("async-incremental currently requires standard checkpoints")
        parent = self._parent(parent_id)
        # Admission is bounded and its wait is part of the actual checkpoint API.
        if not self.slots.acquire(timeout=self.timeout):
            raise TimeoutError("async checkpoint admission timed out")
        admission_ms = (time.perf_counter() - started) * 1000
        checkpoint_id = uuid.uuid4().hex[:12]
        final = Path(c.snapshot_store) / f"{tag}_{checkpoint_id}_mem"
        staging = final.with_name(final.name + ".pending")
        warm = dump_pid = dump_pidfd = None
        future = None
        published = False
        try:
            # Implementations of this contract validate the quiescent worker and
            # prepare a dump-private file view; unsupported resources fail here.
            try:
                from .async_resources import validate_replay_resources, dump_child_contract
            except ImportError:
                from async_resources import validate_replay_resources, dump_child_contract
            if c.root_overlays is not None:
                raise RuntimeError("async-incremental does not yet support multiple root overlays")
            protected = [e["async_dump_template_pid"] for e in c.registry.values()
                         if e.get("async_dump_template_pid") and e.get("dump_future") is not None
                         and not e["dump_future"].done()]
            resources = validate_replay_resources(c.agent_pid,
                overlay_mount_point=c.overlay_mount_point, allowed_child_pids=protected)
            layers, dirty, overlay_ms, overlay_preparation_ms = self._sink(checkpoint_id, parent)
            view = {"contract": dump_child_contract(resources), "lower_layers": layers, "workspace": None}
            warm, fork_ms, attempted = c._bootstrap_active_before_dump(checkpoint_id)
            if attempted and warm is None:
                raise RuntimeError("failed to move active off PID namespace init")
            if c._is_pidns_init(c.agent_pid):
                raise RuntimeError("asynchronous writers cannot be children of an active PID namespace init")
            if warm is None:
                before = time.perf_counter()
                warm = c.template_pool.request_stash_template(
                    c.agent_pid, checkpoint_id, timeout=2, fresh_pidns=False)
                fork_ms = (time.perf_counter() - before) * 1000
            if warm is None or not c._wait_pid_stopped(warm):
                raise RuntimeError("warm template failed to stop")
            before = time.perf_counter()
            dump_pid = c.template_pool.request_stash_template(
                c.agent_pid, None, timeout=10, fresh_pidns=True,
                async_resources=view)
            fork_ms += (time.perf_counter() - before) * 1000
            if dump_pid is None or not c._wait_pid_stopped(dump_pid):
                raise RuntimeError("disposable dump task failed to stop")
            if not c._is_pidns_init(dump_pid):
                raise RuntimeError("exact-parent writer must have virtual PID 1")
            dump_pidfd = open_pidfd(dump_pid)
            staging.mkdir()
            future = Future()
            stats = {}
            entry = dict(
                id=checkpoint_id, mem_path=str(final), parent_id=parent_id,
                prev_ckpt_id=parent["id"] if parent else None,
                layers=layers, strategy="standard", memory_protocol=PROTOCOL,
                state="WARM_READY", template_pid=warm,
                async_dump_template_pid=dump_pid, async_warm_template_pid=warm,
                dump_tree_pid=dump_pid, ns_init_pid=c.ns_init_pid,
                external_pidns=False, fixed_active_pid=None,
                dump_future=future, dump_stats=stats, dump_error=None,
                dispose_done=False, cleanup_error=None,
                dump_started_at=time.time(), dump_completed_ms=None,
                checkpoint_started_mono=started,
                criu_ms=0.0, predump_ms=0.0, fork_ms=fork_ms, overlay_ms=overlay_ms,
                overlay_preparation_ms=overlay_preparation_ms,
                dump_size_bytes=0, upper_dirty=dirty, action=None,
                pre_template_dump_join_ms=0.0, pending_dump_join_ms=0.0,
                validation_production_join_ms=0.0, validation_needed=False,
                async_admission_wait_ms=admission_ms, max_pending_dumps=self.max_pending,
                prev_cross_pid=parent is not None,
                prev_pid_mode="exact-page-content" if parent else "full-seed",
                resources=resources, criu_binary=self.binary_identity)
            c.registry[checkpoint_id] = entry
            # Worker owns its PID handle and slot through process disposal.
            owned_submit = getattr(c._dump_pool, "submit_owned_task", None)
            if owned_submit is not None:
                owned_submit(self._run, entry, parent, staging, final, dump_pidfd)
            else:
                c._dump_pool.submit(self._run, subprocess.check_call,
                                    entry, parent, staging, final, dump_pidfd)
            published = True
            entry["checkpoint_returned_mono"] = time.perf_counter()
            entry["dump_pending_at_return"] = not future.done()
            entry["ckpt_wall_ms"] = (time.perf_counter() - started) * 1000
            entry["checkpoint_sync_no_dump_ms"] = entry["ckpt_wall_ms"]
            return entry
        except BaseException as error:
            self.failed = f"{type(error).__name__}: {error}"
            if future is not None and not future.done():
                future.set_exception(error)
            c.registry.pop(checkpoint_id, None)
            if warm is not None and warm != c.ns_init_pid:
                c.template_pool.discard(checkpoint_id)
            if dump_pid is not None:
                try:
                    self._dispose(dump_pid, dump_pidfd)
                except BaseException as disposal_error:
                    self.failed += f"; cleanup: {disposal_error}"
                finally:
                    dump_pidfd = None
            if staging.exists():
                shutil.rmtree(staging)
            raise
        finally:
            if not published:
                if dump_pidfd is not None:
                    os.close(dump_pidfd)
                self.slots.release()

    @staticmethod
    def _dispose(pid, pidfd):
        if pidfd is None:
            # Only used immediately after a failed creation handshake. The
            # controller has not released the command gate to another operation.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                kill_pidfd(pidfd)
                if not select.select([pidfd], [], [], 2.0)[0]:
                    raise TimeoutError("dump task did not exit after SIGKILL")
            except ProcessLookupError:
                pass
            finally:
                os.close(pidfd)

    def _run(self, run_command, entry, parent, staging, final, pidfd):
        c = self.controller
        started = time.perf_counter()
        future = entry["dump_future"]
        error = None
        try:
            if parent is not None:
                parent["dump_future"].result(timeout=self.timeout)
                if parent.get("state") != "DURABLE_READY":
                    raise RuntimeError("physical parent did not commit")
            entry["state"] = "DUMPING"
            command = [c.criu_dump_bin, "dump", "--tree", str(entry["dump_tree_pid"]),
                       "-D", str(staging), "--shell-job", "--leave-stopped", "--tcp-close",
                       "--ext-unix-sk", "--manage-cgroups", "--link-remap",
                       *c._all_ext_mount_map_args(), "-o", "dump.log"]
            if parent is not None:
                command += ["--prev-images-dir", os.path.relpath(parent["mem_path"], staging)]
            env = {**os.environ, "DELTABOX_CRIU_EXACT_PARENT": "1"}
            before = time.perf_counter()
            entry["dump_started_mono"] = before
            run_command(command, timeout=self.timeout, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            dump_ms = (time.perf_counter() - before) * 1000
            page_stats = collect_page_stats(staging)
            # CRIU has closed its image streams. Cold readers only see the
            # final directory after the complete image set is atomically moved.
            os.replace(staging, final)
            size = c._image_dir_size(str(final))
            entry["dump_stats"].update(criu_ms=dump_ms,
                dump_completed_ms=(time.perf_counter() - started) * 1000,
                dump_size_bytes=size, memory_protocol=PROTOCOL,
                parent_id=entry["prev_ckpt_id"], page_stats=page_stats)
            entry.update(entry["dump_stats"])
            entry["dump_completed_mono"] = time.perf_counter()
            entry["state"] = "DURABLE_READY"
        except BaseException as exc:
            error = exc
            entry["state"] = "DURABLE_FAILED"
            entry["dump_error"] = f"{type(exc).__name__}: {exc}"
            entry["dump_stats"]["dump_error"] = entry["dump_error"]
        finally:
            try:
                self._dispose(entry["dump_tree_pid"], pidfd)
                entry["dispose_done"] = True
            except BaseException as dispose_error:
                entry["dispose_done"] = False
                entry["cleanup_error"] = f"{type(dispose_error).__name__}: {dispose_error}"
                entry["dump_stats"]["cleanup_error"] = entry["cleanup_error"]
                entry["state"] = "DURABLE_FAILED"
                if error is None:
                    error = dispose_error
                    entry["dump_error"] = f"disposal: {dispose_error}"
                    entry["dump_stats"]["dump_error"] = entry["dump_error"]
            self.slots.release()
            if error is None:
                future.set_result(None)
            else:
                future.set_exception(error)

    def drain_before_namespace_teardown(self):
        # A cold restore destroys an ancestor namespace of all its dump copies.
        # Warm restore does not take this path and never waits for these copies.
        entries = [v for v in list(self.controller.registry.values())
                   if v.get("memory_protocol") == PROTOCOL and v.get("dump_future") is not None]
        for entry in entries:
            future = entry["dump_future"]
            # A failed image can still have a fully disposed writer. It must
            # not prohibit restoring another successful branch. The target
            # image has its own availability check in restore_action. Waiting
            # for completion without retrieving the result separates image
            # errors from this lifetime barrier and preserves caller interrupts.
            if not future.done() and not wait((future,), timeout=self.timeout).done:
                raise FutureTimeout(f"dump {entry['id']} has not finished disposal")
            if entry.get("dispose_done") is not True:
                reason = entry.get("cleanup_error") or "writer exit was not confirmed"
                raise RuntimeError(
                    f"Cannot tear down namespace: dump {entry['id']} cleanup incomplete: {reason}")
