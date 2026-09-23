#!/bin/bash
# test_bystander_fd.sh
# =====================================================================
# 实验：旁观进程 B 的 FD 在热切换后的行为
#
# 使用 C 程序作为旁观进程（避免 bash FIFO deadlock），
# 通过 SIGUSR1/SIGUSR2 + 文件轮询进行进程间通信。
#
# 验证项：
#   1. B 的 fd 在 checkpoint 后是否仍可读写（不 crash）
#   2. B 通过旧 fd 写入的数据去了哪里（旧 upper vs 新 upper）
#   3. lazy switch 是否对 B 的 write() 生效
#   4. 多代 checkpoint 后旧 fd 是否仍安全
#   5. FS-only rollback 导致的语义不一致
# =====================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

declare -a RESULTS=()
record() { RESULTS+=("$1"); }

BYSTANDER_BIN="${WORK_BASE}/bystander_test"

build_bystander() {
    INFO "Building bystander test program..."
    gcc -O2 -Wall -o "${BYSTANDER_BIN}" "${SCRIPT_DIR}/bystander_test.c"
}

# 等待文件出现（轮询），超时 N 秒
wait_for_file() {
    local path="$1"
    local timeout="${2:-10}"
    local i=0
    while [[ ! -f "${path}" ]]; do
        sleep 0.1
        i=$((i + 1))
        if [[ $i -ge $((timeout * 10)) ]]; then
            FAIL "Timeout waiting for ${path}"
            return 1
        fi
    done
    return 0
}

# 从 result 文件读取某个 key=value
get_result() {
    local file="$1" key="$2"
    grep "^${key}=" "${file}" 2>/dev/null | head -1 | cut -d= -f2-
}

# ================================================================
# 测试 0：基础 ioctl 验证（不涉及旁观进程）
# ================================================================
test_0_ioctl_basic() {
    INFO "=== Test 0: Basic ioctl checkpoint sanity ==="

    setup_dirs

    echo "hello" > "${LOWER1}/basic.txt"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    local content
    content=$(cat "${MERGED}/basic.txt")
    if [[ "${content}" == "hello" ]]; then
        PASS "Test 0a: Overlay read works"
    else
        FAIL "Test 0a: Overlay read got '${content}' expected 'hello'"
        record "FAIL: basic overlay read broken"
        umount_overlay; return 1
    fi

    echo "modified" > "${MERGED}/basic.txt"
    sync

    if [[ -f "${UPPER1}/basic.txt" ]]; then
        PASS "Test 0b: Copy-up works"
    else
        FAIL "Test 0b: No copy-up in upper1"
        record "FAIL: copy-up broken"
        umount_overlay; return 1
    fi

    do_checkpoint "lowerdir=${LOWER1},upperdir=${UPPER2},workdir=${WORK2}"

    content=$(cat "${MERGED}/basic.txt")
    INFO "Test 0c: After checkpoint, overlay reads: '${content}'"

    echo "post_checkpoint" > "${MERGED}/basic.txt"
    sync

    if [[ -f "${UPPER2}/basic.txt" ]]; then
        PASS "Test 0d: Write after checkpoint goes to new upper"
        record "PASS: basic checkpoint + write works"
    else
        FAIL "Test 0d: Write not in upper2"
        record "FAIL: post-checkpoint write broken"
    fi

    umount_overlay
    return 0
}

# ================================================================
# 测试 1：checkpoint 后 B 的 fd 是否存活 + 写入去向
# ================================================================
test_1_fd_survives() {
    INFO "=== Test 1: FD survives checkpoint + write destination ==="

    setup_dirs

    echo "original_lower_data" > "${LOWER1}/testfile.txt"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    # A 写入触发 copy-up
    echo "written_by_A_v1" > "${MERGED}/testfile.txt"
    sync

    # 启动旁观进程 B
    local comm="${WORK_BASE}/comm1"
    mkdir -p "${comm}"
    rm -f "${comm}"/{ready,result,result2}
    "${BYSTANDER_BIN}" "${MERGED}/testfile.txt" "${comm}" 2>&1 &
    local B_PID=$!

    if ! wait_for_file "${comm}/ready"; then
        kill ${B_PID} 2>/dev/null; wait ${B_PID} 2>/dev/null || true
        umount_overlay; return
    fi
    INFO "Bystander B ready (PID=${B_PID})"

    # ---- Checkpoint: upper1 → upper2 ----
    INFO "Performing checkpoint: upper1 → upper2"
    do_checkpoint "lowerdir=${LOWER1},upperdir=${UPPER2},workdir=${WORK2}"

    # 通知 B 执行 phase1（读+写）
    kill -USR1 ${B_PID}
    if ! wait_for_file "${comm}/result" 15; then
        FAIL "Test 1: B did not produce result (crashed?)"
        record "FAIL: B crashed or hung during phase1"
        kill -9 ${B_PID} 2>/dev/null; wait ${B_PID} 2>/dev/null || true
        umount_overlay; return
    fi

    # 分析结果
    local read_ok write_ok write_bytes read_content
    read_ok=$(get_result "${comm}/result" "read_ok")
    write_ok=$(get_result "${comm}/result" "write_ok")
    write_bytes=$(get_result "${comm}/result" "write_bytes")
    read_content=$(get_result "${comm}/result" "read_content")

    if [[ "${read_ok}" == "1" ]]; then
        PASS "Test 1a: B can read via fd after checkpoint"
        INFO "  B read: '${read_content}'"
        record "PASS: read via fd works after checkpoint"
    else
        FAIL "Test 1a: B's read failed"
        record "FAIL: read via fd failed"
    fi

    if [[ "${write_ok}" == "1" ]]; then
        PASS "Test 1b: B can write via fd after checkpoint (${write_bytes} bytes)"
        record "PASS: write via fd works (no crash)"
    else
        FAIL "Test 1b: B's write failed"
        record "FAIL: write via fd failed"
    fi

    # 检查写入去向
    local in_old=false in_new=false
    if [[ -f "${UPPER1}/testfile.txt" ]] && grep -q "BYSTANDER_PHASE1" "${UPPER1}/testfile.txt" 2>/dev/null; then
        in_old=true
    fi
    if [[ -f "${UPPER2}/testfile.txt" ]] && grep -q "BYSTANDER_PHASE1" "${UPPER2}/testfile.txt" 2>/dev/null; then
        in_new=true
    fi

    if ${in_new}; then
        PASS "Test 1c: B's write landed in NEW upper2 (lazy switch worked!)"
        record "PASS: lazy switch redirected bystander write to new upper"
    elif ${in_old}; then
        WARN "Test 1c: B's write landed in OLD upper1 (stale backing file)"
        WARN "  → write() went directly to old backing file, not through ovl_write_iter"
        record "WARN: write went to old upper (semantic error, no crash)"
    else
        WARN "Test 1c: B's write data not found in either upper"
        record "WARN: write destination unknown"
    fi

    INFO "  upper1: $(cat "${UPPER1}/testfile.txt" 2>/dev/null | head -2 | tr '\n' ' ')"
    INFO "  upper2: $(cat "${UPPER2}/testfile.txt" 2>/dev/null | head -2 | tr '\n' ' ')"

    # 通过 overlay 新打开看到什么
    local fresh
    fresh=$(cat "${MERGED}/testfile.txt" | head -1)
    INFO "  overlay fresh read: '${fresh}'"

    # Phase2: just exit
    kill -USR2 ${B_PID} 2>/dev/null
    wait ${B_PID} 2>/dev/null || true
    umount_overlay
}

# ================================================================
# 测试 2：多代 checkpoint + deferred cleanup 安全性
# ================================================================
test_2_multi_generation() {
    INFO "=== Test 2: Multi-generation checkpoint safety ==="

    setup_dirs
    mkdir -p "${WORK_BASE}/upper3" "${WORK_BASE}/work3"

    echo "genesis" > "${LOWER1}/multi.txt"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    echo "gen1_data" > "${MERGED}/multi.txt"
    sync

    # B 打开文件
    local comm="${WORK_BASE}/comm2"
    mkdir -p "${comm}"
    rm -f "${comm}"/{ready,result,result2}
    "${BYSTANDER_BIN}" "${MERGED}/multi.txt" "${comm}" 2>&1 &
    local B_PID=$!

    if ! wait_for_file "${comm}/ready"; then
        kill ${B_PID} 2>/dev/null; wait ${B_PID} 2>/dev/null || true
        umount_overlay; return
    fi
    INFO "B holding fd to multi.txt (on upper1)"

    # Checkpoint 1: upper1 → upper2
    INFO "--- Checkpoint 1: upper1 → upper2 ---"
    do_checkpoint "lowerdir=${LOWER1},upperdir=${UPPER2},workdir=${WORK2}"

    # Checkpoint 2: upper2 → upper3
    # prev_layers(upper1 的那代) 会被 ovl_free_layers_array 释放
    # 但 B 的 backing file 持有 upper1 mount 引用 → mount 不会真正释放
    INFO "--- Checkpoint 2: upper2 → upper3 ---"
    INFO "  (prev_layers freed, but B's file holds mount ref)"
    do_checkpoint "lowerdir=${LOWER1},upperdir=${WORK_BASE}/upper3,workdir=${WORK_BASE}/work3"

    # Phase1: 让 B 读写（两代 checkpoint 后）
    kill -USR1 ${B_PID}
    if ! wait_for_file "${comm}/result" 15; then
        FAIL "Test 2: B crashed after 2 checkpoints (mount freed despite open file?)"
        record "FAIL: B crashed after 2-gen checkpoint (potential UAF)"
        kill -9 ${B_PID} 2>/dev/null; wait ${B_PID} 2>/dev/null || true
        umount_overlay; return
    fi

    local read_ok write_ok
    read_ok=$(get_result "${comm}/result" "read_ok")
    write_ok=$(get_result "${comm}/result" "write_ok")

    if [[ "${read_ok}" == "1" ]]; then
        PASS "Test 2a: B's fd readable after 2 checkpoints"
        record "PASS: fd safe after 2 generations (VFS refcount protects mount)"
    else
        FAIL "Test 2a: B's fd broken after 2 checkpoints"
        record "FAIL: fd broken after 2 generations"
    fi

    if [[ "${write_ok}" == "1" ]]; then
        PASS "Test 2b: B can write after 2 checkpoints (no crash)"
        record "PASS: write after 2-gen checkpoint works"
    else
        FAIL "Test 2b: write failed after 2 checkpoints"
        record "FAIL: write failed after 2 generations"
    fi

    kill -USR2 ${B_PID} 2>/dev/null
    wait ${B_PID} 2>/dev/null || true
    umount_overlay
}

# ================================================================
# 测试 3：FS-only rollback（只回滚文件系统，不回滚内存）
# ================================================================
test_3_fs_only_rollback() {
    INFO "=== Test 3: FS-only rollback (the dangerous scenario) ==="

    setup_dirs
    mkdir -p "${WORK_BASE}/work1_rb"

    echo "v0" > "${LOWER1}/rollback.txt"
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"

    echo "v1_from_A" > "${MERGED}/rollback.txt"
    sync

    # B 打开文件
    local comm="${WORK_BASE}/comm3"
    mkdir -p "${comm}"
    rm -f "${comm}"/{ready,result,result2}
    "${BYSTANDER_BIN}" "${MERGED}/rollback.txt" "${comm}" 2>&1 &
    local B_PID=$!

    if ! wait_for_file "${comm}/ready"; then
        kill ${B_PID} 2>/dev/null; wait ${B_PID} 2>/dev/null || true
        umount_overlay; return
    fi
    INFO "B holding fd (content: 'v1_from_A', on upper1)"

    # Forward: upper1 → upper2
    INFO "--- Forward: upper1 → upper2 ---"
    do_checkpoint "lowerdir=${LOWER1},upperdir=${UPPER2},workdir=${WORK2}"

    # A 在新 upper 写入
    echo "v2_from_A" > "${MERGED}/rollback.txt"
    sync

    # ROLLBACK: 回到 upper1（不做 CRIU，B 还在！）
    INFO "--- ROLLBACK: → upper1 (FS only, no CRIU!) ---"
    do_checkpoint "lowerdir=${LOWER1},upperdir=${UPPER1},workdir=${WORK_BASE}/work1_rb"

    # Phase1: B 读写
    kill -USR1 ${B_PID}
    if ! wait_for_file "${comm}/result" 15; then
        FAIL "Test 3: B crashed during rollback scenario"
        record "FAIL: crash on fs-only rollback"
        kill -9 ${B_PID} 2>/dev/null; wait ${B_PID} 2>/dev/null || true
        umount_overlay; return
    fi

    local read_content
    read_content=$(get_result "${comm}/result" "read_content")
    local fresh_content
    fresh_content=$(cat "${MERGED}/rollback.txt" | head -1)

    INFO "  B reads via stale fd: '${read_content}'"
    INFO "  Fresh overlay read:   '${fresh_content}'"

    if [[ "${read_content}" != "${fresh_content}" ]]; then
        WARN "Test 3a: INCONSISTENCY — B sees '${read_content}', overlay shows '${fresh_content}'"
        WARN "  → Semantic divergence when FS rollback lacks CRIU memory restore"
        record "WARN: semantic inconsistency on fs-only rollback (expected by design)"
    else
        PASS "Test 3a: B and overlay consistent after rollback"
        record "PASS: consistent after rollback"
    fi

    local write_ok
    write_ok=$(get_result "${comm}/result" "write_ok")
    if [[ "${write_ok}" == "1" ]]; then
        PASS "Test 3b: B can write after rollback (no crash)"
        record "PASS: write after rollback works"
    else
        FAIL "Test 3b: B's write failed after rollback"
        record "FAIL: write failed after rollback"
    fi

    kill -USR2 ${B_PID} 2>/dev/null
    wait ${B_PID} 2>/dev/null || true
    umount_overlay
}

# ================================================================
# 主流程
# ================================================================
main() {
    echo "=============================================="
    echo " Bystander FD Behavior Under Hot Layer Switch"
    echo "=============================================="

    trap cleanup_all EXIT

    setup_xfs_image
    build_ioctl_tool
    build_bystander

    # 先做基础验证
    if ! test_0_ioctl_basic; then
        FAIL "Basic ioctl test failed — aborting"
        echo "Check: dmesg | grep -i ovl | tail -20"
        return 1
    fi
    echo ""

    test_1_fd_survives
    echo ""
    test_2_multi_generation
    echo ""
    test_3_fs_only_rollback

    echo ""
    echo "=============================================="
    echo " Summary"
    echo "=============================================="
    for r in "${RESULTS[@]}"; do
        echo "  ${r}"
    done
    echo ""
    echo "Key design insights:"
    echo "  - B's fd won't crash (VFS refcount protects mount lifetime)"
    echo "  - write() on overlay fd goes through ovl_write_iter → lazy switch"
    echo "  - Lazy switch: write lands in new upper (correct)"
    echo "  - No lazy switch: write lands in old upper (semantic error)"
    echo "  - FS-only rollback without CRIU = semantic inconsistency"
    echo "  - CRIU restore rebuilds all FDs → no stale fd problem"
    echo "=============================================="
    echo ""
    echo "Kernel logs: dmesg | grep -i ovl | tail -30"
}

main "$@"
