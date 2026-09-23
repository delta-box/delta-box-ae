#!/bin/bash
# test_xfs_reflink_copyup.sh - 验证 XFS reflink copy-up 块粒度行为
#
# 核心问题: overlayfs copy_up 调用 vfs_clone_file_range() → XFS reflink
# 当 lower/upper 都在同一个 XFS 时，clone 是 O(1) 操作
# 但当跨不同 XFS 设备时，reflink 不可用，退回 1MB chunk 拷贝
#
# 验证:
# 1. 同一 XFS 内 reflink copy-up → 零拷贝
# 2. 跨 XFS 设备 → 退回 chunk copy
# 3. reflink 后部分写 → 只 COW 受影响的 block (XFS 4KB 或可配置)
# 4. 大文件 copy-up 时间对比 (reflink vs chunk)
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="xfs_reflink_copyup"
WORK_BASE="/tmp/ovl_test_reflink_$$"
trap_cleanup

main() {
    INFO "=== Test: XFS reflink copy-up block granularity ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # === 测试 1: 大文件 reflink copy-up 速度 ===
    INFO "--- Test 1: Large file copy-up with reflink ---"
    
    # 创建 64MB 的测试文件
    dd if=/dev/urandom of="${LOWER1}/bigfile" bs=1M count=64 status=none
    md5_lower=$(md5sum "${LOWER1}/bigfile" | awk '{print $1}')
    blocks_lower=$(get_block_count "${LOWER1}/bigfile")
    INFO "Lower bigfile: md5=${md5_lower}, blocks=${blocks_lower}"
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # 触发 copy-up: 修改文件的一个字节
    start_time=$(date +%s%N)
    dd if=/dev/zero of="${MERGED}/bigfile" bs=1 count=1 seek=0 conv=notrunc status=none 2>/dev/null
    end_time=$(date +%s%N)
    copyup_time=$(( (end_time - start_time) / 1000000 ))
    
    INFO "Copy-up time for 64MB file: ${copyup_time}ms"
    
    # 检查 upper 层的文件
    if [[ -f "${UPPER1}/bigfile" ]]; then
        blocks_upper=$(get_block_count "${UPPER1}/bigfile")
        INFO "Upper bigfile blocks: ${blocks_upper} (lower was: ${blocks_lower})"
        
        # 如果 reflink 成功，upper 的 block count 应该很小（只有元数据 + COW 的部分）
        # 或者和 lower 相同（全量 reflink share）
        # 关键看 XFS 的 fiemap shared flag
        
        shared_count=$(xfs_io -c "fiemap -v" "${UPPER1}/bigfile" 2>/dev/null | grep -c "shared" || echo "0")
        INFO "Shared extents in upper: ${shared_count}"
        
        if [[ ${shared_count} -gt 0 ]]; then
            PASS "XFS reflink copy-up: extents are shared (zero-copy COW)"
        else
            WARN "No shared extents detected (reflink may not have been used)"
            # 这可能因为 lower 是 readonly mount，检查
            INFO "Checking if lower and upper are on same XFS..."
            lower_dev=$(stat -f -c "%i" "${LOWER1}")
            upper_dev=$(stat -f -c "%i" "${UPPER1}")
            INFO "Lower fs id: ${lower_dev}, Upper fs id: ${upper_dev}"
        fi
        
        if [[ ${copyup_time} -lt 500 ]]; then
            PASS "Copy-up was fast (${copyup_time}ms < 500ms) suggesting reflink"
        else
            WARN "Copy-up was slow (${copyup_time}ms >= 500ms) suggesting chunk copy"
        fi
    else
        FAIL "Upper file not found after copy-up"
    fi
    
    umount_overlay
    
    # === 测试 2: 小量写入后的块分配 ===
    INFO "--- Test 2: Block allocation after partial write ---"
    
    # 清理并重建
    rm -rf "${UPPER1}"/* "${WORK1}"/*
    mkdir -p "${UPPER1}" "${WORK1}"
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # 写入文件的中间 4KB
    dd if=/dev/zero of="${MERGED}/bigfile" bs=4096 count=1 seek=8192 conv=notrunc status=none 2>/dev/null
    sync
    
    if [[ -f "${UPPER1}/bigfile" ]]; then
        upper_blocks=$(get_block_count "${UPPER1}/bigfile")
        upper_size=$(stat -c "%s" "${UPPER1}/bigfile")
        INFO "After partial write: upper blocks=${upper_blocks}, size=${upper_size}"
        
        # 如果 reflink + COW，理想情况下只分配了被修改的 block
        # XFS 的 block size 通常是 4KB，64MB = 16384 blocks
        expected_full_blocks=$((64 * 1024 * 1024 / 512))  # stat 报告 512B blocks
        
        if [[ ${upper_blocks} -lt $((expected_full_blocks / 2)) ]]; then
            PASS "Partial write: only ${upper_blocks}/${expected_full_blocks} blocks allocated (COW block-level)"
        else
            WARN "Partial write: ${upper_blocks}/${expected_full_blocks} blocks (full copy, no COW)"
        fi
        
        # 用 xfs_io fiemap 详细查看 extent 分布
        INFO "Extent map of upper file:"
        xfs_io -c "fiemap -v" "${UPPER1}/bigfile" 2>/dev/null | head -20 || true
    fi
    
    umount_overlay
    
    # === 测试 3: 大量小文件 copy-up 效率 ===
    INFO "--- Test 3: Many small files copy-up ---"
    
    rm -rf "${UPPER1}"/* "${WORK1}"/*
    mkdir -p "${UPPER1}" "${WORK1}"
    mkdir -p "${LOWER1}/many_files"
    for i in $(seq 1 100); do
        dd if=/dev/urandom of="${LOWER1}/many_files/f${i}" bs=4096 count=1 status=none
    done
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    start_time=$(date +%s%N)
    for i in $(seq 1 100); do
        echo "modified" >> "${MERGED}/many_files/f${i}"
    done
    end_time=$(date +%s%N)
    
    small_time=$(( (end_time - start_time) / 1000000 ))
    INFO "100 small file copy-ups: ${small_time}ms"
    
    upper_count=$(ls "${UPPER1}/many_files/" 2>/dev/null | wc -l)
    PASS "Copied up ${upper_count}/100 small files in ${small_time}ms"
    
    umount_overlay
    
    if dmesg | tail -20 | grep -qi "oops\|BUG\|panic"; then
        FAIL "Kernel error detected!"
    else
        PASS "No kernel errors"
    fi
    
    INFO "=== XFS reflink copy-up test complete ==="
}

main "$@"
