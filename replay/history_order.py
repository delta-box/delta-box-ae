"""Restore historical set serialization at the synthetic ViewCode producer.

The recorded inputs retain set iteration order; ContextFile.spans does not.
A frozen, unambiguous (path, span multiset) table restores only that order.
This module never reads a mock cursor or substitutes a recorded message.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil


SYNTHETIC_PREFIX = (
    "Thought: <thoughts>Let's view the content in the updated files</thoughts>\n"
    "Action: ViewCode\n"
)
PINNED_SOURCES = {
    "file_context.py": "3b632fa075c66ab43929929b5bbeac78379f810c3bdd28b1be60dd60e0e3677d",
    "message_history.py": "0e95dda822531032191c2670c1c740dea5a7831673666a6ca729c3287ee856bc",
}
_CALL = "                                        span_ids=context_file.span_ids,"
_CLASS = "class MessageHistoryGenerator(BaseModel):"
_counts = {"lookups": 0, "reordered": 0, "misses": 0}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(file_path, span_ids):
    if not isinstance(file_path, str) or not isinstance(span_ids, list):
        raise ValueError("synthetic ViewCode requires a path and span list")
    if any(not isinstance(span, str) for span in span_ids):
        raise ValueError("synthetic ViewCode span IDs must be strings")
    # Keep multiplicity: adding/removing a duplicate is a different signature.
    return file_path, tuple(sorted(span_ids))


def _recorded_inputs(node):
    for value in (node.get("completions") or {}).values():
        for completion in value if isinstance(value, list) else [value]:
            if isinstance(completion, dict):
                yield completion.get("input") or []
    for step in node.get("action_steps") or []:
        completion = (step.get("observation") or {}).get("execution_completion")
        if isinstance(completion, dict):
            yield completion.get("input") or []
    for child in node.get("children") or []:
        yield from _recorded_inputs(child)


def build_order_table(trace: Path, *, message_policy='strict') -> dict:
    """Extract only synthetic-history ordering; ambiguous recordings fail closed."""
    data = json.loads(trace.read_text())
    orders = {}
    ambiguous = set()
    synthetic_messages = 0
    for messages in _recorded_inputs(data["root"]):
        for message in messages:
            content = message.get("content")
            if (message.get("role") != "assistant" or not isinstance(content, str)
                    or not content.startswith(SYNTHETIC_PREFIX)):
                continue
            args = json.loads(content[len(SYNTHETIC_PREFIX):])
            # Do not broaden parsing to other actions or unknown serialization.
            if set(args) != {"files"} or not isinstance(args["files"], list):
                raise ValueError("unsupported synthetic ViewCode schema")
            if content != SYNTHETIC_PREFIX + json.dumps(args, indent=2, ensure_ascii=False):
                raise ValueError("unsupported synthetic ViewCode serialization")
            synthetic_messages += 1
            for file in args["files"]:
                if set(file) != {"file_path", "start_line", "end_line", "span_ids"}:
                    raise ValueError("unsupported synthetic ViewCode file schema")
                spans = file["span_ids"]
                if spans is None or spans == []:  # show_all_spans does not use the adapter.
                    continue
                key = _signature(file["file_path"], spans)
                if key in ambiguous:
                    continue
                if key in orders and orders[key] != spans:
                    if message_policy == 'strict':
                        raise ValueError(f"ambiguous historical span order for {key[0]!r}")
                    ambiguous.add(key)
                    del orders[key]
                    continue
                orders[key] = spans
    return {
        "schema_version": 1,
        "scope": "synthetic ViewCode span order only; message differences follow replay message policy",
        "trace_sha256": _sha256(trace),
        "synthetic_messages": synthetic_messages,
        "ambiguous_keys_passthrough": len(ambiguous),
        "orders": [{"file_path": path, "span_ids": spans}
                   for (path, _), spans in sorted(orders.items())],
    }


@lru_cache(maxsize=1)
def _runtime_table():
    # Staging copies this module and its frozen table together into moatless/.
    path = Path(__file__).with_suffix(".json")
    table = json.loads(path.read_text())
    if table["schema_version"] != 1:
        raise ValueError("unsupported history order table version")
    orders = {}
    for row in table["orders"]:
        key = _signature(row["file_path"], row["span_ids"])
        if key in orders and orders[key] != row["span_ids"]:
            raise ValueError("ambiguous staged history order table")
        orders[key] = row["span_ids"]
    return orders, _sha256(path), table["trace_sha256"]


def restore_span_order(file_path: str, span_ids: list[str]) -> list[str]:
    """Serialize live spans in a frozen historical order, without changing content."""
    orders, _, _ = _runtime_table()
    key = _signature(file_path, span_ids)
    ordered = orders.get(key)
    _counts['lookups'] += 1
    if ordered is None:
        _counts['misses'] += 1
        if os.environ.get('MOCK_MESSAGE_POLICY', 'audit') == 'strict':
            raise ValueError(f"unrecorded synthetic ViewCode span signature: {file_path!r}")
        # Preserve the live request. The mock buffers its difference for export.
        return list(span_ids)
    _counts['reordered'] += ordered != span_ids
    return list(ordered)


def stage_history_order(payload: Path, trace: Path, *, message_policy='audit') -> dict:
    """Patch a private source copy; refuse changed upstream sources or ambiguity."""
    source = (payload / "moatless-det-src").resolve(strict=True)
    source_hashes = {name: _sha256(source / "moatless" / name) for name in PINNED_SOURCES}
    if source_hashes != PINNED_SOURCES:
        raise ValueError(f"history serializer source lock mismatch: {source_hashes}")
    original = (source / "moatless/message_history.py").read_text()
    if original.count(_CALL) != 1 or original.count(_CLASS) != 1:
        raise ValueError("history serializer patch must target exactly one synthetic branch")
    if message_policy not in ('audit', 'strict'):
        raise ValueError('unknown history message policy')
    table = build_order_table(trace, message_policy=message_policy)
    table["upstream_sha256"] = source_hashes
    staged = payload / "moatless-det-src"
    if not staged.is_symlink():
        raise ValueError("history adapter requires the newly staged source symlink")
    staged.unlink()
    shutil.copytree(source, staged, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    package = staged / "moatless"
    if any(_sha256(package / name) != digest for name, digest in source_hashes.items()):
        raise ValueError("history serializer sources changed while staging")
    adapter = package / "_replay_history_order.py"
    table_path = adapter.with_suffix(".json")
    shutil.copy2(Path(__file__), adapter)
    table_path.write_text(json.dumps(table, indent=2, ensure_ascii=False) + "\n")
    patched = original.replace(
        _CALL,
        "                                        span_ids=_restore_span_order(file_path, context_file.span_ids),",
    ).replace(_CLASS, "from moatless._replay_history_order import restore_span_order as _restore_span_order\n\n\n" + _CLASS)
    history = package / "message_history.py"
    history.write_text(patched)
    return {
        "schema_version": 1,
        "adapter": "historical-set-order-v1",
        "scope": table["scope"],
        "trace_sha256": table["trace_sha256"],
        "upstream_sha256": source_hashes,
        "table_entries": len(table["orders"]),
        "synthetic_messages": table["synthetic_messages"],
        "ambiguous_keys_passthrough": table['ambiguous_keys_passthrough'],
        "staged_sha256": {path.name: _sha256(path) for path in
                          (history, adapter, table_path, package / "file_context.py")},
        "mock_source_sha256": {name: _sha256(payload / name) for name in
                               ("mock_llm_server.py", "protocol.py", "replay_driver.py", "trajectory_index.py")},
        "hit_evidence": "in-memory scalar counters only; remaining message differences exported by mock after measurement",
    }
