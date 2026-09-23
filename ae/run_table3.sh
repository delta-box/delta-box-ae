#!/usr/bin/env bash
# Full 12-input Table 3, RAM-backed disks, NUMA 2 and maximum P-state by default.
# AE_CONFIG selects an explicit alternative profile; defaults to async incremental.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec bash "$repo/ae/run_all.sh" --group table-03 \
    --config "${AE_CONFIG:-$repo/ae/configs/spr4numa-table3.json}" "$@"
