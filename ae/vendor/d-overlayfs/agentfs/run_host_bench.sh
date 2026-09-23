#!/bin/bash
# run_host_bench.sh
# 完整流程：备份 host overlay → 编译 patched → 加载 → 跑 bench → 恢复
#
# 用法: sudo bash run_host_bench.sh
#
# 完成后输出在 agentfs/bench_results/，恢复原 overlay 模块。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KERN="${REPO_ROOT}/linux-6.8"
BACKUP_KO="/tmp/overlay.ko.host_backup_$$"
PATCHED_KO=""

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: must run as sudo / root"
  exit 1
fi

KREL=$(uname -r)
HOST_OVERLAY="/lib/modules/${KREL}/kernel/fs/overlayfs/overlay.ko"
[[ -f "${HOST_OVERLAY}.zst" ]] && HOST_OVERLAY="${HOST_OVERLAY}.zst"

echo "[1/8] kernel: ${KREL}"
echo "[1/8] host overlay module: ${HOST_OVERLAY}"

echo "[2/8] checking for overlay users (docker / snap / containers)..."
in_use=$(mount | grep -c "type overlay" || true)
if [[ "${in_use}" -gt 0 ]]; then
  echo "ERROR: ${in_use} overlay mounts active. Stop docker/snap/containers first:"
  mount | grep "type overlay"
  echo ""
  echo "  sudo systemctl stop docker snap.lxd.daemon"
  exit 2
fi
echo "  ✓ no active overlay mounts"

echo "[3/8] backing up host overlay.ko..."
cp -v "${HOST_OVERLAY}" "${BACKUP_KO}"

echo "[4/8] building patched overlay module..."
cd "${KERN}"
# need to have prepared kernel headers; if .config missing, copy host config
if [[ ! -f .config ]]; then
  if [[ -f "/boot/config-${KREL}" ]]; then
    cp "/boot/config-${KREL}" .config
    make olddefconfig
  else
    echo "ERROR: no .config and no /boot/config-${KREL} to bootstrap from"
    exit 3
  fi
fi
[[ -f Module.symvers ]] || make modules_prepare
make M=fs/overlayfs modules -j$(nproc)
PATCHED_KO="${KERN}/fs/overlayfs/overlay.ko"
ls -la "${PATCHED_KO}"

echo "[5/8] swapping overlay module..."
rmmod overlay || { echo "rmmod failed — likely in use"; exit 4; }
insmod "${PATCHED_KO}"
dmesg | tail -5
echo "  ✓ patched overlay loaded"

# Make sure we restore on exit (even on error)
restore_module() {
  echo ""
  echo "[restore] restoring host overlay.ko..."
  rmmod overlay 2>/dev/null || true
  if [[ "${HOST_OVERLAY}" == *.zst ]]; then
    # need to handle compressed module
    cp "${BACKUP_KO}" /tmp/host_overlay.ko.zst
    zstd -d -f /tmp/host_overlay.ko.zst -o /tmp/host_overlay.ko
    insmod /tmp/host_overlay.ko && echo "  ✓ original overlay restored"
  else
    insmod "${BACKUP_KO}" && echo "  ✓ original overlay restored"
  fi
}
trap restore_module EXIT

echo "[6/8] running fast vs slow bench..."
cd "${SCRIPT_DIR}"
bash bench_ioctl_fast_vs_slow.sh 200 3   # N=200, CPU 3

echo "[7/8] bench done. Outputs:"
ls -la "${SCRIPT_DIR}/bench_results/" | tail -5

echo "[8/8] EXIT trap will restore original overlay.ko..."
