"""Lightweight file actions for the heavy deltabox stack.

The original moatless ViewCode/StringReplace actions depend on tree-sitter
parsers and a code index. In the current environment that parser stack is
version-skewed, so using the original ViewCode can crash the whole search.

These actions intentionally keep the same public action names but implement
direct repository-root file I/O. That preserves the experimental contract:
the D group has the original structured edit surface plus Bash/PyREPL, while
all edits persist into the deltabox overlay filesystem.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

from pydantic import Field, field_validator

from moatless.actions.action import Action
from moatless.actions.model import ActionArguments, Observation
from moatless.file_context import FileContext
from moatless.workspace import Workspace


def _obs(
    message: str,
    *,
    terminal: bool = False,
    properties: dict | None = None,
) -> Observation:
    return Observation(
        message=message,
        terminal=terminal,
        properties=properties or {},
    )


def _repo_root(action: Action, workspace: Workspace | None = None) -> Path:
    workspace = workspace or getattr(action, "workspace", None)
    if workspace is None:
        raise RuntimeError("action has no workspace")
    repo = workspace.repository
    root = getattr(repo, "repo_path", None) or getattr(repo, "repo_dir", None)
    if not root:
        raise RuntimeError("workspace.repository has no repo_path")
    return Path(root)


def _safe_path(root: Path, path: str) -> Path:
    path = path.removeprefix("/repo/").removeprefix("repo/")
    path = path.lstrip("/")
    full = (root / path).resolve()
    root_resolved = root.resolve()
    if full != root_resolved and root_resolved not in full.parents:
        raise ValueError(f"path escapes repository root: {path}")
    return full


SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".tox", ".nox", "node_modules",
    "locale", "translations", "docs", "doc", "build", "dist",
}


def _is_hidden_or_skipped(path: Path, root: Path, show_hidden: bool = False) -> bool:
    rel_parts = path.relative_to(root).parts
    for part in rel_parts:
        if part in SKIP_DIRS:
            return True
        if not show_hidden and part.startswith("."):
            return True
    return False


def _format_list(root: Path, base: Path, recursive: bool, max_results: int,
                 show_hidden: bool) -> str:
    if not base.exists():
        return f"Error: Directory {base.relative_to(root) if root in base.parents else base} does not exist"
    dirs: list[str] = []
    files: list[str] = []
    if recursive:
        for p in base.rglob("*"):
            if _is_hidden_or_skipped(p, root, show_hidden):
                continue
            rel = str(p.relative_to(base if base != root else root))
            if p.is_dir():
                dirs.append(rel + "/")
            elif p.is_file():
                files.append(rel)
    else:
        for p in base.iterdir():
            if _is_hidden_or_skipped(p, root, show_hidden):
                continue
            rel = p.name + ("/" if p.is_dir() else "")
            if p.is_dir():
                dirs.append(rel)
            elif p.is_file():
                files.append(rel)
    dirs.sort()
    files.sort()
    entries = dirs + files
    limited = entries[:max_results]
    rel_base = "." if base == root else str(base.relative_to(root))
    msg = [f"Contents of directory '{rel_base}'" + (" recursively" if recursive else "") + ":"]
    if not limited:
        msg.append("(empty)")
    else:
        msg.extend(limited)
    if len(entries) > max_results:
        msg.append(f"... truncated {len(entries) - max_results} entries; narrow directory/pattern")
    return "\n".join(msg)


def _run_git_diff(root: Path) -> str:
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": str(root),
    })
    p = subprocess.run(
        ["git", "--no-pager", "diff", "--no-color", "HEAD"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return (p.stdout or "") + (p.stderr or "")


DEFAULT_VIEW_LINES = 80
MAX_VIEW_LINES = 140


def _line_window(text: str, start: int | None, end: int | None) -> tuple[str, str]:
    lines = text.splitlines()
    if start is None and end is None:
        lo, hi = 1, min(len(lines), DEFAULT_VIEW_LINES)
    else:
        lo = max(1, start or 1)
        hi = min(len(lines), end or lo)
    if hi - lo + 1 > MAX_VIEW_LINES:
        hi = lo + MAX_VIEW_LINES - 1
    shown = "\n".join(
        f"{i:6}\t{lines[i - 1]}" for i in range(lo, hi + 1)
    )
    suffix = ""
    if len(lines) > hi:
        suffix = (
            f"\n... truncated at line {hi} of {len(lines)}. "
            f"Call ViewCode with start_line/end_line to inspect a narrower "
            f"region, or use `grep -n` to find the relevant function."
        )
    return shown, suffix


class AgentViewCodeArgs(ActionArguments):
    """View a file or line range without tree-sitter parsing."""


    class Config:
        title = "ViewCode"

    path: str = Field(..., description="Path of the file to view")
    start_line: int | None = Field(None, description="First line to view, 1-based")
    end_line: int | None = Field(None, description="Last line to view, 1-based")

    @field_validator("start_line", "end_line", mode="before")
    @classmethod
    def _none_string_to_none(cls, value):
        if isinstance(value, str) and value.strip().lower() in {"none", "null", ""}:
            return None
        return value


class AgentViewCode(Action):
    args_schema = AgentViewCodeArgs

    def execute(
        self,
        args: AgentViewCodeArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        full = _safe_path(root, args.path)
        if not full.exists():
            return _obs(
                message=f"File not found: {args.path}",
                properties={"fail_reason": "file_not_found"},
            )
        if not full.is_file():
            return _obs(
                message=f"Path is not a file: {args.path}",
                properties={"fail_reason": "not_file"},
            )
        text = full.read_text(encoding="utf-8", errors="replace")
        shown, suffix = _line_window(text, args.start_line, args.end_line)
        return Observation.create(message=f"{args.path}\n```\n{shown}{suffix}\n```")


class AgentListFilesArgs(ActionArguments):
    """List repository files/directories from the real overlay filesystem."""


    class Config:
        title = "ListFiles"

    directory: str = Field("", description="Directory path; empty means repository root")
    recursive: bool = Field(False, description="List recursively")
    max_results: int = Field(60, description="Maximum entries to show")
    show_hidden: bool = Field(False, description="Include hidden files except .git/.venv")


class AgentListFiles(Action):
    args_schema = AgentListFilesArgs

    def execute(
        self,
        args: AgentListFilesArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        target = _safe_path(root, args.directory or ".")
        if not target.exists():
            return _obs(
                message=f"Error: Directory {args.directory or '.'} does not exist",
                properties={"fail_reason": "directory_not_found"},
            )
        if not target.is_dir():
            return _obs(
                message=f"Error: {args.directory} is not a directory",
                properties={"fail_reason": "not_directory"},
            )
        msg = _format_list(
            root, target, bool(args.recursive),
            max(1, min(int(args.max_results or 60), 160)),
            bool(args.show_hidden),
        )
        return Observation.create(message=msg)


class AgentGlobArgs(ActionArguments):
    """Find files by glob pattern from the overlay filesystem."""


    class Config:
        title = "GlobTool"

    pattern: str = Field(..., description="Glob pattern such as '**/*.py'")
    max_results: int = Field(40, description="Maximum file paths to show")


class AgentGlobTool(Action):
    args_schema = AgentGlobArgs

    def execute(
        self,
        args: AgentGlobArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        matches: list[str] = []
        limit = max(1, min(int(args.max_results or 40), 160))

        # Path.rglob("**") can walk enormous vendored/docs trees before we can
        # apply SKIP_DIRS. Use os.walk with pruning and fnmatch on repo-relative
        # paths so GlobTool remains bounded inside the heavy sandbox.
        seen = 0
        max_visited = 20_000
        pattern = args.pattern
        for dirpath, dirnames, filenames in os.walk(root):
            dirp = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
            if _is_hidden_or_skipped(dirp, root):
                continue
            for name in filenames:
                p = dirp / name
                seen += 1
                if seen > max_visited:
                    matches.sort()
                    msg = (
                        f"Found {len(matches)} files matching glob pattern "
                        f"{pattern!r} before visiting {max_visited} files:\n"
                        + ("\n".join(matches) if matches else "(none)")
                        + "\n... search truncated; use a narrower pattern or directory."
                    )
                    return Observation.create(message=msg)
                if _is_hidden_or_skipped(p, root):
                    continue
                rel = str(p.relative_to(root))
                if not (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern)):
                    continue
                if not p.is_file() or _is_hidden_or_skipped(p, root):
                    continue
                matches.append(rel)
                if len(matches) >= limit:
                    break
            if len(matches) >= limit:
                break
        matches.sort()
        if not matches:
            return Observation.create(
                message=f"No files found matching glob pattern '{args.pattern}'"
            )
        return Observation.create(
            message=(
                f"Found {len(matches)} files matching glob pattern "
                f"'{args.pattern}':\n" + "\n".join(matches)
            )
        )


class AgentGrepArgs(ActionArguments):
    """Search file contents with a regex from the overlay filesystem."""


    class Config:
        title = "GrepTool"

    pattern: str = Field(..., min_length=1, description="Regex pattern")
    include: str | None = Field(None, description="Optional glob such as '*.py'")
    max_results: int = Field(50, description="Maximum matching lines")


class AgentGrepTool(Action):
    args_schema = AgentGrepArgs

    def execute(
        self,
        args: AgentGrepArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        try:
            regex = re.compile(args.pattern)
        except re.error as e:
            return _obs(
                message=f"Invalid regex pattern {args.pattern!r}: {e}",
                properties={"fail_reason": "invalid_regex"},
            )
        include = args.include or "**/*"
        paths: list[Path] = []
        if any(ch in include for ch in "*?["):
            paths = [p for p in root.glob(include) if p.is_file()]
        else:
            p = _safe_path(root, include)
            if p.is_file():
                paths = [p]
            elif p.is_dir():
                paths = [x for x in p.rglob("*") if x.is_file()]
        out: list[str] = []
        limit = max(1, min(int(args.max_results or 50), 160))
        for p in sorted(paths):
            if _is_hidden_or_skipped(p, root):
                continue
            # Skip obvious binary/large files.
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
            return Observation.create(
                message=f"No matches for regex pattern {args.pattern!r}"
                + (f" in files matching {include!r}" if args.include else "")
            )
        return Observation.create(
            message=(
                f"Found {len(out)} matches for regex pattern {args.pattern!r}"
                + (f" in files matching {include!r}" if args.include else "")
                + ":\n" + "\n".join(out)
            )
        )


class AgentViewDiffArgs(ActionArguments):
    """View the current git diff from the overlay filesystem."""


    class Config:
        title = "ViewDiff"


class AgentViewDiff(Action):
    args_schema = AgentViewDiffArgs

    def execute(
        self,
        args: AgentViewDiffArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        diff = _run_git_diff(root)
        if not diff.strip():
            return Observation.create(message="No changes detected in the workspace.")
        return Observation.create(message=f"Current changes in workspace:\n{diff[:6000]}")


class AgentStringReplaceArgs(ActionArguments):
    """Replace one exact string in a file."""


    class Config:
        title = "StringReplace"

    path: str = Field(..., description="Path of the file to edit")
    old_str: str = Field(..., description="Exact existing text to replace")
    new_str: str = Field(..., description="Replacement text")


class AgentStringReplace(Action):
    args_schema = AgentStringReplaceArgs

    def execute(
        self,
        args: AgentStringReplaceArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        full = _safe_path(root, args.path)
        if not full.exists():
            return _obs(
                message=f"File not found: {args.path}",
                properties={"fail_reason": "file_not_found"},
            )
        text = full.read_text(encoding="utf-8", errors="replace")
        if args.old_str == args.new_str:
            return _obs(
                message=(
                    "StringReplace made no change because old_str and new_str "
                    "are identical. Provide a replacement that changes the "
                    "production code and creates a non-empty git diff."
                ),
                properties={"fail_reason": "no_op_replacement"},
            )
        count = text.count(args.old_str)
        if count == 0:
            return _obs(
                message=(
                    f"old_str not found in {args.path}. Use ViewCode or grep "
                    "to copy the exact current text before retrying."
                ),
                properties={"fail_reason": "string_not_found"},
            )
        if count > 1:
            return _obs(
                message=(
                    f"old_str occurs {count} times in {args.path}; include "
                    "more surrounding context so it is unique."
                ),
                properties={"fail_reason": "multiple_occurrences"},
            )
        new_text = text.replace(args.old_str, args.new_str, 1)
        full.write_text(new_text, encoding="utf-8")
        diff = _run_git_diff(root)
        if not diff.strip():
            # In the deltabox path, copy-up/sink can make host-side git diff
            # temporarily look empty even though the file content changed and
            # search's physical-layer patch capture will export it. Do
            # not report this as a failed edit; that caused the agent to waste
            # dozens of turns rerunning git diff instead of testing/revising.
            return _obs(
                message=(
                    f"The file {args.path} was edited with StringReplace. "
                    "A direct `git diff` is currently empty, which can happen "
                    "with the rollbackable overlay; do NOT repeat the same "
                    "diff check. Continue by running the focused failing test "
                    "or ViewCode on this file to inspect the changed region."
                ),
                properties={"success": True, "diff_visible": False},
            )
        preview = diff[:3000]
        return _obs(
            message=(
                f"The file {args.path} has been edited with StringReplace. "
                "Current source diff preview:\n" + preview
            ),
            properties={"success": True},
        )


class AgentRegexReplaceArgs(ActionArguments):
    """Replace text in a file using a Python regular expression."""


    class Config:
        title = "RegexReplace"

    path: str = Field(..., description="Path of the file to edit")
    pattern: str = Field(..., description="Python regular expression to replace")
    replacement: str = Field(..., description="Replacement text")
    count: int = Field(1, description="Maximum replacements; default 1")


class AgentRegexReplace(Action):
    args_schema = AgentRegexReplaceArgs

    def execute(
        self,
        args: AgentRegexReplaceArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        full = _safe_path(root, args.path)
        if not full.exists():
            return _obs(
                message=f"File not found: {args.path}",
                properties={"fail_reason": "file_not_found"},
            )
        text = full.read_text(encoding="utf-8", errors="replace")
        try:
            regex = re.compile(args.pattern, re.MULTILINE | re.DOTALL)
        except re.error as e:
            return _obs(
                message=f"Invalid regex pattern {args.pattern!r}: {e}",
                properties={"fail_reason": "invalid_regex"},
            )
        count = max(1, min(int(args.count or 1), 20))
        new_text, n = regex.subn(args.replacement, text, count=count)
        if n == 0:
            return _obs(
                message=(
                    f"RegexReplace pattern did not match {args.path}. "
                    "Use a narrower pattern copied from the source snippet."
                ),
                properties={"fail_reason": "pattern_not_found"},
            )
        if new_text == text:
            return _obs(
                message="RegexReplace matched but produced identical file content.",
                properties={"fail_reason": "no_op_replacement"},
            )
        full.write_text(new_text, encoding="utf-8")
        diff = _run_git_diff(root)
        preview = diff[:3000] if diff.strip() else (
            f"RegexReplace edited {args.path}; direct git diff is currently "
            "empty in the rollbackable overlay. Continue by running the "
            "focused failing test or ViewCode around the changed region."
        )
        return _obs(
            message=(
                f"RegexReplace edited {args.path}; replacements={n}. "
                "Current source diff preview:\n" + preview
            ),
            properties={"success": True, "replacements": n},
        )


class AgentReplaceLinesArgs(ActionArguments):
    """Replace an inclusive 1-based line range in a file."""


    class Config:
        title = "ReplaceLines"

    path: str = Field(..., description="Path of the file to edit")
    start_line: int = Field(..., description="First line to replace, 1-based")
    end_line: int | None = Field(
        None,
        description="Last line to replace, inclusive. Defaults to start_line.",
    )
    new_str: str = Field(
        ...,
        description=(
            "Replacement text for the whole line range. Include indentation. "
            "A trailing newline is added automatically if missing."
        ),
    )


class AgentReplaceLines(Action):
    args_schema = AgentReplaceLinesArgs

    def execute(
        self,
        args: AgentReplaceLinesArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        full = _safe_path(root, args.path)
        if not full.exists():
            return _obs(
                message=f"File not found: {args.path}",
                properties={"fail_reason": "file_not_found"},
            )
        text = full.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        start = int(args.start_line)
        end = int(args.end_line or start)
        if start < 1 or end < start or end > len(lines):
            return _obs(
                message=(
                    f"Invalid line range {start}-{end} for {args.path}; "
                    f"file has {len(lines)} lines."
                ),
                properties={"fail_reason": "invalid_line_range"},
            )
        replacement = args.new_str
        if replacement and not replacement.endswith("\n"):
            replacement += "\n"
        old_block = "".join(lines[start - 1:end])
        if old_block == replacement:
            return _obs(
                message="ReplaceLines matched but produced identical file content.",
                properties={"fail_reason": "no_op_replacement"},
            )
        new_text = "".join(lines[:start - 1]) + replacement + "".join(lines[end:])
        full.write_text(new_text, encoding="utf-8")
        diff = _run_git_diff(root)
        preview = diff[:3000] if diff.strip() else (
            f"ReplaceLines edited {args.path}:{start}-{end}; direct git diff "
            "is currently empty in the rollbackable overlay. Continue by "
            "running the focused failing test or ViewCode around the changed "
            "region."
        )
        return _obs(
            message=(
                f"ReplaceLines edited {args.path}:{start}-{end}. "
                "Current source diff preview:\n" + preview
            ),
            properties={"success": True, "start_line": start, "end_line": end},
        )


class AgentApplyPatchArgs(ActionArguments):
    """Apply a small unified diff patch to the repository."""


    class Config:
        title = "ApplyPatch"

    patch: str = Field(
        ...,
        description=(
            "Unified diff patch. Use repo-relative paths in diff headers, "
            "for example `diff --git a/pkg/file.py b/pkg/file.py`."
        ),
    )


class AgentApplyPatch(Action):
    args_schema = AgentApplyPatchArgs

    def execute(
        self,
        args: AgentApplyPatchArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        patch = args.patch.strip()
        if not patch:
            return _obs(
                message="ApplyPatch received an empty patch.",
                properties={"fail_reason": "empty_patch"},
            )
        if not patch.endswith("\n"):
            patch += "\n"
        if "diff --git " not in patch and not re.search(r"(?m)^--- ", patch):
            return _obs(
                message=(
                    "ApplyPatch expects a unified diff with `diff --git`, "
                    "`---`, `+++`, and `@@` hunk headers."
                ),
                properties={"fail_reason": "not_unified_diff"},
            )
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(root),
        })
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(root),
            env=env,
            input=patch,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return _obs(
                message=(
                    "ApplyPatch failed. Use ViewCode/ReplaceLines or adjust "
                    "the hunk context.\nSTDOUT:\n"
                    f"{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-4000:]}"
                ),
                properties={
                    "fail_reason": "patch_apply_failed",
                    "returncode": proc.returncode,
                },
            )
        diff = _run_git_diff(root)
        preview = diff[:4000] if diff.strip() else (
            "ApplyPatch succeeded, but direct git diff is currently empty in "
            "the rollbackable overlay. Continue by running the focused test."
        )
        return _obs(
            message="ApplyPatch applied successfully. Current source diff preview:\n" + preview,
            properties={"success": True},
        )


class AgentCreateFileArgs(ActionArguments):
    """Create a new file."""


    class Config:
        title = "CreateFile"

    path: str = Field(..., description="Path where the new file should be created")
    file_text: str = Field(..., description="Complete file content")


class AgentCreateFile(Action):
    args_schema = AgentCreateFileArgs

    def execute(
        self,
        args: AgentCreateFileArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        full = _safe_path(root, args.path)
        if full.exists():
            return _obs(
                message=f"File already exists: {args.path}",
                properties={"fail_reason": "file_exists"},
            )
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(args.file_text, encoding="utf-8")
        return _obs(
            message=f"Created file {args.path}",
            properties={"success": True},
        )


class AgentAppendStringArgs(ActionArguments):
    """Append text to an existing file."""


    class Config:
        title = "AppendString"

    path: str = Field(..., description="Path of the file to append to")
    new_str: str = Field(..., description="Text to append")


class AgentAppendString(Action):
    args_schema = AgentAppendStringArgs

    def execute(
        self,
        args: AgentAppendStringArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        root = _repo_root(self, workspace)
        full = _safe_path(root, args.path)
        if not full.exists():
            return _obs(
                message=f"File not found: {args.path}",
                properties={"fail_reason": "file_not_found"},
            )
        old = full.read_text(encoding="utf-8", errors="replace")
        sep = "" if old.endswith("\n") or not old else "\n"
        full.write_text(old + sep + args.new_str, encoding="utf-8")
        return _obs(
            message=f"Appended text to {args.path}",
            properties={"success": True},
        )
