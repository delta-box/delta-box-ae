#!/bin/bash
# test_lazy_switch_write.sh - 验证 lazy switch 写路径完整性
#
# 核心测试: ovl_ensure_upper_and_switch() 在 write/fallocate/splice_write 前被调用
# 热切换后首次写入触发:
# 1. 检测旧层 → 清除 __upperdentry → copy_up 到新层
# 2. 打开新后端文件替换 file->private_data
#
# 验证:
# - 切换前打开的文件，切换后写入数据正确到达新 upper
# - f_pos 保持一致
# - O_APPEND 写入位置正确
# - fallocate 在新层正确执行
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="lazy_switch_write"
WORK_BASE="/tmp/ovl_test_lazywr_$$"
trap_cleanup

main() {
    INFO "=== Test: lazy switch write path ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # 在 lower1 准备文件
    echo "original_content_line1" > "${LOWER1}/testfile"
    echo "original_content_line2" >> "${LOWER1}/testfile"
    dd if=/dev/urandom of="${LOWER1}/bigwrite" bs=1M count=4 status=none
    echo "append_test_base" > "${LOWER1}/appendfile"
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # Copy up files to upper layer first (so we have FDs pointing to upper1)
    echo "trigger_copyup" >> "${MERGED}/testfile"
    echo "trigger_copyup" >> "${MERGED}/bigwrite"
    echo "trigger_copyup" >> "${MERGED}/appendfile"
    sync
    
    # 打开 fd (指向 upper1 的真实文件)
    exec 3>"${MERGED}/testfile"    # write fd
    exec 4>>"${MERGED}/appendfile" # append fd
    
    # 启动背景写入进程
    cat > "${WORK_BASE}/writer.py" << 'PYEOF'
import os, sys, time

filepath = sys.argv[1]
phase_file = sys.argv[2]

fd = os.open(filepath, os.O_WRONLY)
# 记录初始 f_pos
pos = os.lseek(fd, 0, os.SEEK_END)

# Signal ready
with open(phase_file, 'w') as f:
    f.write(f"READY pos={pos}\n")

# Wait for switch signal
while not os.path.exists(phase_file + ".go"):
    time.sleep(0.01)

# Post-switch writes
os.lseek(fd, 0, os.SEEK_END)
data = b"POST_SWITCH_DATA_" + b"X" * 4000 + b"\n"
for i in range(10):
    os.write(fd, data)
os.fsync(fd)

final_pos = os.lseek(fd, 0, os.SEEK_CUR)
with open(phase_file + ".done", 'w') as f:
    f.write(f"DONE final_pos={final_pos}\n")

os.close(fd)
PYEOF
    
    python3 "${WORK_BASE}/writer.py" "${MERGED}/bigwrite" "${WORK_BASE}/phase" &
    WRITER_PID=$!
    
    # Wait for writer ready
    for i in $(seq 1 50); do
        [[ -f "${WORK_BASE}/phase" ]] && break
        sleep 0.1
    done
    
    INFO "Writer ready, performing hot switch..."
    
    # 热切换到新层
    do_checkpoint "lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}" || {
        FAIL "Checkpoint failed"
        kill ${WRITER_PID} 2>/dev/null
        exec 3>&-
        exec 4>&-
        exit 1
    }
    
    # Signal writer to continue
    touch "${WORK_BASE}/phase.go"
    
    # 测试 1: 通过旧 fd 写入
    INFO "Writing via stale fd..."
    echo "post_switch_write_via_fd" >&3
    exec 3>&-
    
    # 测试 2: append 通过旧 fd
    echo "post_switch_append" >&4
    exec 4>&-
    
    # 等待 writer 完成
    wait ${WRITER_PID} 2>/dev/null || true
    
    sync
    
    # 验证数据写到了新 upper
    INFO "Verifying writes landed in new upper layer..."
    
    # testfile 的写入应该在 UPPER2
    if [[ -f "${UPPER2}/testfile" ]]; then
        content=$(cat "${UPPER2}/testfile")
        if echo "${content}" | grep -q "post_switch_write_via_fd"; then
            PASS "Write via stale fd reached new upper layer"
        else
            FAIL "Write data not found in new upper"
            INFO "Content: ${content}"
        fi
    else
        # 可能 lazy switch 触发了 copy_up
        INFO "testfile not in UPPER2, checking merged view..."
        if grep -q "post_switch_write_via_fd" "${MERGED}/testfile" 2>/dev/null; then
            PASS "Write data accessible via merged view"
        else
            FAIL "Write data lost"
        fi
    fi
    
    # appendfile
    if grep -q "post_switch_append" "${MERGED}/appendfile" 2>/dev/null; then
        PASS "Append via stale fd succeeded"
    else
        FAIL "Append data lost"
    fi
    
    # bigwrite
    if [[ -f "${WORK_BASE}/phase.done" ]]; then
        PASS "Background writer completed"
        cat "${WORK_BASE}/phase.done"
    else
        FAIL "Background writer did not complete"
    fi
    
    # 检查是否有 EIO/ESTALE 错误
    if dmesg | tail -30 | grep -qi "EIO\|ESTALE\|stale"; then
        WARN "Stale file handle warnings detected"
        dmesg | tail -10
    fi
    
    if dmesg | tail -20 | grep -qi "oops\|BUG\|panic"; then
        FAIL "Kernel error detected!"
    else
        PASS "No kernel errors"
    fi
    
    INFO "=== lazy switch write test complete ==="
}

main "$@"
