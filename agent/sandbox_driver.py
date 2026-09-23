"""sandbox_driver — host-side wrapper around deltabox SandboxController.

Owns lifecycle of:
  1. one shell_server process (PID-1 in PID-ns + Mount-ns, cwd inside an
     overlay mount we set up)
  2. one SandboxController hooked to that process (used for checkpoint /
     restore between MCTS actions)

Hands the agent-driver layer (env / search) a clean API:
  drv = SandboxDriver(workdir, base_image_path=...)
  await drv.start()
  ckpt_root = await drv.checkpoint("root")
  # ... agent issues bash/python commands via FIFOs ...
  ckpt_after_action = await drv.checkpoint("node5", parent=ckpt_root)
  # ... another branch wants to start from ckpt_root again:
  await drv.restore(ckpt_root)
  await drv.shutdown()

Design notes
------------
* LW (lightweight) optimization is **OFF by default** here. Adaptive
  classifier off. Every checkpoint runs the full standard path: real CRIU
  dump + overlayfs sink ioctl. This matches the paper's all-standard event
  semantics outside the §6.X LW ablation.

* On P1 we *don't* run inside a firecracker VM yet. We mount overlayfs on
  the host (sudo required) at WORKDIR/merged using:
     lower = base_image_path (e.g. an extracted base.xfs dir, or just "/"
                              for the fizzbuzz toy — see comments)
     upper = WORKDIR/upper
     work  = WORKDIR/work
  and launch shell_server under namespace_launcher so the process becomes
  PID 1 in a fresh PID + Mount namespace, with /proc remounted inside.

* On P2/P3 the same driver moves into the firecracker VM. The host wrapper
  just changes from "host subprocess" to "ssh into vm + invoke driver"; the
  API surface above does not change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# This driver drives the DeltaBox GSD (SandboxController) directly for the
# host-overlay run. The decoupled VM path routes through
# common.backend.SandboxBackend instead; SandboxController is imported LAZILY in
# _build_controller so this module stays import-self-contained.
REPO_ROOT = Path(__file__).resolve().parents[1]
# Default-arg paths are env-overridable: shell_server.py lives in upper/agent/
# (this dir), namespace_launcher.py in backends/deltabox/gsd/.
GUEST_DIR = Path(os.environ.get(
    "DELTABOX_GUEST_DIR",
    str(REPO_ROOT / "backends" / "deltabox" / "gsd")))
GUEST_HEAVY_DIR = Path(os.environ.get(
    "DELTABOX_GUEST_HEAVY_DIR", str(REPO_ROOT / "upper" / "agent")))


LOG = logging.getLogger("agent.driver")


@dataclass
class CkptRecord:
    ckpt_id: str
    parent_ckpt_id: Optional[str]
    tag: str
    wall_ms: float
    fork_or_criu: str   # "fork" if served via warm-template fork, else "criu"
    raw_command: str = ""
    meta: dict = field(default_factory=dict)


class SandboxDriver:
    """One-instance driver: one shell_server + one SandboxController."""

    def __init__(
        self,
        workdir: Path,
        base_lower: Path,
        shell_server_path: Path = GUEST_HEAVY_DIR / "shell_server.py",
        namespace_launcher_path: Path = GUEST_DIR / "namespace_launcher.py",
        action_timeout_s: float = 60.0,
        enable_warm_template: bool = True,
        # LW (lightweight) classification stays off everywhere this layer
        # touches. Paper's standard event path = full CRIU + overlay sink.
        enable_adaptive: bool = False,
        enable_prewarm: bool = False,
        # The warm profile below enables fixed-PID incremental dumps. A full
        # profile must be explicitly selected by the entry point.
        enable_incremental_dump: bool = False,
        checkpoint_profile: str = "runtime-default",
        sudo_wrap: bool = True,
    ):
        self.workdir = Path(workdir).resolve()
        self.base_lower = Path(base_lower).resolve()
        self.shell_server_path = shell_server_path
        self.namespace_launcher_path = namespace_launcher_path
        self.action_timeout_s = action_timeout_s
        self.enable_warm_template = enable_warm_template
        self.enable_adaptive = enable_adaptive
        self.enable_prewarm = enable_prewarm
        self.enable_incremental_dump = enable_incremental_dump
        self.sudo_wrap = sudo_wrap

        # Optional whole-root rollback coverage. Comma-separated dirs in
        # DELTABOX_ROOT_OVERLAY_DIRS (e.g. "/usr,/etc,/var") make system-root
        # changes (apt/yum installs, etc.) roll back with each checkpoint. Empty
        # (default) leaves behaviour unchanged: only /testbed is covered.
        _rod = os.environ.get("DELTABOX_ROOT_OVERLAY_DIRS", "").strip()
        self._root_overlay_dirs = (
            [d.strip() for d in _rod.split(",") if d.strip()] or None
        )

        # Both supported entry points select the same authoritative protocol.
        # Fixed-PID incremental and historical full dumps remain separate arms.
        if self.enable_warm_template:
            from common.runtime_profile import checkpoint_environment
            os.environ.update(checkpoint_environment(checkpoint_profile))
            self.enable_incremental_dump = checkpoint_profile == "runtime-default"

        # paths laid out under workdir
        self.merged_dir     = self.workdir / "merged"
        self.upper_dir      = self.workdir / "upper"
        self.work_dir       = self.workdir / "work"
        self.layers_root    = self.workdir / "layers"
        self.snapshot_store = self.workdir / "snapshots"
        self.fifo_in_path   = self.workdir / "shell_cmd_in.fifo"
        self.fifo_out_path  = self.workdir / "shell_cmd_out.fifo"
        self.shell_log      = self.workdir / "shell_server.log"
        self.trace_log      = self.workdir / "shell_server_trace.jsonl"
        # namespace_launcher's sentinel writes the *child* host PID here
        # (= PID 1 in the new PID ns, in host-PID terms). SandboxController
        # needs THIS PID, not the launcher subprocess PID.
        self.agent_pid_file = self.workdir / "agent_ns_pid"
        # In fixed-active-pid mode the launcher's sentinel writes the REAPER
        # (PID-1-in-ns) host PID here, which differs from the active worker
        # (PID-100). The controller needs it as the external PID namespace owner
        # for the cold CRIU restore path (template miss); without it cold restore
        # fails with "external pid namespace is unavailable".
        self.ns_init_pid_file = self.workdir / "agent_ns_init_pid"
        self.launcher_log   = self.workdir / "namespace_launcher.log"

        self._shell_proc:    Optional[subprocess.Popen] = None
        self._agent_host_pid: Optional[int] = None
        self._ns_init_host_pid: Optional[int] = None
        self._ns_init_start_time: Optional[str] = None
        self._ctrl:          Optional[SandboxController] = None
        self._overlay_mounted = False
        self._ckpts: dict[str, CkptRecord] = {}
        self._current_ckpt_id: Optional[str] = None

    # ───── lifecycle ───────────────────────────────────────────────────
    async def start(self) -> None:
        """Set up overlay mount, launch shell_server in PID-ns, attach controller."""
        for d in (self.workdir, self.upper_dir, self.work_dir,
                  self.layers_root, self.snapshot_store):
            d.mkdir(parents=True, exist_ok=True)
        # merged dir must exist before mount
        self.merged_dir.mkdir(parents=True, exist_ok=True)

        await self._mount_overlay()
        await self._launch_shell_server()
        await self._build_controller()
        LOG.info(f"SandboxDriver started, shell_server pid={self._shell_proc.pid}")

    async def shutdown(self) -> None:
        # Let the controller finish any in-flight async CRIU dump before we
        # terminate the namespace init. Otherwise a root-only quick-check can look
        # successful while the durable checkpoint image is being corrupted by
        # teardown.
        drain_error = None
        if self._ctrl is not None:
            try:
                await asyncio.to_thread(self._ctrl.shutdown)
                for ckpt_id, entry in self._ctrl.registry.items():
                    future = entry.get("dump_future")
                    if future is not None:
                        future.result()
                    if entry.get("dump_error") or entry.get("dump_stats", {}).get("dump_error"):
                        raise RuntimeError(f"checkpoint {ckpt_id} has no valid durable dump")
            except Exception as e:
                drain_error = e

        # 1. clean exit: shutdown via FIFO → shell_server exits → sentinel's
        #    waitpid drops → sentinel exits → self._shell_proc finishes.
        try:
            await self._send_shutdown()
        except Exception as e:
            LOG.warning(f"shell_server shutdown ignore: {e}")
        # The namespace init remains PID 1 across every warm restore; the
        # initial active PID does not. Killing PID 1 tears down all descendants.
        # Never kill just the host sentinel, which would orphan the namespace.
        await asyncio.to_thread(self._terminate_namespace)
        if self._shell_proc is not None:
            await asyncio.to_thread(self._shell_proc.wait, timeout=10)
        # 3. close controller's mount_fd before umount — otherwise the open
        # ioctl handle keeps the mount busy and umount fails.
        if self._ctrl is not None:
            try:
                if hasattr(self._ctrl, "mount_fd") and self._ctrl.mount_fd is not None:
                    os.close(self._ctrl.mount_fd)
                    self._ctrl.mount_fd = None
            except Exception as e:
                LOG.warning(f"mount_fd close ignore: {e}")

        # 4. unmount overlay. Try regular umount first; on busy fall back to
        # lazy umount (-l) so the workdir can be rm'd even when residual fds
        # exist (CRIU dump worker, parent template still SIGSTOPed, etc.).
        if self._overlay_mounted:
            for attempt in ("normal", "lazy"):
                cmd = ["umount"]
                if attempt == "lazy":
                    cmd.append("-l")
                cmd.append(str(self.merged_dir))
                if self.sudo_wrap:
                    cmd = ["sudo", "-n", *cmd]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    self._overlay_mounted = False
                    if attempt == "lazy":
                        LOG.info(f"overlay lazy-unmounted at {self.merged_dir}")
                    break
                LOG.warning(f"umount {attempt} attempt failed: {r.stderr.strip()}")

        if drain_error is not None:
            raise RuntimeError("checkpoint drain failed") from drain_error

    @staticmethod
    def _process_start_time(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return None

    def _terminate_namespace(self):
        from backends.deltabox.gsd.process_wait import wait_for_process_exit
        pid = self._ns_init_host_pid
        if pid is None:
            if self._shell_proc is not None and self._shell_proc.poll() is None:
                raise RuntimeError("namespace owner is unknown; refusing to orphan the sandbox")
            return
        # Guard against a PID being recycled if the namespace exited early.
        identity = self._process_start_time(pid)
        if identity is None or identity != self._ns_init_start_time:
            return
        command = ["kill", "-KILL", str(pid)]
        if self.sudo_wrap:
            command = ["sudo", "-n", *command]
        sent = subprocess.run(command, capture_output=True, text=True)
        def done(target):
            try:
                fields = Path(f"/proc/{target}/stat").read_text().rsplit(")", 1)[1].split()
                return fields[0] == "Z" or fields[19] != identity
            except FileNotFoundError:
                return True
        if sent.returncode and not done(pid):
            raise RuntimeError(f"cannot terminate namespace {pid}")
        if wait_for_process_exit([pid], 10, done):
            raise RuntimeError(f"namespace {pid} did not exit")

    # ───── checkpoint / restore (the deltabox C&R API surface) ─────────
    async def checkpoint(self, tag: str,
                         parent_ckpt_id: Optional[str] = None,
                         raw_command: str = "") -> CkptRecord:
        """Run a *standard* checkpoint (no LW). Captures process + overlay state."""
        if self._ctrl is None:
            raise RuntimeError("driver not started")
        parent_id = parent_ckpt_id or self._current_ckpt_id or "root"

        t0 = time.perf_counter()
        # checkpoint_action is sync (subprocess.Popen + async CRIU dump
        # serialized via ThreadPoolExecutor in the controller). Run it off the
        # event loop so the asyncio LLM call doesn't stall.
        info = await asyncio.to_thread(
            self._ctrl.checkpoint_action,
            parent_id, tag, raw_command,
        )
        wall_ms = (time.perf_counter() - t0) * 1000.0

        new_id = info.get("id") if isinstance(info, dict) else None
        if not new_id:
            raise RuntimeError(f"checkpoint_action returned no 'id': {info!r}")

        rec = CkptRecord(
            ckpt_id=new_id,
            parent_ckpt_id=parent_id,
            tag=tag,
            wall_ms=wall_ms,
            fork_or_criu="criu",   # checkpoint side always dumps; fork only happens on restore
            raw_command=raw_command,
            meta=info if isinstance(info, dict) else {},
        )
        self._ckpts[new_id] = rec
        self._current_ckpt_id = new_id
        LOG.info(f"ckpt[{tag}] {new_id} parent={parent_id} {wall_ms:.1f}ms")
        return rec

    async def restore(self, target_ckpt_id: str) -> CkptRecord:
        """Restore to a previous ckpt. Deltabox prefers warm-template fork."""
        if self._ctrl is None:
            raise RuntimeError("driver not started")
        if target_ckpt_id not in self._ckpts and target_ckpt_id != "root":
            raise KeyError(f"unknown ckpt_id: {target_ckpt_id}")

        t0 = time.perf_counter()
        info = await asyncio.to_thread(
            self._ctrl.restore_action, target_ckpt_id
        )
        wall_ms = (time.perf_counter() - t0) * 1000.0
        # SandboxController.restore_action returns dict with "path" key
        # ∈ {"warm-template", "criu"}. "warm-template" = fast path (fork);
        # "criu" = slow path (full CRIU restore). LW restore path keeps
        # the same field.
        info_path = info.get("path") if isinstance(info, dict) else None
        path = "fork" if info_path == "warm-template" else "criu"

        rec = CkptRecord(
            ckpt_id=target_ckpt_id,
            parent_ckpt_id=self._ckpts.get(target_ckpt_id, CkptRecord("", None, "", 0, "")).parent_ckpt_id,
            tag=f"restored:{target_ckpt_id}",
            wall_ms=wall_ms,
            fork_or_criu=path,
            meta=info if isinstance(info, dict) else {},
        )
        self._current_ckpt_id = target_ckpt_id
        LOG.info(f"restore -> {target_ckpt_id} via {path} {wall_ms:.1f}ms")
        return rec

    # ───── overlay mount ───────────────────────────────────────────────
    async def _mount_overlay(self) -> None:
        # Skip if already mounted
        try:
            with open("/proc/self/mountinfo") as f:
                for line in f:
                    if f" {self.merged_dir} " in line:
                        LOG.info(f"overlay already mounted at {self.merged_dir}")
                        self._overlay_mounted = True
                        return
        except Exception:
            pass

        # Mount: lowerdir = base_lower, upperdir / workdir under self.workdir.
        # index=off,metacopy=off,redirect_dir=off mirrors guest/main.py for
        # robustness against rare overlay edge cases.
        opts = (f"lowerdir={self.base_lower},"
                f"upperdir={self.upper_dir},"
                f"workdir={self.work_dir},"
                f"index=off,metacopy=off,redirect_dir=off")
        cmd = ["mount", "-t", "overlay", "overlay", "-o", opts, str(self.merged_dir)]
        if self.sudo_wrap:
            cmd = ["sudo", "-n", *cmd]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"overlay mount failed: rc={r.returncode}\n"
                               f"cmd={' '.join(cmd)}\nstderr={r.stderr}")
        self._overlay_mounted = True
        LOG.info(f"overlay mounted at {self.merged_dir} (lower={self.base_lower})")

    # ───── shell_server launch ─────────────────────────────────────────
    async def _launch_shell_server(self) -> None:
        # Recreate FIFOs each start (don't trust stale state)
        for p in (self.fifo_in_path, self.fifo_out_path):
            if p.exists():
                p.unlink()
            os.mkfifo(p, 0o600)
        # Clear any stale AGENT_PID_FILE — we wait for the sentinel to write
        # a fresh one as our readiness signal.
        if self.agent_pid_file.exists():
            self.agent_pid_file.unlink()
        if self.ns_init_pid_file.exists():
            self.ns_init_pid_file.unlink()

        # We launch shell_server through namespace_launcher.py. The launcher
        # *itself* becomes the sentinel in the host PID ns; it forks a child
        # which is PID 1 in the new PID ns, the child execs shell_server. The
        # sentinel writes the *child*'s host-side PID to AGENT_PID_FILE.
        # CRIU --tree must target the child PID (not the sentinel).
        env = {
            **os.environ,
            "SHELL_CMD_IN":      str(self.fifo_in_path),
            "SHELL_CMD_OUT":     str(self.fifo_out_path),
            "SHELL_LOG":         str(self.shell_log),
            "SHELL_TRACE_PATH":  str(self.trace_log),
            "SHELL_INITIAL_CWD": str(self.merged_dir),
            "AGENT_PID_FILE":    str(self.agent_pid_file),
            # Make the launcher publish the reaper (PID-1-in-ns) host PID so the
            # controller can hand its PID namespace to CRIU on cold restore.
            "AGENT_NS_INIT_PID_FILE": str(self.ns_init_pid_file),
        }

        py = sys.executable or "python3"
        cmd = [py, str(self.namespace_launcher_path), py, str(self.shell_server_path)]
        # Redirect launcher sentinel stdout/stderr to a log so its prints don't
        # spam the driver's stdout.
        launcher_log_fd = open(self.launcher_log, "a")
        if self.sudo_wrap:
            # Preserve env across sudo so SHELL_CMD_IN / AGENT_PID_FILE survive
            cmd = ["sudo", "-n", "-E", *cmd]

        LOG.info(f"launching shell_server: {' '.join(cmd)}")
        self._shell_proc = subprocess.Popen(
            cmd, env=env,
            stdout=launcher_log_fd, stderr=launcher_log_fd,
        )

        # Wait for sentinel to write AGENT_PID_FILE (= sandbox is ready)
        for _ in range(100):                  # 100 × 50 ms = 5 s
            if self._shell_proc.poll() is not None:
                raise RuntimeError(
                    f"shell_server died before ready: rc={self._shell_proc.returncode}; "
                    f"see {self.launcher_log} and {self.shell_log}")
            if self.agent_pid_file.exists():
                try:
                    self._agent_host_pid = int(self.agent_pid_file.read_text().strip())
                    break
                except ValueError:
                    # Sentinel half-wrote (rename races); retry
                    pass
            time.sleep(0.05)
        else:
            raise RuntimeError(
                f"AGENT_PID_FILE {self.agent_pid_file} never appeared. "
                f"Launcher log: {self.launcher_log}; shell log: {self.shell_log}")
        # Read the reaper (ns-init) host PID. In fixed-active-pid mode this is a
        # distinct process from the active worker and is the external PID
        # namespace owner the cold CRIU restore path needs. Written concurrently
        # by the sentinel, so allow a brief grace window.
        for _ in range(40):                   # 40 × 50 ms = 2 s
            if self.ns_init_pid_file.exists():
                try:
                    self._ns_init_host_pid = int(self.ns_init_pid_file.read_text().strip())
                    self._ns_init_start_time = self._process_start_time(self._ns_init_host_pid)
                    break
                except ValueError:
                    pass
            time.sleep(0.05)
        LOG.info(f"sentinel PID={self._shell_proc.pid}, agent (PID-100-in-ns) host "
                 f"PID={self._agent_host_pid}, ns-init (reaper) host "
                 f"PID={self._ns_init_host_pid}")

    # ───── controller construction ─────────────────────────────────────
    async def _build_controller(self) -> None:
        if self._shell_proc is None or self._agent_host_pid is None:
            raise RuntimeError("shell_proc/agent_host_pid not initialised")
        # Sanity: the child PID we got from AGENT_PID_FILE should be alive in
        # host /proc (host PID ns can see it as long as it lives).
        if not Path(f"/proc/{self._agent_host_pid}").exists():
            raise RuntimeError(
                f"Agent host PID {self._agent_host_pid} not in /proc — "
                f"namespace_launcher may have written a stale value")
        # If warm-template enabled, wait for shell_server to call
        # install_template_endpoint() — otherwise the controller's first
        # request_fork races and fails with FileNotFoundError on the FIFO.
        if self.enable_warm_template:
            tmpl_in = Path("/tmp/template_ctrl.in")
            for _ in range(100):    # 5s budget
                if tmpl_in.exists():
                    break
                await asyncio.sleep(0.05)
            else:
                LOG.warning(f"template_ctrl FIFO {tmpl_in} never appeared; "
                            f"warm-template path may fail")

        # SandboxController uses agent_pid for /proc/<pid>/maps reads and as
        # the CRIU --tree target. ns_init_pid is also seeded to this and
        # diverges after warm-template fork.
        # LEGACY lazy import (see module header): decoupled path uses SandboxBackend.
        from backends.deltabox.gsd.sandbox_controller import SandboxController
        self._ctrl = SandboxController(
            agent_pid=self._agent_host_pid,
            # The reaper (PID-1-in-ns); the cold CRIU restore path hands its PID
            # namespace to CRIU. Falls back to agent_pid inside the controller
            # when None (non-fixed-active mode, where they coincide).
            ns_init_pid=self._ns_init_host_pid,
            snapshot_store=str(self.snapshot_store),
            layers_root=str(self.layers_root),
            initial_upper=str(self.upper_dir),
            initial_work=str(self.work_dir),
            overlay_mount_point=str(self.merged_dir),
            enable_adaptive=self.enable_adaptive,   # LW classifier OFF
            enable_warm_template=self.enable_warm_template,
            enable_prewarm=self.enable_prewarm,
            enable_incremental_dump=self.enable_incremental_dump,
            # critical: controller's restore_action passes base_layer to the
            # overlay sink ioctl as the bottom lowerdir. The default
            # /testbed_original_data only exists inside the deltabox VM; host
            # runs need our host-side base_lower dir (the git clone tree).
            base_layer=str(self.base_lower),
            root_overlay_dirs=self._root_overlay_dirs,
        )

    # ───── shutdown helpers ────────────────────────────────────────────
    async def _send_shutdown(self) -> None:
        if not self.fifo_in_path.exists():
            return
        # Open non-blocking: a blocking open("w") on a FIFO hangs forever when
        # there is no reader. The shell_server path has a reader (open succeeds);
        # the decoupled agent_worker path does not use this FIFO at all, so the
        # open raises ENXIO and we simply skip (the worker is torn down by the
        # SIGTERM/kill fallback below).
        try:
            fd = os.open(self.fifo_in_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            os.write(fd, (json.dumps({"type": "shutdown"}) + "\n").encode())
        except OSError:
            pass
        finally:
            os.close(fd)
