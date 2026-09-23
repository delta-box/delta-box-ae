#!/usr/bin/env bash
# =============================================================================
# build_base.sh — build minimal base.xfs from existing ubuntu-24.04.xfs
#
# 产出：~3 GB 的 base.xfs（XFS, reflink=1），只含 base OS + criu 二进制；
# 不含 /opt/miniconda3、/testbed catalog、/testbed_original_data 等数据内容。
# start_vm.py 每次启动前会把 base.xfs 复制为 base-run.xfs 作为 RW rootfs，
# data-<group>.xfs 则作为只读第二盘挂到 /mnt/data。
#
# 运行：sudo bash scripts/build_base.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC_IMG="${REPO_ROOT}/ubuntu-24.04.xfs"
OUT_IMG="${REPO_ROOT}/base.xfs"
SIZE_MB=3072        # 3 GB
SRC_MNT="/tmp/src_rootfs_$$"
DST_MNT="/tmp/base_build_$$"

INFO() { echo "[build_base] $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root"; exit 1
fi
if [[ ! -f "${SRC_IMG}" ]]; then
    echo "ERROR: ${SRC_IMG} not found"; exit 1
fi

cleanup() {
    umount -l "${DST_MNT}" 2>/dev/null || true
    umount -l "${SRC_MNT}" 2>/dev/null || true
    rmdir  "${DST_MNT}" 2>/dev/null || true
    rmdir  "${SRC_MNT}" 2>/dev/null || true
}
trap cleanup EXIT

INFO "creating ${OUT_IMG} (${SIZE_MB} MB)"
rm -f "${OUT_IMG}"
dd if=/dev/zero of="${OUT_IMG}" bs=1M count=${SIZE_MB} status=none
mkfs.xfs -q -f -m reflink=1 "${OUT_IMG}"

mkdir -p "${SRC_MNT}" "${DST_MNT}"

# 源盘可能已被别处挂载（例如 /tmp/xfs_inspect），先尽力复用已有挂载点
if mountpoint -q /tmp/xfs_inspect; then
    INFO "reusing existing mount at /tmp/xfs_inspect"
    SRC_MNT_USE="/tmp/xfs_inspect"
    SRC_MOUNTED_BY_US=0
else
    INFO "mounting source ${SRC_IMG} at ${SRC_MNT} (ro)"
    mount -o loop,ro "${SRC_IMG}" "${SRC_MNT}"
    SRC_MNT_USE="${SRC_MNT}"
    SRC_MOUNTED_BY_US=1
fi

INFO "mounting new ${OUT_IMG} at ${DST_MNT}"
mount -o loop "${OUT_IMG}" "${DST_MNT}"

INFO "rsyncing base OS (excluding /opt, /testbed, /root/criu, /dev/shm junk, etc.)"
rsync -aHAX --numeric-ids \
    --exclude='/opt/***' \
    --exclude='/testbed/***' \
    --exclude='/testbed_original_data/***' \
    --exclude='/testbed_src/***' \
    --exclude='/overlay_workspace/***' \
    --exclude='/app/***' \
    --exclude='/dev/shm/***' \
    --exclude='/dev/pts/***' \
    --exclude='/proc/***' \
    --exclude='/sys/***' \
    --exclude='/tmp/***' \
    --exclude='/run/***' \
    --exclude='/var/cache/apt/archives/*.deb' \
    --exclude='/var/lib/apt/lists/***' \
    --exclude='/var/log/**' \
    --exclude='/root/criu/***' \
    --exclude='/root/testcriu/***' \
    --exclude='/root/agentfs/***' \
    --exclude='/root/.cache/***' \
    --exclude='/root/.local/share/Trash/***' \
    --exclude='/root/*.log' \
    --exclude='/var/tmp/***' \
    --exclude='**/__pycache__/***' \
    --exclude='**/*.pyc' \
    --exclude='/custom_init.sh' \
    "${SRC_MNT_USE}/" "${DST_MNT}/"

INFO "creating mount-point stubs + /opt symlink"
# 统一目录权限/存在性
install -d -m 0755 "${DST_MNT}/mnt/data"
install -d -m 0755 "${DST_MNT}/testbed"
install -d -m 0755 "${DST_MNT}/testbed_original_data"
install -d -m 0755 "${DST_MNT}/overlay_workspace"
install -d -m 0755 "${DST_MNT}/app"
install -d -m 0755 "${DST_MNT}/dev/shm"
install -d -m 0755 "${DST_MNT}/dev/pts"
install -d -m 0755 "${DST_MNT}/proc"
install -d -m 0755 "${DST_MNT}/sys"
install -d -m 0755 "${DST_MNT}/tmp"
install -d -m 0755 "${DST_MNT}/run"
install -d -m 0700 "${DST_MNT}/root/agentfs"

# /opt 指向 data.xfs 挂载点，agent 运行时 conda 绝对路径 /opt/miniconda3/... 才能解析
# （由 /etc/fstab 把 /dev/vdb 挂到 /mnt/data 后生效）
rm -rf "${DST_MNT}/opt"
ln -s /mnt/data/opt "${DST_MNT}/opt"

INFO "installing /etc/fstab entry for data disk (ro)"
# 去掉旧 fstab 的冲突行（如果之前有 /dev/vdb 或 /mnt/swe_env），再附加新行
FSTAB="${DST_MNT}/etc/fstab"
[[ -f "${FSTAB}" ]] || : > "${FSTAB}"
# 删掉任何旧的 /dev/vdb 行
sed -i '/^\/dev\/vdb/d' "${FSTAB}"
sed -i '/\/mnt\/swe_env/d' "${FSTAB}"
cat >> "${FSTAB}" <<'EOF'
/dev/vdb  /mnt/data  xfs  ro,nouuid,noatime  0  0
EOF

INFO "writing /etc/systemd/system/data-postmount.service (symlink fixups + per-instance testbed copy)"
# 这个 unit 在 /mnt/data 挂好之后跑 —— 目前只做 sanity check 日志；
# 每实例 testbed 拷贝由 guest/main.py 在 overlay mount 前做，不放这里。
cat > "${DST_MNT}/etc/systemd/system/data-postmount.service" <<'EOF'
[Unit]
Description=Report data.xfs contents after mount
RequiresMountsFor=/mnt/data
After=mnt-data.mount

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo "[data] /opt/miniconda3/envs:" $(ls /opt/miniconda3/envs 2>/dev/null | wc -l) "envs; /mnt/data/testbeds:" $(ls /mnt/data/testbeds 2>/dev/null | wc -l) "trees"'
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
EOF
chroot "${DST_MNT}" /bin/bash -c "systemctl enable data-postmount.service 2>/dev/null || true"

INFO "purging machine-id so new VM gets a fresh one"
: > "${DST_MNT}/etc/machine-id" || true
rm -f "${DST_MNT}/var/lib/dbus/machine-id"

INFO "syncing + unmounting"
sync
umount "${DST_MNT}"
if [[ "${SRC_MOUNTED_BY_US}" -eq 1 ]]; then
    umount "${SRC_MNT_USE}"
fi

INFO "DONE: $(ls -lh "${OUT_IMG}" | awk '{print $5, $9}')"
INFO "  used vs allocated:"
df -h --output=used,avail,pcent,target "${OUT_IMG}" 2>/dev/null || true
