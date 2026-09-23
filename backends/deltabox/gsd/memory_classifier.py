#!/usr/bin/env python3
"""
memory_classifier.py — Workload-Aware 内存区域分类器

读取 /proc/<pid>/maps 和 /proc/<pid>/smaps，将进程的虚拟内存区域 (VMA)
按语义分为三类：

  - Cold (冷区): 库代码段、共享库的只读映射。几乎不会变脏。
                  优化：可以标记为 CRIU --external 跳过，或者首次 dump 后永不更新。
  - Warm (温区): Agent 的 Python 堆、chat_history 等追加增长的小区域。
                  优化：标准增量 --track-mem。
  - Hot  (热区): 巨大的匿名 RW 映射 (>= HOT_THRESHOLD)，通常来自
                  Pandas/Numpy 的 ndarray 或 mmap 分配。
                  优化：先做 criu pre-dump 异步预拷贝，再做最终 dump。
"""

import os
import re
import sys
import json

# ──────────────── 分类阈值 ────────────────
# 匿名 RW 区域超过此阈值 (字节) 即判定为 Hot
HOT_THRESHOLD = 100 * 1024 * 1024  # 100 MB


def parse_maps(pid: int) -> list[dict]:
    """解析 /proc/<pid>/maps，返回 VMA 列表"""
    maps_file = f"/proc/{pid}/maps"
    vmas = []
    with open(maps_file, "r") as f:
        for line in f:
            parts = line.strip().split(None, 5)
            if len(parts) < 5:
                continue
            addr_range = parts[0]
            perms = parts[1]
            offset = parts[2]
            dev = parts[3]
            inode = parts[4]
            pathname = parts[5] if len(parts) > 5 else ""

            start_addr, end_addr = addr_range.split("-")
            start = int(start_addr, 16)
            end = int(end_addr, 16)
            size = end - start

            vmas.append({
                "start": start,
                "end": end,
                "size": size,
                "perms": perms,
                "offset": offset,
                "dev": dev,
                "inode": inode,
                "pathname": pathname.strip(),
            })
    return vmas


def parse_smaps_rss(pid: int) -> dict:
    """
    解析 /proc/<pid>/smaps，返回每个 VMA 起始地址到 RSS (KB) 的映射。
    用于剔除 size 很大但实际驻留为 0 的 VMA。
    """
    smaps_file = f"/proc/{pid}/smaps"
    rss_map = {}
    current_start = None

    with open(smaps_file, "r") as f:
        for line in f:
            # 新 VMA 头部行
            m = re.match(r'^([0-9a-f]+)-([0-9a-f]+)\s', line)
            if m:
                current_start = int(m.group(1), 16)
                continue
            # Rss 行
            m = re.match(r'^Rss:\s+(\d+)\s+kB', line)
            if m and current_start is not None:
                rss_map[current_start] = int(m.group(1)) * 1024  # 转为字节
    return rss_map


def classify_vmas(pid: int) -> dict:
    """
    对目标进程的所有 VMA 进行分类。
    返回 { "cold": [...], "warm": [...], "hot": [...], "summary": {...} }
    """
    vmas = parse_maps(pid)
    rss_map = parse_smaps_rss(pid)

    cold = []
    warm = []
    hot = []

    total_cold_size = 0
    total_warm_size = 0
    total_hot_size = 0

    for vma in vmas:
        perms = vma["perms"]
        pathname = vma["pathname"]
        size = vma["size"]
        rss = rss_map.get(vma["start"], 0)
        is_anonymous = (pathname == "" or pathname.startswith("["))
        is_file_backed = not is_anonymous
        readable = "r" in perms
        writable = "w" in perms
        executable = "x" in perms

        vma["rss"] = rss
        vma["is_anonymous"] = is_anonymous

        # ── 分类逻辑 ──

        # 1. 冷区：文件映射且带执行权限 (代码段)，或者只读的库数据段
        if is_file_backed and (executable or not writable):
            vma["zone"] = "cold"
            cold.append(vma)
            total_cold_size += size
            continue

        # 2. 热区：巨大的匿名可写区域 (Pandas/Numpy 缓冲)
        if is_anonymous and writable and size >= HOT_THRESHOLD:
            vma["zone"] = "hot"
            hot.append(vma)
            total_hot_size += size
            continue

        # 3. 温区：其他所有 (Python 堆、小匿名段、可写的文件映射数据段等)
        vma["zone"] = "warm"
        warm.append(vma)
        total_warm_size += size

    summary = {
        "pid": pid,
        "total_vmas": len(vmas),
        "cold_count": len(cold),
        "warm_count": len(warm),
        "hot_count": len(hot),
        "cold_size_mb": round(total_cold_size / (1024 * 1024), 2),
        "warm_size_mb": round(total_warm_size / (1024 * 1024), 2),
        "hot_size_mb": round(total_hot_size / (1024 * 1024), 2),
    }

    return {
        "cold": cold,
        "warm": warm,
        "hot": hot,
        "summary": summary,
    }


def get_hot_region_addresses(pid: int) -> list[tuple[int, int]]:
    """返回热区的地址范围列表 [(start, end), ...]，供 CRIU pre-dump 使用"""
    result = classify_vmas(pid)
    return [(v["start"], v["end"]) for v in result["hot"]]


def print_report(pid: int):
    """打印人类可读的分类报告"""
    result = classify_vmas(pid)
    s = result["summary"]

    print(f"=== Memory Classification Report for PID {pid} ===")
    print(f"Total VMAs: {s['total_vmas']}")
    print(f"  Cold (skip):   {s['cold_count']} regions, {s['cold_size_mb']} MB")
    print(f"  Warm (incr):   {s['warm_count']} regions, {s['warm_size_mb']} MB")
    print(f"  Hot  (pre-dump): {s['hot_count']} regions, {s['hot_size_mb']} MB")

    if result["hot"]:
        print("\n--- Hot Regions (candidates for pre-dump) ---")
        for v in result["hot"]:
            rss_mb = round(v["rss"] / (1024 * 1024), 2)
            size_mb = round(v["size"] / (1024 * 1024), 2)
            print(f"  0x{v['start']:x}-0x{v['end']:x}  "
                  f"size={size_mb}MB  rss={rss_mb}MB  "
                  f"perms={v['perms']}  path={v['pathname'] or '[anon]'}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <PID>")
        sys.exit(1)
    target_pid = int(sys.argv[1])
    print_report(target_pid)
