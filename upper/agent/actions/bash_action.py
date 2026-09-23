"""Bash action for the heavy live-MCTS stack.

The deterministic Moatless source used by finalbench does not ship
`moatless.actions.bash.BashTool`, while this runtime's heavy path expects a
shell action that routes through `workspace.environment.execute()`. This module
provides that action locally instead of depending on a missing Moatless module.
"""
from __future__ import annotations

import asyncio

from pydantic import Field

from moatless.actions.action import Action
from moatless.actions.model import ActionArguments, Observation
from moatless.file_context import FileContext
from moatless.workspace import Workspace


def run_awaitable(coro):
    """Run a coroutine from Moatless's synchronous ActionAgent.

    The current deterministic Moatless ActionAgent calls action.execute()
    synchronously. AgentEnvironment is async internally because FIFO roundtrips
    use asyncio.to_thread, so action wrappers bridge that gap here.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if loop.is_running():
        raise RuntimeError(
            "Cannot run async heavy action from a running event loop through "
            "the synchronous Moatless ActionAgent. Use AgentMCTS's executor "
            "bridge or a synchronous environment call path.")
    return loop.run_until_complete(coro)


class AgentBashArgs(ActionArguments):
    """Run a shell command in the rollbackable sandbox."""

    command: str = Field(..., description="Shell command to run")
    timeout: int = Field(default=60, description="Soft timeout in seconds")


    class Config:
        title = "Bash"

    @property
    def log_name(self) -> str:
        return f"Bash({self.command[:80]!r})"

    def to_prompt(self) -> str:
        return f"Running shell command:\n```bash\n{self.command}\n```"


class AgentBashTool(Action):
    args_schema = AgentBashArgs

    def execute(
        self,
        args: AgentBashArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        workspace = workspace or getattr(self, "workspace", None)
        if workspace is None or getattr(workspace, "environment", None) is None:
            raise ValueError("Bash requires workspace.environment")
        output = run_awaitable(
            workspace.environment.execute(args.command, fail_on_error=False))
        return Observation.create(message=output if output else "(no output)")
