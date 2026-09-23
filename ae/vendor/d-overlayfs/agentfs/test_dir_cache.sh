#!/bin/bash
# test_dir_cache.sh - 验证热切换后目录缓存 readdir 是否正确
#
# 核心问题: struct ovl_dir_cache 使用 version 计数器做缓存验证
# 热切换后 version 不会自增，旧缓存被认为仍然有效
# 导致 readdir 返回旧层的文件列表
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="dir_cache"
WORK_BASE="/tmp/ovl_test_dircache_$$"
trap_cleanup

main() {
    INFO "=== Test: directory cache staleness after hot switch ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # Lower1: 大量文件 (触发 dir cache 被填充)
    for i in $(seq 1 50); do
        echo "lower1_file_${i}" > "${LOWER1}/file_${i}.txt"
    done
    mkdir -p "${LOWER1}/bigdir"
    for i in $(seq 1 100); do
        echo "content_${i}" > "${LOWER1}/bigdir/entry_${i}"
    done
    
    # Lower2: 完全不同的文件集
    for i in $(seq 51 100); do
        echo "lower2_file_${i}" > "${LOWER2}/file_${i}.txt"
    done
    mkdir -p "${LOWER2}/bigdir"
    for i in $(seq 101 200); do
        echo "new_content_${i}" > "${LOWER2}/bigdir/entry_${i}"
    done
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # 先触发 dir cache 使其被填充
    INFO "Populating dir cache..."
    ls "${MERGED}/" > /dev/null 2>&1
    ls "${MERGED}/bigdir/" > /dev/null 2>&1
    count_before=$(ls "${MERGED}/bigdir/" | wc -l)
    INFO "Files in bigdir before switch: ${count_before}"
    
    # 在 merger 目录保持一个 opendir 状态的 fd
    exec 9< <(ls -1 "${MERGED}/bigdir/" 2>&1)
    
    # 热切换
    INFO "Hot switching..."
    do_checkpoint "lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}" || {
        FAIL "Checkpoint ioctl failed"
        exec 9<&-
        exit 1
    }
    
    # 验证 readdir 结果
    INFO "Post-switch readdir..."
    
    # 测试 1: 根目录 readdir
    root_files=$(ls -1 "${MERGED}/" 2>&1)
    INFO "Root files after switch: $(echo "${root_files}" | head -5)..."
    
    if echo "${root_files}" | grep -q "file_51"; then
        PASS "New files from lower2 visible in root"
    else
        FAIL "New files from lower2 NOT visible (dir cache stale)"
    fi
    
    if echo "${root_files}" | grep -q "file_1.txt"; then
        WARN "Old files from lower1 still visible (may be in upper or stale cache)"
    else
        PASS "Old files from lower1 correctly gone"
    fi
    
    # 测试 2: bigdir readdir
    bigdir_files=$(ls -1 "${MERGED}/bigdir/" 2>&1)
    count_after=$(echo "${bigdir_files}" | wc -l)
    INFO "Files in bigdir after switch: ${count_after}"
    
    if echo "${bigdir_files}" | grep -q "entry_101"; then
        PASS "New entries in bigdir visible"
    else
        FAIL "New entries in bigdir NOT visible (stale dir cache)"
    fi
    
    if echo "${bigdir_files}" | grep -q "entry_1$"; then
        FAIL "Old entries still visible (dir cache not invalidated)"
    else
        PASS "Old entries correctly absent"
    fi
    
    # 测试 3: 多次 readdir 一致性
    pass_count=0
    for trial in $(seq 1 5); do
        trial_count=$(ls -1 "${MERGED}/bigdir/" | wc -l)
        if [[ "${trial_count}" -eq "${count_after}" ]]; then
            pass_count=$((pass_count + 1))
        fi
    done
    if [[ ${pass_count} -eq 5 ]]; then
        PASS "readdir consistent across 5 trials"
    else
        FAIL "readdir inconsistent: only ${pass_count}/5 matched"
    fi
    
    exec 9<&-
    
    if dmesg | tail -20 | grep -qi "oops\|BUG\|panic"; then
        FAIL "Kernel error detected!"
    else
        PASS "No kernel errors"
    fi
    
    INFO "=== dir cache test complete ==="
}

main "$@"
