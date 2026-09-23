#!/usr/bin/env python3
"""shell_server (heavy edition) — long-running PID-1 inside the deltabox sandbox.

Same FIFO protocol as guest/shell_server.py but tightened for the heavy-action
stack:
  * persistent Python globals dict for cross-action state
  * persistent bash cwd / env tracked server-side (cd / export handled inline)
  * exposes a stable command surface that maps 1:1 to the agent's actions:
      bash, python, read_file, write_file, set_cwd, set_env, shutdown
  * survives CRIU dump + fork-restore (no per-action state held in transient
    file handles; the FIFOs are RDWR so they keep their inode across fork)

Run as:
  python3 namespace_launcher.py python3 shell_server.py

Env:
  SHELL_CMD_IN          path to host-writes FIFO
  SHELL_CMD_OUT         path to host-reads FIFO
  SHELL_LOG             append-only log path
  SHELL_INITIAL_CWD     initial cwd; defaults to /testbed when running in vm
  SHELL_TRACE_PATH      optional: append per-command timing rows to this jsonl
"""
from __future__ import annotations

import io
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import time
import traceback
import tokenize

_REPO_ROOT = os.environ.get(
    "DELTABOX_REPO_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
_GUEST_DIR = os.environ.get(
    "DELTABOX_GUEST_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "backends", "deltabox", "gsd"))
if _GUEST_DIR not in sys.path:
    sys.path.insert(0, _GUEST_DIR)
try:
    import template_fork  # noqa: E402
    HAS_TEMPLATE_FORK = True
except Exception as _e:  # pragma: no cover
    HAS_TEMPLATE_FORK = False
    template_fork = None
    _IMPORT_ERROR = _e


SHELL_CMD_IN  = os.environ.get("SHELL_CMD_IN",   "/tmp/shell_cmd_in.fifo")
SHELL_CMD_OUT = os.environ.get("SHELL_CMD_OUT",  "/tmp/shell_cmd_out.fifo")
SHELL_LOG     = os.environ.get("SHELL_LOG",      "/tmp/shell_server.log")
INITIAL_CWD   = os.environ.get("SHELL_INITIAL_CWD",
                               "/testbed" if os.path.isdir("/testbed") else "/tmp")
TRACE_PATH    = os.environ.get("SHELL_TRACE_PATH", "")


def _escape_newlines_inside_string_literals(code: str) -> str:
    """Repair a common LLM/JSON round-trip failure in PyREPL code.

    Models often intend Python source like:

        print("\\nsection")

    but after XML/ReAct/JSON parsing we sometimes receive:

        print("
        section")

    That is invalid Python because a physical newline appeared inside a
    single-line string literal. Preserve real code newlines, but convert
    newlines that occur while inside a single- or double-quoted string to the
    two-character escape sequence ``\\n``. Triple-quoted strings are left alone.
    """
    out: list[str] = []
    quote: str | None = None
    triple = False
    escaped = False
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        if quote is None:
            # Skip over common string prefixes: r, u, b, f, fr, rf, br, rb.
            if ch in "'\"" and i > 0 and code[i - 1].isalpha():
                j = i - 1
                while j >= 0 and code[j].isalpha():
                    j -= 1
                prefix = code[j + 1:i].lower()
                if prefix and all(c in "rubf" for c in prefix):
                    pass
            if ch in "'\"":
                quote = ch
                triple = code.startswith(ch * 3, i)
                out.append(ch)
                if triple:
                    out.append(ch)
                    out.append(ch)
                    i += 3
                    continue
                i += 1
                continue
            out.append(ch)
            i += 1
            continue

        # Inside a string literal.
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if triple and code.startswith(quote * 3, i):
            out.append(quote)
            out.append(quote)
            out.append(quote)
            quote = None
            triple = False
            i += 3
            continue
        if (not triple) and ch == quote:
            out.append(ch)
            quote = None
            i += 1
            continue
        if (not triple) and ch == "\n":
            out.append("\\n")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _log(msg: str) -> None:
    try:
        with open(SHELL_LOG, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def _trace(kind: str, **fields) -> None:
    if not TRACE_PATH:
        return
    try:
        with open(TRACE_PATH, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "pid": os.getpid(),
                                **fields}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _to_text(value) -> str:
    """Normalize command output before it crosses the JSON FIFO protocol.

    subprocess.TimeoutExpired can carry stdout/stderr as bytes even when the
    process was launched with text=True. Bytes are not JSON serializable; if
    they reach _respond(), the active shell_server child exits before sending a
    reply and the host blocks on FIFO readline().
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _json_safe_payload(payload: dict) -> dict:
    safe = {}
    for k, v in payload.items():
        if isinstance(v, (bytes, bytearray)):
            safe[k] = bytes(v).decode("utf-8", errors="replace")
        else:
            safe[k] = v
    return safe


class ShellServer:
    def __init__(self):
        os.makedirs(INITIAL_CWD, exist_ok=True)
        self._cwd: str = INITIAL_CWD
        self._repo_root: str = os.path.realpath(INITIAL_CWD)
        self._env: dict[str, str] = dict(os.environ)
        self._env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._env["PWD"] = self._cwd
        # The overlay mount is owned by root inside the sandbox, while the
        # lower git clone is owned by the host user. Git can reject `status` /
        # `diff` with "detected dubious ownership", which misleads the agent
        # into thinking there is no patch. Configure this one repo as safe for
        # every child bash command without mutating global git config.
        self._env["GIT_CONFIG_COUNT"] = "1"
        self._env["GIT_CONFIG_KEY_0"] = "safe.directory"
        self._env["GIT_CONFIG_VALUE_0"] = self._repo_root
        # Globals dict for python action. We seed __builtins__ so exec doesn't
        # rebind it on first call. The dict survives CRIU dump because it's
        # python-owned RSS — no fds, no kernel objects.
        self._globals: dict = {
            "__name__":     "__main__",
            "__builtins__": __builtins__,
        }
        sys.dont_write_bytecode = True
        try:
            os.chdir(self._cwd)
        except OSError:
            pass

    # ───── bash ─────────────────────────────────────────────────────────
    def _inside_repo(self, path: str) -> bool:
        path = os.path.realpath(path)
        return path == self._repo_root or path.startswith(self._repo_root + os.sep)

    def do_bash(self, cmd: str, timeout_s: float) -> dict:
        # PyREPL code can call os.chdir() inside this long-lived Python
        # process. subprocess.run(cwd=...) is normally enough for the child,
        # but git and relative-path helpers in later actions become fragile if
        # the server process itself has drifted. Re-anchor before every bash
        # action so "current repository root" remains true.
        try:
            os.chdir(self._cwd)
        except OSError:
            pass
        if (
            "/tmp/heavy_swebench_" in cmd
            or "/home/" in cmd
            or "/home/user" in cmd
            or "cd /tmp" in cmd
            or "cd /home" in cmd
            or "cd /root" in cmd
        ):
            return {
                "stdout": "",
                "stderr": (
                    "Rejected command: use repository-relative paths only. "
                    "The current working directory is already the repository "
                    "root inside the rollbackable sandbox; do not cd to "
                    "/tmp, /home, /root, /tmp/heavy_swebench_*, or /home/*."
                ),
                "rc": 2,
                "error": "unsafe_absolute_path",
            }

        # Detect bare `cd <path>` and persist self._cwd. A subprocess `cd ; cmd`
        # would lose the cwd after the subprocess exits.
        stripped = cmd.strip()
        if stripped.startswith("cd ") or stripped == "cd":
            try:
                parts = shlex.split(stripped)
            except ValueError:
                parts = stripped.split()
            target = parts[1] if len(parts) > 1 else self._env.get("HOME", "/")
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(self._cwd, target))
            if not os.path.isdir(target):
                return {"stdout": "", "stderr": f"cd: no such directory: {target}",
                        "rc": 1, "error": None}
            if not self._inside_repo(target):
                return {
                    "stdout": "",
                    "stderr": (
                        "Rejected cd: stay inside the repository root. "
                        "Use repo-relative paths; do not cd to /tmp or other "
                        "external directories."
                    ),
                    "rc": 2,
                    "error": "cwd_escape",
                }
            self._cwd = target
            self._env["PWD"] = self._cwd
            return {"stdout": "", "stderr": "", "rc": 0, "error": None}

        # `export FOO=bar` — persist into self._env
        if stripped.startswith("export ") and "=" in stripped:
            tail = stripped[len("export "):]
            for piece in shlex.split(tail):
                if "=" not in piece:
                    continue
                k, v = piece.split("=", 1)
                self._env[k] = v
            return {"stdout": "", "stderr": "", "rc": 0, "error": None}

        # Real shell command
        try:
            p = subprocess.run(
                ["bash", "-lc", cmd],
                cwd=self._cwd,
                env={**self._env, "PWD": self._cwd},
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            return {"stdout": p.stdout, "stderr": p.stderr, "rc": p.returncode,
                    "error": None}
        except subprocess.TimeoutExpired as e:
            return {"stdout": _to_text(e.stdout),
                    "stderr": _to_text(e.stderr) + f"\n[TIMEOUT after {timeout_s}s]",
                    "rc": 124, "error": "timeout"}
        except Exception as e:
            return {"stdout": "", "stderr": traceback.format_exc(),
                    "rc": 1, "error": f"{type(e).__name__}: {e}"}

    # ───── python (stateful exec in shared globals) ───────────────────
    def do_python(self, code: str, timeout_s: float) -> dict:
        # exec in our own process: any binding / import / spawned subprocess
        # held in self._globals survives across calls (and across CRIU restore,
        # since the entire process is dumped/restored as a unit).
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = buf_out = io.StringIO()
        sys.stderr = buf_err = io.StringIO()
        try:
            os.chdir(self._cwd)
        except OSError:
            pass

        rc = 0
        error = None
        had_alarm = False
        try:
            try:
                compiled = compile(code, "<pyrepl>", "exec")
            except SyntaxError as e:
                # Qwen frequently emits PyREPL code with intended "\n" escapes
                # decoded into physical newlines inside string literals.
                # Retry once with that specific damage repaired.
                if "unterminated string literal" not in str(e):
                    raise
                fixed_code = _escape_newlines_inside_string_literals(code)
                if fixed_code == code:
                    raise
                compiled = compile(fixed_code, "<pyrepl>", "exec")

            def _alarm(_sig, _frame):
                raise TimeoutError(f"python exec exceeded {timeout_s}s")

            if hasattr(signal, "SIGALRM"):
                signal.signal(signal.SIGALRM, _alarm)
                signal.alarm(max(1, int(timeout_s)))
                had_alarm = True
            try:
                exec(compiled, self._globals)
            finally:
                if had_alarm:
                    signal.alarm(0)
        except TimeoutError:
            rc = 124
            error = "timeout"
            buf_err.write(f"[TIMEOUT after {timeout_s}s]\n")
        except SystemExit as e:
            rc = int(e.code) if isinstance(e.code, int) else 1
        except Exception as e:
            rc = 1
            error = f"{type(e).__name__}: {e}"
            buf_err.write(traceback.format_exc())
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            try:
                os.chdir(self._cwd)
            except OSError:
                pass

        return {"stdout": buf_out.getvalue(), "stderr": buf_err.getvalue(),
                "rc": rc, "error": error}

    def do_read_file(self, path: str) -> dict:
        try:
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(self._cwd, path))
            if not self._inside_repo(path):
                return {"stdout": "", "stderr": f"path escapes repository root: {path}",
                        "rc": 2, "error": "path_escape"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return {"stdout": f.read(), "stderr": "", "rc": 0, "error": None}
        except Exception as e:
            return {"stdout": "", "stderr": f"{type(e).__name__}: {e}",
                    "rc": 1, "error": f"{type(e).__name__}: {e}"}

    def do_write_file(self, path: str, content: str) -> dict:
        try:
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(self._cwd, path))
            if not self._inside_repo(path):
                return {"stdout": "", "stderr": f"path escapes repository root: {path}",
                        "rc": 2, "error": "path_escape"}
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"stdout": f"wrote {len(content)} bytes to {path}",
                    "stderr": "", "rc": 0, "error": None}
        except Exception as e:
            return {"stdout": "", "stderr": f"{type(e).__name__}: {e}",
                    "rc": 1, "error": f"{type(e).__name__}: {e}"}

    def do_set_cwd(self, path: str) -> dict:
        if not os.path.isdir(path):
            return {"stdout": "", "stderr": f"no such dir: {path}",
                    "rc": 1, "error": None}
        if not self._inside_repo(path):
            return {"stdout": "", "stderr": "cwd escapes repository root",
                    "rc": 2, "error": "cwd_escape"}
        self._cwd = path
        self._env["PWD"] = self._cwd
        return {"stdout": path, "stderr": "", "rc": 0, "error": None}

    def do_set_env(self, key: str, value: str) -> dict:
        self._env[key] = value
        return {"stdout": "", "stderr": "", "rc": 0, "error": None}

    # ───── main loop ────────────────────────────────────────────────────
    def run(self):
        for p in (SHELL_CMD_IN, SHELL_CMD_OUT):
            if not os.path.exists(p):
                os.mkfifo(p, 0o600)
        # CRITICAL: open SHELL_CMD_IN with O_RDWR | O_NONBLOCK so we don't block
        # waiting for the host to open the writer end. The controller wants to
        # start checkpointing (and registering us as a warm-template) BEFORE
        # the moatless agent emits its first action — so by the time the agent
        # opens the writer side, we'd be too late if we'd blocked on a plain
        # open("r") at start.
        in_fd = os.open(SHELL_CMD_IN, os.O_RDWR | os.O_NONBLOCK)
        f_in  = os.fdopen(in_fd, "r")
        # O_RDWR on the out side too, so a brief close on the host doesn't EOF us.
        out_fd = os.open(SHELL_CMD_OUT, os.O_RDWR)
        f_out  = os.fdopen(out_fd, "w")

        # ── template_fork endpoint: enables deltabox warm-template fast path ──
        # Without this, SandboxController.restore falls back to slow CRIU.
        # install_template_endpoint() creates /tmp/template_ctrl.{in,out} FIFOs
        # and returns the read fd. We add it to the select() set; when GSD
        # (controller) sends a "fork" op, handle_template_message forks: the
        # parent SIGSTOPs (becomes a stashed template the pool can fork-again),
        # the child returns and continues this main loop as the new active
        # agent — same FIFOs, same globals dict, same state.
        template_fd = None
        template_write_path = None
        if HAS_TEMPLATE_FORK:
            try:
                template_fd, template_write_path = template_fork.install_template_endpoint()
                _log(f"template_fork endpoint installed: fd={template_fd}, "
                     f"write_path={template_write_path}")
            except Exception as e:
                _log(f"template_fork install failed: {e!r}; warm-template will be unavailable")
        else:
            _log(f"template_fork module unavailable: {_IMPORT_ERROR!r}")

        _log(f"shell_server (heavy) ready; cwd={self._cwd}; "
             f"trace={'on' if TRACE_PATH else 'off'}; "
             f"warm_template={'on' if template_fd is not None else 'off'}")

        in_fd = f_in.fileno()
        while True:
            watch = [in_fd]
            if template_fd is not None:
                watch.append(template_fd)
            try:
                ready, _wr, _ex = select.select(watch, [], [], 1.0)
            except InterruptedError:
                continue
            if not ready:
                continue

            # ── 1. warm-template fork (highest priority — short critical path) ──
            if template_fd is not None and template_fd in ready:
                try:
                    role = template_fork.handle_template_message(template_fd, template_write_path)
                except Exception as e:
                    _log(f"template_fork handler error: {e!r}\n{traceback.format_exc()}")
                    role = None
                if role == "child":
                    # We just became the new active agent. Globals + cwd + env
                    # all came along across fork. Continue main loop.
                    _log(f"forked into new active agent; my pid={os.getpid()}")
                    continue
                if role == "parent_resumed":
                    # We were SIGSTOPed as a template, just got SIGCONT'd. Caller
                    # (controller) will send another fork op shortly.
                    _log(f"template parent resumed (pid={os.getpid()}); awaiting next fork")
                    continue
                # role is None — no actionable message; fall through

            # ── 2. host command FIFO ──
            if in_fd not in ready:
                continue
            line = f_in.readline()
            if not line:
                # EOF — host closed without reopening. With FIFOs opened RDWR
                # this is rare, but be defensive.
                time.sleep(0.05)
                continue
            line = line.rstrip("\n")
            if not line:
                continue
            t_start = time.perf_counter()
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                self._respond(f_out, {"stdout": "", "stderr": "", "rc": 1,
                                      "error": f"bad json: {e}",
                                      "elapsed_ms": 0.0})
                continue

            kind = req.get("type")
            timeout_s = float(req.get("timeout_s", 60.0))
            try:
                if kind == "bash":
                    r = self.do_bash(req.get("cmd", ""), timeout_s)
                elif kind == "python":
                    r = self.do_python(req.get("code", ""), timeout_s)
                elif kind == "read_file":
                    r = self.do_read_file(req.get("path", ""))
                elif kind == "write_file":
                    r = self.do_write_file(req.get("path", ""), req.get("content", ""))
                elif kind == "set_cwd":
                    r = self.do_set_cwd(req.get("path", ""))
                elif kind == "set_env":
                    r = self.do_set_env(req.get("key", ""), req.get("value", ""))
                elif kind == "shutdown":
                    self._respond(f_out, {"stdout": "bye", "stderr": "",
                                          "rc": 0, "error": None,
                                          "elapsed_ms": 0.0})
                    _log("shutdown requested; exit")
                    return
                else:
                    r = {"stdout": "", "stderr": "",
                         "rc": 1, "error": f"unknown type: {kind!r}"}
            except Exception as e:
                r = {"stdout": "", "stderr": traceback.format_exc(),
                     "rc": 1, "error": f"{type(e).__name__}: {e}"}

            r["elapsed_ms"] = (time.perf_counter() - t_start) * 1000.0
            _trace("cmd", cmd_kind=kind, ms=r["elapsed_ms"], rc=r.get("rc"))
            self._respond(f_out, r)

    def _respond(self, f_out, payload: dict) -> None:
        try:
            f_out.write(json.dumps(_json_safe_payload(payload), ensure_ascii=False) + "\n")
            f_out.flush()
        except BrokenPipeError:
            _log("BrokenPipe on response — host gone, drop")
            time.sleep(0.1)


def main():
    server = ShellServer()
    try:
        server.run()
    except KeyboardInterrupt:
        _log("keyboard interrupt; exit")


if __name__ == "__main__":
    main()
