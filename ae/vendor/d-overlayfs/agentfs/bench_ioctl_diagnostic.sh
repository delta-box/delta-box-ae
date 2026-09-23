#!/bin/bash
# bench_ioctl_diagnostic.sh
#
# Detailed instrumentation of ioctl bench under fast (fork) vs slow (criu)
# background load. Captures:
#   - /proc/interrupts CPU3 column (per-IRQ delta)
#   - /proc/softirqs   CPU3 column (per-softirq delta)
#   - /proc/stat       cpu3 row    (jiffy breakdown: user/sys/irq/softirq/iowait)
#   - perf stat        on the ioctl bench python (context-switches, cpu-migrations,
#                                                 page-faults, instructions, cycles,
#                                                 cache-misses)
#   - /proc/diskstats deltas
#
# Goal: identify which kernel resource contention systematically widens
# the slow-path ioctl latency vs fast.
#
# Usage:  sudo bash bench_ioctl_diagnostic.sh [N] [BENCH_CPU] [LOAD_CPU]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

N_REPS="${1:-200}"
BENCH_CPU="${2:-3}"
LOAD_CPU="${3:-4}"
WORK_BASE="/tmp/ovl_diag_$$"
OUT_DIR="${SCRIPT_DIR}/bench_results/diagnostic_$(date +%s)"
mkdir -p "${OUT_DIR}"

PERF=/usr/lib/linux-hwe-6.8-tools-6.8.0-111/perf

# --- snapshot helpers ----------------------------------------------------
snap_interrupts() { awk 'NR>1' /proc/interrupts > "$1"; }
snap_softirqs()   { awk 'NR>1' /proc/softirqs   > "$1"; }
snap_stat_cpu3()  { grep -E "^cpu3 " /proc/stat > "$1"; }
snap_disk()       { cp /proc/diskstats "$1"; }
snap_meminfo()    { cp /proc/meminfo "$1"; }

diff_cpu_col() {
    # diff a given CPU column out of /proc/interrupts-style files
    local before="$1" after="$2" cpu_idx="$3" outfile="$4"
    paste "${before}" "${after}" | awk -v c="${cpu_idx}" '
    {
        # before half has (NF/2) fields, same for after
        half = NF/2
        # row name = $1 (e.g. "8:" or "NMI:")
        name = $1
        # the CPU column index in each half is c+1 (because col 1 is name)
        b = $((c+1)+0)
        a = $((half + c + 1)+0)
        # last column(s) are the device description — print only if delta != 0 or for known IRQ types
        delta = a - b
        # print name b a delta + tail (description columns from after side)
        desc = ""
        for (i = half + 21 + 1; i <= NF; i++) desc = desc " " $i  # 20 CPU cols
        printf "%-12s before=%-12d after=%-12d delta=%-10d %s\n", name, b, a, delta, desc
    }' > "${outfile}"
}

# CPU3 = 0-indexed column 3 → in interrupts file, name is col 1, CPU0=2, CPU3=5
# /proc/interrupts row: "  8:    n0  n1  n2  n3  ..."  (after stripping header)
CPU_COL_IDX=$((BENCH_CPU + 1))   # 1=name, 2=CPU0, so CPU3=5

# --- setup ---------------------------------------------------------------
MIN=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_min_freq)
MAX=$(cat /sys/devices/system/cpu/cpu${BENCH_CPU}/cpufreq/scaling_max_freq)
INFO "CPU${BENCH_CPU} locked: min=${MIN} max=${MAX}"
[[ "${MIN}" == "${MAX}" ]] || { WARN "cpu freq NOT locked!"; }

setup_xfs_image
setup_dirs
build_ioctl_tool

FORK_LOAD="${SCRIPT_DIR}/bg_fork_load"
[[ -x "${FORK_LOAD}" ]] || gcc -O2 -Wall -o "${FORK_LOAD}" "${SCRIPT_DIR}/bg_fork_load.c"

mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
mkdir -p "${LOWER2}" "${UPPER2}" "${WORK2}"
OPT_A="lowerdir=${LOWER1},upperdir=${UPPER1},workdir=${WORK1}"
OPT_B="lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}"

# --- single diag run -----------------------------------------------------
run_diag() {
    local label="$1"
    local load_kind="$2"  # "fork" or "criu"
    local out_dir="${OUT_DIR}/${label}"
    mkdir -p "${out_dir}"
    INFO "================ ${label} (load=${load_kind}) ================"

    # Start background load
    local load_pid=""
    if [[ "${load_kind}" == "fork" ]]; then
        taskset -c "${LOAD_CPU}" "${FORK_LOAD}" 300 2>"${out_dir}/load.log" &
        load_pid=$!
    elif [[ "${load_kind}" == "criu" ]]; then
        bash "${SCRIPT_DIR}/bg_criu_load.sh" 300 "${LOAD_CPU}" "${out_dir}/load.log" &
        load_pid=$!
    fi
    sleep 1.5

    # PRE snapshots
    snap_interrupts "${out_dir}/int_before.txt"
    snap_softirqs   "${out_dir}/sirq_before.txt"
    snap_stat_cpu3  "${out_dir}/stat_before.txt"
    snap_disk       "${out_dir}/disk_before.txt"
    snap_meminfo    "${out_dir}/mem_before.txt"

    # warmup (8 reps, untimed, OPT_B first so first call is real switch)
    for i in 1 2 3 4 5 6 7 8; do
        local opt=$([[ $((i%2)) -eq 0 ]] && echo "${OPT_A}" || echo "${OPT_B}")
        taskset -c "${BENCH_CPU}" "${OVL_IOCTL_TOOL}" "${MERGED}" "${opt}" >/dev/null 2>&1 || true
    done

    # Measurement with perf wrapping the python wrapper that does N subprocess calls
    local jsonl="${out_dir}/ioctl_samples.jsonl"
    local perflog="${out_dir}/perf.log"

    INFO "[${label}] running ${N_REPS} reps on CPU${BENCH_CPU}, load on CPU${LOAD_CPU}"
    taskset -c "${BENCH_CPU}" "${PERF}" stat \
        -e task-clock,context-switches,cpu-migrations,page-faults \
        -e cycles,instructions,cache-references,cache-misses \
        -e L1-dcache-loads,L1-dcache-load-misses \
        -e dTLB-loads,dTLB-load-misses \
        -o "${perflog}" \
        -- python3 - "${jsonl}" "${OVL_IOCTL_TOOL}" "${MERGED}" "${OPT_A}" "${OPT_B}" "${BENCH_CPU}" "${N_REPS}" <<'PYEOF'
import subprocess, time, json, sys
out_jsonl, TOOL, MERGED, OPT_A, OPT_B, CPU, N = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], int(sys.argv[7])
samples = []
for i in range(N):
    opt = OPT_B if i % 2 == 0 else OPT_A
    t0 = time.monotonic_ns()
    r = subprocess.run(['taskset','-c',CPU,TOOL,MERGED,opt],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    t1 = time.monotonic_ns()
    samples.append({'rep': i, 'wall_ms': (t1-t0)/1e6, 'rc': r.returncode})
with open(out_jsonl, 'w') as f:
    for s in samples: f.write(json.dumps(s) + '\n')
PYEOF

    # POST snapshots
    snap_interrupts "${out_dir}/int_after.txt"
    snap_softirqs   "${out_dir}/sirq_after.txt"
    snap_stat_cpu3  "${out_dir}/stat_after.txt"
    snap_disk       "${out_dir}/disk_after.txt"
    snap_meminfo    "${out_dir}/mem_after.txt"

    # Stop load
    if [[ -n "${load_pid}" ]]; then
        kill -TERM "${load_pid}" 2>/dev/null || true
        sleep 0.5
    fi
    pkill -f "bg_criu_load.sh" 2>/dev/null || true
    pkill -f "criu dump --tree" 2>/dev/null || true
    pkill -f "victim.py" 2>/dev/null || true
    pkill -x "bg_fork_load" 2>/dev/null || true

    # ---- compute diffs ----
    diff_cpu_col "${out_dir}/int_before.txt"  "${out_dir}/int_after.txt"  "${CPU_COL_IDX}" "${out_dir}/int_delta_cpu${BENCH_CPU}.txt"
    diff_cpu_col "${out_dir}/sirq_before.txt" "${out_dir}/sirq_after.txt" "${CPU_COL_IDX}" "${out_dir}/sirq_delta_cpu${BENCH_CPU}.txt"

    # stat cpu3 delta
    python3 - "${out_dir}/stat_before.txt" "${out_dir}/stat_after.txt" > "${out_dir}/stat_delta.txt" <<'PYEOF2'
import sys
b = open(sys.argv[1]).read().split()
a = open(sys.argv[2]).read().split()
# /proc/stat cpu line: name user nice sys idle iowait irq softirq steal guest guest_nice
names = ['user','nice','sys','idle','iowait','irq','softirq','steal','guest','guest_nice']
print(f"{'field':<12} {'before':>12} {'after':>12} {'delta(jiffies)':>16}")
for i, n in enumerate(names):
    bv = int(b[i+1])
    av = int(a[i+1])
    print(f"{n:<12} {bv:>12} {av:>12} {av-bv:>16}")
PYEOF2

    # ioctl sample summary
    python3 - "${jsonl}" > "${out_dir}/ioctl_summary.txt" <<'PYEOF3'
import sys, json, statistics
v = [json.loads(l)['wall_ms'] for l in open(sys.argv[1]) if json.loads(l).get('rc',1)==0]
print(f"n={len(v)} mean={statistics.mean(v):.4f}ms median={statistics.median(v):.4f} stdev={statistics.stdev(v):.4f} p50={statistics.median(v):.4f} p90={sorted(v)[int(0.9*len(v))-1]:.4f} p99={sorted(v)[int(0.99*len(v))-1]:.4f} min={min(v):.4f} max={max(v):.4f}")
PYEOF3
    INFO "[${label}] done. results: ${out_dir}/"
    cat "${out_dir}/ioctl_summary.txt"
}

run_diag "fast" "fork"
sleep 3
run_diag "slow" "criu"

# ---- side-by-side report ----
REPORT="${OUT_DIR}/REPORT.md"
{
echo "# Diagnostic bench: fast (fork) vs slow (criu) — CPU${BENCH_CPU}"
echo "Generated: $(date)"
echo "N=${N_REPS}  BENCH_CPU=${BENCH_CPU}  LOAD_CPU=${LOAD_CPU}  freq=${MIN} Hz"
echo
echo "## ioctl wall-time summary"
echo '```'
echo "fast: $(cat ${OUT_DIR}/fast/ioctl_summary.txt)"
echo "slow: $(cat ${OUT_DIR}/slow/ioctl_summary.txt)"
echo '```'
echo
echo "## perf stat (CPU${BENCH_CPU})"
echo "### fast"
echo '```'
cat "${OUT_DIR}/fast/perf.log"
echo '```'
echo "### slow"
echo '```'
cat "${OUT_DIR}/slow/perf.log"
echo '```'
echo
echo "## /proc/stat cpu${BENCH_CPU} jiffy delta"
echo "### fast"
echo '```'
cat "${OUT_DIR}/fast/stat_delta.txt"
echo '```'
echo "### slow"
echo '```'
cat "${OUT_DIR}/slow/stat_delta.txt"
echo '```'
echo
echo "## /proc/interrupts CPU${BENCH_CPU} delta (non-zero rows)"
echo "### fast"
echo '```'
awk '/delta=/ { match($0, /delta=([-]?[0-9]+)/, m); if (m[1]+0 != 0) print }' "${OUT_DIR}/fast/int_delta_cpu${BENCH_CPU}.txt" | head -40
echo '```'
echo "### slow"
echo '```'
awk '/delta=/ { match($0, /delta=([-]?[0-9]+)/, m); if (m[1]+0 != 0) print }' "${OUT_DIR}/slow/int_delta_cpu${BENCH_CPU}.txt" | head -40
echo '```'
echo
echo "## /proc/softirqs CPU${BENCH_CPU} delta"
echo "### fast"
echo '```'
cat "${OUT_DIR}/fast/sirq_delta_cpu${BENCH_CPU}.txt"
echo '```'
echo "### slow"
echo '```'
cat "${OUT_DIR}/slow/sirq_delta_cpu${BENCH_CPU}.txt"
echo '```'
} > "${REPORT}"

INFO "REPORT: ${REPORT}"
cleanup_all
