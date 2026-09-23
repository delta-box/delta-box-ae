#!/usr/bin/env bash
# Batch driver: replay all swe-search/mcts traces × {ext4, xfs, xfs_reflink}.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ACTIONS_DIR="$SCRIPT_DIR/swesearch_actions"
LOG="${SCRIPT_DIR}/run_log.txt"
: > "$LOG"

instances=$(ls "$ACTIONS_DIR" | sed 's/.json$//')
n=$(echo "$instances" | wc -l)
echo "running $n instances × 3 fs arms"

i=0
for inst in $instances; do
    i=$((i+1))
    for fs in ext4 xfs xfs_reflink; do
        printf "[%d/%d] %s / %s ... " "$i" "$n" "$inst" "$fs"
        if sudo bash "$SCRIPT_DIR/swesearch_replay.sh" "$inst" "$fs" >>"$LOG" 2>&1; then
            n_ok=$(wc -l < "/home/dong/d-overlayfs/benchresults/2026-05-11_swesearch_war/${inst}_${fs}.jsonl" 2>/dev/null || echo 0)
            echo "ok ($n_ok edits)"
        else
            echo "FAIL"
        fi
    done
done
echo ""
echo "all done; log: $LOG"
