"""Backend-agnostic sandbox C/R interface.

This is the decoupling seam between the search/upper layer (mcts/, worker/, npd/)
and any concrete sandbox backend (backends/deltabox, backends/e2b, ...).

The upper layer depends ONLY on this ABC; it must never import backend-specific
modules. A backend is selected at runtime and injected into the MCTS driver.

Semantics (all per search-tree node):
- checkpoint(node_id): capture the CURRENT (filesystem, process) state of the
  in-sandbox worker and return an opaque checkpoint handle. Must be cheap enough
  to run every iteration (DeltaBox: overlay sink ioctl + async CRIU dump +
  template fork, hidden under LLM inference).
- restore(ckpt): roll the worker back to a previously captured handle. Sits on
  the critical path; must be fast.
- exec_action(action): run one already-decided agent action inside the sandbox
  worker and return its observation. (LLM/value decisions stay in the upper
  layer; the worker is a stateless action executor.)
- fork(n): RL fan-out — materialize n sibling sandboxes from one warm template.

Note: action/observation I/O and LLM calls must NOT be folded into
checkpoint()/restore() timing; those measure C/R only.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Checkpoint:
    """Opaque handle to a captured node state (backend-defined payload)."""
    node_id: str
    handle: Any  # e.g. DeltaBox: (criu_img_id, template_pid, layer_cfg); E2B: build_id


class SandboxBackend(abc.ABC):
    """Async because the only consumer — the live MCTS agent loop — is async
    (it awaits LLM calls). Backends whose ops are blocking (subprocess, e.g. E2B)
    wrap them in ``asyncio.to_thread`` inside these coroutines."""

    @abc.abstractmethod
    async def checkpoint(self, node_id: str) -> Checkpoint: ...

    @abc.abstractmethod
    async def restore(self, ckpt: Checkpoint) -> None: ...

    @abc.abstractmethod
    async def exec_action(self, action: dict) -> dict: ...

    async def fork(self, n: int) -> list["SandboxBackend"]:
        """RL fan-out — materialize n sibling sandboxes from one warm template.
        Optional; not every backend supports it."""
        raise NotImplementedError(f"{type(self).__name__} does not support fork()")

    async def advance(self, node_id: str, action: dict) -> tuple[dict, Checkpoint]:
        """Run one action, then checkpoint the resulting node -> (observation, Checkpoint).

        Default = exec_action() then checkpoint() (DeltaBox: the two are separate
        ops). Backends that FUSE resume+exec+pause (E2B resume-build) may override,
        though the default composes correctly if exec_action() stashes the produced
        handle for checkpoint() to pick up.
        """
        obs = await self.exec_action(action)
        ckpt = await self.checkpoint(node_id)
        return obs, ckpt

    # Optional lifecycle hooks; default no-ops.
    async def setup(self) -> None: ...
    async def teardown(self) -> None: ...
