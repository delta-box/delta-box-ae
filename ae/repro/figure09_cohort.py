"""The sole runnable Figure 9 cohort: 80 frozen, complete edit trajectories.

The original 185-row CSV remains provenance, never a selectable full mode.
Selection uses input features and preservation of completed work, not measurements.
"""
from __future__ import annotations

from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

from .common import AE_ROOT, digest

SOURCE_PATH = "paper/figure-09/cohort-war.csv"
SOURCE_SHA256 = "1631b450698ed76374bd5b1685e079904245f579587ae5f056da8dd3eb9da146"
MANIFEST_PATH = "paper/figure-09/cohort-80.json"
MANIFEST_SHA256 = "3c10e8838a94067a3b6ff1f423b7ac5de7c62ed27f699bf688b4c40909f065b0"
KEYS_SHA256 = "4e2d588bf57cf128a20aead38848a6f9b0ebd4a6e7283820ce67315bc2b5587e"
POOL_QUOTAS = {"claude/linear": 32, "claude/mcts": 16,
               "mimo/linear": 26, "mimo/mcts": 6}


def _require(condition, message):
    if not condition:
        raise ValueError("Figure 9 fixed-80: " + message)


def _key(row):
    return row["pool"].replace("/", "_") + "__" + row["instance"]


def _checked_record(record, expected_path, ae_root):
    _require(isinstance(record, dict) and record.get("path") == expected_path,
             "input path differs: " + expected_path)
    path = ae_root / expected_path
    _require(path.is_file(), "input missing: " + expected_path)
    data = path.read_bytes()
    _require(record.get("bytes") == len(data)
             and record.get("sha256") == hashlib.sha256(data).hexdigest(),
             "input SHA256/size differs: " + expected_path)
    return data


def figure09_rows(rows, config, *, manifest_path=None, ae_root=AE_ROOT):
    """Validate the immutable selection and complete inputs before planning jobs."""
    # Do not silently accept a config purporting to restore the historical scope.
    if "figure09_inputs" in config:
        _require(type(config["figure09_inputs"]) in (str, int)
                 and str(config["figure09_inputs"]) == "80",
                 "only 80 inputs are supported; no all/185 mode")
    ae_root = Path(ae_root)
    source_path = ae_root / SOURCE_PATH
    _require(digest(source_path) == SOURCE_SHA256, "historical source CSV SHA256 differs")
    with source_path.open(newline="") as stream:
        original = list(csv.DictReader(stream))
    _require(rows == original and len(rows) == 185, "historical source row set/order differs")
    source_keys = [_key(row) for row in rows]
    _require(len(set(source_keys)) == 185, "duplicate historical input key")
    by_key = dict(zip(source_keys, rows))
    path = Path(manifest_path) if manifest_path is not None else ae_root / MANIFEST_PATH
    manifest = json.loads(path.read_text())
    _require(manifest.get("schema_version") == 1 and manifest.get("name") == "figure09-80",
             "unsupported selection manifest")
    _require(manifest.get("source") == dict(path=SOURCE_PATH, sha256=SOURCE_SHA256,
             input_count=185, distinct_instances=136), "historical source binding differs")
    entries = manifest.get("inputs")
    _require(manifest.get("input_count") == 80 and isinstance(entries, list)
             and len(entries) == 80, "manifest must contain exactly 80 inputs")
    _require(all(isinstance(entry, dict) for entry in entries), "invalid selected input record")
    keys = [entry.get("key") for entry in entries]
    _require(all(isinstance(key, str) for key in keys) and len(set(keys)) == 80,
             "selection keys must be unique")
    _require(all(key in by_key for key in keys), "unknown selected input key")
    _require(keys == [key for key in source_keys if key in set(keys)],
             "selected input order differs from source CSV")
    key_hash = hashlib.sha256(("\n".join(keys) + "\n").encode()).hexdigest()
    _require(key_hash == manifest.get("input_keys_sha256") == KEYS_SHA256,
             "fixed selection key SHA256 differs")
    chosen = [by_key[key] for key in keys]
    _require(manifest.get("pool_quotas") == POOL_QUOTAS
             and dict(Counter(row["pool"] for row in chosen)) == POOL_QUOTAS,
             "pool quota differs")
    all_cells = {(row["pool"], row["instance"].split("__")[0]) for row in rows}
    cells = {(row["pool"], row["instance"].split("__")[0]) for row in chosen}
    projects = Counter(row["instance"].split("__")[0] for row in chosen)
    _require(cells == all_cells and len(cells) == 27 and len(projects) == 10,
             "pool/project coverage differs")
    _require(all(_key(row) in keys for row in rows
                 if int(row["n_edits"]) > 10 or row["pool"] == "mimo/mcts"),
             "long-tail or sparse-pool input omitted")
    preserved = manifest.get("preserved_prior_run", {})
    _require(preserved.get("input_keys") == source_keys[:26]
             and set(source_keys[:26]).issubset(keys),
             "prior completed input coverage differs")
    _require(manifest.get("selection_policy", {}).get("uses_measured_performance") is False,
             "selection must not depend on measured performance")
    edit_count = patch_bytes = 0
    edit_bins = Counter()
    for row, entry in zip(chosen, entries):
        _require(entry.get("instance") == row["instance"]
                 and entry.get("pool") == row["pool"]
                 and entry.get("project") == row["instance"].split("__")[0],
                 "selected identity differs: " + entry["key"])
        _checked_record(entry.get("trajectory"), row["local"], ae_root)
        _require(entry["trajectory"]["sha256"] == row["sha256"],
                 "trajectory binding differs: " + entry["key"])
        actions = json.loads(_checked_record(entry.get("actions"), row["action_local"], ae_root))
        edits = actions.get("edits")
        _require(isinstance(edits, list) and len(edits) == int(row["n_edits"])
                 and actions.get("n_edits") == entry.get("n_edits") == len(edits)
                 and actions.get("instance_id") == row["instance"]
                 and actions.get("pool") == row["pool"],
                 "complete action plan differs: " + entry["key"])
        _require(all(type(edit.get("diff_bytes")) is int and edit["diff_bytes"] >= 0
                     for edit in edits), "invalid patch size")
        total_patch = sum(edit["diff_bytes"] for edit in edits)
        _require(entry.get("patch_bytes") == total_patch, "patch feature differs")
        edit_count += len(edits)
        patch_bytes += total_patch
        n = len(edits)
        edit_bins["1-2" if n <= 2 else "3-5" if n <= 5 else
                  "6-10" if n <= 10 else "11-20" if n <= 20 else "21+"] += 1
    coverage = manifest.get("coverage", {})
    _require(coverage == dict(projects=dict(projects), pool_project_cells=27,
             edit_count=edit_count, patch_bytes=patch_bytes, edit_bins=dict(edit_bins),
             max_edits=max(int(row["n_edits"]) for row in chosen)),
             "coverage metadata differs")
    _require(digest(path) == MANIFEST_SHA256, "fixed manifest SHA256 differs")
    selection = dict(name="figure09-80", inputs=80, historical_inputs=185,
                     path=MANIFEST_PATH, sha256=digest(path),
                     source_csv_sha256=SOURCE_SHA256, input_keys_sha256=KEYS_SHA256,
                     complete_edit_actions=True, filesystem_arms=["ext4", "xfs", "xfs_reflink"],
                     edit_actions_per_filesystem=edit_count, preserved_input_count=26)
    return chosen, selection
