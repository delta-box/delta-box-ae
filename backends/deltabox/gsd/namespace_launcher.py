from __future__ import annotations

import os
import sys
import ctypes
import signal
import time

# Linux clone flags
CLONE_NEWNS = 0x00020000   # Mount namespace
CLONE_NEWPID = 0x20000000  # PID namespace
SYS_CLONE3_X86_64 = 435


class CloneArgs(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_ulonglong),
        ("pidfd", ctypes.c_ulonglong),
        ("child_tid", ctypes.c_ulonglong),
        ("parent_tid", ctypes.c_ulonglong),
        ("exit_signal", ctypes.c_ulonglong),
        ("stack", ctypes.c_ulonglong),
        ("stack_size", ctypes.c_ulonglong),
        ("tls", ctypes.c_ulonglong),
        ("set_tid", ctypes.c_ulonglong),
        ("set_tid_size", ctypes.c_ulonglong),
        ("cgroup", ctypes.c_ulonglong),
    ]


def _clone3_set_tid(set_tid: int) -> int:
    tid = ctypes.c_ulonglong(set_tid)
    args = CloneArgs()
    args.exit_signal = signal.SIGCHLD
    args.set_tid = ctypes.addressof(tid)
    args.set_tid_size = 1
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ret = libc.syscall(
        ctypes.c_long(SYS_CLONE3_X86_64),
        ctypes.byref(args),
        ctypes.c_size_t(ctypes.sizeof(args)),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(ret)


def _parse_fixed_active_pid() -> int | None:
    raw = os.environ.get("DELTABOX_FIXED_ACTIVE_PID")
    try:
        pid = int(raw) if raw else 0
    except ValueError:
        return None
    return pid if pid > 1 else None


def _nspids_for_host_pid(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("NSpid:"):
                    return [int(x) for x in line.split()[1:]]
    except (OSError, ValueError):
        pass
    return []


def _host_pid_for_ns_pid(inner_pid: int) -> int | None:
    try:
        ns_inode = os.readlink("/proc/self/ns/pid")
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
            nspids = _nspids_for_host_pid(host_pid)
            if nspids and nspids[-1] == inner_pid:
                return host_pid
        except (OSError, ValueError):
            continue
    return None

def isolate_environment():
    """
    通过 glibc 直接调用 Linux 内核系统调用 unshare，
    将当前进程与宿主机的 PID 命名空间和 Mount 命名空间彻底物理隔离。
    这就是 MCTS 状态容器化最坚实的无菌底座。

    关于 CRIU Restore 与子进程同步恢复：
    CRIU dump --tree PID 会捕获以 PID 为根的**整棵进程树**——包括所有 fork 出的
    子进程、它们的 PID、内存、FD、信号掩码等。Restore 时 CRIU 会：
      1. 先 fork 出一个"骨架"进程树，每个节点恢复到原始 PID
      2. 所有进程在 restore 阶段处于 TASK_STOPPED 状态
      3. 全部进程的内存/FD/信号恢复完毕后，CRIU 统一发 SIGCONT
    所以子进程是**原子同步恢复**的——要么整棵树全部恢复成功，要么全部失败。
    PID Namespace 隔离并不影响这一机制，因为 CRIU 在 dump 时记录的是宿主机视角
    的 PID 树，restore 时按同样的树结构重建。
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)

    # ── Step 1: 剥离 PID 和 Mount 命名空间 ──
    if libc.unshare(CLONE_NEWPID | CLONE_NEWNS) != 0:
        errno = ctypes.get_errno()
        raise OSError(f"Failed to unshare CLONE_NEWPID | CLONE_NEWNS (errno={errno}). Requires root.")

    # ── Step 2: 将整个挂载树标记为 MS_REC|MS_PRIVATE ──
    # 阻止后续 mount/umount 事件传播到宿主机——这是安全操作的前提
    MS_REC = 1 << 14      # 16384
    MS_PRIVATE = 1 << 18  # 262144
    if libc.mount(b"none", b"/", b"", MS_REC | MS_PRIVATE, b"") != 0:
        errno = ctypes.get_errno()
        raise OSError(
            f"Failed to set / as MS_REC|MS_PRIVATE (errno={errno}). "
            f"Cannot safely isolate mount namespace — refusing to continue."
        )

    # ── Step 3: 卸载 /proc 子挂载 ──
    # /proc/sys/fs/binfmt_misc 上叠了两层挂载 (autofs + binfmt_misc)，
    # 必须在重新挂载 /proc 前卸载，否则它们作为旧 /proc 的子挂载残留，
    # 导致 CRIU 的 autofs 代码找不到 master pid 而报错。
    MNT_DETACH = 2
    libc.umount2(b"/proc/sys/fs/binfmt_misc", MNT_DETACH)  # binfmt_misc 层
    libc.umount2(b"/proc/sys/fs/binfmt_misc", MNT_DETACH)  # autofs 层

    # ── Step 4: 先卸载旧 /proc，再挂载新的 ──
    # 直接覆盖 mount 在某些内核版本下会返回 EBUSY，先 umount 更可靠
    libc.umount2(b"/proc", MNT_DETACH)

    MS_NOSUID  = 1 << 1   # 2
    MS_NODEV   = 1 << 2   # 4
    MS_NOEXEC  = 1 << 3   # 8
    if libc.mount(b"proc", b"/proc", b"proc", 0, b"") != 0:
        # 某些受限环境 (如容器内) 需要带安全标志重试
        if libc.mount(b"proc", b"/proc", b"proc",
                      MS_NOSUID | MS_NODEV | MS_NOEXEC, b"") != 0:
            errno = ctypes.get_errno()
            print(f"[WARNING] Failed to mount /proc in new namespace (errno={errno}). "
                  f"CRIU may not work correctly.", flush=True)
        else:
            print("[NAMESPACE] /proc mounted with MS_NOSUID|MS_NODEV|MS_NOEXEC", flush=True)

    # ── Step 5: 剔除 CRIU 不支持的挂载点 ──
    # squashfs/vfat/efivarfs/debugfs/tracefs/fusectl/configfs/ramfs 等
    # 我们在独立的 Mount Namespace 中，Step 2 保证了不影响宿主机
    _umount_problematic_mounts()

def _umount_problematic_mounts():
    """白名单策略：只保留 CRIU 确定支持的文件系统，其余全部 umount2(MNT_DETACH)。
    直接使用 libc 系统调用，在新 Mount Namespace 内执行，不影响宿主机。"""
    ALLOWED_TYPES = {
        'ext4', 'ext3', 'ext2', 'xfs', 'btrfs',   # 磁盘文件系统
        'tmpfs',                                     # 内存文件系统 (ramfs 不保留：systemd credentials 的 ramfs 会导致 CRIU 匿名设备号错误)
        'proc', 'sysfs', 'devtmpfs', 'devpts',      # 伪文件系统 (CRIU 必需)
        'cgroup2', 'cgroup', 'overlay',              # 容器/cgroup
    }
    ESSENTIAL_PATHS = {'/', '/proc', '/sys', '/tmp'}
    MNT_DETACH = 2

    libc = ctypes.CDLL("libc.so.6", use_errno=True)

    targets = []
    try:
        with open('/proc/self/mountinfo') as f:
            for line in f:
                parts = line.split()
                sep_idx = parts.index('-') if '-' in parts else -1
                if sep_idx < 0 or sep_idx + 1 >= len(parts):
                    continue
                mount_point = parts[4]
                fs_type = parts[sep_idx + 1]
                
                # 保留必需挂载点本身
                if mount_point in ESSENTIAL_PATHS:
                    continue
                
                # CRIU 不支持的类型直接卸载
                if fs_type not in ALLOWED_TYPES:
                    targets.append((mount_point, fs_type))
                    continue
                    
                # 处理 tmpfs 泄漏问题。
                # 宿主机的 /run 和 /dev/shm 等都是 tmpfs，但 /run 下挂了太多 sockets。
                # 不能卸载 /run 本身（会导致 /run 变回宿主机的 underlying fs），
                # 但需要卸载 /run 下的子 tmpfs 或不必要子挂载（如 /run/user/1000, /run/docker, 等等）。
                # 以及卸载所有以 snapd, systemd, docker, containerd 结尾的挂载点。
                if mount_point.startswith('/run/') and mount_point != '/run':
                    targets.append((mount_point, fs_type))
                elif any(x in mount_point for x in ['docker', 'containerd', 'snapd', 'systemd', 'vscode']):
                    targets.append((mount_point, fs_type))
                    
    except Exception as e:
        print(f"[NAMESPACE WARN] Cannot parse mountinfo: {e}", flush=True)
        return

    ok = 0
    # Over-mounts require repeated unmounting
    for mp, _ft in sorted(targets, key=lambda x: len(x[0]), reverse=True):
        while libc.umount2(mp.encode(), MNT_DETACH) == 0:
            ok += 1

    # For /run, since the host has way too many sockets, just mask it with a fresh tmpfs entirely to bypass host pollution
    if libc.mount(b"tmpfs", b"/run", b"tmpfs", 0, b"mode=755") == 0:
        # Re-create essential dirs in the clean /run
        for essential_dir in [b"/run/lock", b"/run/user"]:
            try:
                os.makedirs(essential_dir)
            except OSError:
                pass
        ok += 1

    # For /dev, mask the entire device tree to avoid dumping thousands of complex host char devices and loopbacks
    # This specifically fixes CRIU 'Bug at mount.c:48' crashes upon restoration.
    if libc.mount(b"tmpfs", b"/dev", b"tmpfs", 0, b"mode=755") == 0:
        import stat
        os.makedirs("/dev/pts", exist_ok=True)
        os.makedirs("/dev/shm", exist_ok=True)
        
        # mkdev devices
        devices = {
            "null": (1, 3), "zero": (1, 5), "full": (1, 7),
            "random": (1, 8), "urandom": (1, 9), "tty": (5, 0)
        }
        for name, (major, minor) in devices.items():
            try:
                os.mknod(f"/dev/{name}", stat.S_IFCHR | 0o666, os.makedev(major, minor))
            except OSError:
                pass
                
        # Mount devpts and tmpfs for shm
        libc.mount(b"devpts", b"/dev/pts", b"devpts", 0, b"gid=5,mode=620,ptmxmode=000")
        libc.mount(b"tmpfs", b"/dev/shm", b"tmpfs", 0, b"mode=1777")
        
        # Symlinks for standard fds and ptmx
        try:
            os.symlink("pts/ptmx", "/dev/ptmx")
            os.symlink("/proc/self/fd", "/dev/fd")
            os.symlink("/proc/self/fd/0", "/dev/stdin")
            os.symlink("/proc/self/fd/1", "/dev/stdout")
            os.symlink("/proc/self/fd/2", "/dev/stderr")
        except OSError:
            pass

    remaining = []
    try:
        with open('/proc/self/mountinfo') as f:
            for line in f:
                parts = line.split()
                sep_idx = parts.index('-') if '-' in parts else -1
                if sep_idx < 0:
                    continue
                fs_type = parts[sep_idx + 1]
                mount_point = parts[4]
                if fs_type not in ALLOWED_TYPES and mount_point not in ESSENTIAL_PATHS:
                    remaining.append(f"{mount_point}({fs_type})")
    except Exception:
        pass

    if remaining:
        print(f"[NAMESPACE WARN] {ok} unmounted, {len(remaining)} remain: "
              f"{', '.join(remaining[:5])}{'...' if len(remaining) > 5 else ''}", flush=True)
    else:
        print(f"[NAMESPACE] Cleaned {ok} non-essential mounts (whitelist: {len(ALLOWED_TYPES)} types)", flush=True)


def _reopen_std_fds():
    """在新 Mount Namespace 内重新打开 stdout/stderr 指向的文件。
    benchmark_runner 在原始 namespace 打开 /tmp/criu_agent.log 并 redirect 到 fd 1/2，
    但 CRIU dump 按新 namespace 的 mountinfo 解析 fd，会因 mount ID 失配而报
    'Can't lookup mount=XX for fd=1'。此函数通过 readlink 找到原始路径，
    在新 namespace 重新 open + dup2，使 fd 归属于当前 mount 表。"""
    for fd_num in (1, 2):
        try:
            path = os.readlink(f"/proc/self/fd/{fd_num}")
            if path.startswith("/") and not path.startswith("/dev/"):
                new_fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                os.dup2(new_fd, fd_num)
                os.close(new_fd)
        except (OSError, ValueError):
            pass


# ═══════════════════════════════════════════════════════════════════════════
#  方向2 — 宿主机模式：会话 / 进程组级联清理（无 PID Namespace）
#
#  benchmark_runner 用 Popen(..., start_new_session=True) 启动 agent 时，
#  该 agent 为 session leader，SID == agent PID；其子进程继承同一 SID。
#  回滚前 killpg + 扫描 /proc 中 SID 匹配的残留，等价于工业界常用的
#  “tear down session” 做法，避免 CRIU restore 时 PID 占用，且不触发
#  宿主 mount namespace 与 CRIU 的深层不兼容。
# ═══════════════════════════════════════════════════════════════════════════


def find_session_pids(sid: int) -> list:
    """返回 SID == sid 的所有进程 PID（含脱离进程组但仍同会话的进程）。"""
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    stat = f.read().split()
                    if len(stat) > 5 and int(stat[5]) == sid:
                        pids.append(int(entry))
            except (OSError, IndexError, ValueError):
                continue
    except OSError:
        pass
    return pids


def kill_session_tree(root_pid: int) -> None:
    """对以 root_pid 为会话锚点的沙箱会话执行 SIGKILL 级联（Phase1: killpg, Phase2: SID 扫描）。"""
    sid = root_pid
    try:
        pgid = os.getpgid(root_pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(root_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    time.sleep(0.05)
    for straggler in find_session_pids(sid):
        try:
            os.kill(straggler, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    try:
        os.waitpid(root_pid, 0)
    except (ChildProcessError, ProcessLookupError, OSError):
        pass


def wait_session_empty(sid: int, timeout: float = 5.0) -> None:
    """直到 SID==sid 的进程全部从 /proc 消失（持续 SIGKILL 残留）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        remaining = find_session_pids(sid)
        if not remaining:
            return
        for p in remaining:
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.1)
    left = find_session_pids(sid)
    if left:
        print(f"  [WARN] Session {sid} still has PIDs {left} after {timeout}s", flush=True)


class SandboxIsolator:
    """
    方向2 沙箱回收：两种模式

    - use_pid_namespace=False（默认，宿主机推荐）:
        会话 + 进程组级联 SIGKILL（kill_session_tree），与 CRIU restore 前
        controller 内逻辑一致，作为二次加固回调。

    - use_pid_namespace=True（Firecracker / 显式 unshare 环境）:
        杀掉子 PID namespace 中的 init（PID 1 等效），依赖内核级联回收。
    """

    def __init__(self, init_pid: int, use_pid_namespace: bool = False):
        self.init_pid = init_pid
        self.use_pid_namespace = use_pid_namespace

    def get_cleanup_fn(self):
        """返回供 CRIU Restore 前调用的无菌回收回调函数"""

        if self.use_pid_namespace:

            def cleanup_ns():
                print(f"  [CLEANUP] PID-NS init teardown (PID {self.init_pid})", flush=True)
                try:
                    os.kill(self.init_pid, signal.SIGKILL)
                except OSError:
                    pass

                t0 = time.time()
                while time.time() - t0 < 3.0:
                    if not os.path.exists(f"/proc/{self.init_pid}"):
                        break
                    time.sleep(0.05)
                try:
                    os.waitpid(self.init_pid, 0)
                except Exception:
                    pass
                print("  [CLEANUP] PID namespace root destroyed.", flush=True)

            return cleanup_ns

        def cleanup_host():
            print(
                f"  [CLEANUP] Direction-2 host: session/PG teardown (root={self.init_pid})",
                flush=True,
            )
            kill_session_tree(self.init_pid)
            wait_session_empty(self.init_pid, timeout=5.0)
            print("  [CLEANUP] Session tree cleared.", flush=True)

        return cleanup_host

    def update_pid(self, new_pid: int):
        self.init_pid = new_pid

def isolate_minimal():
    """PID-only isolation. No mount ns — CRIU can't reconcile copy-mounts.

    Attempts with CLONE_NEWNS (with or without aggressive mount cleanup)
    both tripped CRIU's mount walker:
      `Error (criu/mount.c:724): mnt: FS mnt ./mnt/data dev 0x... unsupported id`
    The issue is that CLONE_NEWNS makes a copy of the parent mount tree with
    fresh mount IDs that have no upstream master — CRIU rejects them as
    "unsupported." Previously (v6b, no launcher) CRIU saw only init-ns
    mounts and dumped cleanly.

    Solution: unshare PID-only and keep the init mount ns. /proc still
    belongs to init mount ns and therefore still resolves PIDs against init
    PID ns — which is fine for everything except the agent's
    `_assert_single_threaded` check. That helper is patched to use
    /proc/self/task, which always resolves relative to the caller regardless
    of /proc's own PID ns binding.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.unshare(CLONE_NEWPID) != 0:
        errno = ctypes.get_errno()
        raise OSError(f"unshare(CLONE_NEWPID) failed errno={errno}")
    return libc


def main():
    """Launch target under a fresh PID+Mount namespace.

    unshare(CLONE_NEWPID) does NOT migrate the caller into the new ns — only
    future children enter it. To make the agent PID 1 in the new ns, we must
    fork() after unshare, then have the child execvp the target.

    Layout:
      - This launcher: stays in host PID ns (new mount ns). Acts as sentinel:
        writes child host PID to AGENT_PID_FILE, then waitpid()s forever.
      - Forked child: PID 1 in new PID ns; re-mounts /proc so it shows new-ns
        PIDs, reopens std fds, execvps the target.

    The sentinel in host ns is essential: GSD (sandbox_controller) lives in
    the VM's init PID ns and needs to SIGCONT/SIGKILL warm-template
    processes (which are in the agent's ns). Because the sentinel holds the
    new ns open via being its parent + /proc/<sentinel>/ns/pid referring to
    it, the ns persists even during rollback churn.
    """
    if len(sys.argv) < 2:
        print("Usage: python3 namespace_launcher.py <target_script.py>")
        sys.exit(1)

    ns_init_pid_file = os.environ.get("AGENT_NS_INIT_PID_FILE")

    print("[NAMESPACE] Entering new PID/Mount universe...", flush=True)
    # Minimal isolation — full isolate_environment() breaks CRIU by mutating
    # /dev and /run in ways CRIU's mnt_dump_parse_bind_info can't reconcile.
    libc = isolate_minimal()

    fixed_active_pid = _parse_fixed_active_pid()

    # Fork: parent = sentinel (old PID ns), child = PID 1 in new PID ns.
    child_host_pid = os.fork()
    if child_host_pid == 0:
        # Child: we are PID 1 in the new PID ns. Mount ns is shared with init,
        # so /proc still belongs to init; agent uses /proc/self/* everywhere
        # that matters. No mount manipulation here — avoids CRIU mount errors.
        #
        # setsid(): CRIU rejects dumping a PID-ns-1 whose session leader is
        # outside that PID ns (cr-dump.c:1681 "session leader of N(1) is
        # outside of its pid namespace"). The launcher sentinel inherits the
        # SSH session's sid, which lives in init PID ns. By calling setsid()
        # *after* fork, the child becomes its own session leader inside the
        # new PID ns, satisfying CRIU's precondition.
        try:
            os.setsid()
        except OSError as e:
            print(f"[NAMESPACE child] setsid failed: {e}", flush=True)
        if fixed_active_pid is not None:
            try:
                active_pid = _clone3_set_tid(fixed_active_pid)
            except OSError as e:
                print(f"[NAMESPACE child] clone3 set_tid={fixed_active_pid} "
                      f"failed: {e}", flush=True)
                sys.exit(1)
            if active_pid == 0:
                try:
                    os.setsid()
                except OSError as e:
                    print(f"[NAMESPACE active] setsid failed: {e}", flush=True)
                os.environ["DELTABOX_FIXED_ACTIVE_PID"] = str(fixed_active_pid)
                print(f"[NAMESPACE active] Agent PID={os.getpid()} "
                      f"fixed in new PID ns", flush=True)
                os.execvp(sys.argv[1], sys.argv[1:])
                sys.exit(127)
            active_host_pid = None
            deadline = time.time() + 2.0
            while time.time() < deadline:
                active_host_pid = _host_pid_for_ns_pid(fixed_active_pid)
                if active_host_pid is not None:
                    break
                time.sleep(0.01)
            if active_host_pid is None:
                print(f"[NAMESPACE child] failed to translate fixed pid "
                      f"{fixed_active_pid} to host pid", flush=True)
                sys.exit(1)
            agent_pid_file = os.environ.get("AGENT_PID_FILE", "/tmp/agent_ns_pid")
            tmp = agent_pid_file + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(f"{active_host_pid}\n")
                os.rename(tmp, agent_pid_file)
            except OSError as e:
                print(f"[NAMESPACE child] Failed to write {agent_pid_file}: {e}",
                      flush=True)
            print(f"[NAMESPACE child] Reaper PID=1, active host PID={active_pid} "
                  f"translated={active_host_pid} fixed_ns_pid={fixed_active_pid}",
                  flush=True)
            while True:
                try:
                    os.waitpid(-1, 0)
                except ChildProcessError:
                    time.sleep(0.05)
                except OSError as e:
                    print(f"[NAMESPACE child] waitpid failed: {e}", flush=True)
                    time.sleep(0.05)

        print(f"[NAMESPACE child] Agent PID={os.getpid()} in new PID ns, sid=self",
              flush=True)
        os.execvp(sys.argv[1], sys.argv[1:])
        # execvp never returns on success.
        sys.exit(127)

    # Sentinel: still in old (host) PID ns. In fixed-active mode, pid1 writes
    # the active PID after clone3(set_tid); otherwise publish pid1 itself.
    if ns_init_pid_file:
        tmp = ns_init_pid_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(f"{child_host_pid}\n")
            os.rename(tmp, ns_init_pid_file)
        except OSError as e:
            print(f"[NAMESPACE] Failed to write {ns_init_pid_file}: {e}",
                  flush=True)
    if fixed_active_pid is None:
        agent_pid_file = os.environ.get("AGENT_PID_FILE", "/tmp/agent_ns_pid")
        tmp = agent_pid_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(f"{child_host_pid}\n")
            os.rename(tmp, agent_pid_file)
        except OSError as e:
            print(f"[NAMESPACE] Failed to write {agent_pid_file}: {e}", flush=True)

    print(f"[NAMESPACE sentinel] ns-init host PID = {child_host_pid}", flush=True)
    try:
        _, status = os.waitpid(child_host_pid, 0)
        if os.WIFEXITED(status):
            sys.exit(os.WEXITSTATUS(status))
        if os.WIFSIGNALED(status):
            sys.exit(128 + os.WTERMSIG(status))
        sys.exit(1)
    except OSError as e:
        print(f"[NAMESPACE sentinel] waitpid failed: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
