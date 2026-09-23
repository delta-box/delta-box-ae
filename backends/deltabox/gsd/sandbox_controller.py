import os
import json
import shutil
import signal
import subprocess
import uuid
import time
import ctypes
import errno
import fcntl
import glob
import threading
import struct
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from contextlib import ExitStack
from typing import Callable, List, Optional

try:
    from .process_wait import wait_for_process_exit
except ImportError:
    from process_wait import wait_for_process_exit


# Append-only JSONL trace, shared with agent.py (same path via AGENT_TRACE_PATH
# env). The kernel guarantees write atomicity for line-sized appends in O_APPEND
# mode, so concurrent writes from agent (inside PID ns) + controller (outside)
# don't interleave. Tag every controller-emitted event with source="controller"
# so analysis scripts can disambiguate from agent.py's events that share the
# same TRACE_CTX (instance_id / run_id / strategy).
_TRACE_PATH = os.environ.get("AGENT_TRACE_PATH", "/tmp/agent_trace.jsonl")
_TRACE_CTX = {
    "instance_id": os.environ.get("AGENT_INSTANCE_ID", ""),
    "run_id":      os.environ.get("AGENT_RUN_ID", ""),
    "strategy":    os.environ.get("AGENT_STRATEGY", ""),
    "source":      "controller",
}

def _trace_event(kind: str, **fields) -> None:
    try:
        ev = {"ts": time.time(), "kind": kind, **_TRACE_CTX, **fields}
        with open(_TRACE_PATH, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never fail the agent loop on a trace write


class DumpUnavailableError(Exception):
    """Async CRIU dump failed or timed out; checkpoint is not restorable via
    the normal CRIU path. Callers (MCTS rollback) handle via parent-restore
    + action replay."""
    def __init__(self, ckpt_id: str, parent_id: Optional[str], cause: BaseException):
        self.ckpt_id = ckpt_id
        self.parent_id = parent_id
        self.cause = cause
        super().__init__(f"dump for {ckpt_id} unavailable: "
                         f"{type(cause).__name__}: {cause}")


class OverlaySwitchUnsupportedError(RuntimeError):
    """The mounted overlayfs does not implement DeltaBox's layer-switch ioctl."""

# Adaptive checkpoint support (optional)
try:
    from .semantic_parser import InstructionSemanticParser
    from .memory_classifier import classify_vmas
    ADAPTIVE_AVAILABLE = True
except ImportError:
    try:
        from semantic_parser import InstructionSemanticParser
        from memory_classifier import classify_vmas
        ADAPTIVE_AVAILABLE = True
    except ImportError:
        ADAPTIVE_AVAILABLE = False

# Warm-template fast-path support (optional)
try:
    from .template_fork import TemplatePool
    WARM_TEMPLATE_AVAILABLE = True
except ImportError:
    try:
        from template_fork import TemplatePool
        WARM_TEMPLATE_AVAILABLE = True
    except ImportError:
        WARM_TEMPLATE_AVAILABLE = False
        TemplatePool = None

# Whole-root rollback coverage (optional): overlay /usr //etc //var … so that
# apt/yum installs and other system-root changes roll back with the checkpoint.
try:
    from .root_overlay import RootOverlaySet
except ImportError:
    try:
        from root_overlay import RootOverlaySet
    except ImportError:
        RootOverlaySet = None

# Hot-page prewarm support (optional)
try:
    from .prewarm import spawn_prewarm, _read_rss_mb, validate_prewarm_mode
    PREWARM_AVAILABLE = True
except ImportError:
    try:
        from prewarm import spawn_prewarm, _read_rss_mb, validate_prewarm_mode
        PREWARM_AVAILABLE = True
    except ImportError:
        PREWARM_AVAILABLE = False
        spawn_prewarm = None
        def _read_rss_mb(pid):
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1]) / 1024.0
            except OSError:
                pass
            return -1.0

# =====================================================
# 内核 ioctl 接口定义 (匹配 overlayfs.h)
# struct ovl_checkpoint_args {
#     char __user *options;    // 8 bytes (64-bit pointer)
#     __u32 options_len;       // 4 bytes
# };                           // sizeof = 16 (padding to 8-byte alignment)
# =====================================================

class OvlCheckpointArgs(ctypes.Structure):
    """匹配内核 struct ovl_checkpoint_args（string-based 接口）"""
    _fields_ = [
        ("options", ctypes.c_uint64),       # char __user * (作为 uint64 传递用户态指针)
        ("options_len", ctypes.c_uint32),    # __u32
    ]

def _iow(type_char, nr, size):
    """计算 _IOW(type, nr, size) 的值，匹配 Linux 内核的 ioctl 编号"""
    IOC_WRITE = 1
    return (IOC_WRITE << 30) | (size << 16) | (ord(type_char) << 8) | nr

OVL_IOCTL_CHECKPOINT_CMD = _iow('O', 1, ctypes.sizeof(OvlCheckpointArgs))

class SandboxController:
    def __init__(self, agent_pid: int, snapshot_store: str,
                 layers_root: str, initial_upper: str, initial_work: str,
                 overlay_mount_point: str, enable_adaptive: bool = False,
                 enable_warm_template: bool = False,
                 enable_prewarm: bool = False,
                 enable_incremental_dump: bool = True,
                 base_layer: str = "/testbed_original_data",
                 ns_init_pid: Optional[int] = None,
                 root_overlay_dirs: Optional[List[str]] = None):
        # When False, CRIU dump never appends --prev-images-dir. Plain
        # fork-replace changes the process identity and can trip CRIU's PID
        # reuse checks; fixed-active mode is the deliberate exception because
        # clone3(set_tid=X) keeps the CRIU-visible in-ns PID stable.
        self.enable_incremental_dump = enable_incremental_dump
        # base_layer = the bottom (pristine) layer used at restore-time when
        # we strip all accumulated overlay layers and start fresh from the
        # parent ckpt's layer chain. Default targets the VM-internal path
        # /testbed_original_data; guest-heavy passes a host-side dir instead.
        self.agent_pid = agent_pid
        # ns_init_pid: host PID of the process that's PID 1 in the new PID ns.
        # In legacy fork-replace mode CRIU dumps this whole namespace tree. In
        # stash mode we dump only the stable active worker subtree; detached
        # templates are reparented under ns-init and must stay out of the dump.
        self.ns_init_pid = ns_init_pid or agent_pid
        self.snapshot_store = snapshot_store

        self.layers_root = layers_root
        self.current_upper = initial_upper#指向/overlay_workspace/upper
        self.current_work = initial_work  #指向/overlay_workspace/work
        self.overlay_mount_point = overlay_mount_point

        self.base_layer = base_layer

        self.registry = {}
        os.makedirs(snapshot_store, exist_ok=True)
        os.makedirs(layers_root, exist_ok=True)
        default_dump_bin = "/app/bin/criu" if os.path.exists("/app/bin/criu") else "criu"
        default_restore_bin = "/usr/sbin/criu" if os.path.exists("/usr/sbin/criu") else "criu"
        self.criu_dump_bin = os.environ.get("DELTABOX_CRIU_DUMP_BIN", default_dump_bin)
        self.criu_restore_bin = os.environ.get(
            "DELTABOX_CRIU_RESTORE_BIN", default_restore_bin)
        print(f"[Controller] CRIU dump bin={self.criu_dump_bin} "
              f"restore bin={self.criu_restore_bin}")

        # Adaptive checkpoint: semantic-aware CRIU strategy selection
        self.enable_adaptive = enable_adaptive and ADAPTIVE_AVAILABLE
        self._last_child_count = 0
        self._last_predump_dir = None
        self._last_predump_tree_pid = None
        if self.enable_adaptive:
            print(f"[Controller] Adaptive checkpoint ENABLED (semantic parser + memory classifier)")

        # Warm-template fast-path: SIGSTOP'd template + fork() for restore.
        self.enable_warm_template = enable_warm_template and WARM_TEMPLATE_AVAILABLE
        self.template_pool = (TemplatePool(reaper_pid=self.ns_init_pid)
                              if self.enable_warm_template else None)
        self.checkpoint_stash_template = (
            os.environ.get("DELTABOX_CHECKPOINT_STASH_TEMPLATE", "1") == "1"
        )
        self.fixed_active_pid = self._parse_fixed_active_pid()
        self.restore_fastfork_dump_pid = (
            self.fixed_active_pid is not None
            and os.environ.get("DELTABOX_RESTORE_FASTFORK_DUMP_PID", "0") == "1"
        )
        self.prefork_template_dump = (
            os.environ.get("DELTABOX_PREFORK_TEMPLATE_DUMP", "0") == "1"
        )
        self.async_template_full_dump = (
            os.environ.get("DELTABOX_ASYNC_TEMPLATE_FULL_DUMP", "0") == "1"
        )
        if self.async_template_full_dump:
            self.enable_incremental_dump = False
        if self.enable_warm_template:
            ckpt_mode = "stash" if self.checkpoint_stash_template else "fork-replace"
            if self.prefork_template_dump:
                ckpt_mode = "prefork-template-dump"
            if self.async_template_full_dump:
                ckpt_mode = "async-template-full-dump"
            print(f"[Controller] Warm-template fast path ENABLED "
                  f"(checkpoint_mode={ckpt_mode}, "
                  f"fixed_active_pid={self.fixed_active_pid or '-'}, "
                  f"restore_fastfork_dump_pid={int(self.restore_fastfork_dump_pid)})")

        # External prewarm via /proc/<child_pid>/mem after fork-restore.
        self.enable_prewarm = enable_prewarm and PREWARM_AVAILABLE
        if (self.enable_prewarm
                and os.environ.get("DELTABOX_DISABLE_PREWARM") == "1"):
            # Write-based prewarm forces a CoW copy of every anon page via
            # /proc/<pid>/mem, which sets the soft-dirty bit on every page it
            # touches and therefore turns every post-restore incremental dump
            # into a full-size one. The two features are mutually exclusive
            # on the same address space; under the incremental master switch
            # prewarm is disabled and post-restore CoW faults are absorbed by
            # the LLM idle window instead.
            self.enable_prewarm = False
            print("[Controller] Hot-page prewarm DISABLED "
                  "(DELTABOX_DISABLE_PREWARM=1; incompatible with "
                  "incremental soft-dirty tracking)")
        if self.enable_prewarm:
            validate_prewarm_mode()
            print("[Controller] Read-only page prefetch ENABLED (no CoW prepayment)")

        self.enable_criu_lazy_restore = (
            os.environ.get("DELTABOX_CRIU_LAZY_RESTORE", "0") == "1"
        )
        self.parallel_lazy_restore = (
            os.environ.get("DELTABOX_CRIU_LAZY_RESTORE_PARALLEL", "1") != "0"
        )
        self._lazy_page_daemons = []
        if self.enable_criu_lazy_restore:
            mode = "parallel" if self.parallel_lazy_restore else "serial"
            print(f"[Controller] CRIU lazy-pages slow restore ENABLED ({mode})")
        
        print(f"[Controller] Opening OverlayFS mount point: {self.overlay_mount_point}")
        self.mount_fd = os.open(self.overlay_mount_point, os.O_RDONLY | os.O_DIRECTORY)
        print(f"[Controller] ioctl cmd = 0x{OVL_IOCTL_CHECKPOINT_CMD:08X}, "
              f"args size = {ctypes.sizeof(OvlCheckpointArgs)} bytes")

        # Optional whole-root rollback coverage: each configured system dir is
        # mounted as its own hot-switchable overlay and checkpointed/restored in
        # lock-step with /testbed, so apt/yum installs and other root changes
        # roll back too. Disabled (no behaviour change) unless dirs are given.
        self.root_overlays = None
        if root_overlay_dirs:
            if RootOverlaySet is None:
                raise RuntimeError("root_overlay_dirs requested but root_overlay "
                                   "module is unavailable")
            self.root_overlays = RootOverlaySet(
                root_overlay_dirs,
                layers_root=os.path.join(self.layers_root, "rootovl"))
            self.root_overlays.mount_all()
            print(f"[Controller] Root overlay coverage ENABLED on "
                  f"{self.root_overlays.targets}")

        # Epoch counter for in-flight LLM cancellation on restore.
        # NPD workers read NPD_EPOCH_FILE at response-emit time; any response
        # whose rid carries an older epoch is dropped. We bump and persist on
        # every restore (warm or slow), then the agent tags new requests with
        # the new epoch. Initial value 0 lets pre-restore rids flow.
        self.current_epoch = 0
        self.npd_epoch_file = os.environ.get(
            "NPD_EPOCH_FILE", "/tmp/npd_current_epoch")
        self._persist_epoch()
        self._last_restore_target_id = None

        # Async CRIU dump pool. Single worker: concurrent dumps on the same
        # PID tree would race on /proc/[pid]/pagemap and track-mem soft-dirty
        # state. Typical dump ~40ms, parallel window (next iter's LLM wait)
        # ~7-9s, so serialized-in-pool is effectively parallel-with-caller.
        self._dump_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="criu-dump")
        self._restamp_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="criu-restamp")
        self._restamp_cache: dict[str, dict] = {}
        self._restamp_lock = threading.Lock()
        self._restamp_cache_root = os.path.join(
            self.snapshot_store, "_async_restamp_cache")
        os.makedirs(self._restamp_cache_root, exist_ok=True)
        self._pending_cleanup_reaps: dict[int, set[tuple[int, int]]] = {}
        self._active_dump_owner_id: Optional[str] = None
        self._async_incremental = None
        if os.environ.get("DELTABOX_ASYNC_INCREMENTAL_DUMP") == "1":
            try:
                from .async_checkpoint import AsyncIncrementalCheckpoint
            except ImportError:
                from async_checkpoint import AsyncIncrementalCheckpoint
            self._async_incremental = AsyncIncrementalCheckpoint(self)

    def _parse_fixed_active_pid(self) -> Optional[int]:
        raw = os.environ.get("DELTABOX_FIXED_ACTIVE_PID")
        try:
            pid = int(raw) if raw else 0
        except ValueError:
            print(f"[Controller] WARN: bad DELTABOX_FIXED_ACTIVE_PID={raw!r}")
            return None
        if pid <= 1:
            return None
        return pid

    def _reap_lazy_page_daemons(self):
        alive = []
        for proc in self._lazy_page_daemons:
            if proc.poll() is None:
                alive.append(proc)
        self._lazy_page_daemons = alive

    def _stop_lazy_pages_daemon(self, proc):
        """Stop only a daemon owned by this controller; reap it before GC."""
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._reap_lazy_page_daemons()

    def _lazy_reader_ids(self):
        """A live UFFD reader pins its image and, via retained_ids, ancestors."""
        self._reap_lazy_page_daemons()
        paths = {proc.deltabox_mem_path for proc in self._lazy_page_daemons}
        return {rid for rid, entry in self.registry.items()
                if entry.get("mem_path") in paths}

    def _start_lazy_pages_daemon(self, mem_path: str) -> subprocess.Popen:
        self._reap_lazy_page_daemons()
        sock = os.path.join(mem_path, "lazy-pages.socket")
        log_path = os.path.join(mem_path, "lazy-pages.log")
        for p in (sock, os.path.join(mem_path, "lazy-pages.pid")):
            try:
                os.unlink(p)
            except OSError:
                pass
        rfd, wfd = os.pipe()
        fcntl.fcntl(rfd, fcntl.F_SETFL, os.O_NONBLOCK)
        logf = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                [self.criu_restore_bin, "lazy-pages", "-D", mem_path,
                 "--status-fd", str(wfd)],
                stdout=logf,
                stderr=subprocess.STDOUT,
                pass_fds=(wfd,),
            )
        except BaseException:
            os.close(rfd)
            raise
        finally:
            logf.close()
            os.close(wfd)
        try:
            proc.deltabox_mem_path = mem_path
            self._lazy_page_daemons.append(proc)
            ready = b""
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    try:
                        tail = open(log_path, errors="ignore").read()[-1200:]
                    except OSError:
                        tail = ""
                    raise RuntimeError(
                        f"criu lazy-pages daemon died rc={proc.returncode}: {tail}"
                    )
                try:
                    chunk = os.read(rfd, 1)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    ready += chunk
                    if b"\0" in ready:
                        return proc
                time.sleep(0.001)
            raise TimeoutError(f"criu lazy-pages daemon did not signal ready: {sock}")
        except BaseException:
            self._stop_lazy_pages_daemon(proc)
            raise
        finally:
            os.close(rfd)

    def _persist_epoch(self):
        tmp = self.npd_epoch_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(str(self.current_epoch))
            os.rename(tmp, self.npd_epoch_file)
        except OSError as e:
            print(f"[Controller] WARN: epoch persist failed: {e}")

    def _bump_epoch(self):
        self.current_epoch += 1
        self._persist_epoch()
        print(f"[Controller] epoch -> {self.current_epoch}")

    def _unlink_pending_resp_files(self) -> None:
        # Defensive cleanup for the window between bump_epoch and the agent's
        # abort_pending_all handler: a pre-bump NPD worker may have completed
        # its epoch check and be mid-atomic-rename of a resp file. We sweep
        # the resp dir so no stale file survives; the agent re-unlinks any
        # that race in after this call.
        resp_dir = os.environ.get("NPD_RESP_DIR", "/tmp/npd_responses")
        try:
            names = os.listdir(resp_dir)
        except OSError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                os.unlink(os.path.join(resp_dir, name))
            except OSError:
                pass

    def _write_ctrl_abort_pending_all(self) -> None:
        # Signal the restored agent to drop every entry in its pending[] dict
        # (<=LLM_INFLIGHT_CAP rids, see agent.py). Atomic pipe write, <PIPE_BUF.
        # Record separators belong to the replay protocol. Live workers retain
        # the existing newline-delimited JSON interface.
        separator = "\x1e" if os.environ.get("DELTABOX_REPLAY_STRICT_EPOCH") == "1" else ""
        msg = (separator + json.dumps({"ctrl": "abort_pending_all",
                                   "_replay_epoch": self.current_epoch}) + "\n").encode()
        try:
            fd = os.open("/tmp/agent.in", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f"[Controller] abort_pending_all: open failed: {e}")
            return
        try:
            os.write(fd, msg)
        except OSError as e:
            print(f"[Controller] abort_pending_all: write failed: {e}")
        finally:
            os.close(fd)

    def _invalidate_inflight_llm(self) -> None:
        # State/transition decoupling: after a restore the pre-restore LLM
        # round-trip is no longer part of the live trajectory. Neutralize it
        # in the order required by design memo §8 to close the race window
        # between NPD's write path and the agent's select loop.
        self._bump_epoch()
        self._unlink_pending_resp_files()
        self._write_ctrl_abort_pending_all()

    def _all_ext_mount_map_args(self) -> List[str]:
        """CRIU --ext-mount-map args for /testbed plus any root-coverage overlays."""
        args = ["--ext-mount-map",
                f"{self.overlay_mount_point}:{self.overlay_mount_point}"]
        if self.root_overlays is not None:
            args += self.root_overlays.criu_ext_mount_map_args()
        return args

    def _apply_overlay_switch(self, layers: List[str], upper_path: str, work_path: str,
                              kind: str = "unknown"):
        """
        通过 string-based ioctl 执行 overlayfs 层切换。
        构造格式: "lowerdir=/path1:/path2,upperdir=/path3,workdir=/path4,ovl_kind=<kind>"

        `kind` ∈ {"ckpt", "restore", "unknown"} — used by kernel-side timing
        instrumentation to attribute per-phase cost to event type.
        """
        # 构造 options 字符串
        lowerdir_str = ":".join(layers)
        options_str = (f"lowerdir={lowerdir_str},upperdir={upper_path},"
                       f"workdir={work_path},ovl_kind={kind}")
        options_bytes = options_str.encode('utf-8')

        print(f"[FS] Switching overlay layers via ioctl:")
        print(f"     options = \"{options_str}\"")
        print(f"     options_len = {len(options_bytes)}")

        # 创建 ctypes buffer 保持 options_bytes 在内存中
        buf = ctypes.create_string_buffer(options_bytes)

        # 构造 args
        args = OvlCheckpointArgs()
        args.options = ctypes.cast(buf, ctypes.c_void_p).value
        args.options_len = len(options_bytes)

        try:
            fcntl.ioctl(self.mount_fd, OVL_IOCTL_CHECKPOINT_CMD, args)
            print(f"[FS] Switch successful. New stack: {len(layers)} lower layers")
        except OSError as e:
            print(f"[FS] IOCTL Failed: {e}")
            print(f"     errno = {e.errno}, strerror = {os.strerror(e.errno)}")
            if e.errno == errno.ENOTTY:
                raise OverlaySwitchUnsupportedError(
                    "DeltaBox OverlayFS layer switching is unavailable on this "
                    "mount. This usually means the runtime is using the stock "
                    "kernel overlayfs instead of the patched DeltaBox OverlayFS "
                    "module/guest kernel. Clean checkpoints can still run, but "
                    "dirty filesystem checkpoints and restores require the "
                    "patched OverlayFS ioctl."
                ) from e
            raise

    def _current_dump_tree_pid(self) -> int:
        """Return the CRIU dump root for the current checkpoint chain."""
        if (self.enable_warm_template
                and (self.checkpoint_stash_template
                     or self.async_template_full_dump
                     or os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1")):
            return self.agent_pid
        return self.ns_init_pid

    def _agent_inner_pid(self) -> Optional[int]:
        nspids = self._pid_nspids(self.agent_pid)
        return nspids[-1] if nspids else None

    def _need_fixed_dump_clone(self, strategy: str) -> bool:
        if not (self.restore_fastfork_dump_pid
                and self.enable_warm_template
                and self.template_pool is not None
                and strategy != "lightweight"):
            return False
        inner_pid = self._agent_inner_pid()
        return inner_pid is not None and inner_pid != self.fixed_active_pid

    def _pid_ns_inode(self, pid: int) -> Optional[str]:
        try:
            target = os.readlink(f"/proc/{pid}/ns/pid")
        except OSError:
            return None
        if target.startswith("pid:[") and target.endswith("]"):
            return target[5:-1]
        return None

    def _append_external_pidns(self, cmd: List[str], dump_tree_pid: int) -> None:
        if self._is_pidns_init(dump_tree_pid):
            return
        inode = self._pid_ns_inode(dump_tree_pid)
        if inode:
            cmd.extend(["--external", f"pid[{inode}]:deltabox_pidns"])

    def _is_pidns_init(self, pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("NSpid:"):
                        fields = line.split()
                        return bool(fields) and fields[-1] == "1"
        except OSError:
            return pid == self.ns_init_pid
        return pid == self.ns_init_pid

    def _is_external_pidns_dump(self, entry: dict) -> bool:
        if "external_pidns" in entry:
            return bool(entry["external_pidns"])
        dump_tree_pid = entry.get("dump_tree_pid")
        ns_init_pid = entry.get("ns_init_pid")
        return (dump_tree_pid is not None
                and ns_init_pid is not None
                and not self._is_pidns_init(dump_tree_pid))

    def _open_external_pidns_fd(self) -> Optional[int]:
        try:
            return os.open(f"/proc/{self.ns_init_pid}/ns/pid", os.O_RDONLY)
        except OSError as e:
            print(f"[Controller] WARN: failed to open external pidns "
                  f"for ns_init={self.ns_init_pid}: {e}")
            return None

    def _append_external_pidns_restore(self, cmd: List[str], pidns_fd: Optional[int]) -> None:
        if pidns_fd is None:
            return
        # CRIU stores --external pid[...] using the bare label as pid_ns->ext_key;
        # restore looks up that exact key in the inherit-fd table.
        cmd.extend(["--inherit-fd", f"fd[{pidns_fd}]:deltabox_pidns"])

    def _image_dir_size(self, image_dir: str) -> int:
        total = 0
        try:
            for root, dirs, files in os.walk(image_dir):
                dirs[:] = [d for d in dirs if d != "_restamped_parent"]
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def _wait_pid_stopped(self, pid: int, timeout: float = 1.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(f"/proc/{pid}/stat") as f:
                    fields = f.read().split()
                if len(fields) > 2 and fields[2] in ("T", "t"):
                    return True
            except OSError:
                return False
            time.sleep(0.005)
        return False

    def _process_memory_digest(self, pid: int) -> dict:
        """Validation-only digest of every readable writable private mapping."""
        import hashlib

        digest = hashlib.sha256()
        ranges: list[dict] = []
        try:
            maps = open(f"/proc/{pid}/maps").read().splitlines()
        except OSError as e:
            return {"ok": False, "err": f"maps: {e}", "pid": pid}
        smaps_meta: dict[str, dict] = {}
        try:
            current_key = None
            for line in open(f"/proc/{pid}/smaps").read().splitlines():
                fields = line.split(None, 5)
                if fields and "-" in fields[0]:
                    current_key = fields[0]
                    smaps_meta[current_key] = {}
                    continue
                if current_key is None or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                if key in (
                    "Size", "Rss", "Pss", "Shared_Clean",
                    "Shared_Dirty", "Private_Clean", "Private_Dirty",
                    "Referenced", "Anonymous", "AnonHugePages",
                    "VmFlags",
                ):
                    smaps_meta[current_key][key] = value.strip()
        except OSError:
            smaps_meta = {}
        try:
            mem_fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        except OSError as e:
            return {"ok": False, "err": f"mem: {e}", "pid": pid}
        try:
            for line in maps:
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                addr, perms = parts[0], parts[1]
                path = parts[5] if len(parts) > 5 else ""
                if "r" not in perms or "w" not in perms or "p" not in perms:
                    continue
                start_s, end_s = addr.split("-")
                start = int(start_s, 16)
                end = int(end_s, 16)
                size = end - start
                rd = 0
                range_digest = hashlib.sha256()
                try:
                    os.lseek(mem_fd, start, os.SEEK_SET)
                    remaining = size
                    while remaining > 0:
                        chunk = os.read(mem_fd, min(1 << 20, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        range_digest.update(chunk)
                        rd += len(chunk)
                        remaining -= len(chunk)
                except OSError as e:
                    ranges.append({
                        "start": start_s, "end": end_s, "path": path,
                        "perms": perms, "size": size, "read": rd,
                        "smaps": smaps_meta.get(addr, {}),
                        "err": str(e),
                    })
                    continue
                ranges.append({
                    "start": start_s, "end": end_s, "path": path,
                    "perms": perms, "size": size, "read": rd,
                    "smaps": smaps_meta.get(addr, {}),
                    "sha256": range_digest.hexdigest(),
                })
        finally:
            os.close(mem_fd)
        total = sum(r.get("read", 0) for r in ranges)
        return {
            "ok": total > 0,
            "pid": pid,
            "mode": "all_rw_private",
            "mem_sha256": digest.hexdigest(),
            "mem_bytes": total,
            "ranges": ranges,
        }

    def _dump_validation_full_twin(self, ckpt_id: str, tag: str,
                                   dump_tree_pid: int) -> dict:
        """Dump a validation-only full image from the same stopped task state.

        Production correctness validation compares a restored incremental
        image against this full image.  This helper intentionally omits
        --track-mem and --prev-images-dir so it cannot advance or restamp the
        production incremental chain.
        """
        full_id = f"{ckpt_id}_full"
        full_dir = os.path.join(self.snapshot_store,
                                f"{tag}_{ckpt_id}_full_twin_mem")
        if os.path.exists(full_dir):
            shutil.rmtree(full_dir)
        os.makedirs(full_dir, exist_ok=True)
        cmd = [
            self.criu_dump_bin, "dump", "--tree", str(dump_tree_pid),
            "-D", full_dir,
            "--shell-job", "--leave-stopped", "--tcp-close",
            "--ext-unix-sk", "--manage-cgroups",
            *self._all_ext_mount_map_args(),
            "--link-remap",
        ]
        self._append_external_pidns(cmd, dump_tree_pid)
        if os.environ.get("CRIU_DEBUG"):
            cmd.extend(["-v4"])
        t0 = time.time()
        subprocess.check_call(cmd)
        elapsed_ms = (time.time() - t0) * 1000
        size_bytes = self._image_dir_size(full_dir)
        print(f"[PROBE] validation full-twin dump ckpt={ckpt_id} "
              f"full_id={full_id} root={dump_tree_pid} "
              f"criu={elapsed_ms:.2f}ms "
              f"dump_size={size_bytes/(1024*1024):.2f}MB",
              flush=True)
        return {
            "id": full_id,
            "mem_path": full_dir,
            "criu_ms": elapsed_ms,
            "dump_size_bytes": size_bytes,
            "dump_tree_pid": dump_tree_pid,
        }

    def _dump_full_fallback_image(self, ckpt_id: str, image_dir: str,
                                  dump_tree_pid: int,
                                  reason: str) -> dict:
        """Replace a failed incremental image with a full durable image.

        CRIU's incremental parent chain can occasionally reject a restamped
        parent image (for example, a missing parent pagemap hole). The warm
        template can still serve the fast path, but the checkpoint must also
        have a durable slow-path image. When the incremental dump fails before
        we publish metadata, fall back to a no-parent dump from the same live
        state instead of registering an unrestorable checkpoint.
        """
        if os.environ.get("DELTABOX_DISABLE_FULL_FALLBACK_ON_INCREMENTAL_FAIL") == "1":
            raise RuntimeError("full fallback on incremental dump failure disabled")

        if os.path.exists(image_dir):
            shutil.rmtree(image_dir)
        os.makedirs(image_dir, exist_ok=True)

        cmd = [
            self.criu_dump_bin, "dump", "--tree", str(dump_tree_pid),
            "-D", image_dir,
            "--shell-job", "--tcp-close",
            "--ext-unix-sk", "--manage-cgroups",
            *self._all_ext_mount_map_args(),
            "--link-remap",
        ]
        if self.enable_incremental_dump:
            # Keep memory tracking enabled so this full fallback can become a
            # valid parent for later incremental dumps.
            cmd.append("--track-mem")
        self._append_external_pidns(cmd, dump_tree_pid)
        if os.environ.get("CRIU_DEBUG"):
            cmd.extend(["-v4"])
        cmd.append("--leave-running")

        t0 = time.time()
        subprocess.check_call(cmd)
        elapsed_ms = (time.time() - t0) * 1000
        size_bytes = self._image_dir_size(image_dir)
        pagemap_summary = self._debug_pagemap_summary(
            image_dir, f"ckpt={ckpt_id} full-fallback")
        print(f"[PERF] Checkpoint ID {ckpt_id}: full-fallback CRIU="
              f"{elapsed_ms:.2f}ms reason={reason} "
              f"dump_size={size_bytes/(1024*1024):.2f}MB",
              flush=True)
        _trace_event(
            "checkpoint_dump_full_fallback",
            ckpt_id=ckpt_id,
            reason=reason,
            dur_ms=elapsed_ms,
            dump_size_bytes=size_bytes,
            pagemap_summary=pagemap_summary,
        )
        return {
            "criu_ms": elapsed_ms,
            "dump_completed_ms": elapsed_ms,
            "dump_size_bytes": size_bytes,
            "pagemap_summary": pagemap_summary,
            "dump_fallback_full": True,
            "dump_fallback_reason": reason,
        }

    def _probe_clear_soft_dirty(self, pid: int) -> None:
        if not (os.environ.get("DELTABOX_PROBE_CROSS_PID_INCREMENTAL") == "1"
                or os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1"
                or self.fixed_active_pid is not None):
            return
        try:
            t0 = time.time()
            with open(f"/proc/{pid}/clear_refs", "w") as f:
                f.write("4")
            elapsed_ms = (time.time() - t0) * 1000
            print(f"[Controller] clear_refs soft-dirty reset for pid={pid} "
                  f"elapsed={elapsed_ms:.3f}ms")
        except OSError as e:
            print(f"[Controller] clear_refs failed for pid={pid}: {e}")

    def _debug_soft_dirty_summary(self, pid: Optional[int],
                                  label: str) -> Optional[dict]:
        if os.environ.get("DELTABOX_DEBUG_SOFT_DIRTY_SUMMARY") != "1":
            return None
        if pid is None or not os.path.isdir(f"/proc/{pid}"):
            summary = {"ok": False, "error": "pid unavailable", "pid": pid}
            print(f"[DEBUG_SOFT_DIRTY] {label}: {summary}")
            return summary

        max_pages = int(os.environ.get(
            "DELTABOX_DEBUG_SOFT_DIRTY_MAX_PAGES", "250000"))
        include_file = (
            os.environ.get("DELTABOX_DEBUG_SOFT_DIRTY_INCLUDE_FILE") == "1")
        summary = {
            "ok": True,
            "pid": pid,
            "sampled_pages": 0,
            "soft_dirty_pages": 0,
            "present_pages": 0,
            "vmas": 0,
            "truncated": False,
        }

        ranges: list[tuple[int, int]] = []
        try:
            with open(f"/proc/{pid}/maps", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    perms = parts[1]
                    path = parts[5] if len(parts) >= 6 else ""
                    if "r" not in perms:
                        continue
                    if not include_file and path and not path.startswith("["):
                        continue
                    try:
                        lo_s, hi_s = parts[0].split("-", 1)
                        lo = int(lo_s, 16)
                        hi = int(hi_s, 16)
                    except ValueError:
                        continue
                    if hi > lo:
                        ranges.append((lo, hi))
        except OSError as e:
            summary.update({"ok": False, "error": f"maps: {e}"})
            print(f"[DEBUG_SOFT_DIRTY] {label}: {summary}")
            return summary

        page_size = os.sysconf("SC_PAGE_SIZE")
        soft_dirty_bit = 1 << 55
        present_bit = 1 << 63
        try:
            with open(f"/proc/{pid}/pagemap", "rb", buffering=0) as pm:
                for lo, hi in ranges:
                    if summary["sampled_pages"] >= max_pages:
                        summary["truncated"] = True
                        break
                    summary["vmas"] += 1
                    start_page = lo // page_size
                    end_page = (hi + page_size - 1) // page_size
                    for page in range(start_page, end_page):
                        if summary["sampled_pages"] >= max_pages:
                            summary["truncated"] = True
                            break
                        pm.seek(page * 8)
                        data = pm.read(8)
                        if len(data) != 8:
                            continue
                        val = struct.unpack("Q", data)[0]
                        summary["sampled_pages"] += 1
                        if val & present_bit:
                            summary["present_pages"] += 1
                        if val & soft_dirty_bit:
                            summary["soft_dirty_pages"] += 1
        except OSError as e:
            summary.update({"ok": False, "error": f"pagemap: {e}"})
            print(f"[DEBUG_SOFT_DIRTY] {label}: {summary}")
            return summary

        sampled = summary["sampled_pages"] or 1
        summary["soft_dirty_ratio"] = (
            summary["soft_dirty_pages"] / sampled)
        print(f"[DEBUG_SOFT_DIRTY] {label}: pid={pid} "
              f"sampled={summary['sampled_pages']} "
              f"present={summary['present_pages']} "
              f"soft_dirty={summary['soft_dirty_pages']} "
              f"ratio={summary['soft_dirty_ratio']:.4f} "
              f"vmas={summary['vmas']} "
              f"truncated={summary['truncated']}")
        return summary

    def _debug_addr_maps(self, label: str, targets: List[tuple[str, Optional[int]]]) -> None:
        raw = os.environ.get("DELTABOX_DEBUG_ADDRS", "")
        if not raw or os.environ.get("DELTABOX_DEBUG_ADDR_MAPS") != "1":
            return
        addrs: list[int] = []
        for item in raw.replace(",", " ").split():
            try:
                addrs.append(int(item, 0))
            except ValueError:
                print(f"[DEBUG_ADDR] {label}: bad address {item!r}")
        if not addrs:
            return

        def _mapping_for(pid: int, addr: int) -> tuple[Optional[str], list[str]]:
            try:
                lines = open(f"/proc/{pid}/maps").read().splitlines()
            except OSError as e:
                return None, [f"maps_error={e}"]
            hit = None
            for line in lines:
                try:
                    span = line.split(None, 1)[0]
                    lo_s, hi_s = span.split("-", 1)
                    lo = int(lo_s, 16)
                    hi = int(hi_s, 16)
                except Exception:
                    continue
                if lo <= addr < hi:
                    hit = line
                    break
            if hit is None:
                return None, []

            smaps: list[str] = []
            try:
                all_smaps = open(f"/proc/{pid}/smaps").read().splitlines()
            except OSError as e:
                return hit, [f"smaps_error={e}"]
            capture = False
            for line in all_smaps:
                if "-" in line.split(None, 1)[0]:
                    capture = (line == hit)
                    if capture:
                        smaps.append(line)
                    continue
                if capture and (
                    line.startswith("Size:")
                    or line.startswith("Rss:")
                    or line.startswith("Pss:")
                    or line.startswith("Shared_Clean:")
                    or line.startswith("Shared_Dirty:")
                    or line.startswith("Private_Clean:")
                    or line.startswith("Private_Dirty:")
                    or line.startswith("Referenced:")
                    or line.startswith("Anonymous:")
                    or line.startswith("VmFlags:")
                ):
                    smaps.append(line)
            return hit, smaps

        for name, pid in targets:
            if pid is None:
                print(f"[DEBUG_ADDR] {label}: {name}=None")
                continue
            alive = os.path.isdir(f"/proc/{pid}")
            print(f"[DEBUG_ADDR] {label}: {name} pid={pid} alive={alive}")
            if not alive:
                continue
            for addr in addrs:
                mapping, smaps = _mapping_for(pid, addr)
                if mapping is None:
                    print(f"[DEBUG_ADDR] {label}: {name} pid={pid} "
                          f"addr=0x{addr:x} mapping=NONE")
                else:
                    print(f"[DEBUG_ADDR] {label}: {name} pid={pid} "
                          f"addr=0x{addr:x} mapping={mapping}")
                    for line in smaps[:16]:
                        print(f"[DEBUG_ADDR] {label}: {name} smaps {line}")

    def _debug_parent_pagemap_chain(self, label: str,
                                    parent_mem_path: Optional[str]) -> None:
        raw = os.environ.get("DELTABOX_DEBUG_ADDRS", "")
        if not raw or not parent_mem_path:
            return
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        try:
            from pycriu import images as criu_images  # type: ignore
        except Exception as e:
            print(f"[DEBUG_PAGEMAP] {label}: pycriu unavailable: {e}")
            return

        addrs: list[int] = []
        for item in raw.replace(",", " ").split():
            try:
                addrs.append(int(item, 0))
            except ValueError:
                pass
        if not addrs:
            return

        def _parent_dir(path: str) -> Optional[str]:
            inv = os.path.join(path, "inventory.img")
            try:
                with open(inv, "rb") as f:
                    img = criu_images.load(f, no_payload=True)
            except Exception:
                return None
            entries = img.get("entries") or []
            if not entries:
                return None
            for key in ("parent_img", "parent_img_dir", "parent_images_dir",
                        "parent"):
                val = entries[0].get(key)
                if isinstance(val, str) and val:
                    return os.path.abspath(os.path.join(path, val))
            return None

        def _entry_at(path: str, addr: int) -> tuple[str, Optional[str]]:
            for pm_path in sorted(glob.glob(os.path.join(path, "pagemap-*.img"))):
                try:
                    with open(pm_path, "rb") as f:
                        img = criu_images.load(f, no_payload=True)
                except Exception as e:
                    print(f"[DEBUG_PAGEMAP] {label}: read_error "
                          f"{pm_path}: {e}")
                    continue
                for ent in img.get("entries", []):
                    vaddr = ent.get("vaddr")
                    nr_pages = ent.get("nr_pages")
                    if vaddr is None or nr_pages is None:
                        continue
                    start = int(vaddr)
                    end = start + int(nr_pages) * 4096
                    if start <= addr < end:
                        flags = int(ent.get("flags", 0))
                        if flags & 4:
                            kind = "present"
                        elif flags & 1:
                            kind = "parent"
                        elif flags & 2:
                            kind = "lazy"
                        else:
                            kind = f"flags:{flags}"
                        return kind, os.path.basename(pm_path)
            return "missing", None

        for addr in addrs:
            parts: list[str] = []
            seen: set[str] = set()
            path: Optional[str] = parent_mem_path
            for level in range(8):
                if not path or path in seen or not os.path.isdir(path):
                    break
                seen.add(path)
                kind, pm_name = _entry_at(path, addr)
                base = os.path.basename(path)
                suffix = f":{pm_name}" if pm_name else ""
                parts.append(f"L{level}:{base}:{kind}{suffix}")
                path = _parent_dir(path)
            print(f"[DEBUG_PAGEMAP] {label}: addr=0x{addr:x} "
                  f"chain={' -> '.join(parts) if parts else 'none'}")

    def _debug_pagemap_summary(self, image_dir: str, label: str) -> Optional[dict]:
        if os.environ.get("DELTABOX_DEBUG_PAGEMAP_SUMMARY") != "1":
            return None
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        try:
            from pycriu import images as criu_images  # type: ignore
        except Exception as e:
            print(f"[DEBUG_PAGEMAP_SUMMARY] {label}: pycriu unavailable: {e}")
            return {"ok": False, "error": f"pycriu unavailable: {e}"}

        summary = {
            "ok": True,
            "pagemap_files": 0,
            "entries": 0,
            "present_entries": 0,
            "parent_entries": 0,
            "lazy_entries": 0,
            "other_entries": 0,
            "total_pages": 0,
            "present_pages": 0,
            "parent_pages": 0,
            "lazy_pages": 0,
            "other_pages": 0,
            "pages_files_bytes": 0,
            "files": [],
        }
        for pages_path in sorted(glob.glob(os.path.join(image_dir, "pages-*.img"))):
            try:
                summary["pages_files_bytes"] += os.path.getsize(pages_path)
            except OSError:
                pass
        for pm_path in sorted(glob.glob(os.path.join(image_dir, "pagemap-*.img"))):
            file_summary = {
                "file": os.path.basename(pm_path),
                "entries": 0,
                "present_entries": 0,
                "parent_entries": 0,
                "lazy_entries": 0,
                "other_entries": 0,
                "total_pages": 0,
                "present_pages": 0,
                "parent_pages": 0,
                "lazy_pages": 0,
                "other_pages": 0,
            }
            try:
                with open(pm_path, "rb") as f:
                    img = criu_images.load(f, no_payload=True)
            except Exception as e:
                file_summary["error"] = f"{type(e).__name__}: {e}"
                summary["files"].append(file_summary)
                print(f"[DEBUG_PAGEMAP_SUMMARY] {label}: read_error "
                      f"{pm_path}: {e}")
                continue

            raw_entries = img.get("entries", [])
            pagemap_entries: list[dict] = []
            for top in raw_entries:
                extra = top.get("extra")
                if isinstance(extra, list):
                    pagemap_entries.extend(
                        ent for ent in extra if isinstance(ent, dict))
                elif isinstance(top, dict):
                    pagemap_entries.append(top)
            if os.environ.get("DELTABOX_DEBUG_PAGEMAP_SAMPLE") == "1":
                top_sample = raw_entries[:2]
                entry_sample = pagemap_entries[:4]
                print(f"[DEBUG_PAGEMAP_SUMMARY] {label}: sample "
                      f"file={os.path.basename(pm_path)} "
                      f"top={top_sample} entries={entry_sample}")

            for ent in pagemap_entries:
                if "vaddr" not in ent:
                    continue
                nr_pages = ent.get("nr_pages")
                if nr_pages is None:
                    nr_pages = ent.get("nr")
                pages = int(nr_pages or 0)
                flags = int(ent.get("flags", 0))
                file_summary["entries"] += 1
                file_summary["total_pages"] += pages
                summary["entries"] += 1
                summary["total_pages"] += pages
                if flags & 4:
                    bucket = "present_pages"
                    entry_bucket = "present_entries"
                elif flags & 1:
                    bucket = "parent_pages"
                    entry_bucket = "parent_entries"
                elif flags & 2:
                    bucket = "lazy_pages"
                    entry_bucket = "lazy_entries"
                else:
                    bucket = "other_pages"
                    entry_bucket = "other_entries"
                file_summary[entry_bucket] += 1
                summary[entry_bucket] += 1
                file_summary[bucket] += pages
                summary[bucket] += pages

            summary["pagemap_files"] += 1
            summary["files"].append(file_summary)

        compact_files = ", ".join(
            f"{f.get('file')}:present_entries={f.get('present_entries', 0)}"
            f"/parent_entries={f.get('parent_entries', 0)}"
            f"/lazy_entries={f.get('lazy_entries', 0)}"
            for f in summary["files"][:8])
        if len(summary["files"]) > 8:
            compact_files += f", ...(+{len(summary['files']) - 8})"
        print(f"[DEBUG_PAGEMAP_SUMMARY] {label}: "
              f"files={summary['pagemap_files']} "
              f"entries={summary['entries']} "
              f"present_entries={summary['present_entries']} "
              f"parent_entries={summary['parent_entries']} "
              f"lazy_entries={summary['lazy_entries']} "
              f"pages_total={summary['total_pages']} "
              f"present={summary['present_pages']} "
              f"parent={summary['parent_pages']} "
              f"lazy={summary['lazy_pages']} "
              f"other={summary['other_pages']} "
              f"pages_bytes={summary['pages_files_bytes']} "
              f"[{compact_files}]")
        return summary

    def _clear_soft_dirty_after_fork(self, pid: int) -> None:
        meta = getattr(self.template_pool, "last_fork_meta", {}) if self.template_pool else {}
        if (meta.get("preserve_soft_dirty_after_settid")
                and os.environ.get("DELTABOX_PRESERVE_SOFT_DIRTY_AFTER_SETTID") == "1"):
            print(f"[PROBE] keep soft-dirty after set_tid fork pid={pid}; "
                  "new stack/control pages must be dumped as delta")
            return
        if (meta.get("clone3_child_self_clear_refs")
                or meta.get("clone_parent_child_self_clear_refs")):
            print(f"[PROBE] clear_refs already reset in fork child pid={pid}; "
                  "skip controller-side reset")
            return
        self._probe_clear_soft_dirty(pid)
        self._debug_soft_dirty_summary(pid, f"after-clear pid={pid}")

    def _maybe_restamp_parent_inventory(self, parent_mem_path: str,
                                        curr_dir: str) -> str:
        if os.environ.get("DELTABOX_RESTAMP_PARENT_INVENTORY") != "1":
            return parent_mem_path
        return self._restamp_parent_inventory_sync(
            parent_mem_path, curr_dir, source="sync")

    def _restamp_parent_inventory_sync(self, parent_mem_path: str,
                                       curr_dir: str, *,
                                       source: str = "sync") -> str:
        stamp_dir = os.path.join(curr_dir, "_restamped_parent")
        if os.path.exists(stamp_dir):
            shutil.rmtree(stamp_dir)
        self._copy_restamp_image_dir(parent_mem_path, stamp_dir)
        return self._finalize_restamped_parent_inventory(
            parent_mem_path, stamp_dir, source=source)

    def _copy_restamp_image_dir(self, src_dir: str, dst_dir: str) -> None:
        """Hard-link one CRIU image directory without following parent chains.

        CRIU image directories are normally flat, but incremental dumps may
        contain a ``parent`` pointer.  ``shutil.copytree`` follows symlinks by
        default; when the parent image already carries a parent/restamp chain,
        following it recursively creates ``parent/_restamped_parent/...`` trees
        that can fill the guest disk.  Restamping only needs a mutable copy of
        the immediate parent's top-level image files, so keep this copy shallow.
        """
        os.makedirs(dst_dir, exist_ok=False)
        for entry in os.scandir(src_dir):
            name = entry.name
            if name == "_restamped_parent":
                continue
            dst = os.path.join(dst_dir, name)
            if entry.is_symlink():
                target = os.path.realpath(entry.path)
                os.symlink(target, dst)
                continue
            if entry.is_dir(follow_symlinks=False):
                if name == "parent":
                    os.symlink(os.path.abspath(entry.path), dst)
                    continue
                # Any other directory here would be aux logs or old restamp
                # scratch, not part of the immediate image set we need to
                # mutate.
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                os.link(entry.path, dst)
            except OSError:
                shutil.copy2(entry.path, dst)

    def _prepare_restamp_parent_copy(self, ckpt_id: str, parent_mem_path: str,
                                     dump_future=None) -> dict:
        t0 = time.time()
        try:
            if dump_future is not None:
                dump_future.result(timeout=30.0)
            if not os.path.isdir(parent_mem_path):
                raise FileNotFoundError(parent_mem_path)
            cache_dir = os.path.join(self._restamp_cache_root, ckpt_id)
            tmp_dir = f"{cache_dir}.tmp.{uuid.uuid4().hex[:8]}"
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
            self._copy_restamp_image_dir(parent_mem_path, tmp_dir)
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
            os.replace(tmp_dir, cache_dir)
            elapsed_ms = (time.time() - t0) * 1000
            print(f"[PROBE] async restamp prepared parent {ckpt_id} "
                  f"from {parent_mem_path} in {elapsed_ms:.2f}ms")
            return {
                "ok": True,
                "ckpt_id": ckpt_id,
                "parent_mem_path": parent_mem_path,
                "cache_dir": cache_dir,
                "elapsed_ms": elapsed_ms,
            }
        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000
            print(f"[PROBE] async restamp prepare failed parent {ckpt_id} "
                  f"after {elapsed_ms:.2f}ms: {type(e).__name__}: {e}")
            return {
                "ok": False,
                "ckpt_id": ckpt_id,
                "parent_mem_path": parent_mem_path,
                "elapsed_ms": elapsed_ms,
                "error": f"{type(e).__name__}: {e}",
            }

    def _schedule_async_parent_restamp(self, ckpt_id: str) -> None:
        if os.environ.get("DELTABOX_RESTAMP_PARENT_INVENTORY") != "1":
            return
        if not (self.fixed_active_pid is not None
                and self.enable_incremental_dump):
            return
        entry = self.registry.get(ckpt_id)
        if not entry or entry.get("strategy") == "lightweight":
            return
        if entry.get("dump_error") or entry.get("dump_stats", {}).get("dump_error"):
            return
        parent_mem_path = entry.get("mem_path")
        if not parent_mem_path:
            return
        with self._restamp_lock:
            for old_id, old in list(self._restamp_cache.items()):
                if old_id == ckpt_id:
                    continue
                old_future = old.get("future")
                if old_future is not None and not old_future.done():
                    old_future.cancel()
                    if not old_future.done():
                        continue
                old_cache_dir = old.get("cache_dir")
                if old_cache_dir:
                    shutil.rmtree(old_cache_dir, ignore_errors=True)
                self._restamp_cache.pop(old_id, None)
            cached = self._restamp_cache.get(ckpt_id)
            if cached and (cached.get("future") is not None
                           or cached.get("cache_dir")):
                return
            future = self._restamp_pool.submit(
                self._prepare_restamp_parent_copy,
                ckpt_id, parent_mem_path, entry.get("dump_future"))
            self._restamp_cache[ckpt_id] = {
                "future": future,
                "parent_mem_path": parent_mem_path,
            }
        print(f"[PROBE] async restamp scheduled parent {ckpt_id} "
              f"mem={parent_mem_path}")

    def _consume_async_parent_restamp(self, ckpt_id: str,
                                      parent_mem_path: str,
                                      curr_dir: str) -> Optional[str]:
        with self._restamp_lock:
            cached = self._restamp_cache.get(ckpt_id)
        if not cached:
            return None
        future = cached.get("future")
        result = None
        if future is not None:
            if not future.done():
                return None
            result = future.result()
            with self._restamp_lock:
                current = self._restamp_cache.get(ckpt_id, {})
                current.update(result)
                current["future"] = None
                self._restamp_cache[ckpt_id] = current
                cached = current
        cache_dir = cached.get("cache_dir")
        if not cached.get("ok", True) or not cache_dir or not os.path.isdir(cache_dir):
            print(f"[PROBE] async restamp unusable parent {ckpt_id}: "
                  f"{cached.get('error', 'missing cache')}")
            return None
        stamp_dir = os.path.join(curr_dir, "_restamped_parent")
        if os.path.exists(stamp_dir):
            shutil.rmtree(stamp_dir)
        os.replace(cache_dir, stamp_dir)
        print(f"[PROBE] async restamp hit parent {ckpt_id} via {cache_dir}")
        try:
            return self._finalize_restamped_parent_inventory(
                parent_mem_path, stamp_dir, source="async-hit")
        finally:
            with self._restamp_lock:
                self._restamp_cache.pop(ckpt_id, None)

    def _restamp_parent_inventory_for_checkpoint(self, ckpt_id: str,
                                                 parent_mem_path: str,
                                                 curr_dir: str) -> str:
        if os.environ.get("DELTABOX_RESTAMP_PARENT_INVENTORY") != "1":
            return parent_mem_path
        prepared = self._consume_async_parent_restamp(
            ckpt_id, parent_mem_path, curr_dir)
        if prepared:
            return prepared
        print(f"[PROBE] async restamp miss parent {ckpt_id}; "
              "falling back to synchronous restamp")
        return self._restamp_parent_inventory_sync(
            parent_mem_path, curr_dir, source="sync-miss")

    def _finalize_restamped_parent_inventory(self, parent_mem_path: str,
                                             stamp_dir: str, *,
                                             source: str) -> str:
        # The image files are hard-linked for speed, but inventory.img is the
        # one file this probe mutates. Break that link before re-encoding.
        shutil.copy2(os.path.join(parent_mem_path, "inventory.img"),
                     os.path.join(stamp_dir, "inventory.img.tmp"))
        inv_path = os.path.join(stamp_dir, "inventory.img")
        os.replace(os.path.join(stamp_dir, "inventory.img.tmp"), inv_path)
        uptime_us = int(float(open("/proc/uptime").read().split()[0]) * 1000000)
        new_uptime = uptime_us + 10 * 1000000
        old = self._restamp_inventory_dump_uptime_pycriu(inv_path, new_uptime)
        print(f"[PROBE] restamped parent inventory {parent_mem_path} "
              f"dump_uptime {old}->{new_uptime} via {stamp_dir} "
              f"source={source}")
        return stamp_dir

    def _restamp_inventory_dump_uptime_pycriu(self, inv_path: str,
                                              new_uptime: int) -> Optional[int]:
        """Patch inventory_entry.dump_uptime using CRIU's official pycriu."""
        os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
        from pycriu import images as criu_images  # type: ignore

        with open(inv_path, "rb") as f:
            img = criu_images.load(f)
        entries = img.get("entries", [])
        if not entries:
            raise RuntimeError("inventory has no entries")
        old = entries[0].get("dump_uptime")
        entries[0]["dump_uptime"] = new_uptime
        tmp_path = inv_path + ".encoded"
        with open(tmp_path, "wb") as f:
            criu_images.dump(img, f)
        os.replace(tmp_path, inv_path)
        return old

    def _bootstrap_active_before_dump(self, snapshot_id: str) -> tuple[Optional[int], float, bool]:
        """Move active off PID-ns init before the first durable dump in a chain."""
        if self.fixed_active_pid is not None:
            if self._is_pidns_init(self.agent_pid):
                print(f"[Controller] WARN: fixed-active mode requires "
                      f"namespace_launcher to start agent at PID "
                      f"{self.fixed_active_pid}; current agent is pidns init")
            return None, 0.0, False
        fresh_pidns_active = os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1"
        if not (self.enable_warm_template
                and self.agent_pid == self.ns_init_pid
                and (self.checkpoint_stash_template or fresh_pidns_active)):
            return None, 0.0, False

        rss_mb = _read_rss_mb(self.agent_pid)
        print(f"[PERF] Agent RSS at pre-dump bootstrap: {rss_mb:.1f} MB "
              f"(pid={self.agent_pid})")
        t_f0 = time.time()
        parent_pid, child_pid = self.template_pool.request_fork(
            self.agent_pid, snapshot_id, timeout=2.0)
        fork_ms = (time.time() - t_f0) * 1000
        if parent_pid is not None and child_pid is not None:
            self.agent_pid = child_pid
            self._clear_soft_dirty_after_fork(child_pid)
            print(f"[PERF] Warm-template bootstrap-fork(pre-dump)={fork_ms:.2f}ms "
                  f"(template={parent_pid}, active={child_pid}, rss={rss_mb:.1f}MB)")
            return parent_pid, fork_ms, True

        print(f"[Controller] Warm-template pre-dump bootstrap timed out; "
              f"snapshot {snapshot_id} will use slow restore path")
        return None, fork_ms, True

    def checkpoint_action(self, parent_ckpt_id: str, tag: str,
                          raw_command: str = "",
                          replay_worker_ops: Optional[List[dict]] = None) -> dict:
        if getattr(self, "_async_incremental", None) is not None:
            return self._async_incremental.checkpoint(
                parent_ckpt_id, tag, raw_command, replay_worker_ops)
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

        # Phase 0: Pre-dump for hot memory regions
        if strategy == "predump":
            predump_dir = os.path.join(self.snapshot_store, f"{tag}_{new_id}_predump")
            os.makedirs(predump_dir, exist_ok=True)
            predump_cmd = [
                self.criu_dump_bin, "pre-dump",
                "--tree", str(dump_tree_pid),
                "-D", predump_dir,
                "--track-mem",
            ]
            self._append_external_pidns(predump_cmd, dump_tree_pid)
            if self._last_predump_dir and self._last_predump_tree_pid == dump_tree_pid:
                rel = os.path.relpath(self._last_predump_dir, predump_dir)
                predump_cmd.extend(["--prev-images-dir", rel])
            pd_t0 = time.time()
            subprocess.check_call(predump_cmd)
            predump_ms = (time.time() - pd_t0) * 1000
            try:
                os.kill(dump_tree_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            self._last_predump_dir = predump_dir
            self._last_predump_tree_pid = dump_tree_pid
            print(f"[PERF] Pre-dump: {predump_ms:.2f}ms")

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
        can_prefork_template_dump = (
            self.prefork_template_dump
            and self.enable_warm_template
            and strategy != "lightweight"
            and self.template_pool is not None
            and not bootstrap_attempted_before_dump
        )
        if can_prefork_template_dump:
            for tpid in list(self.template_pool.templates.values()):
                if tpid == self.agent_pid:
                    continue
                try:
                    os.kill(tpid, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
            rss_mb = _read_rss_mb(self.agent_pid)
            print(f"[PERF] Agent RSS at pre-dump template fork: {rss_mb:.1f} MB "
                  f"(pid={self.agent_pid})")
            t_f0 = time.time()
            parent_pid, child_pid = self.template_pool.request_fork(
                self.agent_pid, new_id, timeout=2.0,
                clone_parent_active=True)
            pre_dump_fork_ms = (time.time() - t_f0) * 1000
            if parent_pid is not None and child_pid is not None:
                pre_dump_template_pid = parent_pid
                template_pid = parent_pid
                fork_ms = pre_dump_fork_ms
                self.agent_pid = child_pid
                dump_tree_pid = parent_pid
                self._clear_soft_dirty_after_fork(child_pid)
                print(f"[PERF] Warm-template prefork-fork={fork_ms:.2f}ms "
                      f"(template={template_pid}, active={child_pid}, "
                      f"rss={rss_mb:.1f}MB)")
            else:
                msg = (f"Warm-template prefork-fork failed for {new_id}; "
                       "detached template dump would silently fall back to "
                       "active dump")
                print(f"[Controller] {msg}")
                if (os.environ.get(
                        "DELTABOX_PREFORK_TEMPLATE_DUMP_ALLOW_FALLBACK")
                        != "1"):
                    raise RuntimeError(msg)
        can_fixed_dump_clone = (
            self._need_fixed_dump_clone(strategy)
            and not self.prefork_template_dump
            and not self.async_template_full_dump
            and not bootstrap_attempted_before_dump
        )
        if can_fixed_dump_clone:
            if not self._wait_fixed_active_slot_free():
                raise RuntimeError(
                    f"fixed dump PID {self.fixed_active_pid} is not free")
            rss_mb = _read_rss_mb(self.agent_pid)
            print(f"[PERF] Agent RSS at fixed-dump clone: {rss_mb:.1f} MB "
                  f"(active={self.agent_pid}, dump_ns_pid={self.fixed_active_pid})")
            t_clone0 = time.time()
            fixed_dump_clone_pid = self.template_pool.request_stash_template(
                self.agent_pid, None, timeout=2.0, fresh_pidns=False,
                setsid_template=True, fixed_template_pid=self.fixed_active_pid)
            fixed_dump_clone_ms = (time.time() - t_clone0) * 1000
            if fixed_dump_clone_pid is None:
                raise RuntimeError(
                    f"fixed dump clone set_tid={self.fixed_active_pid} failed")
            dump_tree_pid = fixed_dump_clone_pid
            print(f"[PERF] Fixed-dump clone={fixed_dump_clone_ms:.2f}ms "
                  f"(dump_root={fixed_dump_clone_pid}, active={self.agent_pid}, "
                  f"rss={rss_mb:.1f}MB)")
        if (self.async_template_full_dump
                and self.enable_warm_template
                and strategy != "lightweight"
                and self.template_pool is not None):
            if self._pid_gone_or_zombie(self.agent_pid):
                raise RuntimeError(
                    f"async-template active pid {self.agent_pid} is gone")
            for tpid in list(self.template_pool.templates.values()):
                if tpid == self.agent_pid:
                    continue
                try:
                    os.kill(tpid, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
            rss_mb = _read_rss_mb(self.agent_pid)
            print(f"[PERF] Agent RSS at async-template stash: {rss_mb:.1f} MB "
                  f"(pid={self.agent_pid})")
            t_warm0 = time.time()
            async_template_warm_pid = self.template_pool.request_stash_template(
                self.agent_pid, new_id, timeout=2.0, fresh_pidns=False,
                clear_soft_dirty_template=self.fixed_active_pid is not None)
            warm_stash_ms = (time.time() - t_warm0) * 1000
            if async_template_warm_pid is None:
                raise RuntimeError(
                    f"async-template warm stash failed for {new_id}")
            t_dump0 = time.time()
            async_template_dump_pid = self.template_pool.request_stash_template(
                self.agent_pid, None, timeout=2.0, fresh_pidns=False,
                setsid_template=True)
            dump_stash_ms = (time.time() - t_dump0) * 1000
            if async_template_dump_pid is None:
                self.template_pool.templates.pop(new_id, None)
                try:
                    os.kill(async_template_warm_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise RuntimeError(
                    f"async-template full-dump stash failed for {new_id}")
            template_pid = async_template_warm_pid
            fork_ms += warm_stash_ms + dump_stash_ms
            dump_tree_pid = async_template_dump_pid
            print(f"[PERF] Warm-template async-stash="
                  f"warm={warm_stash_ms:.2f}ms dump={dump_stash_ms:.2f}ms "
                  f"(warm_template={async_template_warm_pid}, "
                  f"dump_root={async_template_dump_pid}, active={self.agent_pid}, "
                  f"rss={rss_mb:.1f}MB)")
        if strategy == "lightweight":
            # Lightweight is a logical read-only checkpoint: skip CRIU and
            # reuse the effective parent image. OverlayFS sinking remains gated
            # by the normal upper-dirty check; clean read-only actions do not
            # create a layer.
            marker_file = os.path.join(curr_dir, "lightweight_marker.txt")
            with open(marker_file, "w") as f:
                f.write(f"parent={parent_ckpt_id}\naction={tag}\ntimestamp={time.time()}\n")
            print(f"  [LIGHTWEIGHT] File-only snapshot (0 ms CRIU)")
        else:
            cmd = [
                self.criu_dump_bin, "dump", "--tree", str(dump_tree_pid),
                "-D", curr_dir,
                "--shell-job", "--tcp-close",
                "--ext-unix-sk", "--manage-cgroups",
                *self._all_ext_mount_map_args(),
                "--link-remap"
            ]
            if self.enable_incremental_dump:
                cmd.append("--track-mem")
            self._append_external_pidns(cmd, dump_tree_pid)
            if os.environ.get("CRIU_DEBUG"):
                cmd.extend(["-v4"])  # stream verbose to stdout -> /tmp/replay.log

            prev_ckpt_id = None
            prev_rel_parent = None
            parent_mem_path = None
            prev_cross_pid = False
            prev_pid_mode = None
            validation_needed = False
            cross_pid_probe = (
                os.environ.get("DELTABOX_PROBE_CROSS_PID_INCREMENTAL") == "1"
                or os.environ.get("DELTABOX_FRESH_PIDNS_ACTIVE") == "1"
            )
            if (self.enable_incremental_dump
                    and parent_ckpt_id and parent_ckpt_id in self.registry):
                parent_mem_path = self.registry[parent_ckpt_id]['mem_path']
                # Skip lightweight parents for --prev-images-dir
                walk_id = parent_ckpt_id
                parent_mem_path = None
                while walk_id and walk_id in self.registry:
                    entry = self.registry[walk_id]
                    same_tree = entry.get("dump_tree_pid") == dump_tree_pid
                    fixed_pid_lineage = (
                        self.fixed_active_pid is not None
                        and entry.get("fixed_active_pid") == self.fixed_active_pid
                        and entry.get("ns_init_pid") == self.ns_init_pid
                        and entry.get("external_pidns") is True
                    )
                    pid_identity_ok = (
                        same_tree or fixed_pid_lineage or cross_pid_probe)
                    if (entry.get("strategy") != "lightweight"
                            and pid_identity_ok
                            and not entry.get("dump_error")
                            and not entry.get("dump_stats", {}).get("dump_error")):
                        parent_mem_path = entry['mem_path']
                        prev_ckpt_id = walk_id
                        prev_cross_pid = not same_tree
                        if same_tree:
                            prev_pid_mode = "same-host-pid"
                        elif fixed_pid_lineage:
                            prev_pid_mode = "fixed-active-pid"
                        else:
                            prev_pid_mode = "cross-pid-probe"
                        break
                    walk_id = entry.get("parent_id")
                if parent_mem_path:
                    parent_mem_path = self._restamp_parent_inventory_for_checkpoint(
                        prev_ckpt_id, parent_mem_path, curr_dir)
                    prev_rel_parent = os.path.relpath(parent_mem_path, curr_dir)
                    cmd.extend(["--prev-images-dir", prev_rel_parent])
            print(f"[CRIU] dump root={dump_tree_pid} active={self.agent_pid} "
                  f"ns_init={self.ns_init_pid} prev={prev_ckpt_id or '-'} "
                  f"prev_used={prev_rel_parent is not None} "
                  f"prev_cross_pid={prev_cross_pid} "
                  f"prev_pid_mode={prev_pid_mode or '-'}")
            parent_entry = self.registry.get(prev_ckpt_id) if prev_ckpt_id else None
            self._debug_addr_maps(
                f"pre-dump ckpt={new_id} prev={prev_ckpt_id or '-'}",
                [
                    ("dump_root", dump_tree_pid),
                    ("active", self.agent_pid),
                    ("prev_template",
                     parent_entry.get("template_pid") if parent_entry else None),
                    ("prev_dump_root",
                     parent_entry.get("dump_tree_pid") if parent_entry else None),
                ],
            )
            pre_dump_soft_dirty = self._debug_soft_dirty_summary(
                dump_tree_pid,
                f"pre-dump ckpt={new_id} prev={prev_ckpt_id or '-'}")
            if pre_dump_soft_dirty is not None:
                dump_stats["pre_dump_soft_dirty_summary"] = (
                    pre_dump_soft_dirty)
            self._debug_parent_pagemap_chain(
                f"pre-dump ckpt={new_id} prev={prev_ckpt_id or '-'}",
                parent_mem_path,
            )
            if (os.environ.get("DELTABOX_VALIDATE_INCREMENTAL_EQUIV") == "1"
                    and prev_rel_parent is not None
                    and self._last_restore_target_id == prev_ckpt_id):
                validation_needed = True
            cmd.append("--leave-stopped" if (validation_needed
                                             or pre_dump_template_pid
                                             or async_template_dump_pid
                                             or fixed_dump_clone_pid)
                       else "--leave-running")
            if (validation_needed
                    and os.environ.get("DELTABOX_VALIDATE_LIVE_DIGESTS") == "1"):
                try:
                    os.kill(dump_tree_pid, signal.SIGSTOP)
                    self._wait_pid_stopped(dump_tree_pid)
                    validation_pre_live_mem = self._process_memory_digest(
                        dump_tree_pid)
                    print(f"[PROBE] validation pre-dump live digest "
                          f"ckpt={new_id} root={dump_tree_pid} "
                          f"mem={validation_pre_live_mem.get('mem_sha256')} "
                          f"bytes={validation_pre_live_mem.get('mem_bytes')}",
                          flush=True)
                except Exception as e:
                    validation_pre_live_mem = {
                        "ok": False,
                        "err": f"{type(e).__name__}: {e}",
                    }

            # Async submit to single-worker pool. The dump runs in a background
            # thread while the caller (agent loop) returns from save_step and
            # immediately issues the next iter's LLM request — that LLM wait
            # (median ~7-9s) lets CRIU's ~40ms dump run concurrently.
            # NPD owns the HTTPS socket and survives
            # the process freeze, so the in-flight inference is unaffected.
            # The future is gated only at restore_action: the next dump in this
            # tree pool serializes naturally behind it.
            t0 = time.time()
            dump_started_at = t0
            dump_future = self._dump_pool.submit(subprocess.check_call, cmd)
            _trace_event(
                "checkpoint_dump_submitted",
                ckpt_id=new_id,
                parent_id=parent_ckpt_id,
                dump_strategy=strategy,  # don't shadow TRACE_CTX["strategy"] (mode)
                dump_tree_pid=dump_tree_pid,
                agent_pid=self.agent_pid,
                ns_init_pid=self.ns_init_pid,
                prev_ckpt_id=prev_ckpt_id,
                prev_images_dir=prev_rel_parent,
                dump_prev_used=(prev_rel_parent is not None),
                dump_prev_cross_pid=prev_cross_pid,
                dump_prev_pid_mode=prev_pid_mode,
                pre_dump_soft_dirty_summary=pre_dump_soft_dirty,
            )
            def _record_dump(fut, ckpt=new_id, started=t0, ck_dir=curr_dir,
                             disposable_pid=async_template_dump_pid):
                exc = fut.exception()
                ms = (time.time() - started) * 1000
                try:
                    if exc is None:
                        dump_size_bytes = 0
                        try:
                            for _r, _dirs, _fs in os.walk(ck_dir):
                                _dirs[:] = [d for d in _dirs
                                            if d != "_restamped_parent"]
                                for _f in _fs:
                                    try:
                                        dump_size_bytes += os.path.getsize(os.path.join(_r, _f))
                                    except OSError:
                                        pass
                        except OSError:
                            pass
                        print(f"[PERF] Checkpoint ID {ckpt}: CRIU={ms:.2f}ms (async) "
                              f"dump_size={dump_size_bytes/(1024*1024):.2f}MB")
                        pagemap_summary = self._debug_pagemap_summary(
                            ck_dir, f"ckpt={ckpt}")
                        dump_stats.update({
                            "criu_ms": ms,
                            "dump_completed_ms": ms,
                            "dump_size_bytes": dump_size_bytes,
                            "pagemap_summary": pagemap_summary,
                        })
                        entry = self.registry.get(ckpt)
                        if entry is not None:
                            entry["criu_ms"] = ms
                            entry["dump_completed_ms"] = ms
                            entry["dump_size_bytes"] = dump_size_bytes
                            entry["pagemap_summary"] = pagemap_summary
                        _trace_event(
                            "checkpoint_dump_completed",
                            ckpt_id=ckpt,
                            dur_ms=ms,
                            ok=True,
                            dump_size_bytes=dump_size_bytes,
                            pagemap_summary=pagemap_summary,
                        )
                    else:
                        print(f"[PERF] Checkpoint ID {ckpt}: CRIU FAILED after "
                              f"{ms:.2f}ms (async): {type(exc).__name__}: {exc}")
                        dump_stats.update({
                            "criu_ms": ms,
                            "dump_completed_ms": ms,
                            "dump_error": f"{type(exc).__name__}: {exc}",
                        })
                        _trace_event(
                            "checkpoint_dump_completed",
                            ckpt_id=ckpt,
                            dur_ms=ms,
                            ok=False,
                            err_type=type(exc).__name__,
                            err_msg=str(exc)[:200],
                        )
                finally:
                    if (self.async_template_full_dump and disposable_pid
                            and disposable_pid != self.agent_pid):
                        try:
                            os.kill(disposable_pid, signal.SIGKILL)
                            print(f"[Controller] disposed async dump template "
                                  f"pid={disposable_pid} ckpt={ckpt}")
                        except ProcessLookupError:
                            pass
            dump_future.add_done_callback(_record_dump)
            if validation_needed:
                # Final validation oracle: keep the task frozen after the
                # production incremental dump, dump a no-parent full twin from
                # that same stopped state, then resume.  The validation phase
                # later restores both images with stock CRIU and compares
                # bytes.  This is validation-only and intentionally synchronous.
                try:
                    t_join_0 = time.time()
                    dump_future.result(timeout=30.0)
                    validation_production_join_ms = (time.time() - t_join_0) * 1000
                    if os.environ.get("DELTABOX_VALIDATE_LIVE_DIGESTS") == "1":
                        validation_post_live_mem = self._process_memory_digest(
                            dump_tree_pid)
                        print(f"[PROBE] validation post-incremental live digest "
                              f"ckpt={new_id} root={dump_tree_pid} "
                              f"mem={validation_post_live_mem.get('mem_sha256')} "
                              f"bytes={validation_post_live_mem.get('mem_bytes')}",
                              flush=True)
                    validation_full_twin = self._dump_validation_full_twin(
                        new_id, tag, dump_tree_pid)
                except Exception as e:
                    print(f"[Controller] WARN: validation full-twin setup failed "
                          f"for {new_id}: {type(e).__name__}: {e}",
                          flush=True)
                    dump_stats["validation_full_error"] = (
                        f"{type(e).__name__}: {e}")
                finally:
                    try:
                        if not pre_dump_template_pid and not fixed_dump_clone_pid:
                            os.kill(dump_tree_pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
            if bootstrap_attempted_before_dump and template_pid is None:
                try:
                    dump_future.result(timeout=30.0)
                except Exception as e:
                    print(f"[Controller] WARN: bootstrap-fallback dump failed for "
                          f"{new_id}: {type(e).__name__}: {e}")
                    dump_stats["dump_error"] = f"{type(e).__name__}: {e}"

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
            if dump_future is not None:
                try:
                    t_join_0 = time.time()
                    dump_future.result(timeout=30.0)
                    pre_template_dump_join_ms = (time.time() - t_join_0) * 1000
                    print(f"[PERF] pre-template-dump-join="
                          f"{pre_template_dump_join_ms:.2f}ms "
                          f"ckpt={new_id}")
                except Exception as e:
                    print(f"[Controller] WARN: pre-fork dump join failed for "
                          f"{new_id}: {type(e).__name__}: {e}; "
                          f"trying full durable fallback")
                    fallback_reason = f"{type(e).__name__}: {e}"
                    try:
                        fallback_stats = self._dump_full_fallback_image(
                            new_id, curr_dir, dump_tree_pid, fallback_reason)
                        dump_stats.update(fallback_stats)
                        dump_stats.pop("dump_error", None)
                        dump_future = None
                        prev_ckpt_id = None
                        prev_rel_parent = None
                        parent_mem_path = None
                        prev_cross_pid = False
                        prev_pid_mode = "full-fallback"
                    except Exception as fallback_exc:
                        print(f"[Controller] WARN: full durable fallback failed "
                              f"for {new_id}: {type(fallback_exc).__name__}: "
                              f"{fallback_exc}; warm-template fork will be "
                              f"skipped")
                        dump_stats["dump_error"] = (
                            f"{type(fallback_exc).__name__}: {fallback_exc}")
                        skip_warm_template = True
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

        info = {
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

    def _collect_subtree_pids(self, root_pid: int,
                              exclude_pids: Optional[set[int]] = None) -> list[int]:
        exclude_pids = exclude_pids or set()
        to_visit = [root_pid]
        seen: set[int] = set()
        found: list[int] = []
        while to_visit:
            pid = to_visit.pop()
            if pid in seen or pid in exclude_pids:
                continue
            seen.add(pid)
            found.append(pid)
            try:
                tids = os.listdir(f"/proc/{pid}/task")
            except OSError:
                continue
            for tid in tids:
                try:
                    with open(f"/proc/{pid}/task/{tid}/children") as f:
                        for child_str in f.read().split():
                            try:
                                to_visit.append(int(child_str))
                            except ValueError:
                                pass
                except OSError:
                    continue
        return found

    def _child_pids(self, pid: int) -> list[int]:
        children: list[int] = []
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            return children
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/children") as f:
                    for child_str in f.read().split():
                        try:
                            children.append(int(child_str))
                        except ValueError:
                            pass
            except OSError:
                continue
        return children

    def _collect_descendant_pids(self, root_pid: int) -> list[int]:
        descendants: list[int] = []
        for child in self._child_pids(root_pid):
            descendants.extend(self._collect_subtree_pids(child))
        return descendants

    def _pid_gone_or_zombie(self, pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/stat") as f:
                # comm is parenthesized and can itself contain spaces or ')'.
                _, separator, tail = f.read().rpartition(")")
            fields = tail.split()
            return bool(separator and fields and fields[0] == "Z")
        except OSError:
            return True

    def _pid_ppid(self, pid: int) -> Optional[int]:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
                return None
        return None

    def _pid_nspids(self, pid: int) -> list[int]:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("NSpid:"):
                        return [int(x) for x in line.split()[1:]]
        except (OSError, ValueError):
            pass
        return []

    def _translate_pid_in_ns(self, target_ns_pid: int,
                             ns_owner_pid: int) -> Optional[int]:
        try:
            ns_inode = os.readlink(f"/proc/{ns_owner_pid}/ns/pid")
        except OSError:
            return None
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                host_pid = int(entry)
                if os.readlink(f"/proc/{entry}/ns/pid") != ns_inode:
                    continue
                nspids = self._pid_nspids(host_pid)
                if nspids and nspids[-1] == target_ns_pid:
                    return host_pid
            except (OSError, ValueError):
                continue
        return None

    def _pid_as_seen_by(self, pid: int, ns_owner_pid: int) -> int:
        target = self._pid_nspids(pid)
        owner = self._pid_nspids(ns_owner_pid)
        if owner and len(target) >= len(owner):
            return target[len(owner) - 1]
        return pid

    def _wait_pids_gone_or_zombie(self, pids: list[int],
                                  timeout: float = 1.0) -> None:
        remaining = wait_for_process_exit(pids, timeout, self._pid_gone_or_zombie)
        if remaining:
            print(f"[Warning] timed out waiting for killed pids to exit: "
                  f"{sorted(remaining)[:8]}")

    def _wait_fixed_active_slot_free(self, timeout: float = None) -> bool:
        if not self.fixed_active_pid:
            return True
        if timeout is None:
            # A heavier multi-threaded agent can take longer to fully die and be
            # reaped (freeing the fixed in-ns PID) than the synthetic harness.
            # Tunable so a real-agent run can widen it without a code change.
            try:
                timeout = float(os.environ.get("DELTABOX_FIXED_SLOT_TIMEOUT_S", "1.0"))
            except ValueError:
                timeout = 1.0
        deadline = time.time() + timeout
        while time.time() < deadline:
            holder = self._translate_pid_in_ns(
                self.fixed_active_pid, self.ns_init_pid)
            if holder is None:
                return True
            time.sleep(0.005)
        holder = self._translate_pid_in_ns(
            self.fixed_active_pid, self.ns_init_pid)
        print(f"[Controller] WARN: fixed active PID slot "
              f"{self.fixed_active_pid} still occupied by host pid {holder}")
        return False

    def _dispose_fixed_dump_clone(self, pid: int, ckpt_id: str) -> None:
        """Kill and reap a disposable PID-100 dump clone.

        set_tid dump clones are intentionally not the active worker. After CRIU
        finishes, leaving even a zombie behind keeps the in-namespace PID 100
        occupied and can block the next checkpoint-side set_tid. Reap via the
        clone's actual parent when possible.
        """
        parent_pid = self._pid_ppid(pid)
        parent_child_pid = (
            self._pid_as_seen_by(pid, parent_pid)
            if parent_pid is not None else pid)
        active_pid = self.agent_pid
        active_stopped = False
        try:
            if (active_pid != parent_pid
                    and os.path.isdir(f"/proc/{active_pid}")):
                try:
                    os.kill(active_pid, signal.SIGSTOP)
                    active_stopped = True
                except ProcessLookupError:
                    active_stopped = False
                except OSError as e:
                    print(f"[Controller] WARN: active SIGSTOP before "
                          f"fixed-clone reap failed: {e}")
                    active_stopped = False
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._wait_pids_gone_or_zombie([pid], timeout=1.0)
            if (self.template_pool is not None and parent_pid is not None
                    and os.path.isdir(f"/proc/{parent_pid}")):
                try:
                    reaped = self.template_pool.request_reap_pid(
                        parent_pid, parent_child_pid, timeout=1.0)
                    if not reaped:
                        reaped = self.template_pool.request_reap_children(
                            parent_pid, timeout=1.0)
                    print(f"[Controller] disposed fixed dump clone pid={pid} "
                          f"ckpt={ckpt_id} parent={parent_pid} "
                          f"child={parent_child_pid} reaped={reaped}")
                except Exception as e:
                    print(f"[Controller] WARN: fixed dump clone reap failed "
                          f"pid={pid} parent={parent_pid}: "
                          f"{type(e).__name__}: {e}")
            else:
                print(f"[Controller] disposed fixed dump clone pid={pid} "
                      f"ckpt={ckpt_id}")
        finally:
            if active_stopped and os.path.isdir(f"/proc/{active_pid}"):
                try:
                    os.kill(active_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        self._wait_fixed_active_slot_free(timeout=1.0)

    def _join_active_dump_before_kill(self) -> float:
        """Do not kill the active tree while its checkpoint dump is in flight."""
        ckpt_id = self._active_dump_owner_id
        if not ckpt_id:
            return 0.0
        entry = self.registry.get(ckpt_id)
        if not entry:
            return 0.0
        if entry.get("async_dump_template_pid") and entry["async_dump_template_pid"] != self.agent_pid:
            # A detached writer owns an independent frozen address space. Its
            # lifetime is protected separately by _kill_active_subtree.
            return 0.0
        dump_future = entry.get("dump_future")
        if dump_future is None or dump_future.done():
            return 0.0
        t0 = time.time()
        try:
            dump_future.result(timeout=30.0)
        except Exception as e:
            print(f"[Controller] active dump {ckpt_id} failed before kill: "
                  f"{type(e).__name__}: {e}")
        elapsed_ms = (time.time() - t0) * 1000
        print(f"[PERF] active-dump-prekill-join={elapsed_ms:.2f}ms "
              f"ckpt={ckpt_id} dump_root={entry.get('dump_tree_pid')} "
              f"active={self.agent_pid}")
        return elapsed_ms

    def _kill_pids_and_wait(self, pids: list[int], label: str,
                            during_exit: Optional[Callable[[], None]] = None) -> None:
        # Kill leaves before roots to avoid a live child escaping under a dying
        # pid-ns init during fresh-pidns rollback churn.
        for pid in reversed(pids):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[Warning] {label} kill {pid}: {e}")
        for pid in pids:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError):
                pass
        try:
            if during_exit is not None:
                during_exit()
        finally:
            # Independent preparation may run while the kernel releases the
            # victims' address spaces. Even if preparation fails, finish the
            # existing exit wait before propagating that error to the caller.
            self._wait_pids_gone_or_zombie(pids)

    def _kill_template_descendants(self, template_pid: int,
                                   keep_pids: Optional[set[int]] = None) -> float:
        keep = set(keep_pids or set())
        template_pids = set(self.template_pool.templates.values()) \
            if self.template_pool else set()
        keep.update(template_pids)
        keep.add(template_pid)
        reap_ms = 0.0
        descendants = self._collect_descendant_pids(template_pid)
        stale_descendants = [pid for pid in descendants if pid not in keep]
        preserved_templates = [pid for pid in descendants if pid in template_pids]
        if stale_descendants:
            reaper_pids: set[int] = set()
            for pid in stale_descendants:
                ppid = self._pid_ppid(pid)
                if ppid in template_pids or ppid == template_pid:
                    reaper_pids.add(ppid)
            reaper_pids.add(template_pid)
            print(f"[Controller] Killing {len(stale_descendants)} stale "
                  f"non-template descendant(s) under template {template_pid}: "
                  f"{stale_descendants[:8]} "
                  f"(preserving templates={preserved_templates[:8]})")
            self._kill_pids_and_wait(stale_descendants, "template-descendant")
            if self.template_pool is not None:
                for reaper_pid in sorted(reaper_pids):
                    if self._pid_gone_or_zombie(reaper_pid):
                        continue
                    t_reap = time.time()
                    reaped = self.template_pool.request_reap_children(
                        reaper_pid, timeout=1.0)
                    this_ms = (time.time() - t_reap) * 1000
                    reap_ms += this_ms
                    print(f"[Controller] template {reaper_pid} reaped stale "
                          f"children={reaped} reap_ms={this_ms:.2f}")
            if self.template_pool is not None:
                killed = set(stale_descendants)
                stale_ids = [sid for sid, pid in self.template_pool.templates.items()
                             if pid in killed or self._pid_gone_or_zombie(pid)]
                for sid in stale_ids:
                    del self.template_pool.templates[sid]
                if stale_ids:
                    print(f"[Controller] Dropped {len(stale_ids)} dead template(s): "
                          f"{stale_ids[:8]}")
        return reap_ms

    def _queue_template_cleanup(self, template_pid: int,
                                dead_child_pid: int,
                                dead_child_host_pid: int) -> None:
        if template_pid and dead_child_pid:
            self._pending_cleanup_reaps.setdefault(template_pid, set()).add(
                (dead_child_pid, dead_child_host_pid))

    def _check_external_restore_pool_clear_allowed(self) -> None:
        """Validate the destructive cold-restore policy without changing state."""
        allow_clear = (
            os.environ.get("DELTABOX_FORCE_CRIU_RESTORE") == "1"
            or os.environ.get("DELTABOX_ALLOW_COLD_RESTORE_POOL_CLEAR") == "1"
        )
        if not allow_clear:
            raise DumpUnavailableError(
                "<external-restore>",
                None,
                RuntimeError(
                    "external-pidns cold restore would clear warm-template "
                    "pool; set DELTABOX_FORCE_CRIU_RESTORE=1 for validation "
                    "or DELTABOX_ALLOW_COLD_RESTORE_POOL_CLEAR=1 explicitly"
                ),
            )

    def _clear_templates_for_external_restore(self) -> None:
        """Free PID slots before restoring an external-pidns leaf image.

        Async full-dump checkpoints dump a frozen leaf template while the
        namespace init stays external.  A cold CRIU restore of that image must
        allocate the same ns-local PIDs inside the existing namespace.  Any
        surviving warm templates from the old pool can therefore collide with
        the restored image ("Can't fork for N: File exists").  Warm restore
        never calls this; it is only for the cold durable-image path.
        This intentionally tears down the warm-template pool, so keep it behind
        an explicit cold-restore validation gate.  Production eviction should
        learn to clear only the exact conflicting PID slots instead of taking
        this broad hammer.
        """
        self._check_external_restore_pool_clear_allowed()
        template_pids = {
            pid for pid in self.template_pool.templates.values()
            if pid and pid != self.ns_init_pid
        } if self.template_pool is not None else set()
        # Exit/pidfd readiness includes zombies. CRIU needs the old PID slots
        # released by waitpid, including active descendants reparented to init.
        # Keep this stronger barrier cold-only; warm restore retains its own
        # template-parent reap protocol and does not scan/poll here.
        to_reap = set(getattr(self, "_last_killed_active_pids", []))
        to_reap.update(template_pids)
        if getattr(self, "agent_pid", None) != self.ns_init_pid:
            to_reap.add(self.agent_pid)
        to_kill: list[int] = []
        seen: set[int] = set()
        for pid in sorted(template_pids):
            if self._pid_gone_or_zombie(pid):
                continue
            for sub_pid in self._collect_subtree_pids(pid):
                if sub_pid == self.ns_init_pid or sub_pid in seen:
                    continue
                seen.add(sub_pid)
                to_kill.append(sub_pid)
        to_reap.update(to_kill)
        if to_kill:
            print(f"[Controller] external restore clearing "
                  f"{len(to_kill)} warm-template pid(s): {to_kill[:12]}")
            self._kill_pids_and_wait(to_kill, "external-restore-template")
        if self.template_pool is not None:
            self.template_pool.templates.clear()
        self._pending_cleanup_reaps.clear()
        # In fixed-active mode ns-init is namespace_launcher's dedicated
        # waitpid loop, not an agent/template and has no template FIFO reader.
        # Sending it that protocol yields ENXIO (or wakes the wrong reader).
        if (self.template_pool is not None and not self.fixed_active_pid
                and os.path.isdir(f"/proc/{self.ns_init_pid}")):
            reaped = self.template_pool.request_reap_children(
                self.ns_init_pid, timeout=0.2)
            print(f"[Controller] external restore ns-init reap "
                  f"{self.ns_init_pid}: {reaped}")
        self._wait_external_restore_reaped(to_reap)

    def _wait_external_restore_reaped(self, pids, timeout: float = 1.0) -> None:
        """Cold-only barrier: known old tasks must be reaped before CRIU forks.

        Do not infer a free PID from an unreadable ns symlink or state Z.
        Only disappearance of the known host task's stat file proves its PID
        slot was released. No signals are sent here, including on timeout.
        """
        pending = {pid for pid in pids if pid and pid != self.ns_init_pid}
        deadline = time.monotonic() + timeout
        while pending:
            for pid in list(pending):
                try:
                    with open(f"/proc/{pid}/stat") as stream:
                        stream.read()
                except FileNotFoundError:
                    pending.remove(pid)
                except OSError:
                    # Permission/IO failure is not evidence that PID is free.
                    pass
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.001, remaining))
        if pending:
            diagnostics = {}
            for pid in sorted(pending | {self.ns_init_pid}):
                record = {}
                for name in ("stat", "status"):
                    try:
                        with open(f"/proc/{pid}/{name}") as stream:
                            record[name] = stream.read()
                    except OSError as exc:
                        record[name] = f"{type(exc).__name__}: {exc}"
                try:
                    record["pid_namespace"] = os.readlink(f"/proc/{pid}/ns/pid")
                except OSError as exc:
                    record["pid_namespace"] = f"{type(exc).__name__}: {exc}"
                diagnostics[str(pid)] = record
            print("[Controller] external restore unreaped PID diagnostics: "
                  + json.dumps(diagnostics, sort_keys=True), flush=True)
            _trace_event("external_restore_reap_timeout", pids=sorted(pending),
                         ns_init_pid=self.ns_init_pid, diagnostics=diagnostics)
            raise RuntimeError(
                f"external restore blocked by unreaped host PIDs {sorted(pending)}; "
                "refusing to let CRIU reuse their PID slots")

    def _drain_pending_template_cleanup(self) -> float:
        if not self._pending_cleanup_reaps:
            return 0.0
        pending = {parent: set(children)
                   for parent, children in self._pending_cleanup_reaps.items()}
        self._pending_cleanup_reaps.clear()
        active_pid = self.agent_pid
        try:
            os.kill(active_pid, signal.SIGSTOP)
        except ProcessLookupError:
            active_pid = None
        except OSError as e:
            print(f"[Controller] WARN: active SIGSTOP before cleanup failed: {e}")
            active_pid = None
        t0 = time.time()
        total_ms = 0.0
        total_reqs = 0
        try:
            if self.template_pool is not None:
                for template_pid, child_pids in sorted(pending.items()):
                    if self._pid_gone_or_zombie(template_pid):
                        continue
                    for child_pid, child_host_pid in sorted(child_pids):
                        t_reap = time.time()
                        reaped = self.template_pool.request_reap_pid(
                            template_pid, child_pid, timeout=0.05)
                        this_ms = (time.time() - t_reap) * 1000
                        total_ms += this_ms
                        total_reqs += 1
                        print(f"[Controller] template {template_pid} "
                              f"reap_pid child={child_pid} "
                              f"host_child={child_host_pid} reaped={reaped} "
                              f"reap_ms={this_ms:.2f}")
        finally:
            if active_pid is not None and os.path.isdir(f"/proc/{active_pid}"):
                try:
                    os.kill(active_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        elapsed_ms = (time.time() - t0) * 1000
        print(f"[PERF] checkpoint-cleanup-drain={elapsed_ms:.2f}ms "
              f"roots={len(pending)} reaps={total_reqs} "
              f"reap={total_ms:.2f}ms")
        return elapsed_ms

    def _kill_active_subtree(self, during_exit: Optional[Callable[[], None]] = None):
        """SIGKILL self.agent_pid and every descendant, EXCEPT processes
        owned by the warm-template pool or an unfinished async dump. Used by
        restore_action so the fast-path can fork from a surviving template
        on the next iteration. Templates are SIGSTOPped where the
        warm-template fork left them and have no descendants of their own,
        so excluding them by PID is sufficient.
        """
        template_pids = set(self.template_pool.templates.values()) \
            if self.template_pool else set()
        self._join_active_dump_before_kill()

        # stash_template replies before its double-fork helper exits. A dump
        # clone can therefore still be active -> helper -> dump_root here.
        # It has no warm-pool entry (snapshot_id=None); relying on reparenting
        # to have finished lets restore SIGKILL the process CRIU still owns.
        # Exclude the whole live dump subtree without joining its background
        # work. Completed dumps remain disposable by the existing callback.
        protected_pids = set(template_pids)
        for checkpoint in self.registry.values():
            dump_pid = checkpoint.get("async_dump_template_pid")
            future = checkpoint.get("dump_future")
            if dump_pid and future is not None and not future.done():
                protected_pids.add(dump_pid)

        # Walk DOWN from agent_pid using /proc/<pid>/task/<tid>/children
        # (CONFIG_PROC_CHILDREN=y). Only visits agent's actual subtree —
        # typically 1-3 processes — instead of reading every /proc/<pid>/stat
        # to build a global ppid map. The previous global-ppid approach scaled
        # with system process count and reached ~200 ms once dozens of
        # SIGSTOPped warm templates were sitting in /proc on long replays.
        to_kill = self._collect_subtree_pids(self.agent_pid, protected_pids)
        self._last_killed_active_pids = list(to_kill)
        reaper = self._pid_ppid(self.agent_pid)
        reaper_child_pid = (self._pid_as_seen_by(self.agent_pid, reaper)
                            if reaper is not None else self.agent_pid)
        dead_host_pid = self.agent_pid
        prepare_error = None
        def prepare_before_wait():
            nonlocal prepare_error
            try:
                during_exit()
            except BaseException as error:
                # Keep cleanup identical to the previous serial ordering:
                # even failed preparation must not strand a fixed-PID zombie.
                prepare_error = error

        if during_exit is None:
            self._kill_pids_and_wait(to_kill, "_kill_active_subtree")
        else:
            self._kill_pids_and_wait(to_kill, "_kill_active_subtree", prepare_before_wait)

        # When the next op is a set_tid restore fork that re-occupies the fixed
        # in-ns PID, the just-killed active must be REAPED now: a SIGKILL'd child
        # lingers as a zombie holding that PID until its parent waitpid()s it,
        # and the parent here is a SIGSTOP'd template that cannot reap on its own.
        # Async queuing (_queue_template_cleanup) reaps too late and the set_tid
        # fork then collides on the occupied PID (hang / EEXIST). Reap inline so
        # incremental dumps survive across a warm-fork restore.
        need_fixed_slot = (bool(self.fixed_active_pid)
                           and not self.restore_fastfork_dump_pid)
        reaped_inline = False
        is_reapable_template = (self.template_pool is not None
                                and reaper in template_pids
                                and dead_host_pid in to_kill)
        if (need_fixed_slot and is_reapable_template
                and self._translate_pid_in_ns(
                    self.fixed_active_pid, self.ns_init_pid) is not None):
            try:
                r = self.template_pool.request_reap_pid(
                    reaper, reaper_child_pid, timeout=1.0)
                if not r:
                    r = self.template_pool.request_reap_children(
                        reaper, timeout=1.0)
                reaped_inline = r is not None
                print(f"[Controller] inline-reaped dead active via template "
                      f"{reaper} to free fixed PID {self.fixed_active_pid}: {r}")
            except Exception as e:
                print(f"[Controller] WARN: inline reap of dead active failed: {e}")

        self._wait_fixed_active_slot_free()
        if not reaped_inline and is_reapable_template:
            self._queue_template_cleanup(
                reaper, reaper_child_pid, dead_host_pid)
        if prepare_error is not None:
            raise prepare_error

    def restore_action(self, target_ckpt_id: str, *, allow_cold_fallback: bool = True):
        restore_api_t0 = time.time()
        if target_ckpt_id not in self.registry:
            raise ValueError(f"Unknown ID: {target_ckpt_id}")

        target = self.registry[target_ckpt_id]
        self._last_restore_target_id = target_ckpt_id

        # Handle lightweight checkpoints: redirect to the effective parent with full CRIU image
        effective_id = target.get("effective_restore_id", target_ckpt_id)
        replay_cmds = target.get("replay_cmds", [])

        template_pid = self.template_pool.get(effective_id) if self.template_pool else None
        if template_pid is not None and os.environ.get("DELTABOX_FORCE_CRIU_RESTORE") == "1":
            print(f"[Controller] DELTABOX_FORCE_CRIU_RESTORE=1: "
                  f"ignoring warm template {template_pid} for {effective_id}")
            template_pid = None

        # A live-only checkpoint has no durable image to fall back to. Fail
        # before rolling back root overlays or killing the current active.
        if template_pid is None and not allow_cold_fallback:
            raise RuntimeError(
                f"Restore {effective_id} requires its live template; "
                "durable fallback is prohibited")

        if effective_id != target_ckpt_id:
            print(f"[ADAPTIVE] Lightweight node '{target_ckpt_id}' → "
                  f"physical restore to '{effective_id}', replay {len(replay_cmds)} cmd(s)")
            physical_target = self.registry[effective_id]
        else:
            physical_target = target

        dump_join_ms = 0.0

        def _join_durable_dump(reason: str) -> None:
            nonlocal dump_join_ms
            dump_future = physical_target.get("dump_future")
            if dump_future is None:
                return
            t_wait_0 = time.time()
            already_done = dump_future.done()
            try:
                dump_future.result(timeout=30.0)
            except FutTimeout as e:
                wait_ms = (time.time() - t_wait_0) * 1000
                print(f"[Controller] Dump for {effective_id} still running after "
                      f"{wait_ms:.1f}ms wait; treating as unavailable")
                _trace_event(
                    "checkpoint_dump_join",
                    ckpt_id=effective_id,
                    wait_ms=wait_ms,
                    already_done=False,
                    ok=False,
                    err_type="TimeoutError",
                )
                raise DumpUnavailableError(
                    effective_id, physical_target.get("parent_id"), e)
            except Exception as e:
                wait_ms = (time.time() - t_wait_0) * 1000
                print(f"[Controller] Dump for {effective_id} failed "
                      f"({wait_ms:.1f}ms wait): {type(e).__name__}: {e}")
                _trace_event(
                    "checkpoint_dump_join",
                    ckpt_id=effective_id,
                    wait_ms=wait_ms,
                    already_done=already_done,
                    ok=False,
                    err_type=type(e).__name__,
                )
                raise DumpUnavailableError(
                    effective_id, physical_target.get("parent_id"), e)
            wait_ms = (time.time() - t_wait_0) * 1000
            dump_join_ms += wait_ms
            _trace_event(
                "checkpoint_dump_join",
                ckpt_id=effective_id,
                wait_ms=wait_ms,
                already_done=already_done,
                ok=True,
                reason=reason,
            )
            if wait_ms > 1.0:
                print(f"[PERF] Restore ID {effective_id}: "
                      f"dump-join={wait_ms:.2f}ms reason={reason}")

        external_pidns_restore = None
        if template_pid is None:
            # Cold restore consumes the durable image, so it must wait for the
            # async spill. Warm restore reads only the pristine warm template
            # and must never be blocked by a disposable dump template.
            _join_durable_dump("cold-restore")

            dump_error = (physical_target.get("dump_error")
                          or physical_target.get("dump_stats", {}).get("dump_error"))
            if dump_error:
                raise DumpUnavailableError(
                    effective_id,
                    physical_target.get("parent_id"),
                    RuntimeError(f"durable dump unavailable: {dump_error}"),
                )
            # Reject a known cold restore before changing the current sandbox.
            # Keep the gate at the destructive operation too, for warm-fork
            # failures that only discover their cold fallback after dispatch.
            external_pidns_restore = self._is_external_pidns_dump(physical_target)
            if external_pidns_restore:
                self._check_external_restore_pool_clear_allowed()

        namespace_drain_ms = 0.0
        if (template_pid is None and not external_pidns_restore
                and getattr(self, "_async_incremental", None) is not None):
            drain_start = time.perf_counter()
            self._async_incremental.drain_before_namespace_teardown()
            namespace_drain_ms = (time.perf_counter() - drain_start) * 1000
        active_dump_join_ms = self._join_active_dump_before_kill()

        # Roll back root coverage only after known cold prerequisites pass,
        # and the current active dump can no longer block the transition.
        # The restored process must resume against the saved files.
        if self.root_overlays is not None and self.root_overlays.has_checkpoint(effective_id):
            self.root_overlays.restore(effective_id)

        print(f"[Controller] Restoring to {effective_id}...")

        # Reap only the current active agent + its non-template descendants.
        # The previous "kill ns_init_pid" approach killed every process in the
        # PID ns, including ns_init_pid itself — but ns_init_pid is the very
        # first warm-template (the launcher's unshare-fork child becomes
        # template[ckpt0] after the first ckpt's fork). All subsequent
        # templates also live in this ns. Killing ns_init wiped them all,
        # forcing every restore down the slow CRIU path. We now (a) walk the
        # process tree from agent_pid to find subprocess grandchildren the
        # active spawned (e.g., shell commands), (b) skip any PID currently
        # registered in template_pool, and (c) SIGKILL the rest. Templates
        # remain SIGSTOPped where the warm-template fork left them, so they
        # cannot have spawned descendants of their own.
        target_layers = physical_target['layers']
        restore_id = str(uuid.uuid4())[:6]
        new_upper = os.path.join(self.layers_root, f"restore_{restore_id}_upper")
        new_work = os.path.join(self.layers_root, f"restore_{restore_id}_work")
        rss_mb = -1.0
        prepare_ms = 0.0

        def prepare_restore():
            nonlocal rss_mb, prepare_ms
            t_prepare = time.perf_counter()
            # These are new backing directories, not the active overlay.
            # Do not switch layers or wake a template until the exit wait
            # and any fixed-PID reaping below have both finished.
            os.makedirs(new_upper, exist_ok=True)
            os.makedirs(new_work, exist_ok=True)
            if template_pid is not None:
                rss_mb = _read_rss_mb(template_pid)
            prepare_ms = (time.perf_counter() - t_prepare) * 1000

        t_kill_active_0 = time.time()
        self._kill_active_subtree(during_exit=prepare_restore)
        kill_active_ms = (time.time() - t_kill_active_0) * 1000
        ovl_time = 0.0
        component_t0 = time.time()

        # Warm-template fast path: ioctl ‖ fork.
        # OVL_IOCTL_CHECKPOINT runs in GSD (this thread, kernel time on
        # overlay superblock state); fork() runs in the template process
        # (separate address space, page-table copy proportional to
        # template RSS). The two operations touch disjoint kernel state,
        # so we dispatch the fork over the control FIFO first, run ioctl
        # in this thread while the template's main loop wakes up and
        # fork()s, then reap the response. Critical path collapses from
        # ovl + fork (~24+6=30ms) to max(ovl, fork) (~24ms).
        if template_pid is not None:
            cleanup_ms = 0.0
            t_par_0 = time.time()
            dispatched = self.template_pool.dispatch_fork(
                template_pid,
                fixed_active_pid=(
                    None if self.restore_fastfork_dump_pid
                    else self.fixed_active_pid))
            t_dispatch = time.time()
            dispatch_ms = (t_dispatch - t_par_0) * 1000

            # Concurrent: ioctl in GSD ‖ fork() in template.
            self._apply_overlay_switch(target_layers, new_upper, new_work, kind="restore")
            t_ioctl = time.time()
            self.current_upper = new_upper
            self.current_work = new_work
            ovl_time = (t_ioctl - t_dispatch) * 1000

            if dispatched:
                _, child_pid = self.template_pool.await_fork(
                    template_pid, snapshot_id=None, timeout=2.0)
            else:
                child_pid = None
            t_par_end = time.time()
            fork_wait_ms = (t_par_end - t_ioctl) * 1000  # how much fork lagged ioctl
            crit_path_ms = (t_par_end - t_par_0) * 1000  # parallel total
            fork_meta = (
                getattr(self.template_pool, "last_fork_meta", {})
                if self.template_pool else {})
            fork_timing = (
                fork_meta.get("timing_ms", {})
                if isinstance(fork_meta, dict) else {})
            fork_agent_ms = (
                fork_timing.get("fork_parent_ms")
                or fork_timing.get("clone3_settid_parent")
                or fork_timing.get("clone3_parent")
                if isinstance(fork_timing, dict) else None)
            fork_total_ms = (t_par_end - t_dispatch) * 1000

            if child_pid is not None:
                self.agent_pid = child_pid
                self._active_dump_owner_id = effective_id
                self._schedule_async_parent_restamp(effective_id)
                self._bump_epoch()
                self._clear_soft_dirty_after_fork(child_pid)
                print(f"[PERF] Restore ID {effective_id}: "
                      f"cleanup={cleanup_ms:.2f}ms, dispatch={dispatch_ms:.2f}ms, "
                      f"OverlayFS={ovl_time:.2f}ms ‖ fork (post-ioctl wait "
                      f"{fork_wait_ms:.2f}ms), critical-path={crit_path_ms:.2f}ms, "
                      f"rss={rss_mb:.1f}MB (warm-template HIT)")
                # Start optional prewarm after foreground bookkeeping. Its
                # Python thread otherwise competes for the GIL while epoch
                # publication and logging release it for file I/O. The same
                # work is still started before returning the complete API.
                if self.enable_prewarm:
                    spawn_prewarm(child_pid)
                component_end = time.time()
                restore_api_wall_ms = (component_end - restore_api_t0) * 1000
                fast_coordination_ms = ((t_dispatch - component_t0) + (component_end - t_par_end)) * 1000
                return {
                    "restore_table3_total_ms": (component_end - component_t0) * 1000,
                    "restore_fast_coordination_ms": fast_coordination_ms,
                    "restore_ms": active_dump_join_ms + cleanup_ms + crit_path_ms,
                    # fast-rs HEADLINE = pure fork+ioctl mechanism (crit_path_ms).
                    # The wait for a prior in-flight async dump (active_dump_join_ms)
                    # is a CHECKPOINT-side cost that only surfaces under dense
                    # rollback; it is reported separately (restore_active_dump_join_ms)
                    # and must NOT be folded in here -- doing so inflates/mislabels
                    # the fast-restore number. (restore_ms above keeps the full total.)
                    "restore_critical_ms": crit_path_ms,
                    "restore_api_wall_ms": restore_api_wall_ms,
                    "restore_cleanup_ms": cleanup_ms,
                    "restore_dump_join_ms": dump_join_ms,
                    "restore_active_dump_join_ms": active_dump_join_ms,
                    "restore_kill_active_ms": kill_active_ms,
                    # Overlaps the kill/wait interval; do not add it again.
                    "restore_prepare_overlapped_ms": prepare_ms,
                    "restore_fast_dispatch_ms": dispatch_ms,
                    "restore_fast_ioctl_ms": ovl_time,
                    "restore_fast_fork_wait_ms": fork_wait_ms,
                    "restore_fast_fork_total_ms": fork_total_ms,
                    "restore_fast_fork_agent_ms": fork_agent_ms,
                    "restore_fast_fork_timing": fork_timing,
                    "replay_cmds": replay_cmds,
                    "path": "warm-template",
                }
            if not allow_cold_fallback:
                raise RuntimeError(
                    f"Restore {effective_id} template fork failed; "
                    "durable fallback is prohibited")
            # Fork failed (timeout or template died). The ioctl already
            # mutated overlay state, so we cannot retreat; fall through to
            # CRIU restore which will rebuild the agent regardless.
            print(f"[Controller] Warm-template fork failed "
                  f"(template_pid={template_pid}, "
                  f"alive={os.path.isdir(f'/proc/{template_pid}')}); "
                  f"see /tmp/template_fork.log for reason. "
                  f"Falling back to CRIU restore")
            _join_durable_dump("warm-fork-fallback")
            dump_error = (physical_target.get("dump_error")
                          or physical_target.get("dump_stats", {}).get("dump_error"))
            if dump_error:
                raise DumpUnavailableError(
                    effective_id,
                    physical_target.get("parent_id"),
                    RuntimeError(f"durable dump unavailable: {dump_error}"),
                )
        else:
            print(f"[PERF] Restore ID {effective_id}: warm-template MISS "
                  f"(template not registered for this snapshot — "
                  f"cache miss or discarded)")
            if not (self.enable_criu_lazy_restore and self.parallel_lazy_restore):
                # No template and no lazy-pages parallel partner: apply ioctl
                # synchronously before CRIU restore.
                t0 = time.time()
                self._apply_overlay_switch(target_layers, new_upper, new_work, kind="restore")
                t1 = time.time()
                ovl_time = (t1 - t0) * 1000
                self.current_upper = new_upper
                self.current_work = new_work

        # Slow path: CRIU restore from durable dump. Optional lazy-pages mode
        # uses userfaultfd to return once the process is runnable while a CRIU
        # lazy-pages daemon serves memory on demand.
        slow_coord_t0 = time.time()
        if external_pidns_restore is None:
            external_pidns_restore = self._is_external_pidns_dump(physical_target)
        external_pidns_fd = None
        if external_pidns_restore:
            self._clear_templates_for_external_restore()
            # Active-subtree dumps mark the containing PID namespace external.
            # Keep ns-init alive and hand that namespace to CRIU on restore.
            external_pidns_fd = self._open_external_pidns_fd()
            if external_pidns_fd is None:
                raise DumpUnavailableError(
                    effective_id,
                    physical_target.get("parent_id"),
                    RuntimeError("external pid namespace is unavailable"),
                )
        else:
            # Every disposable writer lives below the old namespace. A failed
            # warm fork can enter cold fallback without the early drain above.
            if getattr(self, "_async_incremental", None) is not None:
                drain_start = time.perf_counter()
                self._async_incremental.drain_before_namespace_teardown()
                namespace_drain_ms += (time.perf_counter() - drain_start) * 1000
            # Legacy full-namespace dumps include ns-init. Tear down the old
            # namespace and let CRIU recreate a fresh one.
            try:
                os.kill(self.ns_init_pid, signal.SIGKILL)
            except ProcessLookupError: pass
            except Exception as e:
                print(f"[Warning] Slow-path ns_init kill failed: {e}")
            try:
                os.waitpid(self.ns_init_pid, 0)
            except (ChildProcessError, ProcessLookupError): pass
            except Exception as e:
                print(f"[Warning] Slow-path ns_init waitpid failed: {e}")

        # --pidfile captures the restored root's host PID. With PID-ns
        # isolation (namespace_launcher), CRIU recreates the tree in a new
        # PID ns, so the host PID of the restored agent DIFFERS from the
        # killed one. We must update self.agent_pid or subsequent kill/dump
        # operations target a dead PID.
        pidfile = os.path.join(physical_target['mem_path'], "restored.pid")
        try: os.unlink(pidfile)
        except OSError: pass
        cmd = [
            self.criu_restore_bin, "restore",
            "-D", physical_target['mem_path'],
            "--restore-detached",
            "--tcp-close",
            "--ext-unix-sk",
            "--pidfile", pidfile,
            *self._all_ext_mount_map_args()
        ]
        if os.environ.get("CRIU_DEBUG"):
            cmd.extend(["-v4"])
        exact_parent_restore = physical_target.get("memory_protocol") == "exact-parent-v1"
        if (os.environ.get("DELTABOX_CRIU_RESTORE_LEAVE_STOPPED") == "1"
                or exact_parent_restore):
            cmd.append("--leave-stopped")
        restore_shell_job = (
            os.environ.get("DELTABOX_EXTERNAL_RESTORE_SHELL_JOB") == "1"
            if external_pidns_restore else True
        )
        if restore_shell_job:
            cmd.insert(4, "--shell-job")
        restore_prev_id = physical_target.get("prev_ckpt_id")
        if restore_prev_id and restore_prev_id in self.registry:
            restore_prev_path = self.registry[restore_prev_id].get("mem_path")
            if restore_prev_path:
                cmd.extend(["--prev-images-dir",
                            os.path.relpath(restore_prev_path,
                                            physical_target['mem_path'])])
        self._append_external_pidns_restore(cmd, external_pidns_fd)
        pass_fds = (external_pidns_fd,) if external_pidns_fd is not None else ()
        inherited_resources = ExitStack()
        lazy_daemon = None
        try:
            if physical_target.get("memory_protocol") == "exact-parent-v1":
                try:
                    from .async_resources import protocol_restore_fds
                except ImportError:
                    from async_resources import protocol_restore_fds
                inherit_args, resource_fds = inherited_resources.enter_context(
                    protocol_restore_fds(physical_target["resources"]))
                cmd.extend(inherit_args)
                pass_fds += resource_fds
            lazy_daemon = None
            lazy_daemon_ms = 0.0
            if self.enable_criu_lazy_restore:
                t_lazy_0 = time.time()
                lazy_daemon = self._start_lazy_pages_daemon(physical_target['mem_path'])
                lazy_daemon_ms = (time.time() - t_lazy_0) * 1000
                cmd.append("--lazy-pages")
            slow_pre_criu_ms = (time.time() - slow_coord_t0) * 1000

            if self.enable_criu_lazy_restore and self.parallel_lazy_restore:
                # Slow restore parallelism mirrors warm-template restore: CRIU
                # rebuilds process state while OverlayFS switches the sandbox
                # layer stack. The restored worker waits for the next mailbox
                # command, so the controller still gates forward progress.
                t2 = time.time()
                restore_proc = subprocess.Popen(cmd, pass_fds=pass_fds)
                if ovl_time == 0.0:
                    t_ovl0 = time.time()
                    self._apply_overlay_switch(target_layers, new_upper, new_work, kind="restore")
                    t_ovl1 = time.time()
                    self.current_upper = new_upper
                    self.current_work = new_work
                    ovl_time = (t_ovl1 - t_ovl0) * 1000
                rc = restore_proc.wait(timeout=30)
                t3 = time.time()
                if rc != 0:
                    raise subprocess.CalledProcessError(rc, cmd)
                criu_time = (t3 - t2) * 1000
                restore_total_ms = criu_time
            else:
                t2 = time.time()
                subprocess.check_call(cmd, pass_fds=pass_fds)
                t3 = time.time()
                criu_time = (t3 - t2) * 1000
                restore_total_ms = ovl_time + criu_time
        except BaseException:
            if lazy_daemon is not None:
                self._stop_lazy_pages_daemon(lazy_daemon)
            raise
        finally:
            inherited_resources.close()
            if external_pidns_fd is not None:
                os.close(external_pidns_fd)
        slow_criu_end = t3
        try:
            with open(pidfile) as pf:
                new_agent_pid = int(pf.read().strip())
            if exact_parent_restore and (new_agent_pid <= 1 or not self._is_pidns_init(new_agent_pid)):
                raise RuntimeError("exact-parent restore did not publish a live namespace init")
            print(f"[Controller] CRIU restored agent host PID: "
                  f"{self.agent_pid} -> {new_agent_pid} "
                  f"(external_pidns={external_pidns_restore})")
            self.agent_pid = new_agent_pid
            self._active_dump_owner_id = effective_id
            if not external_pidns_restore:
                self.ns_init_pid = new_agent_pid
                if self.template_pool is not None:
                    self.template_pool.reaper_pid = new_agent_pid
            if self.enable_criu_lazy_restore or exact_parent_restore:
                try:
                    if exact_parent_restore:
                        try:
                            from .async_checkpoint import open_pidfd, send_pidfd_signal
                        except ImportError:
                            from async_checkpoint import open_pidfd, send_pidfd_signal
                        resume_fd = open_pidfd(new_agent_pid)
                        try:
                            send_pidfd_signal(resume_fd, signal.SIGCONT)
                        finally:
                            os.close(resume_fd)
                    else:
                        os.kill(new_agent_pid, signal.SIGCONT)
                    print(f"[Controller] CRIU restored agent SIGCONT pid={new_agent_pid}")
                except ProcessLookupError:
                    if exact_parent_restore:
                        raise
                    print(f"[Controller] WARN: CRIU lazy restored pid {new_agent_pid} gone before SIGCONT")
                except OSError as e:
                    if exact_parent_restore:
                        raise
                    print(f"[Controller] WARN: CRIU lazy restored pid {new_agent_pid} SIGCONT failed: {e}")
            self._probe_clear_soft_dirty(new_agent_pid)
            self._schedule_async_parent_restamp(effective_id)
        except (OSError, ValueError) as e:
            if exact_parent_restore:
                raise RuntimeError(f"exact-parent restore activation failed: {e}") from e
            print(f"[Controller] WARN: failed to read CRIU pidfile "
                  f"{pidfile}: {e}; agent_pid may be stale")
        # Full-namespace slow restore killed the old PID ns, so prior template
        # PIDs are stale. External-pidns restore keeps ns-init and templates
        # alive; preserve the pool there.
        if self.template_pool is not None and not external_pidns_restore:
            self.template_pool.templates.clear()
            self.template_pool.reset_channels()

        # Deferred: if warm-template is ever re-enabled for MCTS, restore
        # leaves multiple runnable FIFO readers in the ns (CRIU does not
        # preserve SIGSTOP across restore). Not relevant while MCTS runs
        # with --no-warm-template; BoN ns has no CRIU restore path.
        # A prior attempt at ns sanitization was reverted — see
        # /tmp/sanitize_attempt.py.bak for history and why the heuristic
        # (deepest-starttime = active) was unsafe.
        self._invalidate_inflight_llm()
        lazy_label = " lazy" if self.enable_criu_lazy_restore else ""
        print(f"[PERF] Restore ID {effective_id}: OverlayFS={ovl_time:.2f}ms, "
              f"CRIU{lazy_label}={criu_time:.2f}ms")
        print("Full Restore Success")
        component_end = time.time()
        restore_api_wall_ms = (component_end - restore_api_t0) * 1000
        slow_post_criu_ms = (component_end - slow_criu_end) * 1000

        return {
            "restore_table3_total_ms": (component_end - component_t0) * 1000,
            "restore_slow_coordination_ms": slow_pre_criu_ms + slow_post_criu_ms,
            "restore_ms": active_dump_join_ms + restore_total_ms,
            "restore_api_wall_ms": restore_api_wall_ms,
            "restore_dump_join_ms": dump_join_ms,
            "restore_active_dump_join_ms": active_dump_join_ms,
            "restore_kill_active_ms": kill_active_ms,
            "restore_prepare_overlapped_ms": prepare_ms,
            "restore_slow_ioctl_ms": ovl_time,
            "restore_slow_criu_ms": criu_time,
            "restore_slow_total_ms": restore_total_ms,
            "restore_slow_pre_criu_ms": slow_pre_criu_ms,
            "restore_slow_lazy_daemon_ms": lazy_daemon_ms,
            "restore_slow_post_criu_ms": slow_post_criu_ms,
            "restore_slow_lazy": self.enable_criu_lazy_restore,
            "restore_namespace_drain_ms": namespace_drain_ms,
            "restore_slow_parallel_lazy": (
                self.enable_criu_lazy_restore and self.parallel_lazy_restore),
            "replay_cmds": replay_cmds,
            "path": "criu-lazy" if self.enable_criu_lazy_restore else "criu",
        }

    def gc_obsolete_snapshots(self, keep_n: int = 30, keep_ids=None):
        # C11: LRU prune of obsolete CRIU mem_paths + overlay restore dirs.
        # When keep_ids is provided (MCTS-reachability-aware path), use it as
        # the base keep set; recency-based keep_n is bypassed. When absent,
        # fall back to last-N insertion-order LRU (safe for non-MCTS callers
        # like linear search). Ancestor-chain preservation (for CRIU
        # --prev-images-dir linkage) applies in both cases.
        try:
            if keep_ids is not None:
                keep = {rid for rid in keep_ids if rid in self.registry}
            else:
                all_ids = list(self.registry.keys())
                keep = set(all_ids) if len(all_ids) <= keep_n else set(all_ids[-keep_n:])
            try:
                from .async_checkpoint import retained_ids
            except ImportError:
                from async_checkpoint import retained_ids
            keep.update(rid for rid, entry in self.registry.items()
                        if entry.get("template_pid") in {self.agent_pid, self.ns_init_pid})
            if getattr(self, "_lazy_page_daemons", None):
                keep.update(self._lazy_reader_ids())
            keep = retained_ids(self.registry, keep)
            pruned_mem = 0
            for rid in list(self.registry.keys()):
                if rid in keep: continue
                entry = self.registry.pop(rid, None)
                if not entry: continue
                if self.template_pool is not None:
                    self.template_pool.discard(rid)
                mp = entry.get("mem_path")
                if mp and os.path.isdir(mp):
                    shutil.rmtree(mp, ignore_errors=True)
                    pruned_mem += 1
            pruned_dirs = 0
            keep_dirs = {self.current_upper, self.current_work}
            keep_dirs.update(layer for entry in self.registry.values()
                             for layer in entry.get("layers", ()))
            try:
                for name in os.listdir(self.layers_root):
                    if not name.startswith("restore_"): continue
                    full = os.path.join(self.layers_root, name)
                    if full in keep_dirs or not os.path.isdir(full): continue
                    shutil.rmtree(full, ignore_errors=True)
                    pruned_dirs += 1
            except OSError: pass
            if pruned_mem or pruned_dirs:
                print(f"[GC] LRU prune: {pruned_mem} mem_paths + {pruned_dirs} overlay dirs, kept {len(keep)} entries")
        except Exception as e:
            print(f"[GC] Prune failed (continuing): {e}")

    def shutdown(self):
        """Drain the async CRIU dump pool. Must be called from the agent loop's
        finally clause: an in-flight dump on a SIGKILL'd ns_init would leave a
        zombie criu subprocess and a corrupt image dir. wait=True blocks until
        the in-flight task (at most one, max_workers=1) finishes; pending tasks
        in the queue still run because the future was already submitted, which
        matches the at-least-once semantics save_step expects."""
        try:
            self._dump_pool.shutdown(wait=True)
        except Exception as e:
            print(f"[Controller] WARN: dump pool shutdown failed: {e}")
        for proc in list(getattr(self, "_lazy_page_daemons", ())):
            self._stop_lazy_pages_daemon(proc)
        try:
            self._restamp_pool.shutdown(wait=True)
        except Exception as e:
            print(f"[Controller] WARN: restamp pool shutdown failed: {e}")
        try:
            shutil.rmtree(self._restamp_cache_root, ignore_errors=True)
        except Exception as e:
            print(f"[Controller] WARN: restamp cache cleanup failed: {e}")
        if self.root_overlays is not None:
            try:
                self.root_overlays.teardown()
            except Exception as e:
                print(f"[Controller] WARN: root overlay teardown failed: {e}")
