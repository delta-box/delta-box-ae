#!/usr/bin/env python3
"""swesearch_replay_engine.py — Per-edit replay of swe-search/mcts diffs on
overlay-mounted testbed, measuring isolated copy-up + phys I/O per edit.

For each edit:
  1. Mount overlay (lower=testbed, upper=fresh) — fresh upper per edit so
     copy-up bytes attribute cleanly to that edit alone.
  2. Read target file from merged view, record file_size_bytes.
  3. Apply unified diff in memory → produce new content.
  4. Write back via mmap-friendly partial-write (seek to first-diff offset)
     — preserves prefix reflink share with lower.
  5. Snapshot: FIEMAP non-shared bytes in upper, /sys/block writes delta.
  6. Unmount overlay; loop to next edit (each gets a fresh upper).

The per-edit measurement requires unmounting between edits — if we kept
the same merged mount, copy-up effects would accumulate across edits.

Output: jsonl, one row per edit:
  {instance, fs_arm, edit_idx, file_path, file_size_bytes,
   diff_bytes, copyup_bytes, phys_bytes, applied_ok}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NONSHARED_TOOL = str(Path(__file__).with_name("nonshared_bytes.py"))


# ---- unified diff parser (minimal, per-hunk) ----

def _repair_diff(diff_text: str) -> str:
    """moatless traces sometimes record diffs where a line starting with
    '-' and the following '+' line are joined when the original source had
    no trailing newline. Pattern: ``-foo bar+baz qux`` should become two
    lines. Repair: split any line that starts with '-' and contains a
    '+' which is followed by space-then-content (the standard '+ ' diff
    column convention)."""
    fixed_lines = []
    for line in diff_text.splitlines():
        if line.startswith("-"):
            # Look for "+<space-or-tab>" inside the line — that's where
            # the next hunk-line was concatenated.
            for sep_idx in range(1, len(line) - 1):
                if line[sep_idx] == "+" and line[sep_idx + 1] in " \t":
                    # split here
                    fixed_lines.append(line[:sep_idx])
                    fixed_lines.append(line[sep_idx:])
                    break
            else:
                fixed_lines.append(line)
        else:
            fixed_lines.append(line)
    # preserve trailing newline if original had one
    out = "\n".join(fixed_lines)
    if diff_text.endswith("\n"):
        out += "\n"
    return out


def apply_unified_diff(orig: bytes, diff_text: str) -> tuple[bytes, int]:
    """Apply a unified diff to `orig` (file bytes), return (new_bytes,
    first_diff_offset). first_diff_offset is the byte offset in `orig` of
    the first line that any hunk modifies — used so the writer can seek
    there and preserve prefix reflink for everything before it.

    Limitations: assumes diff is well-formed, single-file, unified format
    with @@ -<line>,<count> +<line>,<count> @@ headers.
    """
    diff_text = _repair_diff(diff_text)
    lines = diff_text.splitlines(keepends=True)
    # Skip header lines (--- / +++)
    i = 0
    while i < len(lines) and not lines[i].startswith("@@"):
        i += 1
    if i == len(lines):
        # No hunks → empty diff
        return orig, len(orig)

    orig_lines = orig.split(b"\n")
    # split keeps no terminator; remember if file ended with \n
    file_had_trailing_nl = orig.endswith(b"\n")
    if file_had_trailing_nl:
        # Pop the empty last element from split
        if orig_lines and orig_lines[-1] == b"":
            orig_lines.pop()

    new_lines: list[bytes] = []
    cursor = 0  # 0-based index into orig_lines
    first_diff_line: int | None = None

    hunk_re = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    while i < len(lines):
        header = lines[i]
        if not header.startswith("@@"):
            i += 1
            continue
        m = hunk_re.match(header.encode())
        if not m:
            i += 1
            continue
        old_start = int(m.group(1)) - 1  # 1-based → 0-based
        if old_start < 0:
            old_start = 0
        # copy unchanged lines from cursor to old_start
        while cursor < old_start:
            new_lines.append(orig_lines[cursor])
            cursor += 1
        if first_diff_line is None:
            first_diff_line = old_start

        i += 1
        # process hunk body until next @@ or EOF
        while i < len(lines) and not lines[i].startswith("@@"):
            ln = lines[i]
            # strip the trailing \n only for content comparison
            if ln.endswith("\n"):
                content = ln[:-1].encode()
            else:
                content = ln.encode()
            tag = content[:1] if content else b" "
            body = content[1:]
            if tag == b" ":
                # context line: must match orig
                new_lines.append(body)
                cursor += 1
            elif tag == b"-":
                # remove from orig
                cursor += 1
            elif tag == b"+":
                # add new line
                new_lines.append(body)
            elif tag == b"\\":
                # "\ No newline at end of file" — track but no-op for content
                pass
            i += 1

    # append remaining unchanged tail
    while cursor < len(orig_lines):
        new_lines.append(orig_lines[cursor])
        cursor += 1

    new_bytes = b"\n".join(new_lines)
    if file_had_trailing_nl:
        new_bytes += b"\n"

    if first_diff_line is None:
        return new_bytes, len(orig)
    # Compute byte offset corresponding to first_diff_line
    if first_diff_line == 0:
        first_diff_offset = 0
    else:
        first_diff_offset = sum(len(orig_lines[k]) for k in range(first_diff_line)) + first_diff_line  # +1 \n per line
        if first_diff_offset > len(orig):
            first_diff_offset = len(orig)
    return new_bytes, first_diff_offset


def write_partial(path: Path, new_bytes: bytes, first_diff_offset: int):
    """Write new_bytes back to `path` while preserving reflink share for
    bytes before `first_diff_offset`.

    open(O_RDWR) — does NOT truncate, so overlayfs copy-up uses
    vfs_clone_file_range to reflink the lower file into upper.
    Then seek to first_diff_offset and write new_bytes[first_diff_offset:].
    Truncate to len(new_bytes).
    """
    fd = os.open(str(path), os.O_RDWR)
    try:
        os.lseek(fd, first_diff_offset, os.SEEK_SET)
        os.write(fd, new_bytes[first_diff_offset:])
        os.ftruncate(fd, len(new_bytes))
        os.fsync(fd)
    finally:
        os.close(fd)


# ---- overlay mount helpers ----

def mount_overlay(lower: str, upper: str, work: str, merged: str):
    os.makedirs(upper, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    os.makedirs(merged, exist_ok=True)
    subprocess.check_call([
        "mount", "-t", "overlay", "overlay",
        "-o", f"lowerdir={lower},upperdir={upper},workdir={work}",
        merged,
    ])


def umount_overlay(merged: str):
    subprocess.run(["umount", "-l", merged], check=False)


def read_loop_writes(loop_name: str) -> int:
    stat_file = f"/sys/block/{loop_name}/stat"
    if os.path.exists(stat_file):
        return int(open(stat_file).read().split()[6])  # writes
    raise FileNotFoundError(stat_file)


def fiemap_nonshared(path: Path) -> int:
    if not path.is_file():
        return 0
    cp = subprocess.run(
        ["python3", NONSHARED_TOOL, str(path)],
        capture_output=True, check=True,
    )
    try:
        return int(cp.stdout.strip())
    except (ValueError, AttributeError) as error:
        raise ValueError("FIEMAP helper did not return a byte count") from error


# ---- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", required=True)
    ap.add_argument("--lower", required=True)
    ap.add_argument("--upper-base", required=True)
    ap.add_argument("--merged", required=True)
    ap.add_argument("--fs-mnt", required=True)
    ap.add_argument("--ovl-ioctl-bin", required=True)
    ap.add_argument("--loop-name", required=True)
    ap.add_argument("--fs-arm", required=True)
    ap.add_argument("--instance", required=True)
    ap.add_argument("--out-jsonl", required=True)
    args = ap.parse_args()

    info = json.load(open(args.actions))
    edits = info["edits"]
    out = open(args.out_jsonl, "w")

    for edit in edits:
        ei = edit["edit_idx"]
        rel_path = edit["file_path"]
        diff = edit["diff"]
        upper = f"{args.upper_base}/u{ei:03d}"
        work = f"{args.upper_base}/w{ei:03d}"
        os.makedirs(upper, exist_ok=True)
        os.makedirs(work, exist_ok=True)
        # mount overlay fresh for this edit (lower = original testbed)
        mount_overlay(args.lower, upper, work, args.merged)

        target = Path(args.merged) / rel_path
        result = {
            "instance": args.instance,
            "fs_arm": args.fs_arm,
            "edit_idx": ei,
            "transition_id": edit["transition_id"],
            "file_path": rel_path,
            "diff_bytes": edit["diff_bytes"],
        }

        try:
            if not target.exists():
                result.update({"applied_ok": False, "error": "target file not in lower",
                              "file_size_bytes": 0, "copyup_bytes": 0, "phys_bytes": 0})
                raise FileNotFoundError("target file not in lower")

            file_size_before = target.stat().st_size
            orig = target.read_bytes()
            new_bytes, first_diff = apply_unified_diff(orig, diff)
            applied_ok = (new_bytes != orig)

            # Snapshot phys writes BEFORE the mutation
            subprocess.run(["sync"], check=False)
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3")
            writes_before = read_loop_writes(args.loop_name)

            # Apply the partial-write (this triggers overlay copy-up)
            write_partial(target, new_bytes, first_diff)

            # Snapshot phys writes AFTER
            subprocess.run(["sync"], check=False)
            writes_after = read_loop_writes(args.loop_name)
            phys_bytes = (writes_after - writes_before) * 512

            # Compute copy-up bytes for this single file in upper
            upper_target = Path(upper) / rel_path
            copyup_bytes = fiemap_nonshared(upper_target)

            result.update({
                "applied_ok": applied_ok,
                "file_size_bytes": file_size_before,
                "first_diff_offset": first_diff,
                "new_file_size_bytes": len(new_bytes),
                "copyup_bytes": copyup_bytes,
                "phys_bytes": phys_bytes,
            })
        except Exception as e:
            result.update({"applied_ok": False, "error": str(e),
                          "file_size_bytes": 0, "copyup_bytes": 0, "phys_bytes": 0})
        finally:
            umount_overlay(args.merged)

        out.write(json.dumps(result) + "\n")
        out.flush()

    out.close()
    print(f"[swesearch_replay_engine] wrote {len(edits)} edit results")


if __name__ == "__main__":
    main()
