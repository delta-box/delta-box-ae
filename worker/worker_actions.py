"""worker_actions.py — self-contained agent action space for the fork-safe worker.

These are the SAME actions as upper/agent/actions/file_ops.py (ViewCode, Grep,
ListFiles, Glob, StringReplace, RegexReplace, ReplaceLines, ApplyPatch,
CreateFile, AppendString, ViewDiff) plus Bash — but implemented WITHOUT importing
moatless. That matters for the checkpointed worker: importing moatless pulls in
faiss, whose native OpenMP thread makes the process multi-threaded and therefore
unsafe to warm-fork (a native thread is invisible to Python and cannot be
reaped). file_ops.py's actions are already pure repo-root file I/O + git/grep
subprocess (their docstring says so; they only subclass moatless.Action for the
agent framework), so lifting the logic here loses nothing functionally while
keeping the worker single-threaded and fork-safe.

Each action is `fn(repo_path: str, args: dict) -> (message: str, terminal: bool)`.
Dispatch is by name (case-insensitive); see ACTIONS / CATALOG at the bottom.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".tox", ".nox", "node_modules",
    "locale", "translations", "docs", "doc", "build", "dist",
}
DEFAULT_VIEW_LINES = 80
MAX_VIEW_LINES = 140


def _safe_path(root: Path, path: str) -> Path:
    path = (path or "").removeprefix("/repo/").removeprefix("repo/").lstrip("/")
    full = (root / path).resolve()
    root_resolved = root.resolve()
    if full != root_resolved and root_resolved not in full.parents:
        raise ValueError(f"path escapes repository root: {path}")
    return full


def _is_hidden_or_skipped(path: Path, root: Path, show_hidden: bool = False) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    for part in rel_parts:
        if part in SKIP_DIRS:
            return True
        if not show_hidden and part.startswith("."):
            return True
    return False


def _run_git_diff(root: Path) -> str:
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(root),
    })
    try:
        p = subprocess.run(
            ["git", "--no-pager", "diff", "--no-color", "HEAD"],
            cwd=str(root), env=env, capture_output=True, text=True, timeout=20)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return f"(git diff unavailable: {e})"


def _line_window(text: str, start, end) -> tuple[str, str]:
    lines = text.splitlines()
    if start is None and end is None:
        lo, hi = 1, min(len(lines), DEFAULT_VIEW_LINES)
    else:
        lo = max(1, int(start or 1))
        hi = min(len(lines), int(end or lo))
    if hi - lo + 1 > MAX_VIEW_LINES:
        hi = lo + MAX_VIEW_LINES - 1
    shown = "\n".join(f"{i:6}\t{lines[i - 1]}" for i in range(lo, max(lo, hi) + 1)) \
        if lines else ""
    suffix = ""
    if len(lines) > hi:
        suffix = (f"\n... truncated at line {hi} of {len(lines)}. Call ViewCode "
                  f"with start_line/end_line, or grep -n to find the region.")
    return shown, suffix


# ───────────────────────── actions ─────────────────────────
def view_code(root: Path, a: dict) -> tuple[str, bool]:
    full = _safe_path(root, a.get("path", ""))
    if not full.exists():
        return (f"File not found: {a.get('path')}", False)
    if not full.is_file():
        return (f"Path is not a file: {a.get('path')}", False)
    text = full.read_text(encoding="utf-8", errors="replace")
    shown, suffix = _line_window(text, a.get("start_line"), a.get("end_line"))
    return (f"{a.get('path')}\n```\n{shown}{suffix}\n```", False)


def list_files(root: Path, a: dict) -> tuple[str, bool]:
    target = _safe_path(root, a.get("directory") or ".")
    if not target.exists():
        return (f"Error: Directory {a.get('directory') or '.'} does not exist", False)
    if not target.is_dir():
        return (f"Error: {a.get('directory')} is not a directory", False)
    recursive = bool(a.get("recursive", False))
    show_hidden = bool(a.get("show_hidden", False))
    max_results = max(1, min(int(a.get("max_results", 60) or 60), 160))
    dirs, files = [], []
    it = target.rglob("*") if recursive else target.iterdir()
    for p in it:
        if _is_hidden_or_skipped(p, root, show_hidden):
            continue
        rel = str(p.relative_to(target)) if recursive else p.name
        (dirs if p.is_dir() else files).append(rel + ("/" if p.is_dir() else ""))
    entries = sorted(dirs) + sorted(files)
    rel_base = "." if target == root else str(target.relative_to(root))
    msg = [f"Contents of '{rel_base}'" + (" recursively" if recursive else "") + ":"]
    msg.extend(entries[:max_results] or ["(empty)"])
    if len(entries) > max_results:
        msg.append(f"... truncated {len(entries) - max_results} entries")
    return ("\n".join(msg), False)


def glob_tool(root: Path, a: dict) -> tuple[str, bool]:
    pattern = a.get("pattern", "")
    limit = max(1, min(int(a.get("max_results", 40) or 40), 160))
    matches: list[str] = []
    seen, max_visited = 0, 20_000
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            seen += 1
            if seen > max_visited:
                return (f"Found {len(matches)} matches before visiting "
                        f"{max_visited} files (truncated):\n" + "\n".join(matches), False)
            if _is_hidden_or_skipped(p, root):
                continue
            rel = str(p.relative_to(root))
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
                matches.append(rel)
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    if not matches:
        return (f"No files found matching glob pattern '{pattern}'", False)
    return (f"Found {len(matches)} files matching '{pattern}':\n"
            + "\n".join(sorted(matches)), False)


def grep_tool(root: Path, a: dict) -> tuple[str, bool]:
    pattern = a.get("pattern", "")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return (f"Invalid regex {pattern!r}: {e}", False)
    include = a.get("include") or "**/*"
    limit = max(1, min(int(a.get("max_results", 50) or 50), 160))
    if any(ch in include for ch in "*?["):
        paths = [p for p in root.glob(include) if p.is_file()]
    else:
        p = _safe_path(root, include)
        paths = [p] if p.is_file() else ([x for x in p.rglob("*") if x.is_file()]
                                         if p.is_dir() else [])
    out: list[str] = []
    for p in sorted(paths):
        if _is_hidden_or_skipped(p, root):
            continue
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                out.append(f"{p.relative_to(root)}:{i}: {line[:240]}")
                if len(out) >= limit:
                    break
        if len(out) >= limit:
            break
    if not out:
        return (f"No matches for {pattern!r}", False)
    return (f"Found {len(out)} matches for {pattern!r}:\n" + "\n".join(out), False)


def view_diff(root: Path, a: dict) -> tuple[str, bool]:
    diff = _run_git_diff(root)
    if not diff.strip():
        return ("No changes detected in the workspace.", False)
    return (f"Current changes:\n{diff[:6000]}", False)


def string_replace(root: Path, a: dict) -> tuple[str, bool]:
    full = _safe_path(root, a.get("path", ""))
    if not full.exists():
        return (f"File not found: {a.get('path')}", False)
    old, new = a.get("old_str", ""), a.get("new_str", "")
    text = full.read_text(encoding="utf-8", errors="replace")
    if old == new:
        return ("StringReplace no-op: old_str == new_str.", False)
    count = text.count(old)
    if count == 0:
        return (f"old_str not found in {a.get('path')}. ViewCode the region and "
                f"copy the exact text before retrying.", False)
    if count > 1:
        return (f"old_str occurs {count} times in {a.get('path')}; add more "
                f"surrounding context so it is unique.", False)
    full.write_text(text.replace(old, new, 1), encoding="utf-8")
    diff = _run_git_diff(root)
    if not diff.strip():
        return (f"Edited {a.get('path')} (StringReplace). git diff is empty under "
                f"the overlay; do not re-check the diff — run the failing test "
                f"or ViewCode the region.", False)
    return (f"Edited {a.get('path')} (StringReplace). Diff preview:\n{diff[:3000]}", False)


def regex_replace(root: Path, a: dict) -> tuple[str, bool]:
    full = _safe_path(root, a.get("path", ""))
    if not full.exists():
        return (f"File not found: {a.get('path')}", False)
    try:
        regex = re.compile(a.get("pattern", ""), re.MULTILINE | re.DOTALL)
    except re.error as e:
        return (f"Invalid regex {a.get('pattern')!r}: {e}", False)
    text = full.read_text(encoding="utf-8", errors="replace")
    cnt = max(1, min(int(a.get("count", 1) or 1), 20))
    new_text, n = regex.subn(a.get("replacement", ""), text, count=cnt)
    if n == 0:
        return (f"RegexReplace pattern did not match {a.get('path')}.", False)
    if new_text == text:
        return ("RegexReplace matched but produced identical content.", False)
    full.write_text(new_text, encoding="utf-8")
    diff = _run_git_diff(root)
    return (f"RegexReplace edited {a.get('path')} (n={n}). Diff:\n{diff[:3000]}", False)


def replace_lines(root: Path, a: dict) -> tuple[str, bool]:
    full = _safe_path(root, a.get("path", ""))
    if not full.exists():
        return (f"File not found: {a.get('path')}", False)
    lines = full.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    start = int(a.get("start_line"))
    end = int(a.get("end_line") or start)
    if start < 1 or end < start or end > len(lines):
        return (f"Invalid line range {start}-{end}; file has {len(lines)} lines.", False)
    repl = a.get("new_str", "")
    if repl and not repl.endswith("\n"):
        repl += "\n"
    if "".join(lines[start - 1:end]) == repl:
        return ("ReplaceLines produced identical content.", False)
    full.write_text("".join(lines[:start - 1]) + repl + "".join(lines[end:]),
                    encoding="utf-8")
    diff = _run_git_diff(root)
    return (f"ReplaceLines edited {a.get('path')}:{start}-{end}. Diff:\n{diff[:3000]}", False)


def apply_patch(root: Path, a: dict) -> tuple[str, bool]:
    patch = (a.get("patch") or "").strip()
    if not patch:
        return ("ApplyPatch received an empty patch.", False)
    if not patch.endswith("\n"):
        patch += "\n"
    env = dict(os.environ)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(root)})
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                          cwd=str(root), env=env, input=patch, text=True,
                          capture_output=True, timeout=30)
    if proc.returncode != 0:
        return (f"ApplyPatch failed (rc={proc.returncode}):\n{proc.stderr[-3000:]}", False)
    diff = _run_git_diff(root)
    return (f"ApplyPatch applied. Diff:\n{diff[:4000]}", False)


def create_file(root: Path, a: dict) -> tuple[str, bool]:
    full = _safe_path(root, a.get("path", ""))
    if full.exists():
        return (f"File already exists: {a.get('path')}", False)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(a.get("file_text", ""), encoding="utf-8")
    return (f"Created file {a.get('path')}", False)


def append_file(root: Path, a: dict) -> tuple[str, bool]:
    full = _safe_path(root, a.get("path", ""))
    if not full.exists():
        return (f"File not found: {a.get('path')}", False)
    old = full.read_text(encoding="utf-8", errors="replace")
    sep = "" if old.endswith("\n") or not old else "\n"
    full.write_text(old + sep + a.get("new_str", ""), encoding="utf-8")
    return (f"Appended to {a.get('path')}", False)


def bash(root: Path, a: dict) -> tuple[str, bool]:
    cmd = a.get("command") or a.get("cmd") or ""
    if not cmd:
        return ("Bash requires a 'command'.", False)
    try:
        # Force the repo dir INSIDE the command: this VM's bash startup
        # (SWE-bench BASH_ENV / /etc/bash.bashrc) cd's to /testbed on every
        # invocation, so relying on the subprocess cwd alone makes a relative
        # path like `pytest tests/foo.py` resolve under /testbed. An explicit
        # leading `cd` runs the command in the repo regardless.
        full = f"cd {shlex.quote(str(root))} && {cmd}"
        cp = subprocess.run(["bash", "-c", full], cwd=str(root),
                            capture_output=True, text=True,
                            timeout=float(a.get("timeout", 60)))
        return (f"<returncode>{cp.returncode}</returncode>\n"
                f"{(cp.stdout or '') + (cp.stderr or '')}", False)
    except subprocess.TimeoutExpired:
        return ("Bash command timed out.", False)
    except Exception as e:
        return (f"Bash error: {type(e).__name__}: {e}", False)


# name (any of) → (handler, {arg: "desc"})
ACTIONS: dict[str, tuple] = {
    "ViewCode": (view_code, {"path": "file to view", "start_line?": "1-based",
                             "end_line?": "1-based"}),
    "ListFiles": (list_files, {"directory?": "dir (empty=root)", "recursive?": "bool",
                               "max_results?": "int"}),
    "GlobTool": (glob_tool, {"pattern": "glob e.g. **/*.py", "max_results?": "int"}),
    "GrepTool": (grep_tool, {"pattern": "regex", "include?": "glob e.g. *.py",
                             "max_results?": "int"}),
    "ViewDiff": (view_diff, {}),
    "StringReplace": (string_replace, {"path": "file", "old_str": "exact text",
                                       "new_str": "replacement"}),
    "RegexReplace": (regex_replace, {"path": "file", "pattern": "regex",
                                     "replacement": "text", "count?": "int"}),
    "ReplaceLines": (replace_lines, {"path": "file", "start_line": "1-based",
                                     "end_line?": "1-based", "new_str": "text"}),
    "ApplyPatch": (apply_patch, {"patch": "unified diff"}),
    "CreateFile": (create_file, {"path": "new file", "file_text": "content"}),
    "AppendString": (append_file, {"path": "file", "new_str": "text to append"}),
    "Bash": (bash, {"command": "shell command", "timeout?": "seconds"}),
}
# case-insensitive + a few aliases
_ALIASES = {"run": "Bash", "shell": "Bash"}


def resolve(name: str):
    if not name:
        return None
    for key in (name, _ALIASES.get(name.lower(), ""), ):
        if key in ACTIONS:
            return ACTIONS[key]
    for key in ACTIONS:
        if key.lower() == name.lower():
            return ACTIONS[key]
    return None


def catalog() -> str:
    lines = []
    for name, (_fn, args) in ACTIONS.items():
        argdesc = ", ".join(f"{k}: {v}" for k, v in args.items()) or "(none)"
        lines.append(f"- {name}: {{{argdesc}}}")
    lines.append("- finish: no args; emit when the fix is complete and verified")
    return "\n".join(lines)
