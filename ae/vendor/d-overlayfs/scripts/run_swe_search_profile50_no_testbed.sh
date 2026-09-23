#!/usr/bin/env bash
# Run 50 fresh native moatless-swe-search MCTS trajectories and collect
# per-step RSS/soft-dirty/file-delta metrics.  This intentionally does not use
# Docker/testbed verification and does not reuse older optimized trajectories.
set -euo pipefail

SRC=${SRC:-/mnt/disk2/dyp/spr_payload/moatless-det-src}
PY=${PY:-/mnt/disk2/dyp/moatless_det_venv/bin/python}
DOVERLAY=${DOVERLAY:-/mnt/disk2/dyp/d-overlayfs}
SPR_PAYLOAD=${SPR_PAYLOAD:-/mnt/disk2/dyp/spr_payload}
RUN_ROOT=${RUN_ROOT:-$DOVERLAY/traces/swe-search/qwen3-coder-30b-vllm18086/profile50-native-mcts30-$(date +%Y%m%d_%H%M%S)}
REPO_CACHE=${REPO_CACHE:-$SPR_PAYLOAD/repos}
REPO_DIR=${REPO_DIR:-$RUN_ROOT/repos}
INDEX_STORE_DIR=${INDEX_STORE_DIR:-$SPR_PAYLOAD/index_store}
LOCAL_PROXY_BASE=${LOCAL_PROXY_BASE:-http://127.0.0.1:18086/v1}
MODEL_CONFIG=${MODEL_CONFIG:-qwen3_coder}
MODEL_NAME=${MODEL_NAME:-openai/qwen3-coder-30b}
MAX_ITERATIONS=${MAX_ITERATIONS:-30}
MAX_EXPANSIONS=${MAX_EXPANSIONS:-2}
MAX_COST=${MAX_COST:-10.0}
TIMEOUT_S=${TIMEOUT_S:-3600}
LIMIT=${LIMIT:-50}
NUMA_NODE=${NUMA_NODE:-1}
WORKLIST=${WORKLIST:-}

mkdir -p "$RUN_ROOT" "$REPO_DIR" "$RUN_ROOT/evals" "$RUN_ROOT/step_metrics" "$RUN_ROOT/tmp"

if ! curl --noproxy '*' -fsS --connect-timeout 5 --max-time 15 "$LOCAL_PROXY_BASE/models" >/dev/null; then
  echo "ERROR: model endpoint is not reachable at $LOCAL_PROXY_BASE" >&2
  echo "Expected the h20/allinai tunnel: 127.0.0.1:18086 -> 127.0.0.1:8006/v1" >&2
  exit 2
fi

if [[ -z "$WORKLIST" ]]; then
  WORKLIST="$RUN_ROOT/worklist.tsv"
  "$PY" - "$SRC/moatless/benchmark" "$REPO_CACHE" "$INDEX_STORE_DIR" "$WORKLIST" "$LIMIT" <<'PY'
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

bench = Path(sys.argv[1])
repo_cache = Path(sys.argv[2])
index_store = Path(sys.argv[3])
out = Path(sys.argv[4])
limit = int(sys.argv[5])

rows = []
seen = set()
for split in ("lite", "verified", "cohort212", "cohort45"):
    path = bench / f"swebench_{split}_all_evaluations.json"
    if not path.exists():
        continue
    for item in json.loads(path.read_text(encoding="utf-8")):
        instance_id = item["instance_id"]
        if instance_id in seen:
            continue
        if not (index_store / instance_id).is_dir():
            continue
        if not (repo_cache / f"swe-bench_{instance_id}").is_dir():
            continue
        seen.add(instance_id)
        rows.append((instance_id, split, item.get("repo", "")))

by_repo = defaultdict(list)
for row in rows:
    by_repo[row[2]].append(row)

selected = []
while len(selected) < limit:
    progressed = False
    for repo in sorted(by_repo):
        if by_repo[repo]:
            selected.append(by_repo[repo].pop(0))
            progressed = True
            if len(selected) >= limit:
                break
    if not progressed:
        break

if len(selected) < limit:
    raise SystemExit(f"only {len(selected)} eligible instances found, need {limit}")

with out.open("w", encoding="utf-8") as f:
    for row in selected:
        f.write("\t".join(row) + "\n")
print(f"Wrote {len(selected)} balanced eligible instances to {out}")
PY
fi

while IFS=$'\t' read -r inst _split repo; do
  [[ -n "$inst" ]] || continue
  repo_dir=${repo//\//__}
  if [[ -d "$REPO_CACHE/swe-bench_$repo_dir" && ! -e "$REPO_DIR/swe-bench_$repo_dir" ]]; then
    ln -s "$REPO_CACHE/swe-bench_$repo_dir" "$REPO_DIR/swe-bench_$repo_dir"
  fi
done < "$WORKLIST"

export OPENAI_API_BASE="$LOCAL_PROXY_BASE"
export OPENAI_BASE_URL="$LOCAL_PROXY_BASE"
export OPENAI_API_KEY=dummy
export CUSTOM_LLM_API_KEY=dummy
export NO_PROXY=localhost,127.0.0.1,::1,192.168.1.8
export no_proxy="$NO_PROXY"
export REPO_DIR
export INDEX_STORE_DIR
export MOATLESS_DIR="$RUN_ROOT/evals"
export MOATLESS_STEP_METRICS_DIR="$RUN_ROOT/step_metrics"
export TMPDIR="$RUN_ROOT/tmp"
export PYTHONHASHSEED=0
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_MAX_THREADS=1

total=$(wc -l < "$WORKLIST")
echo "===== profile50 native mcts${MAX_ITERATIONS}: $total instances $(date -Is) ====="
echo "RUN_ROOT=$RUN_ROOT"
echo "LOCAL_PROXY_BASE=$LOCAL_PROXY_BASE"
echo "REPO_DIR=$REPO_DIR"
echo "INDEX_STORE_DIR=$INDEX_STORE_DIR"
echo "NUMA_NODE=$NUMA_NODE"

i=0
while IFS=$'\t' read -r inst split repo; do
  [[ -n "$inst" ]] || continue
  i=$((i + 1))
  final_dir="$RUN_ROOT/$inst"
  eval_name="profile50_native_mcts${MAX_ITERATIONS}_${inst}"
  mkdir -p "$final_dir"

  if [[ -f "$final_dir/manifest.json" ]] && python3 - "$final_dir/manifest.json" <<'PY'
import json, sys
try:
    m=json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if m.get("trajectory_exists") and m.get("step_metrics_lines", 0) > 0 else 1)
PY
  then
    echo "[$i/$total] $inst split=$split SKIP"
    continue
  fi

  rm -f "$RUN_ROOT/step_metrics/$inst.jsonl"
  start_wall=$(python3 - <<'PY'
import time
print(f"{time.time():.6f}")
PY
)
  echo "[$i/$total] START $inst split=$split repo=$repo $(date -Is)"

  set +e
  (
    cd "$SRC"
    numactl --cpunodebind="$NUMA_NODE" --membind="$NUMA_NODE" \
      timeout "$TIMEOUT_S" "$PY" moatless/benchmark/run_evaluation.py \
        --config "$MODEL_CONFIG" \
        --model "$MODEL_NAME" \
        --split "$split" \
        --instance-ids "$inst" \
        --num-workers 1 \
        --max-iterations "$MAX_ITERATIONS" \
        --max-expansions "$MAX_EXPANSIONS" \
        --max-cost "$MAX_COST" \
        --evaluation-name "$eval_name" \
        --no-testbed
  ) > "$final_dir/run.log" 2>&1
  rc=$?
  set -e

  end_wall=$(python3 - <<'PY'
import time
print(f"{time.time():.6f}")
PY
)

  eval_inst_dir="$RUN_ROOT/evals/$eval_name/$inst"
  [[ -f "$eval_inst_dir/trajectory.json" ]] && cp "$eval_inst_dir/trajectory.json" "$final_dir/trajectory.json"
  [[ -f "$eval_inst_dir/eval_result.json" ]] && cp "$eval_inst_dir/eval_result.json" "$final_dir/eval_result.json"
  [[ -f "$RUN_ROOT/step_metrics/$inst.jsonl" ]] && cp "$RUN_ROOT/step_metrics/$inst.jsonl" "$final_dir/step_metrics.jsonl"
  [[ -d "$RUN_ROOT/evals/$eval_name/logs" ]] && rm -rf "$final_dir/logs" && cp -a "$RUN_ROOT/evals/$eval_name/logs" "$final_dir/"
  [[ -d "$RUN_ROOT/evals/$eval_name/prompt_logs/$inst" ]] && rm -rf "$final_dir/prompt_logs" && cp -a "$RUN_ROOT/evals/$eval_name/prompt_logs/$inst" "$final_dir/prompt_logs"
  [[ -f "$RUN_ROOT/evals/$eval_name/evaluation.json" ]] && cp "$RUN_ROOT/evals/$eval_name/evaluation.json" "$final_dir/evaluation.json"

  repo_path="$REPO_DIR/swe-bench_$inst"
  repo_bytes=$(du -sb --exclude=.git "$repo_path" 2>/dev/null | awk '{print $1}' || true)
  step_lines=$(wc -l < "$RUN_ROOT/step_metrics/$inst.jsonl" 2>/dev/null || echo 0)
  python3 - "$final_dir/manifest.json" <<PY
import json
from pathlib import Path

final_dir = Path("$final_dir")
trajectory = final_dir / "trajectory.json"
manifest = {
    "instance_id": "$inst",
    "split": "$split",
    "repo": "$repo",
    "evaluation_name": "$eval_name",
    "trace_kind": "native_moatless_swe_search_no_testbed_profile",
    "model": "$MODEL_NAME",
    "model_base": "$LOCAL_PROXY_BASE",
    "max_iterations": int("$MAX_ITERATIONS"),
    "max_expansions": int("$MAX_EXPANSIONS"),
    "max_cost": float("$MAX_COST"),
    "numa_node": int("$NUMA_NODE"),
    "start_wall_s": float("$start_wall"),
    "end_wall_s": float("$end_wall"),
    "duration_s": float("$end_wall") - float("$start_wall"),
    "return_code": int("$rc"),
    "repo_path": "$repo_path",
    "repo_source_bytes": int("$repo_bytes") if "$repo_bytes".strip().isdigit() else None,
    "trajectory_exists": trajectory.exists(),
    "trajectory_bytes": trajectory.stat().st_size if trajectory.exists() else 0,
    "step_metrics_path": "$RUN_ROOT/step_metrics/$inst.jsonl",
    "step_metrics_lines": int("$step_lines"),
}
Path("$final_dir/manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

  traj_status=MISSING
  [[ -f "$final_dir/trajectory.json" ]] && traj_status=OK
  echo "[$i/$total] DONE $inst rc=$rc traj=$traj_status steps=$step_lines repo_bytes=${repo_bytes:-NA} $(date -Is)"
done < "$WORKLIST"

"$PY" "$DOVERLAY/scripts/aggregate_moatless_profile50.py" \
  --run-root "$RUN_ROOT" \
  --repos-dir "$REPO_DIR" \
  --worklist "$WORKLIST"

echo "===== profile50 native mcts${MAX_ITERATIONS} done $(date -Is) ====="
