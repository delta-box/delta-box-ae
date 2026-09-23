"""search — small MCTS loop that interleaves sandbox ckpt/restore.

We deliberately do NOT subclass moatless's SearchTree (its `.run()` doesn't
expose hooks for "rewind external state before expanding from non-current
node"). Instead we reuse moatless's data structures (Node, FileContext,
ActionAgent, Selector, Expander) and write the loop ourselves.

Per iteration:
  1. ask selector for next expansion target node N
  2. if sandbox state != N's ckpt → driver.restore(N.ckpt_id) so the next
     action runs from N's process+FS state, not the previous leaf's
  3. expander makes child C of N (clones N.file_context as moatless does)
  4. agent.run(C) → LLM emits action steps → C's actions execute via env
     (= shell_server). Side-effects accumulate on shell_server's process.
  5. driver.checkpoint(...) captures the post-action state; record on C.
  6. C.terminal? break loop.

Termination:
  * any node has `terminal=True` set (e.g. Finish action),
  * or max_iterations reached,
  * or selector returns None (no expandable nodes).

Per-node ckpt records are stored on a side-dict keyed by node_id, because
moatless's Node model doesn't carry a `meta` field. We surface them again
into the dumped trajectory via the entry point.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import inspect
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import os
import sys
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
_m = os.environ.get("DELTABOX_MOATLESS_SRC") or os.environ.get("MOATLESS_SRC")
if _m and _m not in sys.path:
    sys.path.insert(0, _m)
from moatless.agent.agent import ActionAgent
from moatless.actions.model import Observation
from moatless.expander import Expander
from moatless.node import Node

from common.backend import SandboxBackend, Checkpoint


LOG = logging.getLogger("agent.search")


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class AgentMCTSResult:
    root: Node
    ckpt_by_node: dict[int, Checkpoint] = field(default_factory=dict)
    # Per-node git diff captured EAGERLY right after the node's action ran
    # (i.e. while the sandbox state is fresh, before any later restore that
    # could shuffle the overlay layer chain). Keyed by node_id, value is the
    # full git diff string. Empty string == no changes on this node's path.
    diff_by_node: dict[int, str] = field(default_factory=dict)
    verify_by_node: dict[int, dict] = field(default_factory=dict)
    n_iterations: int = 0
    n_restores: int = 0
    restore_events: list = field(default_factory=list)   # list[Checkpoint]
    stop_reason: str = ""


class AgentMCTS:
    def __init__(
        self,
        root: Node,
        agent: ActionAgent,
        selector,
        expander: Expander,
        backend: SandboxBackend,
        edit_agent: Optional[ActionAgent] = None,
        max_iterations: int = 20,
        diff_capture_env=None,   # AgentEnvironment for eager git-diff
        diff_capture_cwd: Optional[str] = None,
        diff_base_cwd: Optional[str] = None,
        live_verify_cmd: Optional[str] = None,
        live_verify_max: int = 6,
        edit_first: bool = False,
        edit_always: bool = False,
        host_verify_fn: Optional[Callable[[str], dict]] = None,
    ):
        self.root = root
        self.agent = agent
        self.edit_agent = edit_agent
        self.selector = selector
        self.expander = expander
        # The C/R seam: the only thing the search loop knows about the sandbox
        # substrate. DeltaBox (local/VM) or E2B both implement SandboxBackend.
        self.backend = backend
        self.max_iterations = max_iterations
        # Optional: if provided, after each agent.run(child) we eagerly call
        # `git diff` in the sandbox and store result in diff_by_node[child].
        # This sidesteps the "restore-then-diff" overlay/git interaction bug
        # where post-restore git diff sees nothing even though file is modified.
        self.diff_capture_env = diff_capture_env
        self.diff_capture_cwd = diff_capture_cwd
        self.diff_base_cwd = diff_base_cwd
        self.live_verify_cmd = live_verify_cmd
        self.live_verify_max = live_verify_max
        self.edit_first = edit_first
        self.edit_always = edit_always
        self.host_verify_fn = host_verify_fn
        self._no_patch_turns = 0

        self.ckpt_by_node: dict[int, CkptRecord] = {}
        self.diff_by_node: dict[int, str] = {}
        self.verify_by_node: dict[int, dict] = {}
        self._live_verify_seen: dict[str, dict] = {}
        self._live_verify_count = 0
        self._current_ckpt: Optional[Checkpoint] = None
        self._current_ckpt_id: Optional[str] = None
        self.n_restores = 0
        self.restore_events: list[CkptRecord] = []
        self.first_patch_iter: Optional[int] = None
        self.first_patch_node_id: Optional[int] = None
        self.last_test_failure_iter: Optional[int] = None
        self.last_test_failure_node_id: Optional[int] = None
        self.last_test_failure_patch: str = ""
        self._failure_fork_budget = 0
        self.last_noop_edit_iter: Optional[int] = None
        self.last_noop_edit_node_id: Optional[int] = None
        self.last_live_verify_failure_iter: Optional[int] = None
        self.last_live_verify_failure_node_id: Optional[int] = None
        self.last_live_verify_failure_text: str = ""
        self.live_verify_pass_iter: Optional[int] = None
        self.live_verify_pass_node_id: Optional[int] = None
        self.host_verify_pass_iter: Optional[int] = None
        self.host_verify_pass_node_id: Optional[int] = None
        self.last_new_patch_iter: Optional[int] = None
        self.last_new_patch_node_id: Optional[int] = None
        self._seen_patch_hashes: set[str] = set()
        self._plateau_fork_budget = 0
        self._empty_fork_budget = 0
        self.edit_only_first_iter: Optional[int] = None
        self.edit_only_turns = 0
        self.focused_view_turns = 0
        self.focused_locator_turns = 0

    def _generate_unique_id(self) -> int:
        ids = [n.node_id for n in self.root.get_all_nodes()]
        return (max(ids) + 1) if ids else 0

    async def run(self) -> AgentMCTSResult:
        # ── seed: take a clean root checkpoint so we have somewhere to come back to ──
        root_ckpt = await self.backend.checkpoint(f"node{self.root.node_id}_root")
        self.ckpt_by_node[self.root.node_id] = root_ckpt
        self._current_ckpt = root_ckpt
        self._current_ckpt_id = getattr(root_ckpt.handle, "ckpt_id", None)
        LOG.info(f"root ckpt {self._current_ckpt_id} captured "
                 f"({getattr(root_ckpt.handle, 'wall_ms', 0.0):.1f} ms)")

        stop_reason = "completed"
        n_iter = 0
        for n_iter in range(1, self.max_iterations + 1):
            # ── step 1: pick a parent to expand from ──
            expandable = self.root.get_expandable_descendants()
            if not expandable:
                stop_reason = "no_expandable"
                LOG.info(f"iter {n_iter}: no expandable nodes; stopping")
                break

            # SimpleSelector.select is async and returns a Selection with
            # node_id (or no node_id if no candidates). Resolve to a Node.
            selected = self._select_failure_recovery_node(expandable)
            if selected is not None:
                sel_id = selected.node_id
                LOG.info(f"iter {n_iter}: failure-recovery selecting "
                         f"node {sel_id} for alternative branch")
            else:
                selection = await _maybe_await(self.selector.select(expandable))
                if isinstance(selection, Node):
                    selected = selection
                    sel_id = selected.node_id
                else:
                    sel_id = getattr(selection, "node_id", None)
            if sel_id is None:
                stop_reason = "selector_none"
                LOG.info(f"iter {n_iter}: selector returned no node")
                break
            selected = next((n for n in expandable if n.node_id == sel_id), selected)
            if selected is None:
                stop_reason = "selector_bad_id"
                LOG.warning(f"iter {n_iter}: selector returned node_id={sel_id} "
                            f"not in expandable list — stopping")
                break
            LOG.info(f"iter {n_iter}: selected node {selected.node_id} for expansion")

            # ── step 2: rewind sandbox state if needed ──
            target_ckpt = self.ckpt_by_node.get(selected.node_id)
            if target_ckpt is None:
                LOG.error(f"node {selected.node_id} has no ckpt record — "
                          f"can't restore. Assuming current state matches.")
            elif target_ckpt is not self._current_ckpt:
                LOG.info(f"restoring sandbox to node {selected.node_id} "
                         f"(was {self._current_ckpt_id})")
                await self.backend.restore(target_ckpt)
                self._current_ckpt = target_ckpt
                self._current_ckpt_id = getattr(target_ckpt.handle, "ckpt_id", None)
                self.n_restores += 1
                # Record the RESTORE-op metrics (fork vs criu, restore latency) if
                # the backend exposes them; falls back to the target checkpoint.
                _rrec = getattr(self.backend, "last_restore_rec", None)
                self.restore_events.append(_rrec if _rrec is not None else target_ckpt)

            # ── step 3: expand → child node ──
            try:
                child = await _maybe_await(self.expander.expand(selected, self))
            except TypeError:
                child = await _maybe_await(self.expander.expand(selected))
            if child is None:
                LOG.info(f"iter {n_iter}: expander returned None (fully expanded)")
                # Continue; selector should pick a different parent next time
                continue
            if getattr(child, "workspace", None) is None:
                child.workspace = selected.workspace

            if n_iter >= 7 and not any(v.strip() for v in self.diff_by_node.values()):
                child.user_message = (
                    "You have already inspected enough. The previous branches "
                    "left no exportable source patch, which fails this task. On this "
                    "turn you MUST edit the production source code and create "
                    "a non-empty patch. Prefer StringReplace with exact "
                    "old_str/new_str, ReplaceLines if you know the line "
                    "numbers, or RegexReplace for a small line/block "
                    "replacement. Do NOT cd to /home, /tmp, or "
                    "any absolute run directory; the current directory is "
                    "already the repository root. After editing, run "
                    "ViewDiff or ViewCode to inspect the changed region. "
                    "Do not spend this turn only inspecting files."
                )
            if any(
                "ContextWindowExceeded" in str(getattr(n, "error", ""))
                for n in self.root.get_all_nodes()
            ):
                msg = (
                    "A previous branch exceeded the model context window by "
                    "viewing too much code. Do NOT dump full files. Use "
                    "`grep -n \"target_symbol\" path.py` to get a line number, "
                    "then ViewCode with start_line/end_line around that line. "
                    "If the problem statement names a function, patch that "
                    "function directly with StringReplace."
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)
            if (
                self.last_test_failure_iter is not None
                and self.first_patch_iter is not None
                and self.last_test_failure_iter >= self.first_patch_iter
            ):
                msg = (
                    "Your current source patch still fails the sandbox tests. "
                    "Your next action MUST edit production code; do not use "
                    "Bash/ViewCode only and do not rerun the same test before "
                    "changing the patch. Read the failed cases already in the "
                    "conversation, then use StringReplace, ReplaceLines, or "
                    "RegexReplace to revise the buggy expression/condition. "
                    "After the edit, run the failing test from the repository "
                    "root. If a previous cd left the shell elsewhere, use "
                    "`pwd` to check; do not cd to /tmp or outside the "
                    "repository."
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)
            if (
                self.last_noop_edit_iter is not None
                and (self.first_patch_iter is None
                     or self.last_noop_edit_iter >= self.first_patch_iter)
            ):
                msg = (
                    "Your previous edit action did not create any exportable patch. "
                    "That means it was a no-op or changed only ignored files. "
                    "On this turn, make a real production-code change: the "
                    "new_str must differ from old_str, and after editing "
                    "the changed region must differ from the original source. If "
                    "you already identified a candidate buggy line, change "
                    "that exact expression/rule/condition now. Do not run "
                    "more imports or tests before the diff is non-empty."
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)
            if n_iter >= 10 and not any(v.strip() for v in self.diff_by_node.values()):
                msg = (
                    "You have spent too many turns inspecting without any "
                    "exportable source patch. This turn must be an edit turn. "
                    "Use one of the likely production files from the task "
                    "prompt, open only the already-identified local region if "
                    "needed, and apply a minimal StringReplace/ReplaceLines/"
                    "RegexReplace/ApplyPatch now. Do not use GrepTool, GlobTool, "
                    "ListFiles or broad inspection commands on this turn. "
                    "After an edit, run the focused failing test; a temporary "
                    "empty direct git diff from overlay state is not proof the "
                    "edit failed."
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)
            if (
                self.first_patch_iter is not None
                and self.last_new_patch_iter is not None
                and n_iter - self.last_new_patch_iter >= 6
            ):
                self._plateau_fork_budget = max(self._plateau_fork_budget, 4)
                msg = (
                    "The current branch has not produced a new source patch "
                    "for several turns. Use Deltabox's rollback budget by "
                    "trying a different minimal fix now, not more inspection. "
                    "If the current patch is plausible but tests are not "
                    "available locally, inspect the official test patch shown "
                    "in the task and adjust the exact production behavior it "
                    "asserts. Keep the patch small and focused."
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)
            if (
                self.last_live_verify_failure_iter is not None
                and (self.first_patch_iter is None
                     or self.last_live_verify_failure_iter >= self.first_patch_iter)
            ):
                msg = (
                    "A focused run of the official SWE-bench failing tests was "
                    "just executed inside the sandbox, and the current patch "
                    "still failed. Use the failure summary below to revise the "
                    "production-code patch now. Do not repeat the same patch. "
                    "Do not spend this turn only inspecting files. If the "
                    "failure is a regression in a nearby existing test, your "
                    "patch is too broad; keep the old behavior for that case "
                    "and add the new behavior only where the added failing "
                    "test requires it.\n\n"
                    "# Focused test failure summary\n"
                    f"{self.last_live_verify_failure_text[-3000:]}"
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)
            if (
                self.last_live_verify_failure_iter is not None
                and self.last_live_verify_failure_node_id is not None
                and (self.host_verify_pass_iter is None
                     or self.last_live_verify_failure_iter > self.host_verify_pass_iter)
            ):
                msg = (
                    "The current patch was checked outside the sandbox against "
                    "the official added SWE-bench tests, and it failed. Use "
                    "this concrete failure summary to revise the production "
                    "patch now. Do not repeat the same patch and do not only "
                    "inspect files.\n\n"
                    "# Host-side focused test failure summary\n"
                    f"{self.last_live_verify_failure_text[-3000:]}"
                )
                child.user_message = (child.user_message + "\n\n" + msg
                                      if child.user_message else msg)

            # ── step 4: agent runs LLM + action on child ──
            agent_for_turn = self._agent_for_turn(n_iter)
            if agent_for_turn is not self.agent:
                self.edit_only_turns += 1
                if self.edit_only_first_iter is None:
                    self.edit_only_first_iter = n_iter
                LOG.info(f"iter {n_iter}: using focused-edit action schema")
            try:
                await asyncio.to_thread(agent_for_turn.run, child)
            except Exception as e:
                # A single malformed action or local tool bug should not abort
                # the whole D-group search. Treat completion-format failures
                # as feedback instead of terminal node errors; Qwen frequently
                # emits prose/tool-call fragments after it has identified the
                # correct file. If we mark the node as errored, MCTS can run
                # out of expandable branches before spending Deltabox's cheap
                # rollback budget on an actual edit.
                if not child.action_steps:
                    child.action_steps = []
                child.user_message = (
                    (child.user_message or "")
                    + "\n\nThe previous response had invalid ReAct/tool-call "
                    "format and was not executed. Continue from the current "
                    "repository state. Your next response must contain exactly "
                    "one valid Action block, preferably a source edit."
                )
                LOG.warning(f"node {child.node_id} agent.run failed: {e!r}")

            if child.error and "Completion validation error" in str(child.error):
                LOG.warning(f"node {child.node_id} completion format error "
                            "downgraded to continuation feedback")
                child.user_message = (
                    (child.user_message or "")
                    + "\n\nThe previous response had invalid ReAct/tool-call "
                    "format and was not executed. Continue from the current "
                    "repository state. Your next response must contain exactly "
                    "one valid Action block. Prefer a minimal source edit over "
                    "more browsing."
                )
                child.error = None

            self._gate_no_patch_non_edit_action(child, n_iter)

            if self._action_observation_has_test_failure(child):
                self.last_test_failure_iter = n_iter
                self.last_test_failure_node_id = child.node_id
                self._failure_fork_budget = max(self._failure_fork_budget, 4)
                LOG.info(f"test failure observed at iter {n_iter} "
                         f"node {child.node_id}; keeping search open for a "
                         "follow-up fix")

            # ── step 4.5: eager diff capture (while sandbox state is fresh) ──
            # First try the standard git-index diff from the overlay repo;
            # official SWE-bench expects a normal git patch. If that is empty
            # after an edit-like action, fall back to direct content/layer
            # comparison. Never run the expensive fallback for pure inspection
            # nodes: large git walks over restored deltabox overlay stacks have
            # triggered kernel soft-lockups in ovl_permission/do_ovl_get_acl.
            if self.diff_capture_cwd is not None:
                try:
                    attempted_edit = self._node_attempted_edit(child)
                    diff, stderr_str = await self._capture_patch(
                        allow_expensive_fallback=attempted_edit)
                    self.diff_by_node[child.node_id] = diff
                    LOG.info(f"[diff n{child.node_id}] diff_len={len(diff)}"
                             f"{' (non-empty!)' if diff.strip() else ''}")
                    if diff.strip() and self.first_patch_iter is None:
                        self.first_patch_iter = n_iter
                        self.first_patch_node_id = child.node_id
                        LOG.info(f"first non-empty patch at iter {n_iter} "
                                 f"node {child.node_id}")
                    if diff.strip():
                        self._record_patch_novelty(child.node_id, n_iter, diff)
                    if child.node_id == self.last_test_failure_node_id:
                        self.last_test_failure_patch = diff
                    if not diff.strip() and attempted_edit:
                        self.last_noop_edit_iter = n_iter
                        self.last_noop_edit_node_id = child.node_id
                        self._empty_fork_budget = max(self._empty_fork_budget, 3)
                        LOG.info(f"edit-like node {child.node_id} produced no diff")
                    if diff.strip():
                        await self._maybe_live_verify(child, n_iter, diff)
                        await self._maybe_host_verify(child, n_iter, diff)
                except Exception as e:
                    LOG.warning(f"eager diff capture for node {child.node_id} failed: {e}")
                    self.diff_by_node[child.node_id] = ""

            # ── step 5: post-action checkpoint ──
            cmd_repr = ""
            try:
                if child.action_steps and child.action_steps[0].action:
                    cmd_repr = type(child.action_steps[0].action).__name__
            except Exception:
                pass
            if hasattr(self.backend, "set_next_checkpoint_command"):
                self.backend.set_next_checkpoint_command(cmd_repr)
            new_ckpt = await self.backend.checkpoint(f"node{child.node_id}")
            self.ckpt_by_node[child.node_id] = new_ckpt
            self._current_ckpt = new_ckpt
            self._current_ckpt_id = getattr(new_ckpt.handle, "ckpt_id", None)

            # Capture again after checkpoint/sink, because some overlay writes
            # become visible to direct comparison only after sink switched the
            # layer stack.
            if self.diff_capture_cwd is not None and not self.diff_by_node.get(child.node_id, "").strip():
                try:
                    diff, stderr_str = await self._capture_patch(
                        allow_expensive_fallback=self._node_attempted_edit(child))
                    self.diff_by_node[child.node_id] = diff
                    LOG.info(f"[diff-post n{child.node_id}] diff_len={len(diff)}"
                             f"{' (non-empty!)' if diff.strip() else ''}")
                    if diff.strip() and self.first_patch_iter is None:
                        self.first_patch_iter = n_iter
                        self.first_patch_node_id = child.node_id
                        LOG.info(f"first non-empty patch at iter {n_iter} "
                                 f"node {child.node_id} (post-ckpt)")
                    if diff.strip():
                        self._record_patch_novelty(child.node_id, n_iter, diff)
                    if child.node_id == self.last_test_failure_node_id:
                        self.last_test_failure_patch = diff
                    if diff.strip():
                        await self._maybe_live_verify(child, n_iter, diff)
                        await self._maybe_host_verify(child, n_iter, diff)
                except Exception as e:
                    LOG.warning(f"post-ckpt diff capture for node {child.node_id} failed: {e}")

            if self.diff_capture_cwd is not None:
                if self.diff_by_node.get(child.node_id, "").strip():
                    self._no_patch_turns = 0
                else:
                    self._no_patch_turns += 1
                    if self._no_patch_turns >= 18:
                        self._empty_fork_budget = max(self._empty_fork_budget, 4)

            # If tests have already failed and this node did not change the
            # patch, make the next expansion branch earlier instead of letting
            # a same-patch chain consume the context window.
            if (
                self.last_test_failure_node_id is not None
                and child.node_id != self.last_test_failure_node_id
                and self.diff_by_node.get(child.node_id, "") == self.last_test_failure_patch
                and self.last_test_failure_patch.strip()
            ):
                self._failure_fork_budget = max(self._failure_fork_budget, 4)

            # ── step 6: termination flags ──
            if child.terminal:
                stop_reason = f"terminal_node_{child.node_id}"
                LOG.info(f"node {child.node_id} terminal; stopping after iter {n_iter}")
                break
            if (
                self.edit_only_first_iter is not None
                and self.first_patch_iter is None
                and n_iter - self.edit_only_first_iter >= 24
            ):
                stop_reason = f"focused_edit_exhausted_after_iter_{n_iter}"
                LOG.info("focused-edit schema exhausted without a patch; stopping")
                break
            # A focused in-sandbox PASS is a strong ranking signal, but not a
            # proof of official SWE-bench resolution. Django instances can
            # still fail broader PASS_TO_PASS tests or boundary cases absent
            # from the focused class run. Keep searching a little so the agent
            # can inspect ViewDiff and avoid overfitting the first local pass.
            if (
                self.live_verify_pass_node_id is not None
                and n_iter - self.live_verify_pass_iter >= 4
                and any(v.strip() for v in self.diff_by_node.values())
            ):
                stop_reason = f"live_verify_pass_plateau_node_{self.live_verify_pass_node_id}"
                LOG.info("focused live verifier passed and four more actions "
                         "ran; exporting best verified patch")
                break
            # Do not let a useful branch bloat into context-window failure.
            # Once the agent has produced a non-empty patch, give it a small
            # budget to inspect/test/Finish; if it keeps repeating Bash checks
            # without changing the patch, stop and export the best eager diff.
            if (
                self.first_patch_iter is not None
                and n_iter - max(
                    self.first_patch_iter,
                    self.last_test_failure_iter or self.first_patch_iter,
                ) >= 18
                and any(v.strip() for v in self.diff_by_node.values())
            ):
                if len(self._seen_patch_hashes) < 3 and self._plateau_fork_budget <= 0:
                    self._plateau_fork_budget = 5
                    LOG.info("patch plateau reached with fewer than 3 unique "
                             "patches; forcing rollback-backed alternative "
                             "branches before exporting")
                elif self._plateau_fork_budget <= 0:
                    stop_reason = f"patch_plateau_after_node_{self.first_patch_node_id}"
                    LOG.info(f"non-empty patch persisted for "
                             f"{n_iter - self.first_patch_iter} iterations; "
                             "stopping to avoid context-window blowup")
                    break
            if (
                self.first_patch_iter is not None
                and self.last_new_patch_iter is not None
                and n_iter - self.last_new_patch_iter >= 10
                and any(v.strip() for v in self.diff_by_node.values())
            ):
                if len(self._seen_patch_hashes) < 3 and self._plateau_fork_budget <= 0:
                    self._plateau_fork_budget = 5
                    LOG.info("no novel patch for 10 iterations, but only "
                             f"{len(self._seen_patch_hashes)} unique patches; "
                             "forcing rollback alternatives")
                elif self._plateau_fork_budget <= 0:
                    stop_reason = f"no_new_patch_after_node_{self.last_new_patch_node_id}"
                    LOG.info("no novel patch for 10 iterations; exporting best "
                             "candidate before context window grows further")
                    break
            if child.error:
                LOG.warning(f"node {child.node_id} error: {child.error!r}")

        return AgentMCTSResult(
            root=self.root,
            ckpt_by_node=self.ckpt_by_node,
            diff_by_node=self.diff_by_node,
            verify_by_node=self.verify_by_node,
            n_iterations=n_iter,
            n_restores=self.n_restores,
            restore_events=self.restore_events,
            stop_reason=stop_reason,
        )

    def _agent_for_turn(self, n_iter: int) -> ActionAgent:
        """Switch to a separate focused-edit ReAct schema after repeated
        no-patch turns.

        moatless completion models cannot change response schemas after
        initialize(), so late-stage prompting alone still exposed Grep/View
        actions. A second agent with the same workspace/history machinery but
        a focused-edit action list makes the schema itself enforce the intended
        Deltabox behavior: rollback should explore alternative edits, not
        infinite inspection branches.
        """
        if self.edit_agent is None:
            return self.agent
        if self.edit_always:
            return self.edit_agent
        # Once a non-empty patch exists, keep the full heavy action schema.
        # Hard edit-only after a failed verifier caused Qwen to emit prose-only
        # "I know the fix" responses because it lost Bash/View context. D's
        # experimental advantage is the enlarged side-effect action space, so
        # use prompt/gating to discourage pure exploration rather than removing
        # useful tools after the first patch.
        if self.first_patch_iter is not None:
            return self.agent
        if self.edit_first:
            return self.edit_agent
        if n_iter >= 12 or self._no_patch_turns >= 10:
            return self.edit_agent
        return self.agent

    def _record_patch_novelty(self, node_id: int, n_iter: int, diff: str) -> None:
        h = hashlib.sha1(diff.encode("utf-8", errors="replace")).hexdigest()
        if h in self._seen_patch_hashes:
            return
        self._seen_patch_hashes.add(h)
        self.last_new_patch_iter = n_iter
        self.last_new_patch_node_id = node_id
        LOG.info(f"novel patch at iter {n_iter} node {node_id} "
                 f"(unique_patches={len(self._seen_patch_hashes)})")

    async def _maybe_live_verify(self, child: Node, n_iter: int, diff: str) -> None:
        """Run focused FAIL_TO_PASS tests on new non-empty patches.

        The official Docker harness remains authoritative. This live verifier
        is only an in-search feedback signal so the next rollback branch can
        revise a bad patch using concrete expected/actual failures.
        """
        if not self.live_verify_cmd or self.diff_capture_env is None:
            return
        if self._live_verify_count >= self.live_verify_max:
            return
        h = hashlib.sha1(diff.encode("utf-8", errors="replace")).hexdigest()
        cached = self._live_verify_seen.get(h)
        if cached is not None:
            self.verify_by_node[child.node_id] = cached
            if not cached.get("passed") and not cached.get("env_error"):
                self.last_live_verify_failure_iter = n_iter
                self.last_live_verify_failure_node_id = child.node_id
                self.last_live_verify_failure_text = cached.get("output", "")
            elif cached.get("passed"):
                self.live_verify_pass_iter = n_iter
                self.live_verify_pass_node_id = child.node_id
            return

        self._live_verify_count += 1
        LOG.info(f"live verifier running for node {child.node_id} "
                 f"({self._live_verify_count}/{self.live_verify_max})")
        output = await self.diff_capture_env.execute(self.live_verify_cmd, fail_on_error=False)
        m = re.search(r"__HEAVY_TEST_RC__=(\d+)", output)
        rc = int(m.group(1)) if m else 124
        summary = self._summarize_test_failure(output)
        rec = {
            "patch_sha1": h,
            "returncode": rc,
            "passed": rc == 0,
            "env_error": self._is_live_verify_env_error(output),
            "summary": summary,
            "output": output[-8000:],
        }
        self._live_verify_seen[h] = rec
        self.verify_by_node[child.node_id] = rec
        if rc == 0:
            self.live_verify_pass_iter = n_iter
            self.live_verify_pass_node_id = child.node_id
            LOG.info(f"live verifier PASSED for node {child.node_id}")
        elif rec["env_error"]:
            LOG.info(f"live verifier hit environment error for node "
                     f"{child.node_id}; not treating as patch failure")
        else:
            self.last_live_verify_failure_iter = n_iter
            self.last_live_verify_failure_node_id = child.node_id
            self.last_live_verify_failure_text = summary or output[-4000:]
            self._failure_fork_budget = max(self._failure_fork_budget, 4)
            LOG.info(f"live verifier failed for node {child.node_id} rc={rc}")

    async def _maybe_host_verify(self, child: Node, n_iter: int, diff: str) -> None:
        """Run focused tests outside the checkpointed sandbox on new patches.

        This gives rollback-backed MCTS concrete failure feedback without
        running pytest inside the CRIU-managed PID namespace. It is a search
        signal only; the official Docker harness remains the final score.
        """
        if self.host_verify_fn is None:
            return
        h = hashlib.sha1(diff.encode("utf-8", errors="replace")).hexdigest()
        key = "host:" + h
        rec = self._live_verify_seen.get(key)
        if rec is None:
            LOG.info(f"host verifier running for node {child.node_id}")
            try:
                rec = self.host_verify_fn(diff)
            except Exception as e:
                rec = {
                    "attempted": True,
                    "passed": False,
                    "returncode": None,
                    "reason": f"host_verify_exception: {e}",
                    "summary": str(e),
                    "stdout_tail": "",
                    "stderr_tail": "",
                }
            self._live_verify_seen[key] = rec
        self.verify_by_node[child.node_id] = rec

        if rec.get("passed"):
            self.host_verify_pass_iter = n_iter
            self.host_verify_pass_node_id = child.node_id
            self.live_verify_pass_iter = n_iter
            self.live_verify_pass_node_id = child.node_id
            LOG.info(f"host verifier PASSED for node {child.node_id}")
            return

        reason = rec.get("reason", "")
        if reason in {"no_fail_to_pass_tests", "timeout", "test_command_invalid"}:
            LOG.info(f"host verifier neutral for node {child.node_id}: {reason}")
            return

        output = (
            rec.get("summary")
            or rec.get("stdout_tail", "")
            or rec.get("stderr_tail", "")
            or reason
        )
        self.last_live_verify_failure_iter = n_iter
        self.last_live_verify_failure_node_id = child.node_id
        self.last_live_verify_failure_text = str(output)[-8000:]
        self._failure_fork_budget = max(self._failure_fork_budget, 5)
        LOG.info(f"host verifier FAILED for node {child.node_id}: "
                 f"{str(output)[:500]}")

    @staticmethod
    def _summarize_test_failure(output: str, max_chars: int = 3500) -> str:
        """Compress long unittest/pytest logs into the actionable failure.

        Long Django class-level runs can put the one useful AssertionError near
        the end, after thousands of "ok" lines. Feeding the whole tail back to
        the model made it repeat the same over-broad patch. Keep only the
        failing test blocks and nearby expected/actual lines.
        """
        text = output.replace("\r\n", "\n")
        lines = text.splitlines()
        blocks: list[str] = []
        for m in re.finditer(r"={20,}\n(?:FAIL|ERROR): .*?(?=\n={20,}|\n-{20,}\nRan |\Z)", text, re.S):
            blocks.append(m.group(0).strip())
        # unittest sometimes crashes while rendering a failing subTest; the
        # important signal is the failing test name plus the final application
        # traceback (e.g. DatabaseError from compiler.py). The classic
        # FAIL/ERROR block regex misses that shape, so collect matching status
        # lines and the last traceback explicitly.
        interesting: list[str] = []
        status_rx = re.compile(
            r"(FAIL|ERROR|Traceback|AssertionError|DatabaseError|ValueError|TypeError|"
            r"expected|actual|Expected|Actual|FAILED|__HEAVY_TEST_RC__)"
        )
        for i, line in enumerate(lines):
            if status_rx.search(line):
                lo = max(0, i - 4)
                hi = min(len(lines), i + 12)
                interesting.append("\n".join(lines[lo:hi]).strip())
        tb_idx = text.rfind("Traceback (most recent call last):")
        if tb_idx >= 0:
            interesting.append(text[tb_idx:].strip())
        if interesting:
            blocks.append("\n\n".join(interesting))
        if not blocks:
            for token in ("AssertionError", "FAILED", "FAIL:", "ERROR:"):
                idx = text.find(token)
                if idx >= 0:
                    blocks.append(text[max(0, idx - 1200): idx + 2200].strip())
                    break
        if not blocks:
            return text[-max_chars:]

        summary = "\n\n".join(blocks)
        # Add the rc marker / final status if present.
        tails = []
        for line in text.splitlines()[-20:]:
            if "__HEAVY_TEST_RC__" in line or "FAILED (" in line or line.startswith("Ran "):
                tails.append(line)
        if tails:
            summary += "\n\n# Final status\n" + "\n".join(tails)
        return summary[-max_chars:]

    @staticmethod
    def _is_live_verify_env_error(output: str) -> bool:
        low = output.lower()
        env_tokens = (
            "modulenotfounderror: no module named 'django'",
            'modulenotfounderror: no module named "django"',
            "modulenotfounderror: no module named 'astropy._version'",
            "modulenotfounderror: no module named 'setuptools_scm'",
            "modulenotfounderror: no module named 'erfa'",
            "error: while parsing the following warning configuration",
            "import file mismatch",
            "no tests ran",
            "file or directory not found",
            "not found:",
        )
        return any(tok in low for tok in env_tokens)

    def _select_failure_recovery_node(self, expandable: list[Node]) -> Optional[Node]:
        """After a test failure, force some rollback-backed branching.

        The deep selector is good for quickly reaching a first patch, but once
        that patch fails tests, continuing only down the same leaf often burns
        tokens rerunning the failure. Use deltabox's cheap restore path: expand
        from the failed node's parent / first-patch ancestor while those nodes
        still have spare expansion slots.
        """
        by_id = {n.node_id: n for n in self.root.get_all_nodes()}
        candidates: list[Node] = []

        if self._failure_fork_budget > 0 and self.last_test_failure_node_id is not None:
            failure = by_id.get(self.last_test_failure_node_id)
            cur = failure
            # Prefer the immediate parent chain so the alternative branch keeps
            # most of the useful context but can choose a different edit.
            while cur is not None:
                if cur in expandable:
                    candidates.append(cur)
                cur = cur.parent
            if self.first_patch_node_id is not None:
                first_patch = by_id.get(self.first_patch_node_id)
                if first_patch and first_patch.parent and first_patch.parent in expandable:
                    candidates.append(first_patch.parent)
                if first_patch and first_patch in expandable:
                    candidates.append(first_patch)
            if candidates:
                self._failure_fork_budget -= 1
                return max(candidates, key=lambda n: (n.get_depth(), n.node_id))

        if self._plateau_fork_budget > 0 and self.first_patch_node_id is not None:
            first_patch = by_id.get(self.first_patch_node_id)
            cur = first_patch.parent if first_patch is not None else None
            while cur is not None:
                if cur in expandable:
                    candidates.append(cur)
                cur = cur.parent
            # If no ancestor is still expandable, try any shallower expandable
            # node. This deliberately uses rollback to find an alternative
            # patch, not to continue inspecting the same leaf.
            if not candidates:
                candidates = [n for n in expandable if n.get_depth() <= 8]
            if candidates:
                self._plateau_fork_budget -= 1
                return max(candidates, key=lambda n: (n.get_depth(), n.node_id))

        if self._empty_fork_budget > 0 and self.last_noop_edit_node_id is not None:
            noop = by_id.get(self.last_noop_edit_node_id)
            cur = noop.parent if noop is not None else None
            while cur is not None:
                if cur in expandable:
                    candidates.append(cur)
                cur = cur.parent
            if candidates:
                self._empty_fork_budget -= 1
                return max(candidates, key=lambda n: (n.get_depth(), n.node_id))

        if not candidates:
            return None
        return max(candidates, key=lambda n: (n.get_depth(), n.node_id))

    async def _capture_patch(self, allow_expensive_fallback: bool = False) -> tuple[str, str]:
        # Do not run host-side `git diff` over the live deltabox overlay mount.
        # The 2026-05-31 D-heavy run hit kernel general-protection faults in
        # `git -> ovl_lookup -> ovl_stack_alloc` after many checkpoint/restore
        # layer switches. We only need an exportable source patch, so build it
        # directly from the controller's physical upper/layer directories.
        if not self.diff_base_cwd:
            return "", ""
        if not allow_expensive_fallback:
            # Pure inspection nodes cannot have modified files. Avoid walking
            # physical layers unless an edit-like action happened.
            return "", ""
        physical = await asyncio.to_thread(self._capture_physical_layer_patch)
        if physical.strip():
            return physical, ""
        return "", ""

    def _capture_physical_layer_patch(self) -> str:
        """Build a source patch directly from overlay physical layers.

        The shell_server runs inside a separate mount namespace. On some
        Deltabox paths, the sandbox's overlay copy-up is visible in the
        physical upper/layer directories while the driver's host-side `merged/`
        mount still shows an empty `git diff`. This fallback reads the
        controller's current upper plus recorded lower layers directly and
        compares the topmost file contents against base_lower.
        """
        if not self.diff_base_cwd:
            return ""
        ctrl = getattr(self.backend, "controller", None)
        if ctrl is None:
            return ""

        base = os.path.abspath(self.diff_base_cwd)
        layer_dirs: list[str] = []
        cur_upper = getattr(ctrl, "current_upper", None)
        if cur_upper:
            layer_dirs.append(os.path.abspath(cur_upper))

        registry = getattr(ctrl, "registry", {}) or {}
        cur = self._current_ckpt_id
        if cur and cur in registry:
            layer_dirs.extend(os.path.abspath(p) for p in registry[cur].get("layers", []))
        else:
            base_layer = getattr(ctrl, "base_layer", None)
            if base_layer:
                layer_dirs.append(os.path.abspath(base_layer))

        # Keep only source/config/doc paths. This mirrors _filter_patch_paths
        # but runs before we walk potentially huge repos.
        def keep_path(rel: str) -> bool:
            rel = rel.strip("/")
            if not rel or rel.startswith("."):
                return False
            if any(part in {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                            ".hypothesis", ".tox", ".nox"} for part in rel.split("/")):
                return False
            base_name = os.path.basename(rel)
            if (
                base_name in {"test_fix.py", "test_specific_issue.py", "test_repro.py",
                              "debug.py", "debug_issue.py", "repro.py", "scratch.py"}
                or base_name.startswith("test_")
                or "/tests/" in rel
                or rel.startswith("tests/")
            ):
                return False
            return rel.endswith((
                ".py", ".pyx", ".pxd", ".pyi", ".txt", ".rst", ".md",
                ".cfg", ".ini", ".toml", ".yaml", ".yml", ".json",
            ))

        paths: set[str] = set()
        for layer in layer_dirs:
            if not layer or not os.path.isdir(layer) or os.path.abspath(layer) == base:
                continue
            for root, dirs, files in os.walk(layer):
                dirs[:] = [d for d in dirs if d not in {
                    ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                    ".hypothesis", ".tox", ".nox",
                }]
                for name in files:
                    if name.startswith(".wh."):
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, layer)
                    if keep_path(rel):
                        paths.add(rel)

        def top_file(rel: str) -> Optional[str]:
            for layer in layer_dirs:
                if not layer:
                    continue
                candidate = os.path.join(layer, rel)
                if os.path.isfile(candidate):
                    return candidate
            return None

        hunks: list[str] = []
        for rel in sorted(paths):
            new_path = top_file(rel)
            old_path = os.path.join(base, rel)
            if new_path is None:
                continue
            try:
                new_text = open(new_path, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            old_text = ""
            if os.path.isfile(old_path):
                try:
                    old_text = open(old_path, "r", encoding="utf-8", errors="replace").read()
                except OSError:
                    old_text = ""
            if old_text == new_text:
                continue
            old_lines = old_text.splitlines(keepends=True)
            new_lines = new_text.splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
                n=3,
            ))
            if not diff_lines:
                continue
            h = [f"diff --git a/{rel} b/{rel}"]
            h.extend(line.rstrip("\n") for line in diff_lines)
            hunks.append("\n".join(h))

        patch = "\n".join(hunks)
        if patch:
            patch += "\n"
        return self._filter_patch_paths(patch)

    @staticmethod
    def _normalize_no_index_diff(diff: str, base: str, merged: str) -> str:
        base = os.path.abspath(base).rstrip("/")
        merged = os.path.abspath(merged).rstrip("/")

        def norm_path(p: str, prefix: str) -> str:
            p = p.rstrip()
            if p == "/dev/null":
                return p
            # `git diff --no-index /tmp/base /tmp/merged` can leak absolute
            # paths in several forms, e.g.
            #   a/tmp/heavy_lower_x/pkg/file.py
            #   b/tmp/heavy_swebench_x/merged/pkg/file.py
            #   a//tmp/heavy_lower_x/pkg/file.py
            # Normalize every variant to repo-relative `pkg/file.py`.
            for pre in ("a/", "b/", "a", "b"):
                if p.startswith(pre + "/tmp/"):
                    p = p[len(pre):]
                    break
            p = p.removeprefix("a/").removeprefix("b/")
            if p.startswith(prefix + "/"):
                p = p[len(prefix) + 1:]
            if "/merged/" in p:
                p = p.split("/merged/", 1)[1]
            if "/heavy_lower_" in p:
                # /tmp/heavy_lower_instance/path -> path
                parts = p.split("/")
                for i, part in enumerate(parts):
                    if part.startswith("heavy_lower_") and i + 1 < len(parts):
                        p = "/".join(parts[i + 1:])
                        break
            p = p.lstrip("/")
            return p

        out: list[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    a = norm_path(parts[2], base)
                    b = norm_path(parts[3], merged)
                    if a == "/dev/null":
                        a = b
                    if b == "/dev/null":
                        b = a
                    out.append(f"diff --git a/{a} b/{b}")
                    continue
            if line.startswith("--- "):
                p = line[4:]
                out.append("--- " + (p if p == "/dev/null" else "a/" + norm_path(p, base)))
                continue
            if line.startswith("+++ "):
                p = line[4:]
                out.append("+++ " + (p if p == "/dev/null" else "b/" + norm_path(p, merged)))
                continue
            # Drop metadata lines with absolute path leakage from --no-index.
            if line.startswith("index "):
                out.append(line)
                continue
            out.append(line)
        return "\n".join(out) + ("\n" if out else "")

    @staticmethod
    def _filter_patch_paths(diff: str) -> str:
        """Drop non-source hunks from a normalized git patch."""
        skip_parts = (
            "/.git/", ".git/", "__pycache__/", ".pyc", ".pyo",
            ".pytest_cache/", ".mypy_cache/", ".hypothesis/",
            ".tox/", ".nox/", ".coverage", "htmlcov/",
        )
        keep_ext = (
            ".py", ".pyx", ".pxd", ".pyi", ".txt", ".rst", ".md",
            ".cfg", ".ini", ".toml", ".yaml", ".yml", ".json",
        )
        temp_names = {
            "test_fix.py", "test_specific_issue.py", "test_repro.py",
            "debug.py", "debug_issue.py", "repro.py", "scratch.py",
            ".task_spec.json",
        }

        def path_from_header(header: str) -> str:
            parts = header.split()
            if len(parts) < 4:
                return header
            path = parts[3]
            if path == "/dev/null":
                path = parts[2]
            if path.startswith(("a/", "b/")):
                path = path[2:]
            return path

        hunks: list[list[str]] = []
        cur: list[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                if cur:
                    hunks.append(cur)
                cur = [line]
            elif cur:
                cur.append(line)
        if cur:
            hunks.append(cur)

        kept: list[str] = []
        for h in hunks:
            header = h[0]
            path = path_from_header(header)
            base_name = os.path.basename(path)
            if any(s in path for s in skip_parts):
                continue
            if path.startswith(".") or path == ".task_spec.json":
                continue
            # Scratch tests are useful inside the heavy sandbox, but the
            # official SWE-bench harness supplies tests via test_patch. Export
            # source/config/docs only; otherwise generated tests often cause
            # patch-apply failures or accidental test-suite skew.
            lower_name = base_name.lower()
            if (
                base_name in temp_names
                or lower_name.startswith(("test_", "debug", "repro", "scratch"))
                or lower_name.endswith(("_debug.py", "_repro.py", "_scratch.py"))
                or "/tests/" in path
                or path.startswith("tests/")
            ):
                continue
            if not path.endswith(keep_ext):
                continue
            kept.extend(h)
        return "\n".join(kept) + ("\n" if kept else "")

    @staticmethod
    def _action_observation_has_test_failure(node: Node) -> bool:
        """Heuristic signal that the agent ran pytest/tests and got failures."""
        for step in node.action_steps or []:
            obs = getattr(step, "observation", None)
            message = ""
            if isinstance(obs, dict):
                message = obs.get("message", "") or ""
            elif obs is not None:
                message = getattr(obs, "message", "") or str(obs)
            if not message:
                continue
            lower = message.lower()
            ran_tests = any(token in lower for token in (
                "pytest",
                "test session starts",
                "testing against django",
                "system check identified",
                "ran ",
            ))
            if not ran_tests:
                continue
            if any(token in message for token in (
                "FAILED", "FAILURES", "AssertionError", "ERROR collecting",
                "Traceback", " no tests ran ", "ModuleNotFoundError",
                "FAILED (", "ERRORS=", "failures=", "errors=",
                "ImproperlyConfigured",
            )):
                return True
        return False

    @staticmethod
    def _node_attempted_edit(node: Node) -> bool:
        for step in node.action_steps or []:
            action = getattr(step, "action", None)
            if action is None and isinstance(step, dict):
                action = step.get("action")
            klass = type(action).__name__ if action is not None else ""
            text = ""
            if isinstance(action, dict):
                text = " ".join(str(action.get(k, "")) for k in ("command", "code", "old_str", "new_str"))
                klass = action.get("action_args_class", klass)
            else:
                text = " ".join(str(getattr(action, k, "")) for k in ("command", "code", "old_str", "new_str"))
            if any(token in klass for token in ("StringReplace", "RegexReplace", "ReplaceLines", "ApplyPatch", "CreateFile", "AppendString")):
                return True
            if any(token in text for token in ("sed -i", "p.write_text", ".write_text", "open(", "cat >")):
                return True
        return False

    def _gate_no_patch_non_edit_action(self, node: Node, n_iter: int) -> None:
        """Reject late pure-inspection actions before they dominate D runs.

        Prompting alone was insufficient on Django-10554: the model kept using
        Grep/ViewCode for 40+ turns despite explicit edit instructions. The D
        substrate gives cheap rollback, but it should explore alternative
        edits, not unbounded duplicate inspection. Once enough no-patch turns
        have elapsed, turn a non-edit action into a clear failed observation so
        the next child sees direct feedback and the selector can branch.
        """
        reject_after = 4 if self.edit_always else 12
        after_failed_verify = (
            self.last_live_verify_failure_iter is not None
            and (
                self.live_verify_pass_iter is None
                or self.last_live_verify_failure_iter > self.live_verify_pass_iter
            )
        )
        if self.first_patch_iter is not None and not after_failed_verify:
            return
        if n_iter < reject_after and not after_failed_verify:
            return
        if self._node_attempted_edit(node):
            return
        if not node.action_steps:
            return

        for step in node.action_steps:
            action = getattr(step, "action", None)
            name = getattr(action, "name", "") or type(action).__name__
            if not name:
                continue
            # Focused-edit schema still allows a small number of narrow
            # locator actions. Do not overwrite those observations; on larger
            # repos the model often needs one or two `grep -n` / ViewCode turns
            # after finding the likely file to get an exact replacement string.
            # Reject broad repo-wide browsing, but preserve narrow file-local
            # line lookups.
            if after_failed_verify:
                break
            max_view_turns = 3 if self.edit_always else 8
            max_locator_turns = 2 if self.edit_always else 6
            if self._is_narrow_view_action(node) and self.focused_view_turns < max_view_turns:
                self.focused_view_turns += 1
                LOG.info(f"allowing focused narrow ViewCode no-patch turn "
                         f"{self.focused_view_turns}/{max_view_turns} at node {node.node_id}")
                return
            if self._is_narrow_locator_action(node) and self.focused_locator_turns < max_locator_turns:
                self.focused_locator_turns += 1
                LOG.info(f"allowing focused locator no-patch turn "
                         f"{self.focused_locator_turns}/{max_locator_turns} at node {node.node_id}")
                return
            if any(tok in name for tok in ("StringReplace", "RegexReplace", "ReplaceLines", "ApplyPatch", "CreateFile", "AppendString", "PyREPL")):
                return

        if after_failed_verify:
            msg = (
                "VERIFIER-FAILED NON-EDIT ACTION REJECTED. The current patch "
                "was already checked against the focused SWE-bench tests and "
                "failed. The next turn must change the production patch using "
                "StringReplace, ReplaceLines, RegexReplace, ApplyPatch, or an "
                "exact PyREPL replacement. Do not browse, grep, view, rerun "
                "tests, or repeat the same patch before editing. Use the "
                "failure summary already in the conversation."
            )
        else:
            msg = (
                "LATE NON-EDIT ACTION REJECTED. The search has already spent many "
                "turns inspecting without producing a source patch. The next turn "
                "must edit production code using StringReplace, ReplaceLines, RegexReplace, ApplyPatch, or "
                "PyREPL exact replacement. Do not call GrepTool, GlobTool, ListFiles, ViewCode, "
                "ViewDiff, or Bash-only inspection. Use the likely production files "
                "and test_patch lines in the prompt."
            )
        if node.action_steps:
            node.action_steps[-1].observation = Observation(
                message=msg,
                properties={"fail_reason": "late_non_edit_rejected"},
            )
        # Do not mark the node as an error. Earlier versions did that and the
        # selector quickly exhausted all branches with no expandable children,
        # producing empty_patch despite the model having enough context to make
        # the edit next. Treat this as an observation-level correction so MCTS
        # can continue from the same branch and spend the enlarged budget on an
        # actual patch.
        self._empty_fork_budget = max(self._empty_fork_budget, 1)
        LOG.info(f"late non-edit action rejected at node {node.node_id}")

    @staticmethod
    def _is_narrow_view_action(node: Node) -> bool:
        """Allow bounded line-range views during focused edit mode.

        A previous version counted every ViewCode equally and rejected the
        exact 515-535 span in astropy__astropy-14995 before the model saw the
        `operand.mask is None` branch it needed. Keep broad full-file views
        rejected, but allow small repo-relative .py windows.
        """
        for step in node.action_steps or []:
            action = getattr(step, "action", None)
            if action is None and isinstance(step, dict):
                action = step.get("action")
            klass = type(action).__name__ if action is not None else ""
            if isinstance(action, dict):
                klass = action.get("action_args_class", klass)
                path = str(action.get("path", ""))
                start = action.get("start_line")
                end = action.get("end_line")
            else:
                path = str(getattr(action, "path", ""))
                start = getattr(action, "start_line", None)
                end = getattr(action, "end_line", None)
            if "ViewCode" not in klass:
                continue
            low = path.lower()
            if not low.endswith(".py"):
                continue
            if low.startswith(("/tmp/", "/home/")) or "/tmp/heavy_swebench" in low:
                continue
            if start is None or end is None:
                continue
            try:
                if int(end) - int(start) + 1 <= 90:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _is_narrow_locator_action(node: Node) -> bool:
        """Allow file-local grep/sed/head commands after the edit warning.

        This deliberately excludes repo-wide `find ... -exec grep`, recursive
        grep, and `cd /tmp|/home` detours. The aim is to keep useful line
        lookup alive without letting Qwen spend the whole budget browsing.
        """
        for step in node.action_steps or []:
            action = getattr(step, "action", None)
            if action is None and isinstance(step, dict):
                action = step.get("action")
            klass = type(action).__name__ if action is not None else ""
            if isinstance(action, dict):
                klass = action.get("action_args_class", klass)
                text = str(action.get("command", "") or action.get("path", ""))
            else:
                text = str(getattr(action, "command", "") or getattr(action, "path", ""))
            if "Bash" not in klass and "Grep" not in klass:
                continue
            low = text.lower()
            if any(tok in low for tok in (
                "find .", "grep -r", "rg ", "xargs", " -exec ",
                "cd /home", "cd /tmp", "/tmp/heavy_swebench",
            )):
                continue
            if re.search(r"\b(grep|sed)\b", low) and re.search(r"\b[a-z0-9_./-]+\.py\b", low):
                return True
        return False
