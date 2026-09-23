#!/bin/bash
# test_cross_checkpoint_fd_cow.sh - stale open-fd write policy across checkpoint
#
# Verifies the three-way policy in ovl_ensure_upper_and_switch():
#   1. still-owned path -> re-home to current upper, write is path-visible
#   2. deleted/replaced path in the new branch -> anonymous CoW, no resurrection
#   3. same name already exists in new upper -> anonymous CoW, no overwrite
#
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

WORK_BASE="/tmp/ovl_test_xfd_$$"
LOWER1="${WORK_BASE}/lower1"; LOWER2="${WORK_BASE}/lower2"
UPPER1="${WORK_BASE}/upper1"; UPPER2="${WORK_BASE}/upper2"
WORK1="${WORK_BASE}/work1";   WORK2="${WORK_BASE}/work2"
MERGED="${WORK_BASE}/merged"
OVL_IOCTL_TOOL="${WORK_BASE}/ovl_ioctl"
RC=0
trap_cleanup

reset_dirs() {
    umount_overlay
    rm -rf "${LOWER1}" "${LOWER2}" "${UPPER1}" "${UPPER2}" \
           "${WORK1}" "${WORK2}" "${MERGED}"
    setup_dirs
}

checkpoint_to_lower2() {
    do_checkpoint "lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}"
}

case_rehome_persists() {
    INFO "--- case rehome persists ---"
    reset_dirs

    echo "base" > "${LOWER1}/notes.txt"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    # Copy up before checkpoint and keep an fd open across the switch.
    echo "before_ckpt" >> "${MERGED}/notes.txt"
    exec 7<>"${MERGED}/notes.txt"

    checkpoint_to_lower2 || { FAIL "[rehome] checkpoint failed"; RC=1; exec 7>&-; return; }

    echo "after_ckpt" >&7
    sync

    if grep -q "after_ckpt" "${MERGED}/notes.txt"; then
        PASS "[rehome] stale fd write became path-visible"
    else
        FAIL "[rehome] stale fd write was lost from path"
        RC=1
    fi

    if grep -q "after_ckpt" "${UPPER2}/notes.txt" 2>/dev/null; then
        PASS "[rehome] write landed in current upper"
    else
        FAIL "[rehome] write did not land in current upper"
        RC=1
    fi
    exec 7>&-
}

case_deleted_in_new_branch_anonymous() {
    INFO "--- case deleted path becomes anonymous ---"
    reset_dirs

    echo "from-lower1" > "${LOWER1}/fileA"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    echo "old_branch" >> "${MERGED}/fileA"
    exec 7<>"${MERGED}/fileA"
    checkpoint_to_lower2 || { FAIL "[vanish] checkpoint failed"; RC=1; exec 7>&-; return; }

    rm -f "${MERGED}/fileA"
    if [[ -e "${MERGED}/fileA" ]]; then
        FAIL "[vanish] fileA still visible after rm in new branch"
        RC=1
    else
        PASS "[vanish] fileA hidden by new-branch delete"
    fi

    echo "after_vanish" >&7
    sync

    if [[ -e "${MERGED}/fileA" || -f "${UPPER2}/fileA" ]]; then
        FAIL "[vanish] stale fd resurrected fileA"
        RC=1
    else
        PASS "[vanish] fileA path stayed absent"
    fi

    local fd_data
    fd_data=$(python3 - <<'PYEOF' 2>/dev/null || true
import os, sys
os.lseek(7, 0, os.SEEK_SET)
sys.stdout.buffer.write(os.read(7, 1024 * 1024))
PYEOF
)
    if echo "${fd_data}" | grep -q "after_vanish"; then
        PASS "[vanish] stale fd write stayed private/readable"
    else
        FAIL "[vanish] stale fd private data missing"
        RC=1
    fi
    exec 7>&-
}

case_foreign_upper_anonymous() {
    INFO "--- case foreign upper same-name guard ---"
    reset_dirs

    echo "old_base" > "${LOWER1}/conflict.txt"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    echo "old_branch" >> "${MERGED}/conflict.txt"
    exec 7<>"${MERGED}/conflict.txt"

    checkpoint_to_lower2 || { FAIL "[foreign] checkpoint failed"; RC=1; exec 7>&-; return; }

    echo "new_branch" > "${MERGED}/conflict.txt"
    echo "old_fd_after" >&7
    sync

    if grep -qx "new_branch" "${MERGED}/conflict.txt"; then
        PASS "[foreign] new upper file was not overwritten"
    else
        FAIL "[foreign] stale fd polluted/overwrote new upper file"
        RC=1
    fi

    if grep -q "old_fd_after" "${MERGED}/conflict.txt"; then
        FAIL "[foreign] private stale-fd write leaked into path"
        RC=1
    else
        PASS "[foreign] stale-fd write stayed anonymous"
    fi
    exec 7>&-
}

main() {
    INFO "=== Test: cross-checkpoint stale fd COW policy ==="
    setup_xfs_image
    setup_dirs
    build_ioctl_tool

    case_rehome_persists
    case_deleted_in_new_branch_anonymous
    case_foreign_upper_anonymous

    if dmesg | tail -80 | grep -qiE \
        "kernel BUG|BUG:|Oops:|Kernel panic|panic:|use-after-free|general protection fault|GPF:"; then
        FAIL "Kernel error detected!"
        RC=1
    else
        PASS "No kernel errors"
    fi

    if [[ "${RC}" -eq 0 ]]; then
        PASS "=== ALL cross-checkpoint fd COW checks passed ==="
    else
        FAIL "=== cross-checkpoint fd COW checks FAILED ==="
    fi
    return "${RC}"
}

main "$@"
exit "${RC}"
