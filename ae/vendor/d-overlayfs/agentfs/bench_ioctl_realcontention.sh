#!/bin/bash
# bench_ioctl_realcontention.sh
#
# Realistic concurrent-load ioctl bench:
#   - fast run: background fork+wait loop on a *separate* core
#   - slow run: background criu dump loop on a separate core (with python victim)
# Only the ioctl itself is timed. Each ioctl is invoked via the full
# subprocess wrap (taskset + ovl_ioctl exec + open(merged) + ioctl + close)
# to match the paper's measurement path.
#
# Usage:
#   sudo bash bench_ioctl_realcontention.sh [N_REPS] [BENCH_CPU] [LOAD_CPU]
# Default: N=200 BENCH_CPU=3 LOAD_CPU=4
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

N_REPS="${1:-200}"
BENCH_CPU="${2:-3}"
LOAD_CPU="${3:-4}"
WORK_BASE="/tmp/ovl_realcont_$$"
OUT_DIR="${SCRIPT_DIR}/bench_results"
mkdir -p "${OUT_DIR}"

# verify cpu freq is locked on bench cpu
MIN=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_min_freq)
MAX=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_max_freq)
GOV=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_governor)
INFO "bench cpu ${BENCH_CPU}: governor=${GOV} min=${MIN} max=${MAX}"
if [[ "${MIN}" != "${MAX}" ]]; then
    WARN "bench cpu freq NOT locked (min!=max); jitter will inflate"
fi

setup_xfs_image
setup_dirs
build_ioctl_tool

# build fork load if needed
FORK_LOAD="${SCRIPT_DIR}/bg_fork_load"
if [[ ! -x "${FORK_LOAD}" ]]; then
    gcc -O2 -Wall -o "${FORK_LOAD}" "${SCRIPT_DIR}/bg_fork_load.c"
fi

mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
mkdir -p "${LOWER2}" "${UPPER2}" "${WORK2}"
OPT_A="lowerdir=${LOWER1},upperdir=${UPPER1},workdir=${WORK1}"
OPT_B="lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}"

TS=$(date +%s)
FAST_OUT="${OUT_DIR}/realcont_fast_fork_n${N_REPS}_cpu${BENCH_CPU}_${TS}.jsonl"
SLOW_OUT="${OUT_DIR}/realcont_slow_criu_n${N_REPS}_cpu${BENCH_CPU}_${TS}.jsonl"
FORK_LOG="${OUT_DIR}/realcont_forkload_${TS}.log"
CRIU_LOG="${OUT_DIR}/realcont_criuload_${TS}.log"

run_bench() {
    local label="$1"
    local out_file="$2"
    local load_pid="$3"
    INFO "=== bench '${label}' n=${N_REPS} bench_cpu=${BENCH_CPU} load_pid=${load_pid} ==="
    python3 - <<PYEOF
import subprocess, time, json, os
N = ${N_REPS}
OPT_A = '${OPT_A}'
OPT_B = '${OPT_B}'
TOOL  = '${OVL_IOCTL_TOOL}'
MERGED = '${MERGED}'
BENCH_CPU = '${BENCH_CPU}'
LOAD_PID = ${load_pid}

# warmup: start with OPT_B (initial mount is OPT_A → A→A would be EBUSY)
for i in range(8):
    opt = OPT_B if i % 2 == 0 else OPT_A
    subprocess.run(['taskset','-c',BENCH_CPU,TOOL,MERGED,opt],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

samples = []
for i in range(N):
    # confirm load process is still alive
    try:
        os.kill(LOAD_PID, 0)
    except ProcessLookupError:
        print(f'[WARN] load pid {LOAD_PID} died at rep {i}')
        break
    opt = OPT_B if i % 2 == 0 else OPT_A
    t0 = time.monotonic_ns()
    r = subprocess.run(['taskset','-c',BENCH_CPU,TOOL,MERGED,opt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    t1 = time.monotonic_ns()
    samples.append({'rep': i, 'wall_ms': (t1-t0)/1e6,
                    'opt': 'A' if (i%2==1) else 'B',
                    'rc': r.returncode})
with open('${out_file}', 'w') as f:
    for s in samples:
        f.write(json.dumps(s) + '\n')
import statistics
ok = [s['wall_ms'] for s in samples if s['rc']==0]
print(f'  rc0 n={len(ok)}/{len(samples)} mean={statistics.mean(ok):.4f} median={statistics.median(ok):.4f} stdev={statistics.stdev(ok):.4f} min={min(ok):.4f} max={max(ok):.4f}')
PYEOF
}

# ============ FAST run: fork load on LOAD_CPU ============
INFO "starting bg_fork_load on cpu ${LOAD_CPU} for fast run..."
taskset -c "${LOAD_CPU}" "${FORK_LOAD}" 600 2>"${FORK_LOG}" &
FORK_PID=$!
sleep 0.5
run_bench "fast (fork load)" "${FAST_OUT}" "${FORK_PID}"
kill -TERM "${FORK_PID}" 2>/dev/null || true
wait "${FORK_PID}" 2>/dev/null || true
INFO "fork load done: $(cat "${FORK_LOG}" 2>/dev/null | tail -1)"

sleep 2

# ============ SLOW run: criu load on LOAD_CPU ============
INFO "starting bg_criu_load on cpu ${LOAD_CPU} for slow run..."
bash "${SCRIPT_DIR}/bg_criu_load.sh" 600 "${LOAD_CPU}" "${CRIU_LOG}" &
CRIU_DRIVER_PID=$!
sleep 1.5  # let criu driver spawn victim + start dumping
# find the criu driver's victim/dumper PIDs to wait for
run_bench "slow (criu load)" "${SLOW_OUT}" "${CRIU_DRIVER_PID}"
kill -TERM "${CRIU_DRIVER_PID}" 2>/dev/null || true
sleep 1
pkill -f "bg_criu_load.sh" 2>/dev/null || true
pkill -f "criu dump --tree" 2>/dev/null || true
pkill -f "victim.py" 2>/dev/null || true
INFO "criu load done: $(cat "${CRIU_LOG}" 2>/dev/null | tail -1)"

# ============ stats ============
INFO "=== summary ==="
python3 - <<PYEOF
import json, math, statistics
def load(f):
    return [json.loads(l)['wall_ms'] for l in open(f) if json.loads(l).get('rc',1)==0]
fv = load('${FAST_OUT}'); sv = load('${SLOW_OUT}')
def s(v): return dict(n=len(v), mean=statistics.mean(v), median=statistics.median(v),
                      stdev=statistics.stdev(v), p99=sorted(v)[int(0.99*len(v))-1],
                      mn=min(v), mx=max(v))
sf, ss = s(fv), s(sv)
print(f"FAST (fork load):  n={sf['n']} mean={sf['mean']:.4f} median={sf['median']:.4f} stdev={sf['stdev']:.4f} p99={sf['p99']:.4f} min={sf['mn']:.4f} max={sf['mx']:.4f}")
print(f"SLOW (criu load):  n={ss['n']} mean={ss['mean']:.4f} median={ss['median']:.4f} stdev={ss['stdev']:.4f} p99={ss['p99']:.4f} min={ss['mn']:.4f} max={ss['mx']:.4f}")
mf, ms = sf['mean'], ss['mean']
vf, vs = sf['stdev']**2, ss['stdev']**2
t = (mf - ms) / math.sqrt(vf/sf['n'] + vs/ss['n'])
diff_pct = (mf-ms)/((mf+ms)/2)*100
print(f"mean diff = {mf-ms:+.4f} ms ({diff_pct:+.2f}%)")
print(f"t = {t:.2f}")
print(f"CV fast={sf['stdev']/sf['mean']*100:.2f}%  CV slow={ss['stdev']/ss['mean']*100:.2f}%")
PYEOF

INFO "fast: ${FAST_OUT}"
INFO "slow: ${SLOW_OUT}"
cleanup_all
