"""PyREPLAction — stateful Python REPL action for moatless-tools.

Unlike `RunPythonScript` (which writes a temp file and runs `python file`
in a *new* subprocess each call, so import / globals don't survive across
actions), this one calls `workspace.environment.execute_python_code(code)`.
AgentEnvironment overrides that to exec the code inside the long-lived
shell_server's own globals dict — so:

    PyREPL("x = compute_expensive_thing()")    # x lives in shell_server globals
    PyREPL("print(x)")                         # sees the same x
    ...
    driver.restore(earlier_ckpt)               # x is gone again

This is the single action that most directly exercises deltabox's
process-state-faithful rollback — without it the heavy stack collapses back
to "stateless bash + FS rollback" semantics that moatless already had.
"""
from __future__ import annotations

from pydantic import Field

from moatless.actions.action import Action
from moatless.actions.model import ActionArguments, Observation
from moatless.file_context import FileContext
from moatless.workspace import Workspace

from .bash_action import run_awaitable


class PyREPLArgs(ActionArguments):
    """
    Execute Python code in a persistent REPL session.

    The interpreter is the SAME across every PyREPL call in this trajectory.
    Variables you bind, modules you import, files you open, and subprocesses
    you spawn all persist across calls — until the search tree rolls back to
    an earlier checkpoint, at which point the entire process state rewinds.

    Use this when you want:
      * to keep an expensive object alive across exploratory steps
        (e.g. a parsed AST, a loaded dataset, a connected DB session)
      * to import a module once and use it many times without re-loading
      * to spawn a background subprocess and keep talking to it
      * to drop into a debugger-style inspection ("type(obj); dir(obj); obj.x")

    Output is captured `stdout + stderr`. Long output is truncated.
    """

    code: str = Field(..., description="Python source to execute (multi-line OK)")
    timeout: int = Field(default=60,
                         description="Hard timeout in seconds for this execution.")


    class Config:
        title = "PyREPL"

    @property
    def log_name(self) -> str:
        first = self.code.strip().splitlines()[0] if self.code.strip() else ""
        return f"PyREPL({first[:60]!r})"

    def to_prompt(self) -> str:
        return f"Running Python code:\n```python\n{self.code}\n```"


class PyREPLTool(Action):
    args_schema = PyREPLArgs

    def execute(
        self,
        args: PyREPLArgs,
        file_context: FileContext | None = None,
        workspace: Workspace | None = None,
    ) -> Observation:
        workspace = workspace or getattr(self, "workspace", None)
        if not workspace:
            raise ValueError("PyREPL requires workspace with environment.")
        env = workspace.environment
        if env is None:
            raise ValueError("PyREPL requires workspace.environment (AgentEnvironment).")
        # AgentEnvironment.execute_python_code talks to shell_server's stateful
        # python path. Fall back transparently for other envs that override it.
        output = run_awaitable(env.execute_python_code(args.code))
        # Return as Observation — moatless serializes this into the node's
        # action_steps[0].observation field.
        return Observation.create(message=output if output else "(no output)")
