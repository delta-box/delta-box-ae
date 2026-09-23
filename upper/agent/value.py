"""value — LLM-as-judge value function.

Skeleton: assigns each leaf node a reward 0–10 by asking the same vLLM
endpoint "did the agent solve the task". Used by MCTS selector to pick
promising subtrees on the next iteration.

For P1 (toy fizzbuzz, linear) we don't actually need this — SimpleSelector
just picks the first expandable node. For P3 (real MCTS with branching) we
plug this in.

Kept minimal here; will fill in once search adds reward-based selectors.
"""
from __future__ import annotations

import logging
from typing import Optional

import os
import sys
_m = os.environ.get("DELTABOX_MOATLESS_SRC") or os.environ.get("MOATLESS_SRC")
if _m and _m not in sys.path:
    sys.path.insert(0, _m)
from moatless.completion.base import BaseCompletionModel
from moatless.node import Node

LOG = logging.getLogger("agent.value")


JUDGE_PROMPT = """You are evaluating whether a coding agent has solved a task.

# Task
{task}

# Trajectory (assistant's actions and observations so far)
{trajectory}

# Decide
Score this trajectory 0–10:
  10 = task is fully and correctly solved (tests pass, no obvious issues)
   5 = partial progress, agent is on the right track but unfinished or has bugs
   0 = no progress or wrong direction

Reply ONLY with the integer score (0-10). No prose."""


class AgentValueFunction:
    """LLM-as-judge value function. Optional; only used by MCTS selectors that
    need reward signals (BestFirstSelector, SoftmaxSelector, etc.)."""

    def __init__(self, completion_model: BaseCompletionModel, task: str):
        self.completion_model = completion_model
        self.task = task

    async def evaluate(self, node: Node) -> Optional[float]:
        # Walk root → node chain, render as conversation
        trajectory_lines: list[str] = []
        path: list[Node] = []
        cur: Optional[Node] = node
        while cur is not None:
            path.insert(0, cur)
            cur = cur.parent
        for n in path:
            for step in (n.action_steps or []):
                a = step.action
                if a is None:
                    continue
                cls = type(a).__name__
                cmd_or_code = getattr(a, "command", None) or getattr(a, "code", None) or ""
                trajectory_lines.append(f"[{cls}] {cmd_or_code[:200]}")
                obs = step.observation
                obs_msg = (obs.message if obs is not None else "") or ""
                trajectory_lines.append(f"  -> {obs_msg[:300]}")

        prompt = JUDGE_PROMPT.format(
            task=self.task,
            trajectory="\n".join(trajectory_lines) or "(empty)")
        try:
            # Direct litellm call — bypass moatless's ReAct schema validation
            # since we only want a single integer reply.
            import litellm
            r = await litellm.acompletion(
                model=self.completion_model.model,
                messages=[{"role": "user", "content": prompt}],
                base_url=self.completion_model.model_base_url,
                api_key=self.completion_model.model_api_key,
                temperature=0.0,
                max_tokens=10,
            )
            text = r.choices[0].message.content.strip()
            # extract first integer
            for tok in text.split():
                try:
                    score = int(tok.rstrip(".,;:"))
                    return max(0.0, min(10.0, float(score))) / 10.0  # normalize to 0..1
                except ValueError:
                    continue
            LOG.warning(f"value LLM gave unparseable reply: {text!r}")
            return None
        except Exception as e:
            LOG.warning(f"value LLM call failed: {e}")
            return None
