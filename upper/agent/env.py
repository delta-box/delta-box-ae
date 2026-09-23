"""env — moatless BaseEnvironment routed through the SandboxBackend seam.

A thin moatless `BaseEnvironment` adapter: every sandbox operation becomes one
`backend.exec_action({...})` call. The backend owns how that reaches the worker
(DeltaBox: a JSON-over-FIFO roundtrip to the in-sandbox shell_server). Bash and
python actions run in the same long-lived worker process, so state (cd, env vars,
python globals, open fds, subprocess tree) persists until backend.restore()
rewinds it. This adapter holds NO transport details — that lives in the backend,
so swapping backends does not touch the agent's environment.
"""
from __future__ import annotations

import logging

try:
    from moatless.environment.base import BaseEnvironment, EnvironmentExecutionError
except Exception:
    class EnvironmentExecutionError(RuntimeError):
        def __init__(self, message: str, returncode: int = 1, stderr: str = ""):
            super().__init__(message)
            self.returncode = returncode
            self.stderr = stderr

    class BaseEnvironment:
        pass

LOG = logging.getLogger("agent.env")


class AgentEnvironment(BaseEnvironment):
    """moatless BaseEnvironment that routes every operation through the
    SandboxBackend `exec_action` seam (host agent -> backend -> in-sandbox worker)."""

    def __init__(
        self,
        backend,
        default_timeout_s: float = 60.0,
        max_output_chars: int = 8_000,
    ):
        self._backend = backend
        self.default_timeout_s = default_timeout_s
        self.max_output_chars = max_output_chars

    # ───── BaseEnvironment API ─────────────────────────────────────────
    async def execute(
        self,
        command: str,
        fail_on_error: bool = False,
        patch: str | None = None,
    ) -> str:
        r = await self._backend.exec_action({
            "type": "bash",
            "cmd": command,
            "timeout_s": self.default_timeout_s,
        })
        out = (r.get("stdout", "") or "") + (r.get("stderr", "") or "")
        rc  = r.get("rc", 1)
        if rc != 0 and fail_on_error:
            raise EnvironmentExecutionError(
                f"Command failed (rc={rc}): {command}", rc, r.get("stderr", "") or "")
        return self._truncate(out)

    async def read_file(self, path: str) -> str:
        r = await self._backend.exec_action({"type": "read_file", "path": path})
        if r.get("rc", 1) != 0:
            raise FileNotFoundError(
                r.get("error") or r.get("stderr") or f"read failed: {path}")
        return r.get("stdout", "") or ""

    async def write_file(self, path: str, content: str) -> None:
        r = await self._backend.exec_action({
            "type": "write_file", "path": path, "content": content,
        })
        if r.get("rc", 1) != 0:
            raise EnvironmentExecutionError(
                f"write_file failed: {path}",
                r.get("rc", 1), r.get("stderr", "") or "")

    # ───── heavy-stack extension: stateful python ─────────────────────
    async def execute_python_code(self, code: str, cleanup: bool = True) -> str:
        """Exec in the worker's own globals — variables / imports / subprocess
        refs persist across calls until the next backend.restore() rewinds the
        whole process state."""
        r = await self._backend.exec_action({
            "type": "python",
            "code": code,
            "timeout_s": self.default_timeout_s,
        })
        out = (r.get("stdout", "") or "") + (r.get("stderr", "") or "")
        return self._truncate(out)

    # ───── plumbing ────────────────────────────────────────────────────
    def _truncate(self, s: str) -> str:
        if len(s) > self.max_output_chars:
            half = self.max_output_chars // 2
            s = s[:half] + (
                f"\n\n[... truncated {len(s) - self.max_output_chars} chars ...]\n\n"
            ) + s[-half:]
        return s
