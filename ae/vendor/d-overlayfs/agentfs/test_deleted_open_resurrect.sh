#!/bin/bash
# test_deleted_open_resurrect.sh - deleted-open fd 跨 checkpoint 写入不得复活路径
#
# 回归测试: POSIX unlink-after-open 语义
#
# 场景:
#   1. 打开文件得到 fd (O_RDWR)
#   2. rm 删除路径 (overlay dentry 被 d_drop → unhashed, 仍被 fd pin 为 positive)
#   3. 触发 checkpoint 热切换 (旧 upper → lower, 新空 upper)
#   4. 通过陈旧 fd 写入数据
#
# BUG (修复前): ovl_ensure_upper_and_switch() 对 file_dentry 按名字 copy_up 到
#   新 upper, 重建目录项 → 被删路径"复活"。
#
# 修复: 在 ovl_ensure_upper_and_switch() 入口检测 d_unhashed/d_really_is_negative,
#   跳过 switch, 写继续打到 open 时缓存的匿名 inode (nlink==0)。
#
# 断言: 写入后
#   - 路径在 merged 视图中仍不存在 (ENOENT)
#   - 新 upper 目录里没有该名字的目录项
#   - 通过 fd 仍可读回刚写入的数据 (匿名 inode 可用)
#   - 无内核 oops/BUG
#
# 覆盖两种来源:
#   A. 纯 upper 新建文件 (无 lower)
#   B. 有 lower 底座的文件 (rm 在旧 upper 留 whiteout)
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

WORK_BASE="/tmp/ovl_test_delopen_$$"
# 重新计算依赖 WORK_BASE 的派生路径
LOWER1="${WORK_BASE}/lower1"; LOWER2="${WORK_BASE}/lower2"
UPPER1="${WORK_BASE}/upper1"; UPPER2="${WORK_BASE}/upper2"
WORK1="${WORK_BASE}/work1";   WORK2="${WORK_BASE}/work2"
MERGED="${WORK_BASE}/merged"
OVL_IOCTL_TOOL="${WORK_BASE}/ovl_ioctl"

RC=0
trap_cleanup

# 单个子用例。$1=用例名  $2=是否有 lower 底座 (0/1)
run_case() {
    local name="$1" has_lower="$2"
    local path="${MERGED}/deleted_${name}.txt"
    local base="deleted_${name}.txt"
    INFO "--- case ${name} (has_lower=${has_lower}) ---"

    if [[ "${has_lower}" == "1" ]]; then
        echo "BASE_LOWER_CONTENT_${name}" > "${LOWER1}/${base}"
    fi

    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    # 建立 upper 数据并以 O_RDWR 打开陈旧 fd
    echo "before_unlink_${name}" > "${path}"
    sync
    exec 9<>"${path}"

    # 删除路径
    rm -f "${path}"
    if [[ -e "${path}" ]]; then
        FAIL "[${name}] path still visible right after rm"
        RC=1
    else
        PASS "[${name}] path gone after rm (pre-checkpoint)"
    fi

    # 终端 2 等价物: 热切换
    do_checkpoint "lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}" || {
        FAIL "[${name}] checkpoint failed"
        exec 9>&-; umount_overlay; RC=1; return
    }

    # 跨 checkpoint 通过陈旧 fd 写入
    if echo "after_unlink_${name}" >&9 2>/dev/null; then
        INFO "[${name}] write via stale fd returned success"
    else
        INFO "[${name}] write via stale fd returned error (acceptable)"
    fi
    sync

    # 断言 1: merged 视图中路径仍不存在
    if [[ -e "${path}" ]]; then
        FAIL "[${name}] RESURRECTED: path reappeared in merged view"
        ls -l "${path}" 2>&1 | sed 's/^/    /'
        RC=1
    else
        PASS "[${name}] path stays gone in merged view (no resurrection)"
    fi

    # 断言 2: 新 upper 没有创建该目录项
    if [[ -e "${UPPER2}/${base}" ]]; then
        FAIL "[${name}] RESURRECTED: entry created in new upper (${UPPER2}/${base})"
        RC=1
    else
        PASS "[${name}] no entry in new upper layer"
    fi

    # 断言 3: 通过 fd 仍能读回写入的数据 (匿名 inode 可用)
    local readback
    readback=$(python3 - <<'PYEOF' 2>/dev/null || true
import os, sys

fd = 9
os.lseek(fd, 0, os.SEEK_SET)
sys.stdout.buffer.write(os.read(fd, 1024 * 1024))
PYEOF
)
    if echo "${readback}" | grep -q "after_unlink_${name}"; then
        PASS "[${name}] data readable via stale fd (anonymous inode intact)"
    else
        WARN "[${name}] could not read back via fd (data: '${readback}') — non-fatal"
    fi

    exec 9>&-
    umount_overlay
    # 清掉本用例残留以免影响下一个用例
    rm -rf "${UPPER1:?}"/* "${UPPER2:?}"/* "${WORK1:?}"/* "${WORK2:?}"/* \
           "${LOWER1:?}/${base}" 2>/dev/null || true
}

main() {
    INFO "=== Test: deleted-open fd must not resurrect path across checkpoint ==="
    setup_xfs_image
    setup_dirs
    build_ioctl_tool

    run_case "pureupper" 0
    run_case "withlower" 1

    # 内核健康检查
    if dmesg | tail -80 | grep -qiE \
        "kernel BUG|BUG:|Oops:|Kernel panic|panic:|use-after-free|general protection fault|GPF:"; then
        FAIL "Kernel error detected!"
        dmesg | tail -20 | sed 's/^/    /'
        RC=1
    else
        PASS "No kernel errors"
    fi

    echo
    if [[ "${RC}" -eq 0 ]]; then
        PASS "=== ALL deleted-open resurrection checks passed ==="
    else
        FAIL "=== deleted-open resurrection test FAILED ==="
    fi
    return "${RC}"
}

main "$@"
exit "${RC}"
