"""Read-only external page prefetch via /proc/<pid>/mem.

This optional background walk does not pre-pay CoW write faults. Writing a
previously read byte into a running process races with its own stores and can
corrupt memory. Historical write-prewarm is therefore rejected synchronously.
"""
from __future__ import annotations

import json
import os
import threading
import time

PAGE_SIZE = 4096
HOT_ANON_THRESHOLD = 4 * 1024 * 1024  # ≥4 MiB anon RW → treat as hot

# Per-call JSONL log for Table 1 / §6.2 ground truth. One line per prewarm
# invocation: {"ts", "pid", "ranges", "pages", "skipped", "elapsed_ms",
#              "target_rss_mb"}. Consumed by benchmarks/plot_figures.py.
PREWARM_LOG = os.environ.get("PREWARM_LOG", "/tmp/prewarm.jsonl")


def _read_rss_mb(pid: int) -> float:
    """Return VmRSS in MB for `pid`, or -1 if unavailable."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return -1.0


def _log_result(pid: int, result: dict, rss_mb: float) -> None:
    try:
        with open(PREWARM_LOG, "a") as f:
            row = {"ts": time.time(), "pid": pid,
                   "target_rss_mb": rss_mb, **result}
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _classify_tiered_ranges(pid: int) -> list[tuple[int, list[tuple[int, int]]]]:
    """Walk /proc/<pid>/maps and bucket every writable RW range into priority
    tiers. Returns [(tier_id, [(start, end), ...]), ...] in walk order.

    Tier rule (static, derived from offline profiling of typical Python
    agent post-restore behavior; see benchresults/.../profile_tier_rule.md):

      T1: [heap], [stack], anonymous RW >= 4 MiB           (always touched)
      T2: anonymous RW < 4 MiB                              (glibc/jemalloc
                                                             small arenas)
      T3: file-backed RW (.data segments of .so libs etc)   (sometimes)
      T4: residual RW (named anon, /dev/shm, etc.)          (rarely)

    Pages with no `w` permission (RO/exec) and kernel pseudo-VMAs
    ([vvar]/[vdso]/[vsyscall]) are excluded entirely: writing them through
    /proc/<pid>/mem is rejected by the kernel and they cause no CoW fault
    in the agent's address space anyway.

    BENCH_WARM_MODE env var:
      - "tiered" (default): walk T1 -> T2 -> T3 -> T4 in priority order
      - "naive":            walk every RW range as a single bucket
                             (no priority order, single tier)
    """
    mode = os.environ.get("BENCH_WARM_MODE",
            "naive" if os.environ.get("BENCH_NAIVE_WARM") == "1" else "tiered")
    tiers: dict[int, list[tuple[int, int]]] = {1: [], 2: [], 3: [], 4: []}
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                parts = line.strip().split(None, 5)
                if len(parts) < 5:
                    continue
                addr  = parts[0]
                perms = parts[1]
                path  = parts[5] if len(parts) > 5 else ""
                if "w" not in perms or "r" not in perms:
                    continue
                if path.startswith(("[vvar]", "[vdso]", "[vsyscall]")):
                    continue
                a, b = addr.split("-")
                start = int(a, 16); end = int(b, 16)
                size = end - start
                # Tier classification.
                if path in ("[heap]", "[stack]") or \
                   (not path and size >= HOT_ANON_THRESHOLD):
                    tier = 1
                elif not path:                       # anon, < 4 MiB
                    tier = 2
                elif path.startswith(("/", "./", "../")):  # file-backed
                    tier = 3
                else:                                # named anon, /dev/shm, etc.
                    tier = 4
                tiers[tier].append((start, end))
    except OSError:
        pass
    if mode == "naive":
        # Single bucket, no priority order — for ablation control.
        return [(0, tiers[1] + tiers[2] + tiers[3] + tiers[4])]
    return [(t, tiers[t]) for t in (1, 2, 3, 4) if tiers[t]]


def _classify_target_hot_ranges(pid: int) -> list[tuple[int, int]]:
    """Backward-compatible flat range list (concatenated tiers in order)."""
    out: list[tuple[int, int]] = []
    for _tier, ranges in _classify_tiered_ranges(pid):
        out.extend(ranges)
    return out


def validate_prewarm_mode() -> str:
    mode = os.environ.get("DELTABOX_PREWARM_MODE", "read").strip().lower()
    if mode not in {"read", "readonly", "read-only"}:
        raise ValueError(
            f"Unsafe or unknown DELTABOX_PREWARM_MODE={mode!r}; "
            "only read-only prefetch is supported for a running target. "
            "Read/write-back can overwrite concurrent agent mutations.")
    return "read"


def prewarm_external(target_pid: int) -> dict:
    """Touch every writable page in `target_pid` via /proc/<pid>/mem,
    walking tiers in priority order (T1 -> T2 -> T3 -> T4).

    Synchronous; returns timing/pages_touched. Caller is expected to
    invoke this on a background thread so it overlaps the agent's
    first post-restore turn. Set BENCH_WARM_MODE=naive to bypass tier
    ordering (single-bucket walk for the ablation control).
    """
    mode = validate_prewarm_mode()
    rss_mb = _read_rss_mb(target_pid)
    tiered = _classify_tiered_ranges(target_pid)
    n_ranges_total = sum(len(r) for _, r in tiered)
    write_cow = False
    t0 = time.time()
    n_pages = 0; n_skipped = 0
    tier_stats = []
    try:
        fd = os.open(
            f"/proc/{target_pid}/mem",
            os.O_RDONLY,
        )
    except OSError as e:
        result = {"ok": False, "error": str(e), "pages": 0,
                  "elapsed_ms": 0.0, "ranges": n_ranges_total,
                  "mode": mode, "write_cow": write_cow}
        _log_result(target_pid, result, rss_mb)
        return result
    try:
        for tier_id, ranges in tiered:
            t_tier_start = time.time()
            tier_pages = 0
            for start, end in ranges:
                for addr in range(start, end, PAGE_SIZE):
                    try:
                        b = os.pread(fd, 1, addr)
                        if not b:
                            n_skipped += 1
                            continue
                        n_pages += 1
                        tier_pages += 1
                    except OSError:
                        n_skipped += 1
            tier_stats.append({
                "tier": tier_id,
                "n_ranges": len(ranges),
                "pages": tier_pages,
                "elapsed_ms": (time.time() - t_tier_start) * 1000.0,
            })
    finally:
        os.close(fd)
    result = {"ok": True, "pages": n_pages, "skipped": n_skipped,
              "ranges": n_ranges_total,
              "tiers": tier_stats,
              "mode": mode,
              "write_cow": write_cow,
              "elapsed_ms": (time.time() - t0) * 1000.0}
    _log_result(target_pid, result, rss_mb)
    return result


def spawn_prewarm(target_pid: int) -> threading.Thread:
    """Background thread (in GSD) that prewarms `target_pid`'s hot zones."""
    validate_prewarm_mode()  # Fail in the caller, never silently in a thread.
    def _run():
        prewarm_external(target_pid)
    t = threading.Thread(target=_run,
                         name=f"prewarm-{target_pid}", daemon=True)
    t.start()
    return t
