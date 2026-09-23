#!/bin/bash
# common.sh - 所有实验的公共函数和变量
#
# VM 的根文件系统已经是 XFS reflink=1，不需要创建额外的 XFS 镜像。
# 所有目录直接在 /tmp 下创建（位于根 XFS 上）。
set -euo pipefail

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS() { echo -e "${GREEN}[PASS]${NC} $1"; }
FAIL() { echo -e "${RED}[FAIL]${NC} $1"; }
WARN() { echo -e "${YELLOW}[WARN]${NC} $1"; }
INFO() { echo -e "[INFO] $1"; }

# 全局目录 — 全部在根 XFS 上
WORK_BASE="${WORK_BASE:-/tmp/ovl_test_$$}"

# overlayfs 目录（直接在根 XFS 上，自动享受 reflink）
LOWER1="${WORK_BASE}/lower1"
LOWER2="${WORK_BASE}/lower2"
UPPER1="${WORK_BASE}/upper1"
UPPER2="${WORK_BASE}/upper2"
WORK1="${WORK_BASE}/work1"
WORK2="${WORK_BASE}/work2"
MERGED="${WORK_BASE}/merged"

OVL_IOCTL_TOOL="${WORK_BASE}/ovl_ioctl"

setup_xfs_image() {
    # 根 fs 已经是 XFS reflink=1，只需验证并创建目录
    INFO "Setting up test directories on root XFS..."
    mkdir -p "${WORK_BASE}" "${MERGED}"

    # 验证根 fs 是 XFS 且支持 reflink
    local fstype
    fstype=$(stat -f -c %T / 2>/dev/null || echo "unknown")
    if [[ "${fstype}" == "xfs" ]]; then
        INFO "Root filesystem is XFS ✓"
    else
        WARN "Root filesystem is '${fstype}', not XFS — reflink tests may not work"
    fi

    # 尝试验证 reflink 支持
    if command -v xfs_info >/dev/null 2>&1; then
        local reflink_status
        reflink_status=$(xfs_info / 2>/dev/null | grep -o "reflink=[0-9]" || echo "reflink=unknown")
        INFO "Root XFS: ${reflink_status}"
    fi

    INFO "All directories under ${WORK_BASE} (on root XFS)"
}

setup_dirs() {
    mkdir -p "${LOWER1}" "${LOWER2}" "${UPPER1}" "${UPPER2}" \
             "${WORK1}" "${WORK2}" "${MERGED}"
}

mount_overlay() {
    local lower="$1"
    local upper="$2"
    local work="$3"
    
    mount -t overlay overlay \
        -o "lowerdir=${lower},upperdir=${upper},workdir=${work}" \
        "${MERGED}"
    INFO "Overlay mounted: lower=${lower}, upper=${upper}"
}

umount_overlay() {
    if mountpoint -q "${MERGED}" 2>/dev/null; then
        umount "${MERGED}" 2>/dev/null || umount -l "${MERGED}" 2>/dev/null || true
    fi
}

# 使用 ioctl 工具进行热切换
do_checkpoint() {
    local new_opts="$1"
    INFO "Checkpoint: ${new_opts}"
    "${OVL_IOCTL_TOOL}" "${MERGED}" "${new_opts}"
}

# 编译 ioctl 工具
build_ioctl_tool() {
    if [[ -f "${OVL_IOCTL_TOOL}" ]]; then
        return 0
    fi
    
    local src_dir
    src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    INFO "Building ioctl tool..."
    gcc -O2 -Wall -o "${OVL_IOCTL_TOOL}" "${src_dir}/ovl_ioctl.c"
}

cleanup_all() {
    INFO "Cleaning up..."
    umount_overlay
    rm -rf "${WORK_BASE}"
}

verify_xfs_reflink() {
    local file1="$1"
    local file2="$2"
    
    # 使用 xfs_io fiemap 检查 extent 共享
    local shared
    shared=$(xfs_io -c "fiemap -v" "${file2}" 2>/dev/null | grep -c "shared" || true)
    echo "${shared}"
}

get_block_count() {
    stat -c "%b" "$1"
}

get_extents() {
    # 返回文件的 extent 数量
    filefrag -v "$1" 2>/dev/null | tail -n 1 | awk '{print $NF}' | tr -d '.'
}

trap_cleanup() {
    trap cleanup_all EXIT
}
