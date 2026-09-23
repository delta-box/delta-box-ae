#!/usr/bin/env bash
set -euo pipefail

ARM="$1"
VM_INDEX="$2"
EXTRA_FLAGS="$3"

EXP=/mnt/disk2/dyp/d-overlayfs/experiments/memcurve_forkonly_write_sympy22840_20260611
INSTANCE=sympy__sympy-22840
SCHED=/mnt/disk2/dyp/finalbench/deltabox_peagle_mcts30_2x_numa12_realrtt/schedules/sympy__sympy-22840_p_eagle_mcts30_allstd_realrtt.jsonl
OUT="$EXP/results_$ARM"
LOG="$EXP/run_$ARM.log"
ENVF="$EXP/env_$ARM.txt"

mkdir -p "$EXP/tmp" "$EXP/rootfs_work" "$OUT"

{
  echo "arm=$ARM"
  echo "started_at=$(date -Is)"
  echo "cwd=$(pwd)"
  echo "git_rev=$(git rev-parse HEAD)"
  echo "instance=$INSTANCE"
  echo "schedule=$SCHED"
  echo "host_cpus=48-55"
  echo "numa=2"
  echo "TMPDIR=$EXP/tmp"
  echo "DELTABOX_RUN_ROOTFS_DIR=$EXP/rootfs_work"
  echo "DELTABOX_MEMCURVE=1"
  echo "DELTABOX_FORK_ONLY_MEMCURVE=1"
  echo "DELTABOX_RESTAMP_PARENT_INVENTORY=0"
  echo "DELTABOX_EXTRA_GUEST_FLAGS=$EXTRA_FLAGS"
  case "$ARM" in
    skip*) echo "DELTABOX_MEMCURVE_SKIP=1" ;;
    gc*) echo "DELTABOX_MEMCURVE_GC=1"; echo "DELTABOX_GC_KILL_TEMPLATES=1" ;;
  esac
} > "$ENVF"

env_args=(
  TMPDIR="$EXP/tmp"
  DELTABOX_RUN_ROOTFS_DIR="$EXP/rootfs_work"
  DELTABOX_MEMCURVE=1
  DELTABOX_FORK_ONLY_MEMCURVE=1
  DELTABOX_RESTAMP_PARENT_INVENTORY=0
)
if [[ -n "$EXTRA_FLAGS" ]]; then
  env_args+=(DELTABOX_EXTRA_GUEST_FLAGS="$EXTRA_FLAGS")
fi
case "$ARM" in
  skip*) env_args+=(DELTABOX_MEMCURVE_SKIP=1) ;;
  gc*) env_args+=(DELTABOX_MEMCURVE_GC=1 DELTABOX_GC_KILL_TEMPLATES=1) ;;
esac

sudo env "${env_args[@]}" \
  numactl --cpunodebind=2 --membind=2 \
  python3 benchmarks/trace_replay/runner_vm.py \
    --instance_id "$INSTANCE" \
    --schedule "$SCHED" \
    --backend deltabox-fixed --vcpus 4 --mem-mib 8192 \
    --vm-index "$VM_INDEX" --host-cpus 48-55 --timeout 1800 \
    --out-dir "$OUT" 2>&1 | tee "$LOG"
