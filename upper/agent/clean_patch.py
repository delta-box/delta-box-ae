#!/usr/bin/env python3
"""Clean a trajectory final_patch/candidate patch for SWE-bench scoring."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SKIP_PARTS = (
    "/.git/", ".git/", "__pycache__/", ".pyc", ".pyo",
    ".pytest_cache/", ".mypy_cache/", ".hypothesis/",
    ".tox/", ".nox/", ".coverage", "htmlcov/",
)
KEEP_EXT = (
    ".py", ".pyx", ".pxd", ".pyi", ".txt", ".rst", ".md",
    ".cfg", ".ini", ".toml", ".yaml", ".yml", ".json",
)
TEMP_NAMES = {
    "test_fix.py", "test_specific_issue.py", "test_repro.py",
    "debug.py", "debug_issue.py", "repro.py", "scratch.py",
}


def norm_path(p: str) -> str:
    p = p.strip()
    if p == "/dev/null":
        return p
    for pre in ("a/", "b/", "a", "b"):
        if p.startswith(pre + "/tmp/"):
            p = p[len(pre):]
            break
    p = p.removeprefix("a/").removeprefix("b/")
    if "/merged/" in p:
        p = p.split("/merged/", 1)[1]
    elif "/heavy_lower_" in p:
        parts = p.split("/")
        for i, part in enumerate(parts):
            if part.startswith("heavy_lower_") and i + 1 < len(parts):
                p = "/".join(parts[i + 1:])
                break
    return p.lstrip("/")


def clean(diff: str) -> str:
    hunks = []
    cur = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if cur:
                hunks.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        hunks.append(cur)

    out = []
    for h in hunks:
        header = h[0].split()
        if len(header) < 4:
            continue
        a = norm_path(header[2])
        b = norm_path(header[3])
        path = b if b != "/dev/null" else a
        base = Path(path).name
        if any(s in path for s in SKIP_PARTS):
            continue
        lower_base = base.lower()
        if (
            base in TEMP_NAMES
            or lower_base.startswith(("test_", "debug", "repro", "scratch"))
            or lower_base.endswith(("_debug.py", "_repro.py", "_scratch.py"))
            or "/tests/" in path
            or path.startswith("tests/")
        ):
            continue
        if not path.endswith(KEEP_EXT):
            continue
        for line in h:
            if line.startswith("diff --git "):
                out.append(f"diff --git a/{a} b/{b}")
            elif line.startswith("--- "):
                p = norm_path(line[4:])
                out.append("--- " + (p if p == "/dev/null" else f"a/{p}"))
            elif line.startswith("+++ "):
                p = norm_path(line[4:])
                out.append("+++ " + (p if p == "/dev/null" else f"b/{p}"))
            elif line.startswith("Binary files "):
                continue
            else:
                out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trajectory")
    ap.add_argument("--in-place", action="store_true")
    args = ap.parse_args()
    p = Path(args.trajectory)
    d = json.loads(p.read_text())
    d["final_patch"] = clean(d.get("final_patch", ""))
    for c in d.get("candidate_patches", []):
        c["patch"] = clean(c.get("patch", ""))
        c["diff_len"] = len(c["patch"])
    if args.in_place:
        p.write_text(json.dumps(d, indent=2))
    else:
        print(d["final_patch"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
