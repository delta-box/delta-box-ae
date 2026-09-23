#!/bin/bash
# run_all.sh - 运行所有实验
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================"
echo " OverlayFS Hot Switch + XFS Test Suite"
echo "============================================"
echo ""

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root"
    exit 1
fi

# 检查依赖
for cmd in xfs_io mkfs.xfs filefrag python3 gcc; do
    if ! command -v ${cmd} &>/dev/null; then
        echo "ERROR: ${cmd} not found. Install it first."
        exit 1
    fi
done

PASSED=0
FAILED=0
SKIPPED=0

run_test() {
    local script="$1"
    local name
    name=$(basename "${script}" .sh)
    
    echo ""
    echo ">>> Running: ${name} <<<"
    
    if bash "${script}"; then
        PASSED=$((PASSED + 1))
        echo ">>> ${name}: PASSED <<<"
    else
        FAILED=$((FAILED + 1))
        echo ">>> ${name}: FAILED <<<"
    fi
    
    # 确保清理
    umount /tmp/ovl_test_*/merged 2>/dev/null || true
    umount /tmp/ovl_test_*/xfs_mnt 2>/dev/null || true
    echo ""
}

# 按依赖顺序运行
TESTS=(
    "test_xfs_reflink_copyup.sh"     # 基本: XFS reflink 是否生效
    "test_xfs_partial_write.sh"      # 基本: 块粒度 COW 验证
    "test_lazy_switch_write.sh"      # 核心: lazy switch 写路径
    "test_dentry_stale.sh"           # 核心: dentry 缓存一致性
    "test_dir_cache.sh"              # 核心: 目录缓存一致性
    "test_mmap_crash.sh"             # 风险: mmap crash
    "test_concurrent_rw.sh"          # 压力: 并发 + 切换
)

for test in "${TESTS[@]}"; do
    run_test "${SCRIPT_DIR}/${test}"
done

echo "============================================"
echo " Results: ${PASSED} passed, ${FAILED} failed, ${SKIPPED} skipped"
echo "============================================"

exit ${FAILED}
