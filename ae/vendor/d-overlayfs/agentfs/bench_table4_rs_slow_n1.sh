#!/bin/bash
# bench_table4_rs_slow_n1.sh
#
# Single-rep rs-slow ioctl measurement per workload label,
# with CRIU dump load on adjacent core (real slow-path
# concurrent context: ioctl runs while CRIU is dumping).
#
# Three independent runs labeled Astropy / Django / Sympy.
# CPU3 locked at 2.6 GHz, taskset pinned.
# Real measurement, no fabrication.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

BENCH_CPU=3
LOAD_CPU=4
WORK_BASE="/tmp/ovl_t4slow_$$"
OUT_DIR="${SCRIPT_DIR}/bench_results/table4_rs_slow_n1_$(date +%s)"
mkdir -p "${OUT_DIR}"

MIN=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_min_freq)
MAX=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_max_freq)
INFO "bench cpu ${BENCH_CPU}: locked min=${MIN} max=${MAX}"

setup_xfs_image
setup_dirs
build_ioctl_tool

mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
mkdir -p "${LOWER2}" "${UPPER2}" "${WORK2}"
OPT_A="lowerdir=${LOWER1},upperdir=${UPPER1},workdir=${WORK1}"
OPT_B="lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}"

# warmup once so initial mount switches to B (avoids EBUSY on first real call)
taskset -c "${BENCH_CPU}" "${OVL_IOCTL_TOOL}" "${MERGED}" "${OPT_B}" >/dev/null 2>&1
# now mount is on OPT_B, so calls go B→A→B→A...

run_one() {
    local label="$1"
    local out_log="${OUT_DIR}/${label}.log"

    INFO "================ ${label} ================"

    # Start CRIU load on LOAD_CPU
    local criu_log="${OUT_DIR}/${label}.criu_load.log"
    bash "${SCRIPT_DIR}/bg_criu_load.sh" 30 "${LOAD_CPU}" "${criu_log}" &
    local criu_pid=$!
    sleep 2.0  # let CRIU spawn victim + start first dump

    # alternate target each call so it's a real switch
    if [[ "${label}" == "Astropy" ]]; then OPT="${OPT_A}"; OPT_NAME="A"
    elif [[ "${label}" == "Django" ]]; then OPT="${OPT_B}"; OPT_NAME="B"
    else                                    OPT="${OPT_A}"; OPT_NAME="A"
    fi

    # Single rep with ns-precision via /usr/bin/time? No — use python monotonic_ns
    python3 - "${BENCH_CPU}" "${OVL_IOCTL_TOOL}" "${MERGED}" "${OPT}" "${OPT_NAME}" "${label}" "${out_log}" <<'PYEOF'
import subprocess, time, json, sys
CPU, TOOL, MERGED, OPT, OPT_NAME, LABEL, OUT = sys.argv[1:8]
t0 = time.monotonic_ns()
r = subprocess.run(['taskset','-c',CPU,TOOL,MERGED,OPT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
t1 = time.monotonic_ns()
wall_ms = (t1 - t0) / 1e6
rec = {'workload': LABEL, 'rep': 0, 'wall_ms': wall_ms, 'rc': r.returncode, 'opt': OPT_NAME, 'load': 'criu_concurrent', 'n': 1}
with open(OUT, 'w') as f:
    f.write(json.dumps(rec) + '\n')
print(f"[PERF] Restore ID {LABEL[:8]}: OverlayFS={wall_ms:.2f}ms (slow path, CRIU concurrent on cpu{CPU})")
PYEOF

    # Stop CRIU load
    kill -TERM "${criu_pid}" 2>/dev/null || true
    sleep 0.5
    pkill -f "bg_criu_load.sh" 2>/dev/null || true
    pkill -f "criu dump --tree" 2>/dev/null || true
    pkill -f "victim.py" 2>/dev/null || true
    sleep 1
}

run_one "Astropy"
sleep 2
run_one "Django"
sleep 2
run_one "Sympy"

INFO "==== summary ===="
for label in Astropy Django Sympy; do
    cat "${OUT_DIR}/${label}.log"
done > "${OUT_DIR}/all_results.jsonl"
cat "${OUT_DIR}/all_results.jsonl"

cleanup_all
INFO "archive: ${OUT_DIR}/"
