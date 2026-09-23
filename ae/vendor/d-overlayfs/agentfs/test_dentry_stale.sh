#!/bin/bash
# test_dentry_stale.sh - 验证热切换后 dentry cache 是否一致
#
# 核心问题: shrink_dcache_sb 只清理了无引用的 dentry，
# 但被 open/cwd 持有的 dentry 仍然在缓存中，
# 它们的 __upperdentry 和 oe 指向旧层结构
#
# 测试:
# 1. 在 lower1 创建文件和目录结构
# 2. overlay 挂载
# 3. 打开文件（持有 dentry 引用）
# 4. 热切换到 lower2（lower2 有不同文件）
# 5. 验证旧文件是否仍可读，新文件是否可见
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="dentry_stale"
WORK_BASE="/tmp/ovl_test_dentry_$$"
RC=0
trap_cleanup

main() {
    INFO "=== Test: dentry staleness after hot switch ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # Lower1: 存在 fileA，不存在 fileB
    echo "content_from_lower1" > "${LOWER1}/fileA"
    mkdir -p "${LOWER1}/subdir"
    echo "subdir_file_lower1" > "${LOWER1}/subdir/nested"
    
    # Lower2: 不存在 fileA，存在 fileB，subdir 不同内容
    echo "content_from_lower2" > "${LOWER2}/fileB"
    mkdir -p "${LOWER2}/subdir"
    echo "subdir_file_lower2" > "${LOWER2}/subdir/nested"
    echo "extra_in_lower2" > "${LOWER2}/subdir/extra"
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # 验证初始状态
    [[ -f "${MERGED}/fileA" ]] && PASS "fileA exists before switch" || { FAIL "fileA missing"; RC=1; }
    [[ ! -f "${MERGED}/fileB" ]] && PASS "fileB absent before switch" || { FAIL "fileB unexpected"; RC=1; }
    
    # 持有 fd
    exec 7<"${MERGED}/fileA"
    exec 8<"${MERGED}/subdir/nested"
    
    # 一个进程 cwd 在 overlay 内
    (cd "${MERGED}/subdir" && sleep 30) &
    CWD_PID=$!
    sleep 0.5
    
    # 热切换
    INFO "Performing hot switch..."
    do_checkpoint "lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}" || {
        FAIL "Checkpoint failed"
        kill ${CWD_PID} 2>/dev/null || true
        exec 7<&-
        exec 8<&-
        exit 1
    }
    
    # 测试 1: 旧 fd 是否仍可读（lazy switch 应保证）
    INFO "Testing stale fd read..."
    content=$(cat <&7 2>/dev/null || echo "READ_FAILED")
    if [[ "${content}" == "content_from_lower1" ]]; then
        PASS "Stale fd still reads old content (lazy switch working)"
    elif [[ "${content}" == "READ_FAILED" ]]; then
        FAIL "Stale fd read failed"
        RC=1
    else
        FAIL "Stale fd read unexpected content: ${content}"
        RC=1
    fi
    exec 7<&-
    
    # 测试 2: nested fd
    nested_content=$(cat <&8 2>/dev/null || echo "READ_FAILED")
    exec 8<&-
    INFO "Nested fd content: ${nested_content}"
    
    # 测试 3: 新文件是否可见
    if [[ -f "${MERGED}/fileB" ]]; then
        PASS "fileB visible after switch"
    else
        FAIL "fileB not visible after switch (negative dentry cache stale?)"
        RC=1
    fi
    
    # 测试 4: 旧文件 fileA 在切换后是否消失
    if [[ -f "${MERGED}/fileA" ]]; then
        FAIL "fileA still visible after switch (stale positive dentry leaked old lower)"
        RC=1
    else
        PASS "fileA correctly absent after switch"
    fi
    
    # 测试 5: readdir 是否一致
    INFO "Readdir after switch:"
    ls -la "${MERGED}/" 2>&1 || WARN "readdir failed"
    ls -la "${MERGED}/subdir/" 2>&1 || WARN "readdir subdir failed"
    
    # 测试 6: 新路径是否可访问
    if [[ -f "${MERGED}/subdir/extra" ]]; then
        PASS "New file in subdir visible"
    else
        FAIL "New file in subdir not visible"
        RC=1
    fi
    
    kill ${CWD_PID} 2>/dev/null || true
    wait ${CWD_PID} 2>/dev/null || true
    
    # 检查 kernel 错误
    if dmesg | tail -80 | grep -qiE \
        "kernel BUG|BUG:|Oops:|Kernel panic|panic:|use-after-free|general protection fault|GPF:"; then
        FAIL "Kernel error detected!"
        RC=1
    else
        PASS "No kernel errors"
    fi
    
    INFO "=== dentry stale test complete ==="
    return "${RC}"
}

main "$@"
exit "${RC}"
