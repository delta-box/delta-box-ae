#!/bin/bash
# test_xfs_partial_write.sh - 验证 XFS 上部分写仅 COW 受影响的块
#
# 核心测试: 从 ext4 切换到 XFS 后，块粒度 COW 行为差异
#
# XFS reflink COW 的关键问题:
# 1. XFS 默认 block size = 4096 (可通过 mkfs.xfs -b size=N 配置)
# 2. XFS extent 可能很大，COW 粒度取决于 extent 边界
# 3. XFS speculative preallocation 可能导致额外块分配
# 4. XFS 的 realtime extent size 配置影响最小分配单元
#
# 与 ext4 对比:
# - ext4 没有 reflink，copy_up 总是全量复制
# - XFS reflink 可以 O(1) clone + 按需 COW
# - 但 XFS COW extent 粒度可能 > 1 block (speculative prealloc)
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="xfs_partial_write"
WORK_BASE="/tmp/ovl_test_partial_$$"

# 额外创建一个 ext4 image 做对比
EXT4_IMG="${WORK_BASE}/ext4.img"
EXT4_MNT="${WORK_BASE}/ext4_mnt"

trap_cleanup

setup_ext4_image() {
    INFO "Creating ext4 image for comparison..."
    dd if=/dev/zero of="${EXT4_IMG}" bs=1M count=512 status=none
    mkfs.ext4 -F -q "${EXT4_IMG}"
    mkdir -p "${EXT4_MNT}"
    mount -o loop "${EXT4_IMG}" "${EXT4_MNT}"
}

main() {
    INFO "=== Test: XFS partial write COW granularity ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # 创建测试文件: 32MB，内容可验证
    INFO "Creating 32MB test file with verifiable pattern..."
    python3 -c "
import sys
# 每 4KB 一个不同的 pattern
for block in range(32 * 256):  # 32MB / 4KB = 8192 blocks
    pattern = bytes([block % 256]) * 4096
    sys.stdout.buffer.write(pattern)
" > "${LOWER1}/patternfile"
    
    md5_orig=$(md5sum "${LOWER1}/patternfile" | awk '{print $1}')
    stat_lower=$(stat -c "size=%s blocks=%b" "${LOWER1}/patternfile")
    INFO "Original file: ${stat_lower}, md5=${md5_orig}"
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # === 实验 A: 写入第一个字节，触发 copy-up ===
    INFO "--- Experiment A: Write 1 byte at offset 0 ---"
    
    python3 -c "
import os
fd = os.open('${MERGED}/patternfile', os.O_WRONLY)
os.pwrite(fd, b'X', 0)
os.close(fd)
"
    sync
    
    if [[ -f "${UPPER1}/patternfile" ]]; then
        stat_upper_a=$(stat -c "size=%s blocks=%b" "${UPPER1}/patternfile")
        INFO "After 1-byte write: ${stat_upper_a}"
        
        blocks_a=$(stat -c "%b" "${UPPER1}/patternfile")
        blocks_full=$((32 * 1024 * 1024 / 512))
        ratio_a=$((blocks_a * 100 / blocks_full))
        INFO "Block usage: ${ratio_a}% of full file"
        
        # XFS reflink COW 的理想结果: 只分配 1 个 extent (4KB ~ 几十 KB)
        if [[ ${ratio_a} -lt 10 ]]; then
            PASS "XFS COW: Only ${ratio_a}% blocks allocated for 1-byte write (block-level COW)"
        elif [[ ${ratio_a} -lt 50 ]]; then
            WARN "XFS COW: ${ratio_a}% blocks allocated (partial COW, speculative prealloc?)"
        else
            WARN "XFS COW: ${ratio_a}% blocks allocated (looks like full copy)"
        fi
        
        # 详细 extent 信息
        INFO "Extent map:"
        xfs_io -c "fiemap -v" "${UPPER1}/patternfile" 2>/dev/null | head -30 || true
        echo "---"
        filefrag -v "${UPPER1}/patternfile" 2>/dev/null | head -30 || true
    fi
    
    umount_overlay
    
    # === 实验 B: 在文件中间写入 4KB ===
    INFO "--- Experiment B: Write 4KB at offset 16MB ---"
    
    rm -rf "${UPPER1}"/* "${WORK1}"/*
    mkdir -p "${UPPER1}" "${WORK1}"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    python3 -c "
import os
fd = os.open('${MERGED}/patternfile', os.O_WRONLY)
os.pwrite(fd, b'Y' * 4096, 16 * 1024 * 1024)  # offset 16MB
os.close(fd)
"
    sync
    
    if [[ -f "${UPPER1}/patternfile" ]]; then
        blocks_b=$(stat -c "%b" "${UPPER1}/patternfile")
        ratio_b=$((blocks_b * 100 / blocks_full))
        INFO "After 4KB write at 16MB: blocks=${blocks_b}, ratio=${ratio_b}%"
        
        if [[ ${ratio_b} -lt 10 ]]; then
            PASS "Middle write: Only ${ratio_b}% blocks (excellent COW granularity)"
        else
            WARN "Middle write: ${ratio_b}% blocks (COW granularity larger than expected)"
        fi
        
        filefrag -v "${UPPER1}/patternfile" 2>/dev/null | head -30 || true
    fi
    
    umount_overlay
    
    # === 实验 C: 分散写入多个位置 ===
    INFO "--- Experiment C: Scattered writes at 8 positions ---"
    
    rm -rf "${UPPER1}"/* "${WORK1}"/*
    mkdir -p "${UPPER1}" "${WORK1}"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    python3 -c "
import os
fd = os.open('${MERGED}/patternfile', os.O_WRONLY)
# 在 8 个不同位置各写 4KB
offsets = [0, 4*1024*1024, 8*1024*1024, 12*1024*1024,
           16*1024*1024, 20*1024*1024, 24*1024*1024, 28*1024*1024]
for off in offsets:
    os.pwrite(fd, b'Z' * 4096, off)
os.close(fd)
"
    sync
    
    if [[ -f "${UPPER1}/patternfile" ]]; then
        blocks_c=$(stat -c "%b" "${UPPER1}/patternfile")
        ratio_c=$((blocks_c * 100 / blocks_full))
        INFO "After 8 scattered writes: blocks=${blocks_c}, ratio=${ratio_c}%"
        
        # 理想: 8 * 4KB = 32KB → ~64 blocks (512B单位) + 一些 speculative prealloc
        ideal_blocks=$((8 * 4096 / 512))
        INFO "Ideal blocks: ${ideal_blocks}, actual: ${blocks_c}"
        
        if [[ ${blocks_c} -lt $((ideal_blocks * 4)) ]]; then
            PASS "Scattered writes: ${blocks_c} blocks (~${ratio_c}%) - good COW granularity"
        else
            WARN "Scattered writes: ${blocks_c} blocks (~${ratio_c}%) - COW granularity not ideal"
        fi
        
        filefrag "${UPPER1}/patternfile" 2>/dev/null || true
    fi
    
    umount_overlay
    
    # === 实验 D: 数据完整性验证 ===
    INFO "--- Experiment D: Data integrity after partial COW ---"
    
    rm -rf "${UPPER1}"/* "${WORK1}"/*
    mkdir -p "${UPPER1}" "${WORK1}"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # 只修改 offset 0 的一个字节
    python3 -c "
import os
fd = os.open('${MERGED}/patternfile', os.O_WRONLY)
os.pwrite(fd, b'Q', 0)
os.close(fd)
"
    sync
    
    # 验证未修改区域的数据完整性
    python3 -c "
import sys
with open('${MERGED}/patternfile', 'rb') as f:
    # 检查 byte 0 是否是 'Q'
    b0 = f.read(1)
    assert b0 == b'Q', f'byte 0 should be Q, got {b0}'
    
    # 检查后续每个 4KB block 的完整性
    errors = 0
    for block in range(1, 32 * 256):
        f.seek(block * 4096)
        data = f.read(4096)
        expected = bytes([block % 256]) * 4096
        if data != expected:
            errors += 1
            if errors <= 3:
                print(f'Block {block} corrupted: got {data[:8].hex()}, expected {expected[:8].hex()}')
    
    if errors == 0:
        print('INTEGRITY_OK')
    else:
        print(f'INTEGRITY_FAIL: {errors} corrupted blocks')
    sys.exit(0 if errors == 0 else 1)
"
    integrity_result=$?
    
    if [[ ${integrity_result} -eq 0 ]]; then
        PASS "Data integrity verified after partial COW on XFS"
    else
        FAIL "Data corruption detected after partial COW!"
    fi
    
    umount_overlay
    
    if dmesg | tail -20 | grep -qi "oops\|BUG\|panic"; then
        FAIL "Kernel error detected!"
    else
        PASS "No kernel errors"
    fi
    
    INFO "=== XFS partial write test complete ==="
}

main "$@"
