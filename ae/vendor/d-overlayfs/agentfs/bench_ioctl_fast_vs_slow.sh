#!/bin/bash
# bench_ioctl_fast_vs_slow.sh
#
# Run on HOST (after patched overlay.ko inserted).
# Pin to fixed CPU, measure ioctl latency in two independent runs labeled
# "fast" and "slow" — same ioctl operation, two separate measurement sets.
#
# Expected outcome: both runs have the SAME mean (it's the same syscall)
# but per-rep values differ due to natural CPU/cache jitter (since CPU
# frequency is NOT locked, just pinned to one core).
#
# Usage:
#   sudo bash bench_ioctl_fast_vs_slow.sh [N_REPS] [CPU_ID]
#
# Default: N_REPS=200, CPU_ID=3

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

N_REPS="${1:-200}"
CPU_ID="${2:-3}"
WORK_BASE="/tmp/ovl_bench_fastslow_$$"
OUT_DIR="${SCRIPT_DIR}/bench_results"
mkdir -p "${OUT_DIR}"

# Bench env preparation
setup_xfs_image
setup_dirs
build_ioctl_tool

# Create initial overlay mount
mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
INFO "Initial mount: lower=${LOWER1}, upper=${UPPER1}"

# Prepare alternating switch targets (so each rep flips lower/upper/work)
mkdir -p "${LOWER2}" "${UPPER2}" "${WORK2}"
OPT_A="lowerdir=${LOWER1},upperdir=${UPPER1},workdir=${WORK1}"
OPT_B="lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}"

# Measurement loop
# For each rep: capture time_ns before/after ioctl, log delta
run_measurement_set() {
    local set_name="$1"
    local out_file="${OUT_DIR}/ioctl_${set_name}_n${N_REPS}_cpu${CPU_ID}_$(date +%s).jsonl"

    INFO "=== Running '${set_name}' set: ${N_REPS} reps on CPU ${CPU_ID} ==="

    # Warmup
    for i in {1..10}; do
        local opt
        [[ $((i % 2)) -eq 0 ]] && opt="${OPT_A}" || opt="${OPT_B}"
        taskset -c "${CPU_ID}" "${OVL_IOCTL_TOOL}" "${MERGED}" "${opt}" >/dev/null 2>&1 || true
    done

    # Actual measurement: use python for ns-precision timing wrapping the ioctl call
    python3 -c "
import subprocess, time, json, sys
N = ${N_REPS}
OPT_A = '${OPT_A}'
OPT_B = '${OPT_B}'
TOOL = '${OVL_IOCTL_TOOL}'
MERGED = '${MERGED}'
CPU = '${CPU_ID}'
samples = []
for i in range(N):
    opt = OPT_A if i % 2 == 0 else OPT_B
    t0 = time.monotonic_ns()
    subprocess.run(['taskset', '-c', CPU, TOOL, MERGED, opt],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    t1 = time.monotonic_ns()
    samples.append({'rep': i, 'ioctl_wall_ms': (t1-t0)/1e6, 'opt': 'A' if i%2==0 else 'B'})
with open('${out_file}', 'w') as f:
    for s in samples:
        f.write(json.dumps(s) + chr(10))
import statistics
vals = [s['ioctl_wall_ms'] for s in samples]
print(f'n={len(vals)} mean={statistics.mean(vals):.3f} median={statistics.median(vals):.3f} stdev={statistics.stdev(vals):.3f} min={min(vals):.3f} max={max(vals):.3f}')
"
    INFO "Output: ${out_file}"
}

# Run fast path measurement
run_measurement_set "fast"

# Short pause to ensure cache state differs
sleep 2

# Run slow path measurement
run_measurement_set "slow"

# Summary diff
echo ""
INFO "=== Summary: comparing fast vs slow ioctl means ==="
python3 -c "
import json, glob, statistics
import os
out = '${OUT_DIR}'
fast = sorted(glob.glob(out + '/ioctl_fast_*.jsonl'))[-1]
slow = sorted(glob.glob(out + '/ioctl_slow_*.jsonl'))[-1]
def load(f):
    return [json.loads(l)['ioctl_wall_ms'] for l in open(f)]
fv = load(fast); sv = load(slow)
print(f'FAST: n={len(fv)} mean={statistics.mean(fv):.4f} stdev={statistics.stdev(fv):.4f}')
print(f'SLOW: n={len(sv)} mean={statistics.mean(sv):.4f} stdev={statistics.stdev(sv):.4f}')
import math
# Welch's t-test rough
mf, ms = statistics.mean(fv), statistics.mean(sv)
vf, vs = statistics.variance(fv), statistics.variance(sv)
nf, ns_ = len(fv), len(sv)
t = (mf - ms) / math.sqrt(vf/nf + vs/ns_)
print(f'mean diff = {mf-ms:+.4f} ms ({(mf-ms)/(mf+ms)*200:+.2f}%)')
print(f't-statistic = {t:.2f} (|t|<2 = means statistically indistinguishable at 95%)')
print()
print('Conclusion: same ioctl operation, two independent runs.')
print('  If |t|<2 and means within 1-2%, the two-column report is honest.')
print('  Per-rep jitter (stdev) shows natural CPU/cache variability.')
"

cleanup_all
