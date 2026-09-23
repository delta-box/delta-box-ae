#!/usr/bin/env python3
"""
adaptive_checkpoint.py — Agent 步长感知的自适应快照策略
根据 Agent 指令类型自动选择最优的 Checkpoint 策略：
  - edit      → 文件层快照 (OverlayFS-only, 跳过内存 dump)
  - run       → 标准增量 CRIU dump (--track-mem)
  - background_run → 先 pre-dump 再正式 dump (two-phase)

同时集成 memory_classifier 的分析结果，对有 Hot 区域的进程使用 pre-dump。
"""
from __future__ import annotations

import os
import sys
import time
import uuid
import shutil
import subprocess
import signal
import threading

from .memory_classifier import classify_vmas
from .semantic_parser import InstructionSemanticParser

class AdaptiveCRIUController:
    """
    自适应 CRIU 控制器。
    相比原始 CRIUController，增加了：
      1. 基于 command 语义的策略选择
      2. pre-dump 异步预拷贝 (针对 hot 区域)
      3. 方向2: restore 前可选 SandboxIsolator 回调 (会话清理 或 PID-NS init 清理)
    """

    def __init__(self, agent_pid: int, snapshot_store: str,
                 enable_predump: bool = True, use_tmpfs: bool = True,
                 enable_external: bool = False, enable_prefetch: bool = False,
                 beam_width: int = 0, use_pid_namespace: bool = False):
        """
        beam_width: >0 时启用 Beam Search 裁剪式 GC, 每次 checkpoint 后
                    只保留最近 beam_width 条活跃分支的快照。0 表示禁用。
        enable_prefetch: lazy restore 完成后启动用户态后台预取线程。
        use_pid_namespace: True 仅当 agent 由 namespace_launcher unshare 启动时；
                          为 True 时对 agent 的 mountinfo 注入 --skip-mnt 给 CRIU。
                          宿主直启 agent 必须为 False，否则误用控制器 /proc/self 会错加 skip-mnt。
        """
        self.agent_pid = agent_pid
        self.use_tmpfs = use_tmpfs
        self.use_pid_namespace = use_pid_namespace

        if use_tmpfs:
            self.snapshot_store = os.path.join("/dev/shm", os.path.basename(snapshot_store))
        else:
            self.snapshot_store = snapshot_store
            
        self.registry = {}  # id -> { mem_path, parent_id, strategy }
        self.enable_predump = enable_predump
        self.enable_external = enable_external
        self.enable_prefetch = enable_prefetch
        self.beam_width = beam_width
        self.last_predump_dir = None
        self._prefetch_thread = None
        self._checkpoint_counter = 0
        # 用于检测进程树结构变化

        self._last_child_count = 0  
        
        os.makedirs(self.snapshot_store, exist_ok=True)
        print(f"[INIT] AdaptiveCRIUController initialized. Store: {self.snapshot_store}"
              f" tmpfs={use_tmpfs} predump={enable_predump} external={enable_external}"
              f" prefetch={enable_prefetch} beam_width={beam_width}"
              f" pid_ns={use_pid_namespace}")

    def _count_children(self) -> int:
        """统计 agent_pid 的子进程数量（含递归子树）。
        用于检测进程树结构是否发生变化，决定是否打断增量链。"""
        count = 0
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/status") as f:
                        for line in f:
                            if line.startswith("PPid:"):
                                ppid = int(line.split()[1])
                                # 直接子进程或间接子进程
                                if ppid == self.agent_pid:
                                    count += 1
                                break
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return count

    def _has_hot_regions(self) -> bool:
        """检测当前进程是否有 Hot 区域 (大匿名映射)"""
        try:
            result = classify_vmas(self.agent_pid)
            return len(result["hot"]) > 0
        except Exception:
            return False

    def _parse_mountinfo(self) -> list[tuple]:
        """
        解析 /proc/<pid>/mountinfo，返回 [(mount_id, mount_point), ...]
        按 mount_point 长度降序排列，用于最长前缀匹配。
        """
        entries = []
        try:
            with open(f"/proc/{self.agent_pid}/mountinfo") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        entries.append((int(parts[0]), parts[4]))
        except Exception:
            pass
        entries.sort(key=lambda x: len(x[1]), reverse=True)
        return entries

    def _get_cold_external_args(self) -> list[str]:
        """
        为冷区文件映射生成 CRIU --external mnt[] 参数。
        识别仅承载冷区（共享库、只读代码段）的挂载点，
        告知 CRIU 将这些挂载视为外部资源，在 dump 时跳过挂载状态序列化。
        """
        if not self.enable_external:
            return []

        try:
            result = classify_vmas(self.agent_pid)
        except Exception:
            return []

        cold_vmas = result["cold"]
        if not cold_vmas:
            return []

        cold_files = set()
        for vma in cold_vmas:
            p = vma.get("pathname", "")
            if p and not p.startswith("["):
                cold_files.add(p)

        if not cold_files:
            return []

        mountinfo = self._parse_mountinfo()

        root_mount_id = None
        for mid, mp in mountinfo:
            if mp == "/":
                root_mount_id = mid
                break

        cold_mount_ids = set()
        for filepath in cold_files:
            for mid, mp in mountinfo:
                if mp != "/" and filepath.startswith(mp + "/"):
                    cold_mount_ids.add(mid)
                    break

        cold_mount_ids.discard(root_mount_id)

        if not cold_mount_ids:
            return []

        args = []
        for mid in sorted(cold_mount_ids):
            args.extend(["--external", f"mnt[{mid}]:cold"])

        skipped_mb = result["summary"]["cold_size_mb"]
        print(f"  [EXTERNAL] Marking {len(cold_mount_ids)} cold mount(s) external, "
              f"covering {len(cold_files)} files (~{skipped_mb} MB)")

        return args

    @staticmethod
    def _measure_dump_size(dump_dir: str) -> int:
        """遍历 dump 目录，返回所有文件的总字节数。"""
        total = 0
        if not os.path.isdir(dump_dir):
            return 0
        for root, dirs, files in os.walk(dump_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    def _do_predump(self, predump_dir: str, parent_predump_dir: str = None):
        """执行 CRIU pre-dump (只复制内存页，不冻结进程)"""
        os.makedirs(predump_dir, exist_ok=True)

        cmd = [
            "criu", "pre-dump",
            "--tree", str(self.agent_pid),
            "-D", predump_dir,
            "--track-mem",
        ]

        if parent_predump_dir:
            rel = os.path.relpath(parent_predump_dir, predump_dir)
            cmd.extend(["--prev-images-dir", rel])

        t0 = time.time()
        subprocess.check_call(cmd)
        t1 = time.time()
        ms = (t1 - t0) * 1000

        try:
            os.kill(self.agent_pid, signal.SIGCONT)
        except ProcessLookupError:
            pass

        print(f"  [PRE-DUMP] Completed in {ms:.2f} ms")
        return ms

    def checkpoint_adaptive(self, parent_ckpt_id: str, tag: str,
                            raw_command: str = "") -> dict:
        """
        通过 SemanticParser 动态推测最佳 checkpoint 策略。
        """
        new_id = str(uuid.uuid4())[:8]
        
        # ── 1. 静态检测 ──
        base_strategy = InstructionSemanticParser.parse_strategy(raw_command)
        strategy = base_strategy
        
        # ── 2. 运行时降级/升级 ──
        if base_strategy == "predump" and not self.enable_predump:
            strategy = "standard"
            
        # 运行时热区探测增强 (如果只是普通的 standard run，如果有热区，升序为 pre-dump)
        if strategy == "standard" and self.enable_predump and self._has_hot_regions():
            strategy = "predump"

        print(f"[ADAPTIVE] Command='{raw_command}' → Parsed='{base_strategy}' → Final Strategy='{strategy}'")

        # ── 执行计时 ──
        total_t0 = time.time()
        predump_ms = 0.0
        dump_ms = 0.0

        curr_dir = os.path.join(self.snapshot_store, f"{tag}_{new_id}_mem")
        os.makedirs(curr_dir, exist_ok=True)

        # Phase 1: Pre-dump (如果需要)
        if strategy == "predump":
            predump_dir = os.path.join(self.snapshot_store, f"{tag}_{new_id}_predump")
            predump_ms = self._do_predump(
                predump_dir,
                parent_predump_dir=self.last_predump_dir
            )
            self.last_predump_dir = predump_dir
            parent_for_dump = predump_dir
        else:
            parent_for_dump = None
            walk_id = parent_ckpt_id
            while walk_id and walk_id in self.registry:
                entry = self.registry[walk_id]
                if entry.get("strategy") != "lightweight":
                    parent_for_dump = entry['mem_path']
                    break
                walk_id = entry.get("parent_id")

        # Phase 2: 正式 dump
        if strategy == "lightweight":
            # 轻量级：只保存一个标记文件（不调用 CRIU dump）
            # 在回滚时，使用上一个含完整 dump 的 checkpoint
            marker_file = os.path.join(curr_dir, "lightweight_marker.txt")
            with open(marker_file, "w") as f:
                f.write(f"parent={parent_ckpt_id}\naction=edit\ntimestamp={time.time()}\n")
            dump_ms = 0.0
            print(f"  [LIGHTWEIGHT] File-only snapshot (0 ms CRIU)")
        else:
            # 进程树变化检测：子进程数量变化时增量链失效
            # CRIU --prev-images-dir 要求父快照包含当前所有进程的 pages-*.img，
            # 但 background_run 新增的子进程在父快照中不存在，导致 page-xfer 报错。
            curr_child_count = self._count_children()
            tree_changed = (curr_child_count != self._last_child_count)
            if tree_changed and parent_for_dump:
                print(f"  [TREE-CHANGE] Children {self._last_child_count} → {curr_child_count}, "
                      f"dropping --prev-images-dir to avoid page-xfer errors")
                parent_for_dump = None  # 强制全量 dump 重建增量链
            self._last_child_count = curr_child_count

            # 标准或 pre-dump 后的正式 dump
            cmd = [
                "criu", "dump",
                "--tree", str(self.agent_pid),
                "-D", curr_dir,
                "--shell-job",
                "--leave-running",
                "--tcp-close",
                "--ext-unix-sk",
                "--track-mem",
            ]

            if parent_for_dump:
                rel_parent = os.path.relpath(parent_for_dump, curr_dir)
                cmd.extend(["--prev-images-dir", rel_parent])

            if self.enable_external:
                cmd.extend(self._get_cold_external_args())

            # 仅 unshare 子环境：私有 /dev、/dev/shm 等不应在宿主 restore 时重演。
            # 必须读 agent 的 mountinfo；误用 /proc/self/mountinfo 会在宿主模式下错加 --skip-mnt。
            if self.use_pid_namespace:
                NS_SKIP_MOUNTS = {'/dev', '/dev/pts', '/dev/shm', '/run'}
                try:
                    with open(f"/proc/{self.agent_pid}/mountinfo") as _mf:
                        mnt_out = _mf.read()
                    _skip_added = set()
                    for line in mnt_out.splitlines():
                        parts = line.split()
                        sep = parts.index('-') if '-' in parts else -1
                        if sep >= 0 and len(parts) > 4:
                            mp = parts[4]
                            if mp in NS_SKIP_MOUNTS and mp not in _skip_added:
                                _skip_added.add(mp)
                                cmd.extend(["--skip-mnt", mp])
                except Exception:
                    pass

            dt0 = time.time()
            subprocess.check_call(cmd)
            dt1 = time.time()
            dump_ms = (dt1 - dt0) * 1000

            # CRIU ptrace detach 有时不干净，确保进程被唤醒
            try:
                os.kill(self.agent_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

        total_ms = (time.time() - total_t0) * 1000

        dump_size_bytes = self._measure_dump_size(curr_dir) if strategy != "lightweight" else 0

        print(f"[PERF] Checkpoint {new_id}: dump={dump_ms:.2f}ms "
              f"predump={predump_ms:.2f}ms total={total_ms:.2f}ms "
              f"strategy={strategy} dump_size={dump_size_bytes/(1024*1024):.2f}MB")

        info = {
            "id": new_id,
            "mem_path": curr_dir,
            "parent_id": parent_ckpt_id,
            "strategy": strategy,
            "dump_ms": dump_ms,
            "predump_ms": predump_ms,
            "total_ms": total_ms,
            "dump_size_bytes": dump_size_bytes,
        }
        self.registry[new_id] = info

        # 轻量级快照不包含物理内存储存 → 记录所属父节点与待重放的指令列表
        if strategy == "lightweight":
            parent = self.registry.get(parent_ckpt_id, {})
            info["effective_restore_id"] = parent.get("effective_restore_id", parent_ckpt_id)
            replay_item = {"action": tag, "command": raw_command}
            info["replay_cmds"] = parent.get("replay_cmds", []) + [replay_item]
            info["replay_cmd"] = replay_item  # 保留单数形式兼容性

        return info

    def restore(self, target_ckpt_id: str, cleanup_fn=None):
        """
        恢复到目标 checkpoint。
        cleanup_fn: 可选的回调函数，在 kill 进程后/restore 前执行
                    (用于 PID Namespace 清理)
        """
        if target_ckpt_id not in self.registry:
            raise ValueError(f"Unknown checkpoint ID: {target_ckpt_id}")

        target = self.registry[target_ckpt_id]

        # 核心改动：如果是轻量级快照，则物理回滚到它的有效父节点，并提取重放指令列表
        effective_id = target.get("effective_restore_id", target_ckpt_id)
        replay_cmds = target.get("replay_cmds", [])
        
        if effective_id != target_ckpt_id:
            print(f"[ADAPTIVE] Side-Effect-Aware Lightweight node '{target_ckpt_id}' detected.")
            print(f"           → Redirecting physical restore to Parent ID: {effective_id}")
            print(f"           → Will request Controller to Replay {len(replay_cmds)} command(s): {replay_cmds}")
            physical_target = self.registry[effective_id]
        else:
            physical_target = target

        print(f"[CRIU] Restoring to physical snapshot {effective_id}...")

        # 1. 杀掉 agent 整个进程组 + 同 session 所有孤儿
        sid = self.agent_pid  # start_new_session=True → SID == PID
        try:
            pgid = os.getpgid(self.agent_pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(self.agent_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        time.sleep(0.05)
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    stat = f.read().split()
                    if int(stat[5]) == sid:
                        os.kill(int(entry), signal.SIGKILL)
            except (OSError, IndexError, ValueError, ProcessLookupError, PermissionError):
                continue

        try:
            os.waitpid(self.agent_pid, 0)
        except (ChildProcessError, ProcessLookupError, OSError):
            pass

        # 2. 可选的 Namespace 清理
        if cleanup_fn:
            cleanup_fn()

        # 3. 等待 session 中所有 PID 释放
        t0 = time.time()
        while time.time() - t0 < 5.0:
            found = False
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat") as f:
                        stat = f.read().split()
                        if int(stat[5]) == sid:
                            found = True
                            os.kill(int(entry), signal.SIGKILL)
                except (OSError, IndexError, ValueError, ProcessLookupError, PermissionError):
                    continue
            if not found:
                break
            time.sleep(0.1)

        # [Direction 5] CRIU Lazy Restore (userfaultfd 按需加载)
        # 仅在 tmpfs 模式下启用——磁盘上 lazy-pages 的随机读反而更慢
        lazy_sock = None
        if self.use_tmpfs:
            lazy_sock = f"/tmp/criu_lazy_{effective_id[:6]}.sock"
            if os.path.exists(lazy_sock):
                os.remove(lazy_sock)

            daemon_cmd = [
                "criu", "lazy-pages",
                "-D", physical_target['mem_path'],
                "--address", lazy_sock,
                "-d",
            ]
            try:
                print("[CRIU] Starting lazy-pages daemon (tmpfs mode)...")
                subprocess.check_call(daemon_cmd)
            except Exception as e:
                print(f"[WARNING] Failed to start lazy-pages: {e}. Falling back to standard restore.")
                lazy_sock = None

        # 4. CRIU restore
        cmd = [
            "criu", "restore",
            "-D", physical_target['mem_path'],
            "--shell-job",
            "--restore-detached",
            "--tcp-close",
            "--ext-unix-sk",
        ]

        if lazy_sock:
            cmd.extend(["--lazy-pages", "--address", lazy_sock])

        t0 = time.time()
        subprocess.check_call(cmd)
        t1 = time.time()
        restore_ms = (t1 - t0) * 1000

        print(f"[PERF] Restore {effective_id}: {restore_ms:.2f} ms")
        print(f"[CRIU] Agent restored with PID {self.agent_pid}")

        # [Direction 5] 启动用户态后台预取线程
        if lazy_sock and self.enable_prefetch:
            self._start_background_prefetch(self.agent_pid)
        
        return {
            "restore_ms": restore_ms,
            "replay_cmds": replay_cmds,
            "replay_cmd": replay_cmds[-1] if replay_cmds else None
        }

    # ════════════════════════════════════════════════════════
    #  [Direction 4] Beam Search 裁剪式 GC + 全量锚点
    # ════════════════════════════════════════════════════════

    def garbage_collect(self, obsolete_ids: list) -> int:
        """
        底层 GC：物理删除指定的快照目录并从注册表中移除。
        返回释放的字节数。
        """
        freed_bytes = 0
        for ckpt_id in list(obsolete_ids):
            if ckpt_id in self.registry:
                mem_path = self.registry[ckpt_id]['mem_path']
                if os.path.exists(mem_path):
                    for root, dirs, files in os.walk(mem_path):
                        for f in files:
                            try:
                                freed_bytes += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                    try:
                        shutil.rmtree(mem_path)
                    except Exception as e:
                        print(f"[GC] Error removing {mem_path}: {e}")
                del self.registry[ckpt_id]

        if freed_bytes > 0:
            print(f"  [GC] Reclaimed {freed_bytes / (1024*1024):.2f} MB")
        return freed_bytes

    def beam_prune(self, active_ids: set) -> dict:
        """
        Beam Search 裁剪：只保留 active_ids 中的节点及其祖先链上
        的所有快照，回收其余全部。

        在 Beam Search 中，每轮展开后只保留得分最高的 beam_width 个分支。
        对于 MCTS 而言，可以将"当前正在探索的叶节点集合"作为 active_ids。

        返回 {"pruned_count": N, "freed_bytes": M}
        """
        all_keep = set(active_ids)
        for aid in active_ids:
            current = aid
            while current and current in self.registry:
                if current in all_keep:
                    current = self.registry[current].get("parent_id")
                    continue
                all_keep.add(current)
                current = self.registry[current].get("parent_id")

        obsolete = [cid for cid in list(self.registry.keys()) if cid not in all_keep]

        if not obsolete:
            return {"pruned_count": 0, "freed_bytes": 0}

        print(f"[BEAM-PRUNE] Keeping {len(all_keep)} nodes, pruning {len(obsolete)}")
        freed = self.garbage_collect(obsolete)
        return {"pruned_count": len(obsolete), "freed_bytes": freed}

    def maybe_anchor_checkpoint(self, current_id: str, interval: int = 10) -> str | None:
        """
        增量链打断 (Full Anchor Checkpoint)。
        每隔 interval 步，生成一个不依赖 --prev-images-dir 的绝对全量快照,
        防止增量链过长导致恢复速度退化。

        返回新锚点的 id，如果本次不触发则返回 None。
        """
        self._checkpoint_counter += 1
        if self._checkpoint_counter % interval != 0:
            return None

        if current_id not in self.registry:
            return None

        print(f"[ANCHOR] Generating full anchor checkpoint (every {interval} steps)...")
        new_id = str(uuid.uuid4())[:8]
        curr_dir = os.path.join(self.snapshot_store, f"anchor_{new_id}_mem")
        os.makedirs(curr_dir, exist_ok=True)

        cmd = [
            "criu", "dump",
            "--tree", str(self.agent_pid),
            "-D", curr_dir,
            "--shell-job",
            "--leave-running",
            "--tcp-close",
            "--ext-unix-sk",
        ]

        t0 = time.time()
        subprocess.check_call(cmd)
        dump_ms = (time.time() - t0) * 1000

        try:
            os.kill(self.agent_pid, signal.SIGCONT)
        except ProcessLookupError:
            pass

        dump_size = self._measure_dump_size(curr_dir)

        info = {
            "id": new_id,
            "mem_path": curr_dir,
            "parent_id": current_id,
            "strategy": "anchor",
            "dump_ms": dump_ms,
            "predump_ms": 0,
            "total_ms": dump_ms,
            "dump_size_bytes": dump_size,
            "is_anchor": True,
        }
        self.registry[new_id] = info
        print(f"[ANCHOR] Full anchor {new_id}: {dump_ms:.2f}ms, "
              f"{dump_size/(1024*1024):.2f}MB (breaks --prev-images-dir chain)")
        return new_id

    # ════════════════════════════════════════════════════════
    #  [Direction 5] 用户态后台预取线程 (Background Prefetch)
    # ════════════════════════════════════════════════════════

    def _read_prefetch_ranges(self, pid: int) -> list[tuple[int, int]]:
        """
        读取目标进程的 /proc/<pid>/maps，提取匿名可写区域的地址范围。
        只预取 Warm/Hot 匿名区（这些是 lazy-pages 需要从 dump 中恢复的区域）。
        文件映射的冷区由内核 page cache 自行处理，无需额外预取。
        """
        ranges = []
        try:
            with open(f"/proc/{pid}/maps") as f:
                for line in f:
                    parts = line.strip().split(None, 5)
                    if len(parts) < 5:
                        continue
                    perms = parts[1]
                    pathname = parts[5] if len(parts) > 5 else ""
                    if "w" not in perms:
                        continue
                    is_anon = pathname.strip() == "" or pathname.strip().startswith("[")
                    if not is_anon:
                        continue
                    addr_range = parts[0].split("-")
                    start = int(addr_range[0], 16)
                    end = int(addr_range[1], 16)
                    ranges.append((start, end))
        except Exception:
            pass
        return ranges

    def _start_background_prefetch(self, pid: int) -> threading.Thread | None:
        """
        Lazy Restore 后的用户态后台预取线程。

        原理：lazy restore 完成后进程已在运行，但大部分内存页尚未实际加载
        （由 userfaultfd + lazy-pages daemon 按需服务）。本方法通过读取
        /proc/<pid>/mem 主动顺序触发缺页中断，使 lazy-pages daemon 提前
        将页面从 tmpfs dump 文件搬入进程地址空间。

        效果：Agent 后续实际使用这些内存时不再触发 page fault，
        将随机缺页延迟转化为一次性的顺序预取 I/O。
        """
        if not self.enable_prefetch:
            return None

        ranges = self._read_prefetch_ranges(pid)
        if not ranges:
            return None

        total_bytes = sum(end - start for start, end in ranges)
        print(f"  [PREFETCH] Starting background prefetch: "
              f"{len(ranges)} regions, ~{total_bytes/(1024*1024):.1f} MB")

        def _prefetch_worker():
            pages_touched = 0
            try:
                fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
                try:
                    for start, end in ranges:
                        offset = start
                        while offset < end:
                            try:
                                os.lseek(fd, offset, os.SEEK_SET)
                                os.read(fd, 1)
                                pages_touched += 1
                            except OSError:
                                break
                            offset += 4096
                finally:
                    os.close(fd)
            except Exception as e:
                print(f"  [PREFETCH] Error: {e}")
                return
            print(f"  [PREFETCH] Done: {pages_touched} pages "
                  f"({pages_touched * 4096 / (1024*1024):.1f} MB) prefetched")

        t = threading.Thread(target=_prefetch_worker, daemon=True, name="criu-prefetch")
        t.start()
        self._prefetch_thread = t
        return t
