"""Restore recorded literal-search file priority while preserving all live matches.

The recording retains only files in the bounded search context, not the complete
filesystem traversal. This explicit replay condition puts those files first and
keeps current order for the rest. It never substitutes an observation or an LLM
response. Conflicting recordings stay unadapted. If a recorded file is absent
from the live literal matches, keep the native order: recorded search_hits can
describe the subsequent semantic fallback, not this literal search operation.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import shutil

SOURCE_SHA256 = '0949c58dd1484a38f1016115309b2812ef634d496450312335b1e090eff8a36d'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_table(trace):
    orders = {}
    ambiguous = set()
    def walk(node):
        for step in node.get('action_steps') or []:
            args = step.get('action') or {}
            if args.get('action_args_class') != 'moatless.actions.find_code_snippet.FindCodeSnippetArgs':
                continue
            hits = ((step.get('observation') or {}).get('properties') or {}).get('search_hits')
            if not isinstance(hits, dict):
                continue
            files = [f['file_path'] for f in hits.get('files', [])]
            if not files:
                continue
            if len(files) != len(set(files)):
                raise ValueError('Duplicate recorded search file')
            key = (args['code_snippet'], args.get('file_pattern'))
            if key in orders and orders[key] != files:
                ambiguous.add(key)
            orders[key] = files
        for child in node.get('children') or []:
            walk(child)
    walk(json.loads(trace.read_text())['root'])
    return dict(schema_version=1, trace_sha256=digest(trace),
                scope='recorded bounded-context file priority; live literal matches unchanged',
                ambiguous_queries_passthrough=len(ambiguous),
                orders=[dict(search_text=k[0], file_pattern=k[1], files=v)
                        for k, v in sorted(orders.items(), key=lambda kv: repr(kv[0])) if k not in ambiguous])


def reorder(matches, files):
    """Stable permutation only: no match is created, removed, or rewritten."""
    if not set(files).issubset({path for path, line in matches}):
        raise ValueError('Recorded search priority file has no live literal match')
    priority = {path: index for index, path in enumerate(files)}
    return sorted(matches, key=lambda match: priority.get(match[0], len(priority)))


@lru_cache(maxsize=1)
def table():
    value = json.loads(Path(__file__).with_suffix('.json').read_text())
    if value['schema_version'] != 1:
        raise ValueError('Unsupported search order schema')
    return {(r['search_text'], r['file_pattern']): r['files'] for r in value['orders']}


def restore_search_order(matches, search_text, file_pattern):
    matches = list(matches)
    files = table().get((search_text, file_pattern))
    if files is None or not set(files).issubset({path for path, line in matches}):
        # Do not turn a legitimate empty/partial literal search into an error.
        # SearchBase still performs its original fallback and response logic.
        return matches
    return reorder(matches, files)


def stage_search_order(payload, trace):
    staged = payload / 'moatless-det-src'
    source = staged.resolve(strict=True)
    original = source / 'moatless/repository/file.py'
    if digest(original) != SOURCE_SHA256:
        raise ValueError('Literal search source lock mismatch')
    text = original.read_text()
    marker = '        return matches\n\n    def list_directory('
    if text.count(marker) != 1:
        raise ValueError('Search patch must target exactly one final return')
    frozen = build_table(trace)
    if staged.is_symlink():
        staged.unlink()
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    package = staged / 'moatless'
    target = package / 'repository/file.py'
    if digest(target) != SOURCE_SHA256:
        raise ValueError('Literal search source changed during staging')
    adapter = package / '_replay_search_order.py'
    data = adapter.with_suffix('.json')
    shutil.copy2(Path(__file__), adapter)
    data.write_text(json.dumps(frozen, indent=2, ensure_ascii=False)+'\n')
    target.write_text(text.replace(marker,
        '        from moatless._replay_search_order import restore_search_order\n'
        '        return restore_search_order(matches, search_text, file_pattern)\n\n    def list_directory('))
    return dict(adapter='recorded-search-file-priority-v2', **frozen,
                missing_recorded_files='preserve native literal search results and fallback',
                original_sha256=SOURCE_SHA256,
                staged_sha256={str(p.relative_to(package)): digest(p) for p in (target, adapter, data)})
