#!/bin/bash
# bench_ioctl_inproc.sh
#
# In-process C bench (no Python subprocess wrap, no per-call exec).
# Calls OVL_IOCTL_CHECKPOINT N times inside one process, measures
# ns-precision latency with CLOCK_MONOTONIC around just the ioctl().
#
# Runs the loop TWICE (label "fast" and "slow") - same ioctl, two
# independent measurements. Expectation: means within ~1% of each
# other (it IS the same kernel path); per-rep stdev shows residual
# cache/scheduler jitter even with locked CPU freq.
#
# Usage: sudo bash bench_ioctl_inproc.sh [N_REPS] [CPU_ID]
# Default N=500, CPU=3.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

N_REPS="${1:-500}"
CPU_ID="${2:-3}"
WORK_BASE="/tmp/ovl_inproc_$$"
OUT_DIR="${SCRIPT_DIR}/bench_results"
mkdir -p "${OUT_DIR}"

setup_xfs_image
setup_dirs

# build the in-process C bench (separate from per-call ovl_ioctl)
INPROC_TOOL="${WORK_BASE}/ovl_ioctl_bench"
gcc -O2 -Wall -o "${INPROC_TOOL}" "${SCRIPT_DIR}/ovl_ioctl_bench.c"
INFO "Built in-process tool: ${INPROC_TOOL}"

mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
INFO "Initial mount: lower=${LOWER1}, upper=${UPPER1}"

mkdir -p "${LOWER2}" "${UPPER2}" "${WORK2}"
OPT_A="lowerdir=${LOWER1},upperdir=${UPPER1},workdir=${WORK1}"
OPT_B="lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}"

TS=$(date +%s)
FAST_OUT="${OUT_DIR}/inproc_fast_n${N_REPS}_cpu${CPU_ID}_${TS}.jsonl"
SLOW_OUT="${OUT_DIR}/inproc_slow_n${N_REPS}_cpu${CPU_ID}_${TS}.jsonl"

# NOTE: mount currently uses OPT_A, so warmup[0]=A would be EBUSY (same-target).
# Pass OPT_B first so the first switch goes A→B.
INFO "=== run 'fast' (pinned cpu ${CPU_ID}, n=${N_REPS}) ==="
taskset -c "${CPU_ID}" "${INPROC_TOOL}" "${MERGED}" "${OPT_B}" "${OPT_A}" "${N_REPS}" "${FAST_OUT}"
sleep 2
INFO "=== run 'slow' (pinned cpu ${CPU_ID}, n=${N_REPS}) ==="
taskset -c "${CPU_ID}" "${INPROC_TOOL}" "${MERGED}" "${OPT_B}" "${OPT_A}" "${N_REPS}" "${SLOW_OUT}"

INFO "fast output: ${FAST_OUT}"
INFO "slow output: ${SLOW_OUT}"

python3 - <<PYEOF
import json, math, statistics
fast = '${FAST_OUT}'
slow = '${SLOW_OUT}'

def load(f):
    return [json.loads(l)['ioctl_ns']/1e6 for l in open(f)
            if json.loads(l).get('ioctl_ns', -1) > 0]

fv = load(fast); sv = load(slow)
def stats(v, name):
    return dict(n=len(v),
                mean=statistics.mean(v),
                median=statistics.median(v),
                stdev=statistics.stdev(v),
                p99=sorted(v)[int(0.99*len(v))-1],
                min=min(v), max=max(v))

sf = stats(fv, 'FAST')
ss = stats(sv, 'SLOW')

print(f"FAST: n={sf['n']} mean={sf['mean']:.4f}ms median={sf['median']:.4f} stdev={sf['stdev']:.4f} p99={sf['p99']:.4f} min={sf['min']:.4f} max={sf['max']:.4f}")
print(f"SLOW: n={ss['n']} mean={ss['mean']:.4f}ms median={ss['median']:.4f} stdev={ss['stdev']:.4f} p99={ss['p99']:.4f} min={ss['min']:.4f} max={ss['max']:.4f}")

mf, ms = sf['mean'], ss['mean']
vf, vs = sf['stdev']**2, ss['stdev']**2
nf, ns_ = sf['n'], ss['n']
t = (mf - ms) / math.sqrt(vf/nf + vs/ns_)
diff_pct = (mf - ms) / ((mf + ms)/2) * 100
print(f"mean diff = {mf-ms:+.4f} ms ({diff_pct:+.2f}%)")
print(f"t = {t:.2f} (|t|<2 means statistically indistinguishable @ 95%)")
print(f"CV(fast) = {sf['stdev']/sf['mean']*100:.2f}%  CV(slow) = {ss['stdev']/ss['mean']*100:.2f}%")
PYEOF

cleanup_all
