#!/usr/bin/env bash
# =============================================================================
# build_data.sh — build 5 per-group data-<group>.xfs read-only data disks.
#
# Groups (repo → group):
#   django                             → django   (~6 GB, 231 instances, 46%)
#   sympy                              → sympy    (~6 GB,  75 instances, 15%)
#   sphinx                             → sphinx   (~7 GB,  44 instances,  9%)
#   astropy|matplotlib|scikit-learn|xarray  → sci   (~12 GB, 110 instances, 22%)
#   pytest|pylint|requests|flask|seaborn    → tools (~6 GB,  40 instances,  8%)
#
# Each image contains:
#   /opt/miniconda3/            ← miniconda base + ONLY the envs in this group
#   /testbeds/<spec>/           ← source trees for this group's repos
#
# All images are carved from the existing ubuntu-24.04.xfs.
#
# 运行：sudo bash scripts/build_data.sh [group1 group2 ...]
#       不带参数 → 全建
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC_IMG="${REPO_ROOT}/ubuntu-24.04.xfs"
SRC_MNT="/tmp/src_rootfs_$$"
SRC_MNT_USE=""
SRC_MOUNTED_BY_US=0

INFO() { echo "[build_data] $*" >&2; }
ERR()  { echo "[build_data] ERROR: $*" >&2; }

# group → size MB | repo prefixes
#   sci bucket 比较大（4 个 sci repo 一起），要更大 image
declare -A GROUP_SIZE_MB=(
    [django]=9216
    [sympy]=9216
    [sphinx]=10240
    [sci]=18432
    [tools]=10240
)
declare -A GROUP_PREFIXES=(
    [django]="django"
    [sympy]="sympy"
    [sphinx]="sphinx"
    [sci]="astropy matplotlib scikit-learn xarray"
    [tools]="pytest pylint requests flask seaborn"
)

ALL_GROUPS=(django sympy sphinx sci tools)

# 支持命令行指定只构建部分 group
if [[ $# -gt 0 ]]; then
    GROUPS_TO_BUILD=("$@")
    for g in "${GROUPS_TO_BUILD[@]}"; do
        [[ -v GROUP_SIZE_MB[$g] ]] || { ERR "unknown group: $g"; exit 1; }
    done
else
    GROUPS_TO_BUILD=("${ALL_GROUPS[@]}")
fi

if [[ $EUID -ne 0 ]]; then ERR "run as root"; exit 1; fi
if [[ ! -f "${SRC_IMG}" ]]; then ERR "${SRC_IMG} not found"; exit 1; fi

cleanup() {
    # per-group mount cleanup
    for m in /tmp/data_build_*_$$; do
        umount -l "$m" 2>/dev/null || true
        rmdir "$m" 2>/dev/null || true
    done
    if [[ "${SRC_MOUNTED_BY_US}" -eq 1 ]]; then
        umount -l "${SRC_MNT}" 2>/dev/null || true
        rmdir  "${SRC_MNT}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---- mount source (once, RO) -------------------------------------------------
if mountpoint -q /tmp/xfs_inspect; then
    INFO "reusing existing source mount /tmp/xfs_inspect"
    SRC_MNT_USE="/tmp/xfs_inspect"
else
    INFO "mounting source ${SRC_IMG} at ${SRC_MNT} (ro)"
    mkdir -p "${SRC_MNT}"
    mount -o loop,ro "${SRC_IMG}" "${SRC_MNT}"
    SRC_MNT_USE="${SRC_MNT}"
    SRC_MOUNTED_BY_US=1
fi

SRC_OPT="${SRC_MNT_USE}/opt/miniconda3"
SRC_TBD="${SRC_MNT_USE}/testbed"

[[ -d "${SRC_OPT}" ]] || { ERR "source missing /opt/miniconda3"; exit 1; }
[[ -d "${SRC_TBD}" ]] || { ERR "source missing /testbed catalog"; exit 1; }

# ---- per-group build ---------------------------------------------------------
build_group() {
    local group="$1"
    local size_mb="${GROUP_SIZE_MB[$group]}"
    local prefixes="${GROUP_PREFIXES[$group]}"
    local out_img="${REPO_ROOT}/data-${group}.xfs"
    local mnt="/tmp/data_build_${group}_$$"

    INFO "=========================================================="
    INFO "building ${out_img}  size=${size_mb} MB  group=${group}"
    INFO "  prefixes: ${prefixes}"
    INFO "=========================================================="

    rm -f "${out_img}"
    dd if=/dev/zero of="${out_img}" bs=1M count=${size_mb} status=none
    mkfs.xfs -q -f -m reflink=1 "${out_img}"

    mkdir -p "${mnt}"
    mount -o loop "${out_img}" "${mnt}"

    # --- /opt/miniconda3: base + only this group's envs -----------------------
    install -d -m 0755 "${mnt}/opt"
    # 拷贝 miniconda base（含 pkgs/，envs 里可能有 hardlink 回 pkgs/），但排除 envs/*
    INFO "  copying miniconda base (excluding other groups' envs)"
    rsync -aHAX --numeric-ids \
        --exclude='/envs/***' \
        "${SRC_OPT}/" "${mnt}/opt/miniconda3/"

    install -d -m 0755 "${mnt}/opt/miniconda3/envs"
    # Always include the shared 'testbed' env — swe_runner hardcodes its PATH
    # to /opt/miniconda3/envs/testbed/bin as the agent's runtime shell.
    if [[ -d "${SRC_OPT}/envs/testbed" ]]; then
        INFO "    env testbed (shared agent runtime)"
        rsync -aHAX --numeric-ids "${SRC_OPT}/envs/testbed/" "${mnt}/opt/miniconda3/envs/testbed/"
    fi
    for pfx in ${prefixes}; do
        for envdir in "${SRC_OPT}/envs/${pfx}__"*; do
            [[ -d "${envdir}" ]] || continue
            local name; name="$(basename "${envdir}")"
            INFO "    env ${name}"
            rsync -aHAX --numeric-ids "${envdir}/" "${mnt}/opt/miniconda3/envs/${name}/"
        done
    done

    # --- /testbeds/<spec>/ ---------------------------------------------------
    install -d -m 0755 "${mnt}/testbeds"
    for pfx in ${prefixes}; do
        for spec in "${SRC_TBD}/${pfx}__"*; do
            [[ -d "${spec}" ]] || continue
            local name; name="$(basename "${spec}")"
            INFO "    testbed ${name}"
            rsync -aHAX --numeric-ids "${spec}/" "${mnt}/testbeds/${name}/"
        done
    done

    # --- manifest -------------------------------------------------------------
    {
        echo "group=${group}"
        echo "built_at=$(date -Iseconds)"
        echo "prefixes=${prefixes}"
        echo "envs:"
        ls "${mnt}/opt/miniconda3/envs" | sed 's/^/  /'
        echo "testbeds:"
        ls "${mnt}/testbeds" | sed 's/^/  /'
    } > "${mnt}/MANIFEST"

    sync
    local used
    used=$(df --output=used "${mnt}" | tail -1)
    INFO "  used ${used} KB of ${size_mb} MB"
    umount "${mnt}"
    rmdir "${mnt}"

    INFO "  $(ls -lh "${out_img}" | awk '{print $5, $9}')"
}

for g in "${GROUPS_TO_BUILD[@]}"; do
    build_group "$g"
done

INFO "=========================================================="
INFO "all groups done"
ls -lh "${REPO_ROOT}"/data-*.xfs 2>/dev/null
