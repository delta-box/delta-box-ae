#!/usr/bin/env python3
"""diff_merger.py — Merge Firecracker Diff snapshot memory file into base.

Firecracker Diff snapshots are sparse memory files: pages dirtied since the
previous snapshot are written at their original byte offsets, the rest are
holes (SEEK_HOLE returns the next data start).

To restore from "base + accumulated Diffs" we copy base to a fresh output
file, then for each Diff in chronological order, walk SEEK_DATA / SEEK_HOLE
pairs and copy the dirty regions on top of the output.

Per Firecracker docs (snapshot-support.md): vmstate is full per snapshot
(no diff); memory is the only diffed component. So restore semantics:
    merged_mem = base + diff_1 + diff_2 + ... + diff_N (later wins)
    vmstate    = vmstate_N (latest snapshot's vmstate)
"""
from __future__ import annotations

import os
import shutil
import time

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB I/O chunks


def _copy_data_regions(diff_path: str, out_fd: int) -> tuple[int, int]:
    """Walk Diff file's SEEK_DATA/SEEK_HOLE regions, write to out_fd at same offsets.

    Returns (regions_copied, bytes_copied).
    """
    fd_diff = os.open(diff_path, os.O_RDONLY)
    try:
        st = os.fstat(fd_diff)
        file_size = st.st_size
        n_regions = 0
        n_bytes = 0
        pos = 0
        while pos < file_size:
            # Find next data region
            try:
                data_start = os.lseek(fd_diff, pos, os.SEEK_DATA)
            except OSError:
                break  # No more data (ENXIO)
            if data_start >= file_size:
                break
            # Find end of this data region
            try:
                data_end = os.lseek(fd_diff, data_start, os.SEEK_HOLE)
            except OSError:
                data_end = file_size
            # Copy [data_start, data_end) from diff to out at same offset
            os.lseek(fd_diff, data_start, os.SEEK_SET)
            os.lseek(out_fd, data_start, os.SEEK_SET)
            remaining = data_end - data_start
            while remaining > 0:
                chunk = os.read(fd_diff, min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                os.write(out_fd, chunk)
                remaining -= len(chunk)
                n_bytes += len(chunk)
            n_regions += 1
            pos = data_end
        return n_regions, n_bytes
    finally:
        os.close(fd_diff)


def merge(base_mem_path: str, diff_mem_paths: list[str], out_mem_path: str) -> dict:
    """Merge base + ordered Diffs into out_mem_path.

    Returns dict with timing/sizes:
        {merge_ms, base_copy_ms, diff_apply_ms, n_diffs, total_dirty_bytes,
         out_size_bytes}
    """
    t0 = time.perf_counter()

    # Step 1: clone base to output preserving sparseness (shutil.copyfile is NOT sparse-aware
    # and would materialize a 4 GiB file on tmpfs, blowing it up).
    t_base0 = time.perf_counter()
    if os.path.exists(out_mem_path):
        os.unlink(out_mem_path)
    import subprocess
    subprocess.check_call(["cp", "--sparse=always", base_mem_path, out_mem_path])
    base_copy_ms = (time.perf_counter() - t_base0) * 1000.0

    # Step 2: open out file rw + apply each diff in order
    t_diff0 = time.perf_counter()
    fd_out = os.open(out_mem_path, os.O_RDWR)
    total_regions = 0
    total_bytes = 0
    try:
        for diff_path in diff_mem_paths:
            r, b = _copy_data_regions(diff_path, fd_out)
            total_regions += r
            total_bytes += b
    finally:
        os.fsync(fd_out)
        os.close(fd_out)
    diff_apply_ms = (time.perf_counter() - t_diff0) * 1000.0

    out_size = os.path.getsize(out_mem_path)
    return {
        "merge_ms": round((time.perf_counter() - t0) * 1000.0, 3),
        "base_copy_ms": round(base_copy_ms, 3),
        "diff_apply_ms": round(diff_apply_ms, 3),
        "n_diffs": len(diff_mem_paths),
        "n_dirty_regions": total_regions,
        "total_dirty_bytes": total_bytes,
        "out_size_bytes": out_size,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("usage: diff_merger.py <base_mem> <diff_mem_1> [diff_mem_2 ...] <out_mem>",
              file=sys.stderr)
        sys.exit(1)
    base = sys.argv[1]
    out = sys.argv[-1]
    diffs = sys.argv[2:-1]
    print(merge(base, diffs, out))
