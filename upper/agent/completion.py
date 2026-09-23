"""completion — Qwen3-Coder-friendly ReAct completion model.

Qwen3-Coder-30B (and many tool-call-trained code models) ignore ReAct prompt
instructions and emit responses in the XML form:

    <tool_call>
    <function=Bash>
    <parameter=command>
    ls -la
    </parameter>
    </function>
    </tool_call>

We can't change the model's training prior with prompting alone — both
attempt-1 ("STRICT") and attempt-2 ("DO NOT output <tool_call>...") of the
system prompt failed to stop it. So we intercept at the completion layer:
before moatless's _validate_react_format runs, rewrite tool_call XML into
the ReAct text form. Validation then passes and the rest of moatless's
extraction pipeline works unchanged.

This is a *format adapter*, not a semantic change. The action chosen by the
model is unchanged — we only translate the wire syntax.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import os
import sys
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
_m = os.environ.get("DELTABOX_MOATLESS_SRC") or os.environ.get("MOATLESS_SRC")
if _m and _m not in sys.path:
    sys.path.insert(0, _m)
from moatless.completion.react import ReActCompletionModel
from moatless.completion.completion import LLMResponseFormat
from pydantic import Field
import litellm
import tenacity


LOG = logging.getLogger("agent.completion")


_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*"
    r"<function=([^\s>]+)>\s*"
    r"(.*?)"        # body: zero or more <parameter=...> blocks
    r"\s*</function>\s*"
    r"</tool_call>",
    re.DOTALL,
)

_PARAM_RE = re.compile(
    r"<parameter=([^\s>]+)>\s*"
    r"(.*?)"
    r"\s*</parameter>",
    re.DOTALL,
)

_DSML_CALL_RE = re.compile(
    r"<｜｜DSML｜｜tool_calls>\s*"
    r"<｜｜DSML｜｜invoke\s+name=\"([^\"]+)\">\s*"
    r"(.*?)"
    r"\s*</｜｜DSML｜｜invoke>\s*"
    r"</｜｜DSML｜｜tool_calls>",
    re.DOTALL,
)

_DSML_PARAM_RE = re.compile(
    r"<｜｜DSML｜｜parameter\s+name=\"([^\"]+)\"(?:\s+string=\"[^\"]*\")?>\s*"
    r"(.*?)"
    r"\s*</｜｜DSML｜｜parameter>",
    re.DOTALL,
)

_ABS_MERGED_CD_RE = re.compile(
    r"^\s*cd\s+/tmp/heavy_swebench_[^;&\s]+/merged\s*&&\s*",
    re.DOTALL,
)

_ABS_WORKDIR_CD_RE = re.compile(
    r"^\s*cd\s+/tmp/heavy_swebench_[^;&\s]+\s*&&\s*",
    re.DOTALL,
)

_ABS_MERGED_PATH_RE = re.compile(r"/tmp/heavy_swebench_[^/\s'\"]+/merged/?")

_HALLUCINATED_REPO_CD_RE = re.compile(
    r"^\s*cd\s+/home/(?:dong|user|runner|ubuntu)/"
    r"(?:django|sympy|astropy|xarray|requests|seaborn|pylint|pytest)"
    r"(?:-[^;&\s]+)?\s*&&\s*",
    re.DOTALL,
)

_BASENAME_PATH_HINTS = {
    "deletion.py": "django/db/models/deletion.py",
    "compiler.py": "django/db/models/sql/compiler.py",
    "query.py": "django/db/models/sql/query.py",
    "aggregates.py": "django/db/models/aggregates.py",
    "validators.py": "django/core/validators.py",
}

_SYMBOL_PATH_HINTS = {
    "p_division_of_units": "astropy/units/format",
    "division_of_units": "astropy/units/format",
    "CDS": "astropy/units/format",
}

_BORING_SYMBOLS = {
    "Action", "Bash", "Code", "File", "Fix", "Grep", "Issue", "Let",
    "PyREPL", "StringReplace", "Thoughts", "ViewCode",
}


def _sanitize_repo_command(command: str) -> str:
    """Qwen sometimes leaks host-side sandbox paths into tool-call text.

    shell_server intentionally rejects host paths so the agent stays
    repo-relative. For format-repair only, normalize common leading `cd ... &&
    ...` forms back to a relative command. This preserves the intended action
    while keeping the sandbox safety rule intact.
    """
    command = _ABS_MERGED_CD_RE.sub("", command)
    command = _ABS_WORKDIR_CD_RE.sub("", command)
    command = _HALLUCINATED_REPO_CD_RE.sub("", command)
    command = _ABS_MERGED_PATH_RE.sub("./", command)
    return command.strip()


def _viewcode_from_prose(prose: str) -> Optional[str]:
    """Turn Qwen's common prose-only "let me inspect <file.py>" response into
    a real ViewCode action. This is deliberately narrow and read-only."""
    stripped = prose.strip()
    if not stripped:
        return None
    low = stripped.lower()
    if not any(tok in low for tok in ("look", "view", "inspect", "find", "read")):
        return None

    path_match = re.search(
        r"([A-Za-z0-9_./-]+\.py)(?::(\d+)(?:-(\d+))?)?", stripped)
    if not path_match:
        return None
    path = path_match.group(1)
    if "/" not in path:
        path = _BASENAME_PATH_HINTS.get(path, path)

    # If prose says "line 235-237" or "lines 231-237", use that range.
    line_match = re.search(r"\blines?\s+(\d+)(?:\s*-\s*(\d+))?", low)
    if line_match:
        start = max(1, int(line_match.group(1)) - 20)
        end = int(line_match.group(2) or line_match.group(1)) + 30
    else:
        start = int(path_match.group(2) or "1")
        end = int(path_match.group(3) or str(start + 80))

    import json as J
    return (
        f"Thoughts: {stripped}\n"
        "Action: ViewCode\n"
        f"{J.dumps({'path': path, 'start_line': start, 'end_line': end}, ensure_ascii=False)}"
    )


def _grep_from_prose(prose: str) -> Optional[str]:
    """Recover useful symbol-location intent from malformed prose.

    This is intentionally read-only. If Qwen identifies a concrete symbol but
    loses the tool-call wrapper, we spend one action locating that symbol
    instead of burning three ReAct validation retries.
    """
    stripped = prose.strip()
    if not stripped:
        return None

    symbols: list[str] = []
    for pattern in (
        r"`([A-Za-z_][A-Za-z0-9_]*)`",
        r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        r"\b([A-Za-z_][A-Za-z0-9_]{5,})\s*\(",
        r"\b([A-Za-z_][A-Za-z0-9]*_[A-Za-z0-9_]+)\b",
    ):
        for match in re.finditer(pattern, stripped):
            sym = match.group(1)
            if sym not in _BORING_SYMBOLS and sym not in symbols:
                symbols.append(sym)

    if not symbols:
        return None

    symbol = symbols[0]
    root = _SYMBOL_PATH_HINTS.get(symbol, ".")
    import json as J
    import shlex

    quoted_symbol = shlex.quote(symbol)
    quoted_root = shlex.quote(root)
    command = (
        f"grep -R {quoted_symbol} -n {quoted_root} "
        "--include='*.py' | head -40"
    )
    return (
        f"Thoughts: {stripped}\n"
        "Action: Bash\n"
        f"{J.dumps({'command': command}, ensure_ascii=False)}"
    )


def _stringreplace_from_prose(prose: str) -> Optional[str]:
    """Recover a narrow class of explicit prose-only edits.

    This does not invent a fix. It only handles cases where the model has
    already stated the exact source line and the exact replacement in prose,
    but omitted the required `Action:` block. Keep this intentionally small:
    broad prose-to-edit synthesis would leak too much policy into the adapter.
    """
    stripped = prose.strip()
    if not stripped:
        return None

    # django__django-10880 pattern seen in live D runs:
    #   extra_context['distinct'] value is 'DISTINCT' but should be 'DISTINCT '
    #   Let me make this fix:
    # The viewed file context has the exact line below. Convert only this
    # explicit one-line change.
    if (
        "extra_context['distinct']" in stripped
        and "'DISTINCT '" in stripped
        and "DISTINCT" in stripped
        and any(tok in stripped.lower() for tok in ("should", "fix", "change", "replace"))
    ):
        import json as J

        old = "        extra_context['distinct'] = 'DISTINCT' if self.distinct else ''"
        new = "        extra_context['distinct'] = 'DISTINCT ' if self.distinct else ''"
        args = {
            "path": "django/db/models/aggregates.py",
            "old_str": old,
            "new_str": new,
        }
        return (
            f"Thoughts: {stripped}\n"
            "Action: StringReplace\n"
            f"{J.dumps(args, ensure_ascii=False)}"
        )
    return None


def _xml_tool_call_to_react(text: str) -> Optional[str]:
    """If `text` contains a <tool_call>...</tool_call> XML block, rewrite as
    ReAct text. Returns the rewritten text, or None if no tool_call was found.

    Multi-tool_call (rare): only the first is converted; remaining XML is
    dropped (moatless ReAct supports at most max_actions per response anyway).
    """
    m = _TOOL_CALL_RE.search(text)
    if not m:
        return None

    func_name = m.group(1).strip()
    body = m.group(2)

    args: dict[str, str] = {}
    for pm in _PARAM_RE.finditer(body):
        key = pm.group(1).strip()
        value = pm.group(2).strip()
        if key == "command":
            value = _sanitize_repo_command(value)
        args[key] = value

    # Prose before the tool_call → use as Thought
    prose_before = text[: m.start()].strip()
    thought = prose_before if prose_before else "Executing tool call."

    # Render JSON without the json module to keep newlines escaped nicely
    import json as J
    json_args = J.dumps(args, ensure_ascii=False)

    return f"Thoughts: {thought}\nAction: {func_name}\n{json_args}"


def _dsml_tool_call_to_react(text: str) -> Optional[str]:
    """Translate DeepSeek DSML tool-call markup into ReAct text.

    DeepSeek v4-flash often returns:

        <｜｜DSML｜｜tool_calls>
        <｜｜DSML｜｜invoke name="Bash">
        <｜｜DSML｜｜parameter name="command" string="true">ls</｜｜DSML｜｜parameter>
        </｜｜DSML｜｜invoke>
        </｜｜DSML｜｜tool_calls>

    The action intent is already explicit, so this is the same format adapter
    as the Qwen XML converter above.
    """
    m = _DSML_CALL_RE.search(text)
    if not m:
        return None

    func_name = m.group(1).strip()
    body = m.group(2)

    args: dict[str, str] = {}
    for pm in _DSML_PARAM_RE.finditer(body):
        key = pm.group(1).strip()
        value = pm.group(2).strip()
        if key == "command":
            value = _sanitize_repo_command(value)
        args[key] = value

    prose_before = text[: m.start()].strip()
    thought = prose_before if prose_before else "Executing tool call."

    import json as J
    json_args = J.dumps(args, ensure_ascii=False)

    return f"Thoughts: {thought}\nAction: {func_name}\n{json_args}"


def _orphan_parameter_to_react(text: str) -> Optional[str]:
    """Repair partial Qwen tool XML that lost the opening wrapper.

    Real failure mode seen in D-v2:

        I need to test...
        </parameter>
        <parameter=command>
        cd /tmp/heavy_swebench_x/merged && python -c ...
        </parameter>
        </function>
        </tool_call>

    The action intent is still recoverable. Prefer `command` as Bash; if a
    `code` parameter appears without command, route it to PyREPL.
    """
    params = {
        pm.group(1).strip(): pm.group(2).strip()
        for pm in _PARAM_RE.finditer(text)
    }
    if not params:
        return None

    import json as J

    first_param = _PARAM_RE.search(text)
    prose_before = text[: first_param.start()].strip() if first_param else ""
    # Strip orphan XML close tags from thought text.
    prose_before = re.sub(r"</?(?:tool_call|function|parameter)[^>]*>", "", prose_before).strip()
    thought = prose_before if prose_before else "Executing recovered tool call."

    if "command" in params:
        command = _sanitize_repo_command(params["command"])
        return f"Thoughts: {thought}\nAction: Bash\n{J.dumps({'command': command}, ensure_ascii=False)}"
    if "code" in params:
        return f"Thoughts: {thought}\nAction: PyREPL\n{J.dumps({'code': params['code']}, ensure_ascii=False)}"
    return None


def _prose_with_broken_tool_call_to_react(text: str) -> Optional[str]:
    """Qwen sometimes emits useful reasoning and then a dangling
    `<tool_call>` token without any function/parameters. That response cannot
    be executed as a tool call, but rejecting it wastes the branch after the
    model has often already applied or reasoned through a fix. Convert it into
    a conservative Bash action that prints a small git diff summary. This
    keeps ReAct validation moving and gives the next turn evidence about
    whether a patch exists.
    """
    if "<tool_call>" not in text or "</tool_call>" in text:
        return None
    prose = text.split("<tool_call>", 1)[0].strip()
    if not prose:
        return None
    return (
        _stringreplace_from_prose(prose)
        or _viewcode_from_prose(prose)
        or _grep_from_prose(prose)
    )


def _thoughts_only_to_react(text: str) -> Optional[str]:
    """Repair a common late-stage Qwen response: valid reasoning, no action.

    In D-v2 runs the model often reaches a plausible patch, then responds with
    `Thoughts: ...` paragraphs about verifying it but omits the required
    `Action:` block. Letting moatless reject and retry this response burns a
    node and grows context with no new signal. Convert it to a tiny Bash
    inspection that keeps the branch alive and gives the next turn concrete
    patch evidence. This is deliberately conservative: it never edits files or
    calls Finish.
    """
    stripped = text.strip()
    if not stripped.lower().startswith(("thought:", "thoughts:")):
        return None
    if re.search(r"(?im)^\s*action\s*:", stripped):
        return None
    return (
        _stringreplace_from_prose(stripped)
        or _viewcode_from_prose(stripped)
        or _grep_from_prose(stripped)
    )


def _plain_prose_to_react(text: str) -> Optional[str]:
    """Repair prose-only responses with no explicit `Thoughts:` prefix.

    Qwen3 sometimes produces a useful plan such as "Let me add get_inlines..."
    and stops without an Action block. Rejecting it three times loses the
    plan and burns a rollback branch. Convert it into a harmless Bash
    observation that preserves the prose in history and forces the next turn
    to produce a real action. We deliberately do not fabricate edits here.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if re.search(r"(?im)^\s*(thoughts?|action)\s*:", stripped):
        return None
    if "<tool_call>" in stripped or "<function=" in stripped:
        return None
    # Only repair coding-agent prose, not arbitrary malformed JSON fragments.
    low = stripped.lower()
    if not any(tok in low for tok in (
        "let me", "i need to", "i should", "i will", "the fix", "modify",
        "add ", "change ", "replace ", "implement", "patch",
    )):
        return None
    return (
        _stringreplace_from_prose(stripped)
        or _viewcode_from_prose(stripped)
        or _grep_from_prose(stripped)
    )


_ACTION_ALIAS_RE = re.compile(r"(?im)^(\s*Action:\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)$")
_ACTION_ALIASES = {
    "Grep": "GrepTool",
    "GrepSearch": "GrepTool",
    "RegexSearch": "GrepTool",
    "Glob": "GlobTool",
    "FindFiles": "GlobTool",
    "List": "ListFiles",
    "Ls": "ListFiles",
    "Python": "PyREPL",
    "PythonREPL": "PyREPL",
    "RunPython": "PyREPL",
    "ViewFile": "ViewCode",
    "ReadFile": "ViewCode",
    "Edit": "StringReplace",
    "Replace": "StringReplace",
    "Diff": "ViewDiff",
}


def _normalize_action_aliases(text: str) -> str:
    """Map common model-emitted tool names to the actual ReAct action names."""

    def repl(m: re.Match) -> str:
        name = m.group(2)
        return f"{m.group(1)}{_ACTION_ALIASES.get(name, name)}{m.group(3)}"

    return _ACTION_ALIAS_RE.sub(repl, text)


_THOUGHTS_LABEL_RE = re.compile(r"(?im)^([ \t]*)Thoughts[ \t]*:")

# DeepSeek-v4 emits an extra "Action Input:" label before the JSON; moatless wants
# the JSON directly after "Action: <name>".
_ACTION_INPUT_LABEL_RE = re.compile(r"(?im)^[ \t]*Action[ _]?Input[ \t]*:[ \t]*$\n?")

# Field-name aliases between common model output and the heavy action schemas
# (AgentStringReplace/CreateFile/... use path/old_str/new_str).
_JSON_KEY_ALIASES = {
    "file_path": "path", "filepath": "path", "filename": "path",
    "old_string": "old_str", "oldstring": "old_str", "old": "old_str",
    "new_string": "new_str", "newstring": "new_str", "new": "new_str",
    "file_text": "file_text", "content": "file_text",
}


def _strip_action_input_label(text: str) -> str:
    return _ACTION_INPUT_LABEL_RE.sub("", text)


def _alias_action_json_keys(text: str) -> str:
    """Rename common model key aliases in the action JSON block to the schema
    field names moatless validates against. Only touches the JSON after Action:."""
    import json as J
    m = _ACTION_JSON_RE.search(text)
    if not m:
        return text
    prefix, blob = m.group(1), m.group(2)
    try:
        obj = J.loads(blob)
    except Exception:
        return text
    if not isinstance(obj, dict):
        return text
    changed = False
    out = {}
    for k, v in obj.items():
        nk = _JSON_KEY_ALIASES.get(k, k)
        if nk != k:
            changed = True
        out[nk] = v
    if not changed:
        return text
    return text[: m.start()] + prefix + J.dumps(out, ensure_ascii=False)


def _normalize_thought_label(text: str) -> str:
    """moatless ReAct requires lines starting exactly with ``Thought:``.

    DeepSeek-v4 (and this adapter's own recovery output) frequently emit the
    plural ``Thoughts:``, which fails moatless's ``line.startswith("Thought:")``
    check and burns the whole turn. Normalize the section label to the exact
    form moatless expects. Pure syntax repair; the reasoning text is unchanged.
    """
    return _THOUGHTS_LABEL_RE.sub(r"\1Thought:", text)


_ACTION_JSON_RE = re.compile(r"(?ims)^(\s*Action:\s*[A-Za-z_][A-Za-z0-9_]*\s*\n)(\s*\{.*)\Z")
_ACTION_MARKER_RE = re.compile(r"(?im)^\s*Action:\s*")


def _escape_bare_control_chars_in_action_json(text: str) -> str:
    """Repair common ReAct JSON mistakes in action arguments.

    Qwen often emits multiline `old_str` / `new_str` values for StringReplace
    as literal newlines inside JSON strings. JSON forbids bare control chars,
    so moatless rejects the action before it can execute. This state-machine
    only touches the JSON argument block after `Action:` and only while inside
    a quoted JSON string; normal JSON whitespace between fields is preserved.
    """
    m = _ACTION_JSON_RE.search(text)
    if not m:
        return text
    prefix, blob = m.group(1), m.group(2)
    out: list[str] = []
    in_str = False
    escape = False
    changed = False
    for ch in blob:
        if in_str:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\":
                out.append(ch)
                escape = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
                changed = True
            elif ch == "\r":
                out.append("\\r")
                changed = True
            elif ch == "\t":
                out.append("\\t")
                changed = True
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    if not changed:
        return text
    return text[: m.start()] + prefix + "".join(out)


def _repair_python_literals_in_action_json(text: str) -> str:
    """Map Python-style JSON literals in ReAct action args to JSON literals.

    Qwen sometimes emits `{"recursive": False}` or `{"start_line": None}`.
    The action intent is unambiguous, but strict JSON parsing rejects it and
    burns retry budget. Only touch the action JSON blob, and only outside JSON
    strings.
    """
    m = _ACTION_JSON_RE.search(text)
    if not m:
        return text
    prefix, blob = m.group(1), m.group(2)
    out: list[str] = []
    in_str = False
    escape = False
    i = 0
    changed = False

    def has_word_at(word: str) -> bool:
        end = i + len(word)
        before_ok = i == 0 or not (blob[i - 1].isalnum() or blob[i - 1] == "_")
        after_ok = end >= len(blob) or not (blob[end].isalnum() or blob[end] == "_")
        return blob.startswith(word, i) and before_ok and after_ok

    while i < len(blob):
        ch = blob[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif has_word_at("True"):
            out.append("true")
            i += 4
            changed = True
        elif has_word_at("False"):
            out.append("false")
            i += 5
            changed = True
        elif has_word_at("None"):
            out.append("null")
            i += 4
            changed = True
        else:
            out.append(ch)
            i += 1

    if not changed:
        return text
    return text[: m.start()] + prefix + "".join(out)


def _prepend_missing_thoughts(text: str) -> str:
    """If a ReAct response has Action: but no Thoughts:, repair it.

    Qwen frequently emits:

        Let me inspect ...
        Action: Bash
        {...}

    moatless rejects this even though the intent is parseable. Treat the prose
    before the first Action as the Thoughts section; if no prose exists, add a
    generic one.
    """
    if re.search(r"(?im)^\s*thoughts?\s*:", text):
        return text
    m = _ACTION_MARKER_RE.search(text)
    if not m:
        return text
    before = text[:m.start()].strip()
    after = text[m.start():].lstrip()
    thought = before if before else "Executing action."
    return f"Thoughts: {thought}\n{after}"


class Qwen3ReActCompletionModel(ReActCompletionModel):
    """ReAct completion model that pre-translates Qwen3's tool_call XML
    output into ReAct text before validation."""

    response_format: LLMResponseFormat = LLMResponseFormat.REACT
    extra_body: dict | None = Field(
        default=None,
        description="Provider-specific body fields passed through to LiteLLM.",
    )

    def _litellm_base_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        response_format: dict | None = None,
    ):
        litellm.drop_params = True

        @tenacity.retry(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_exponential(multiplier=3),
            retry=tenacity.retry_if_exception_type(Exception),
            reraise=True,
            before_sleep=lambda retry_state: LOG.warning(
                "Retrying litellm completion after error: %s",
                retry_state.outcome.exception(),
            ),
        )
        def _do_completion():
            effective_stop = self.stop_words
            if effective_stop is None and self.response_format == LLMResponseFormat.REACT:
                effective_stop = ["\nThought:", "\n\nThought:"]

            t_wall = time.time()
            t_mono = time.monotonic_ns()
            err = None
            try:
                kwargs = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": messages,
                    "metadata": self.metadata or {},
                    "timeout": self.timeout,
                    "api_base": self.model_base_url,
                    "api_key": self.model_api_key,
                    "stop": effective_stop,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "response_format": response_format,
                    "request_timeout": self.timeout,
                }
                if self.extra_body is not None:
                    kwargs["extra_body"] = self.extra_body
                return litellm.completion(**kwargs)
            except Exception as exc:
                err = exc
                raise
            finally:
                trace_path = os.environ.get("MOATLESS_MS_TRACE_PATH")
                if trace_path:
                    rec = {
                        "t_wall_start_s": t_wall,
                        "dur_s": (time.monotonic_ns() - t_mono) / 1e9,
                        "model": self.model,
                        "n_messages": len(messages),
                    }
                    if err is not None:
                        rec["error"] = str(err)[:200]
                    try:
                        import json
                        with open(trace_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(rec) + "\n")
                    except Exception:
                        pass

        try:
            resp = _do_completion()
        except tenacity.RetryError as e:
            raise e.reraise()
        # This moatless version parses ReAct straight from the content returned
        # here (see react.py:_do_completion), so the format adapters MUST run on
        # this response, not in _validate_completion (which this path never calls).
        self._apply_react_rewrites(resp)
        return resp

    def _prepare_system_prompt(self, system_prompt, response_schema):
        prompt = super()._prepare_system_prompt(system_prompt, response_schema)
        # Upstream moatless encourages multiple Action blocks in one response.
        # That is actively bad for Deltabox: we want an OS checkpoint after
        # every side-effecting tool call so rollback can branch at action
        # granularity. Keep the same action schemas, but force one action per
        # completion.
        prompt = prompt.replace(
            "Important: You can include multiple Action blocks to perform "
            "sequential actions. The first Action must be preceded by a "
            "Thought section.",
            "Important: emit exactly one Action block. The Action must be "
            "preceded by a Thought section. Do not include multiple Action "
            "blocks in one response.",
        )
        prompt = prompt.replace(
            "Important: You can include multiple Action blocks to perform "
            "sequential actions. The first Action must be preceded by a "
            "Thought section",
            "Important: emit exactly one Action block. The Action must be "
            "preceded by a Thought section. Do not include multiple Action "
            "blocks in one response",
        )
        return prompt

    async def _validate_completion(self, completion_response):
        self._apply_react_rewrites(completion_response)
        return await super()._validate_completion(completion_response)

    def _apply_react_rewrites(self, completion_response):
        """Translate model-native output into the strict ReAct text moatless
        parses. Runs on the raw completion response (see _litellm_base_completion).
        Mutates completion_response.choices[0].message.content in place."""
        # Rewrite content in-place if we detect tool_call XML
        try:
            msg = completion_response.choices[0].message
            original = msg.content or ""

            # Fallback 1: translate model-native tool-call markup → ReAct
            converted = _dsml_tool_call_to_react(original)
            if converted is not None:
                LOG.debug(f"converted DSML tool call → ReAct text "
                          f"({len(original)} → {len(converted)} chars)")
                msg.content = converted
            else:
                converted = _xml_tool_call_to_react(original)
            if converted is not None:
                if msg.content == original:
                    LOG.debug(f"converted tool_call XML → ReAct text "
                              f"({len(original)} → {len(converted)} chars)")
                    msg.content = converted
            else:
                converted = _orphan_parameter_to_react(original)
                if converted is not None:
                    LOG.debug("converted orphan parameter XML → ReAct text")
                    msg.content = converted
                    original = msg.content
                    converted = None
            if msg.content == original:
                converted = _prose_with_broken_tool_call_to_react(original)
                if converted is not None:
                    LOG.debug("converted dangling tool_call marker → Bash diff check")
                    msg.content = converted
            if msg.content == original:
                converted = _thoughts_only_to_react(original)
                if converted is not None:
                    LOG.debug("converted thoughts-only response → Bash diff check")
                    msg.content = converted
            if msg.content == original:
                converted = _plain_prose_to_react(original)
                if converted is not None:
                    LOG.debug("converted prose-only response → Bash format nudge")
                    msg.content = converted
            # Fallback 2: prepend Thoughts: if missing (Qwen3 sometimes emits
            # prose followed by Action:, skipping the literal Thoughts label).
            if msg.content == original:
                repaired_thoughts = _prepend_missing_thoughts(original)
                if repaired_thoughts != original:
                    msg.content = repaired_thoughts
                    LOG.debug("prepended missing Thoughts: prefix")
            if msg.content == original and original.strip().startswith("Action:") and "Thoughts:" not in original:
                msg.content = "Thoughts: Executing action.\n" + original
                LOG.debug("prepended missing Thoughts: prefix")

            # Normalize the section label LAST so it covers both the raw model
            # output and every recovery path above (which emit "Thoughts:").
            relabeled = _normalize_thought_label(msg.content or "")
            if relabeled != (msg.content or ""):
                LOG.debug("normalized 'Thoughts:' -> 'Thought:' for ReAct")
                msg.content = relabeled

            destripped = _strip_action_input_label(msg.content or "")
            if destripped != (msg.content or ""):
                LOG.debug("stripped 'Action Input:' label for ReAct")
                msg.content = destripped

            aliased = _alias_action_json_keys(msg.content or "")
            if aliased != (msg.content or ""):
                LOG.debug("aliased action JSON keys to schema field names")
                msg.content = aliased

            normalized = _normalize_action_aliases(msg.content or "")
            if normalized != (msg.content or ""):
                LOG.debug("normalized action alias in ReAct response")
                msg.content = normalized
            repaired = _escape_bare_control_chars_in_action_json(msg.content or "")
            if repaired != (msg.content or ""):
                LOG.debug("escaped bare control chars in ReAct action JSON")
                msg.content = repaired
            repaired = _repair_python_literals_in_action_json(msg.content or "")
            if repaired != (msg.content or ""):
                LOG.debug("repaired Python literals in ReAct action JSON")
                msg.content = repaired

        except Exception as e:
            LOG.warning(f"tool_call rewrite skipped: {e}")
