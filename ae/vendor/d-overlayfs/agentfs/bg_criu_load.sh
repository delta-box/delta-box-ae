#!/bin/bash
# bg_criu_load.sh — Background CRIU dump load generator
#
# Spawns a victim Python process (with realistic RSS / file descriptors /
# threads — mimics an agent-side workload) and repeatedly criu-dumps it.
# Simulates the slow-path "CRIU restore in progress on another core"
# competition during ioctl measurement.
#
# Usage:
#   bg_criu_load.sh <duration_sec> <cpu_pin> [out_log]
#
# On exit: kills victim, removes dump dirs, reports total dumps to stderr.
set -euo pipefail

DUR="${1:-60}"
CPU_PIN="${2:-4}"
OUT_LOG="${3:-/tmp/bg_criu_load_$$.log}"
WORK="/tmp/bg_criu_$$"
mkdir -p "${WORK}"

VICTIM_PY="${WORK}/victim.py"
cat > "${VICTIM_PY}" <<'PYEOF'
# Realistic agent-side victim: ~50 MB RSS, some file descriptors, a sleep loop
import os, time, mmap
# anon page allocation ~50 MB
chunks = [bytearray(1024*1024) for _ in range(50)]
# touch pages so they're resident
for c in chunks:
    for i in range(0, len(c), 4096):
        c[i] = 1
# keep some FDs open
fds = [open('/dev/null', 'r') for _ in range(8)]
# main loop
while True:
    time.sleep(0.1)
PYEOF

cleanup() {
    if [[ -n "${VICTIM_PID:-}" ]] && kill -0 "${VICTIM_PID}" 2>/dev/null; then
        kill -9 "${VICTIM_PID}" 2>/dev/null || true
    fi
    rm -rf "${WORK}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Launch victim pinned to a different core (NOT the ioctl bench core)
taskset -c "${CPU_PIN}" python3 "${VICTIM_PY}" &
VICTIM_PID=$!
sleep 0.5
if ! kill -0 "${VICTIM_PID}" 2>/dev/null; then
    echo "ERROR: victim failed to start" >&2
    exit 1
fi

echo "[bg_criu_load] victim pid=${VICTIM_PID} cpu=${CPU_PIN} dur=${DUR}s" > "${OUT_LOG}"

START=$(date +%s)
COUNT=0
DUMP_DIR="${WORK}/dump"
while true; do
    NOW=$(date +%s)
    [[ $((NOW - START)) -ge "${DUR}" ]] && break
    rm -rf "${DUMP_DIR}"
    mkdir -p "${DUMP_DIR}"
    # --leave-running: keep victim alive
    # --shell-job: allow non-controlling-tty processes
    # pin criu to same load core
    if taskset -c "${CPU_PIN}" criu dump \
            --tree "${VICTIM_PID}" \
            -D "${DUMP_DIR}" \
            --leave-running \
            --shell-job \
            >> "${OUT_LOG}" 2>&1; then
        COUNT=$((COUNT + 1))
    else
        echo "[bg_criu_load] dump failed at count=${COUNT}" >> "${OUT_LOG}"
        sleep 0.2
    fi
done

echo "[bg_criu_load] completed ${COUNT} dumps in ${DUR}s" >&2
echo "[bg_criu_load] completed ${COUNT} dumps in ${DUR}s" >> "${OUT_LOG}"
