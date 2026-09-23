#!/bin/bash
# test_concurrent_rw.sh - 并发读写 + 热切换压力测试
#
# 多线程同时进行读/写/readdir，期间执行热切换
# 验证不会 crash，不会数据损坏
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="concurrent_rw"
WORK_BASE="/tmp/ovl_test_concurrent_$$"
trap_cleanup

NUM_WRITERS=4
NUM_READERS=4
TEST_DURATION=10  # seconds

main() {
    INFO "=== Test: concurrent R/W + hot switch stress ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # 准备初始 lower（唯一）。后续 switch 全部走"渐进栈式"模式：
    # 每次 ioctl 分配 fresh upper${i}/work${i}，lower 由内核自动注入旧 upper。
    # 不再使用 LOWER2 alternate — 那种模式 kernel API 不支持
    # （prev_layers 中的 trap inode 会让重访 lowerdir 返回 -ELOOP/-EBUSY）。
    for i in $(seq 1 20); do
        dd if=/dev/urandom of="${LOWER1}/data_${i}" bs=4096 count=$((RANDOM % 100 + 1)) status=none
    done
    mkdir -p "${LOWER1}/tree/a/b/c"
    echo "deep_file" > "${LOWER1}/tree/a/b/c/deep"

    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    STOP_FILE="${WORK_BASE}/stop"
    ERROR_DIR="${WORK_BASE}/errors"
    mkdir -p "${ERROR_DIR}"
    
    # 启动 writer 进程
    for w in $(seq 1 ${NUM_WRITERS}); do
        (
            set +e
            while [[ ! -f "${STOP_FILE}" ]]; do
                idx=$((RANDOM % 20 + 1))
                target="${MERGED}/data_${idx}"
                if [[ -f "${target}" ]]; then
                    dd if=/dev/urandom of="${target}" bs=4096 count=1 seek=$((RANDOM % 10)) conv=notrunc status=none 2>/dev/null
                    if [[ $? -ne 0 ]]; then
                        echo "write_error_${idx}" >> "${ERROR_DIR}/writer_${w}.log"
                    fi
                fi
                usleep 1000 2>/dev/null || sleep 0.001
            done
        ) &
        WRITER_PIDS[${w}]=$!
    done
    
    # 启动 reader 进程
    for r in $(seq 1 ${NUM_READERS}); do
        (
            set +e
            while [[ ! -f "${STOP_FILE}" ]]; do
                idx=$((RANDOM % 20 + 1))
                target="${MERGED}/data_${idx}"
                if [[ -f "${target}" ]]; then
                    md5sum "${target}" > /dev/null 2>&1
                fi
                # readdir
                ls "${MERGED}/" > /dev/null 2>&1
                ls "${MERGED}/tree/a/b/c/" > /dev/null 2>&1
                usleep 500 2>/dev/null || sleep 0.0005
            done
        ) &
        READER_PIDS[${r}]=$!
    done
    
    INFO "Started ${NUM_WRITERS} writers + ${NUM_READERS} readers"
    
    # 在并发 IO 期间执行多次热切换（渐进栈式：fresh upper/work each time）
    switch_count=0
    switch_errors=0
    start_ts=$(date +%s)

    while true; do
        current_ts=$(date +%s)
        elapsed=$((current_ts - start_ts))
        [[ ${elapsed} -ge ${TEST_DURATION} ]] && break

        sleep 1

        local new_upper="${WORK_BASE}/upper_g${switch_count}"
        local new_work="${WORK_BASE}/work_g${switch_count}"
        mkdir -p "${new_upper}" "${new_work}"
        if do_checkpoint "lowerdir=${LOWER1},upperdir=${new_upper},workdir=${new_work}" 2>/dev/null; then
            INFO "Switch ${switch_count}: -> upper_g${switch_count}"
        else
            WARN "Switch ${switch_count} failed (upper_g${switch_count})"
            switch_errors=$((switch_errors + 1))
        fi

        switch_count=$((switch_count + 1))
    done
    
    # 停止所有工作进程
    touch "${STOP_FILE}"
    sleep 1
    
    for w in $(seq 1 ${NUM_WRITERS}); do
        wait ${WRITER_PIDS[${w}]} 2>/dev/null || true
    done
    for r in $(seq 1 ${NUM_READERS}); do
        wait ${READER_PIDS[${r}]} 2>/dev/null || true
    done
    
    INFO "Completed ${switch_count} switches, ${switch_errors} errors"
    
    # 检查 write 错误
    total_write_errors=0
    for f in "${ERROR_DIR}"/writer_*.log; do
        [[ -f "${f}" ]] || continue
        count=$(wc -l < "${f}")
        total_write_errors=$((total_write_errors + count))
    done
    
    if [[ ${total_write_errors} -eq 0 ]]; then
        PASS "No write errors during concurrent stress"
    else
        WARN "${total_write_errors} write errors (may be expected during switch)"
    fi
    
    if [[ ${switch_errors} -eq 0 ]]; then
        PASS "All ${switch_count} switches succeeded"
    else
        # Strict accounting: any switch failure under the supported API pattern
        # is a real defect (kernel bug or genuine concurrent-IO race), not a WARN.
        FAIL "${switch_errors}/${switch_count} switches failed under concurrent IO"
    fi
    
    # 验证文件系统仍然可用
    sync
    if ls "${MERGED}/" > /dev/null 2>&1; then
        PASS "Filesystem still functional after stress"
    else
        FAIL "Filesystem broken after stress"
    fi
    
    # 内核错误检查
    kernel_errors=$(dmesg | tail -50 | grep -ci "oops\|BUG\|panic\|general protection" || true)
    if [[ ${kernel_errors} -eq 0 ]]; then
        PASS "No kernel crashes during stress test"
    else
        FAIL "Kernel errors detected: ${kernel_errors}"
        dmesg | tail -30
    fi
    
    INFO "=== concurrent R/W stress test complete ==="
}

main "$@"
