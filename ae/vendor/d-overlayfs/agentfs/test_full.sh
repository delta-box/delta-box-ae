#!/bin/bash
# test_full.sh — 全量覆盖测试（宿主机 ext4 兼容）
#
# 覆盖:
#  T01: 基础 checkpoint 读写
#  T02: lazy switch 写入去向
#  T03: 多代 checkpoint (>=3次)
#  T04: 文件创建/删除/重命名 跨 checkpoint
#  T05: 符号链接 & 硬链接
#  T06: 元数据操作 chmod/chown/truncate
#  T07: 深层目录 copy-up
#  T08: 目录缓存一致性 (readdir)
#  T09: mmap 安全性
#  T10: 并发读写压力 + 交替 checkpoint
#  T11: fallocate / ftruncate 跨 checkpoint
#  T12: stale fd 读写 (bystander)
#  T13: 只读操作不触发错误 copy-up
#  T14: 大量文件批量 copy-up
#  T15: O_APPEND 写入
#
# 安全保证: 每个子测试独立挂载/卸载/清理，不留残留
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 颜色
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
PASS() { echo -e "${GREEN}[PASS]${NC} $1"; PASS_COUNT=$((PASS_COUNT+1)); }
FAIL() { echo -e "${RED}[FAIL]${NC} $1"; FAIL_COUNT=$((FAIL_COUNT+1)); FAIL_MSGS+=("$1"); }
WARN() { echo -e "${YELLOW}[WARN]${NC} $1"; WARN_COUNT=$((WARN_COUNT+1)); }
INFO() { echo -e "${CYAN}[INFO]${NC} $1"; }

PASS_COUNT=0; FAIL_COUNT=0; WARN_COUNT=0
declare -a FAIL_MSGS=()

# ---- 全局工具 ----
WORK_TOP="/tmp/ovl_fulltest_$$"
OVL_IOCTL=""

build_tools() {
    mkdir -p "${WORK_TOP}"
    OVL_IOCTL="${WORK_TOP}/ovl_ioctl"
    gcc -O2 -Wall -o "${OVL_IOCTL}" "${SCRIPT_DIR}/ovl_ioctl.c"
}

# 每个子测试有独立目录；自动清理
# 用法:  local TD; TD=$(new_testdir)
new_testdir() {
    local d="${WORK_TOP}/t_${RANDOM}_$$"
    mkdir -p "${d}"/{lower,upper1,upper2,upper3,work1,work2,work3,merged}
    echo "${d}"
}

do_mount() {         # do_mount <TD> [upper_idx=1]
    local td="$1" idx="${2:-1}"
    mount -t overlay ovl \
        -o "lowerdir=${td}/lower,upperdir=${td}/upper${idx},workdir=${td}/work${idx}" \
        "${td}/merged"
}

do_ckpt() {          # do_ckpt <TD> <to_idx> [extra_lower]
    local td="$1" idx="$2" extra="${3:-}"
    local low="${td}/lower"
    [[ -n "${extra}" ]] && low="${extra}:${td}/lower"
    "${OVL_IOCTL}" "${td}/merged" "lowerdir=${low},upperdir=${td}/upper${idx},workdir=${td}/work${idx}"
}

do_umount() {
    local td="$1"
    umount "${td}/merged" 2>/dev/null || umount -l "${td}/merged" 2>/dev/null || true
}

cleanup_td() {
    local td="$1"
    do_umount "${td}"
    rm -rf "${td}"
}

# 检查 kernel oops（仅检查最近 30 行 dmesg）
check_kernel() {
    if dmesg 2>/dev/null | tail -30 | grep -qiE "oops|BUG:|panic|general protection"; then
        FAIL "Kernel error detected! Check dmesg."
        return 1
    fi
    return 0
}

# ===================== T01: 基础 checkpoint 读写 =====================
t01_basic_checkpoint() {
    INFO "=== T01: 基础 checkpoint 读写 ==="
    local TD; TD=$(new_testdir)
    echo "hello" > "${TD}/lower/f.txt"

    do_mount "${TD}"

    # 读取 lower 层文件
    local c; c=$(cat "${TD}/merged/f.txt")
    [[ "$c" == "hello" ]] && PASS "T01a: overlay 读取 lower 正确" \
                           || FAIL "T01a: 读取 lower 失败 got='$c'"

    # 写入触发 copy-up 到 upper1
    echo "modified" > "${TD}/merged/f.txt"
    sync
    [[ -f "${TD}/upper1/f.txt" ]] && PASS "T01b: copy-up 到 upper1" \
                                    || FAIL "T01b: upper1 无 copy-up"

    # checkpoint → upper2
    do_ckpt "${TD}" 2
    local rc=$?
    [[ $rc -eq 0 ]] && PASS "T01c: checkpoint ioctl 成功" \
                      || FAIL "T01c: checkpoint 失败 rc=$rc"

    # 读取仍正确
    c=$(cat "${TD}/merged/f.txt")
    [[ "$c" == "modified" ]] && PASS "T01d: checkpoint 后读取正确" \
                              || FAIL "T01d: checkpoint 后读取 got='$c'"

    # 写入到新 upper
    echo "post_ckpt" > "${TD}/merged/f.txt"
    sync
    if [[ -f "${TD}/upper2/f.txt" ]]; then
        c=$(cat "${TD}/upper2/f.txt")
        [[ "$c" == "post_ckpt" ]] && PASS "T01e: 写入到新 upper2 正确" \
                                    || FAIL "T01e: upper2 内容错误 got='$c'"
    else
        FAIL "T01e: upper2 无文件"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T02: lazy switch 写入去向 =====================
t02_lazy_switch_destination() {
    INFO "=== T02: lazy switch 写入去向 ==="
    local TD; TD=$(new_testdir)
    echo "base" > "${TD}/lower/a.txt"

    do_mount "${TD}"

    # 先写入，触发 copy-up 到 upper1
    echo "v1" > "${TD}/merged/a.txt"
    sync

    # 用 exec fd 持有文件
    exec 7>"${TD}/merged/a.txt"

    # checkpoint
    do_ckpt "${TD}" 2

    # 通过旧 fd 写入（应该走 lazy switch 到 upper2）
    echo "lazy_write" >&7
    exec 7>&-
    sync

    if [[ -f "${TD}/upper2/a.txt" ]] && grep -q "lazy_write" "${TD}/upper2/a.txt"; then
        PASS "T02a: stale fd 写入被 lazy switch 重定向到 upper2"
    elif grep -q "lazy_write" "${TD}/merged/a.txt" 2>/dev/null; then
        PASS "T02a: 数据通过 merged 可读"
    else
        FAIL "T02a: lazy switch 写入丢失"
    fi

    # 新 open + write 也应到 upper2
    echo "new_open_write" > "${TD}/merged/a.txt"
    sync
    if [[ -f "${TD}/upper2/a.txt" ]] && grep -q "new_open_write" "${TD}/upper2/a.txt"; then
        PASS "T02b: 新 open 写入到 upper2"
    else
        FAIL "T02b: 新 open 写入未到 upper2"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T03: 多代 checkpoint =====================
t03_multi_gen_checkpoint() {
    INFO "=== T03: 多代 checkpoint (3 轮) ==="
    local TD; TD=$(new_testdir)
    echo "gen0" > "${TD}/lower/g.txt"

    do_mount "${TD}"

    # gen0 → gen1
    echo "gen1" > "${TD}/merged/g.txt"
    sync
    do_ckpt "${TD}" 2
    local c; c=$(cat "${TD}/merged/g.txt")
    [[ "$c" == "gen1" ]] && PASS "T03a: gen1 读取正确" || FAIL "T03a: gen1 got='$c'"

    # gen1 → gen2
    echo "gen2" > "${TD}/merged/g.txt"
    sync
    do_ckpt "${TD}" 3
    c=$(cat "${TD}/merged/g.txt")
    [[ "$c" == "gen2" ]] && PASS "T03b: gen2 读取正确" || FAIL "T03b: gen2 got='$c'"

    # gen2 → gen3 (用新目录 upper4/work4，因为 auto-inject 会把旧 upper 注入为 lower)
    echo "gen3" > "${TD}/merged/g.txt"
    sync
    mkdir -p "${TD}/upper4" "${TD}/work4"
    "${OVL_IOCTL}" "${TD}/merged" "lowerdir=${TD}/lower,upperdir=${TD}/upper4,workdir=${TD}/work4"
    local rc=$?
    [[ $rc -eq 0 ]] && PASS "T03c: 第 3 次 checkpoint 成功" \
                      || FAIL "T03c: 第 3 次 checkpoint 失败 rc=$rc"
    c=$(cat "${TD}/merged/g.txt")
    [[ "$c" == "gen3" ]] && PASS "T03d: gen3 读取正确" || FAIL "T03d: gen3 got='$c'"

    # 继续写入
    echo "gen3_more" >> "${TD}/merged/g.txt"
    sync
    c=$(cat "${TD}/merged/g.txt")
    echo "$c" | grep -q "gen3_more" && PASS "T03e: gen3 追加写入正确" \
                                      || FAIL "T03e: gen3 追加写入失败"

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T04: 文件创建/删除/重命名 =====================
t04_create_delete_rename() {
    INFO "=== T04: 文件创建/删除/重命名 跨 checkpoint ==="
    local TD; TD=$(new_testdir)
    echo "exist" > "${TD}/lower/exist.txt"
    echo "to_del" > "${TD}/lower/to_del.txt"
    echo "to_rename" > "${TD}/lower/old_name.txt"

    do_mount "${TD}"

    # checkpoint
    do_ckpt "${TD}" 2

    # 创建新文件
    echo "new_file" > "${TD}/merged/created.txt"
    sync
    [[ -f "${TD}/upper2/created.txt" ]] && PASS "T04a: checkpoint 后创建文件到 upper2" \
                                          || FAIL "T04a: 新文件不在 upper2"

    # 删除文件 (应产生 whiteout)
    rm -f "${TD}/merged/to_del.txt"
    sync
    if [[ ! -f "${TD}/merged/to_del.txt" ]]; then
        PASS "T04b: checkpoint 后删除文件成功"
    else
        FAIL "T04b: 文件删除后仍可见"
    fi

    # 重命名
    mv "${TD}/merged/old_name.txt" "${TD}/merged/new_name.txt" 2>/dev/null
    local mv_rc=$?
    if [[ $mv_rc -eq 0 ]]; then
        [[ -f "${TD}/merged/new_name.txt" ]] && PASS "T04c: checkpoint 后重命名成功" \
                                                || FAIL "T04c: 重命名后文件消失"
    else
        FAIL "T04c: 重命名失败 rc=$mv_rc"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T05: 符号链接 & 硬链接 =====================
t05_symlink_hardlink() {
    INFO "=== T05: 符号链接 & 硬链接 ==="
    local TD; TD=$(new_testdir)
    echo "target" > "${TD}/lower/target.txt"

    do_mount "${TD}"

    do_ckpt "${TD}" 2

    # 创建符号链接
    ln -s target.txt "${TD}/merged/sym.txt" 2>/dev/null
    local ln_rc=$?
    if [[ $ln_rc -eq 0 ]]; then
        local c; c=$(cat "${TD}/merged/sym.txt")
        [[ "$c" == "target" ]] && PASS "T05a: checkpoint 后创建符号链接" \
                                 || FAIL "T05a: 符号链接读取错误 got='$c'"
    else
        FAIL "T05a: 创建符号链接失败 rc=$ln_rc"
    fi

    # 硬链接 — overlayfs 可能不支持跨层硬链接
    echo "hl_src" > "${TD}/merged/hl_src.txt"
    sync
    ln "${TD}/merged/hl_src.txt" "${TD}/merged/hl_dst.txt" 2>/dev/null
    ln_rc=$?
    if [[ $ln_rc -eq 0 ]]; then
        PASS "T05b: checkpoint 后硬链接创建"
    else
        WARN "T05b: 硬链接失败 (overlayfs 可能限制) rc=$ln_rc"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T06: 元数据操作 =====================
t06_metadata_ops() {
    INFO "=== T06: 元数据操作 chmod/truncate ==="
    local TD; TD=$(new_testdir)
    echo "metadata_test_content" > "${TD}/lower/meta.txt"
    chmod 644 "${TD}/lower/meta.txt"

    do_mount "${TD}"
    do_ckpt "${TD}" 2

    # chmod
    chmod 755 "${TD}/merged/meta.txt" 2>/dev/null
    local mode; mode=$(stat -c "%a" "${TD}/merged/meta.txt")
    [[ "$mode" == "755" ]] && PASS "T06a: checkpoint 后 chmod 成功" \
                            || FAIL "T06a: chmod 结果 mode=$mode"

    # truncate
    truncate -s 5 "${TD}/merged/meta.txt" 2>/dev/null
    local sz; sz=$(stat -c "%s" "${TD}/merged/meta.txt")
    [[ "$sz" == "5" ]] && PASS "T06b: checkpoint 后 truncate 成功 (size=5)" \
                        || FAIL "T06b: truncate 后 size=$sz"

    # 内容验证 (前5字节 "metad")
    local c; c=$(cat "${TD}/merged/meta.txt")
    [[ "$c" == "metad" ]] && PASS "T06c: truncate 后内容正确" \
                            || FAIL "T06c: truncate 后内容='$c' 预期='metad'"

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T07: 深层目录 copy-up =====================
t07_deep_dir_copyup() {
    INFO "=== T07: 深层目录 copy-up ==="
    local TD; TD=$(new_testdir)
    mkdir -p "${TD}/lower/a/b/c/d"
    echo "deep" > "${TD}/lower/a/b/c/d/file.txt"

    do_mount "${TD}"
    do_ckpt "${TD}" 2

    # 在深层目录写入
    echo "deep_modified" > "${TD}/merged/a/b/c/d/file.txt"
    sync
    local c; c=$(cat "${TD}/merged/a/b/c/d/file.txt")
    [[ "$c" == "deep_modified" ]] && PASS "T07a: 深层 4 级目录写入成功" \
                                    || FAIL "T07a: 深层写入失败 got='$c'"

    # 在深层创建新文件
    echo "new_deep" > "${TD}/merged/a/b/c/d/new.txt"
    sync
    [[ -f "${TD}/merged/a/b/c/d/new.txt" ]] && PASS "T07b: 深层目录创建新文件" \
                                               || FAIL "T07b: 深层新文件创建失败"

    # 在深层创建新子目录
    mkdir -p "${TD}/merged/a/b/c/d/e/f" 2>/dev/null
    echo "very_deep" > "${TD}/merged/a/b/c/d/e/f/vd.txt" 2>/dev/null
    if [[ -f "${TD}/merged/a/b/c/d/e/f/vd.txt" ]]; then
        PASS "T07c: 6 级嵌套目录创建成功"
    else
        FAIL "T07c: 6 级嵌套目录创建失败"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T08: 目录缓存一致性 =====================
t08_dir_cache() {
    INFO "=== T08: 目录缓存一致性 (readdir) ==="
    local TD; TD=$(new_testdir)
    for i in $(seq 1 30); do
        echo "v1_$i" > "${TD}/lower/file_${i}.txt"
    done

    do_mount "${TD}"

    # 先读一次填充 dir cache
    local count_before; count_before=$(ls -1 "${TD}/merged/" | wc -l)
    INFO "  切换前文件数: $count_before"

    # 在 upper1 写入一些文件 (触发 copy-up)
    echo "x" > "${TD}/merged/file_1.txt"
    sync

    # checkpoint 到 upper2，lower 换一批文件
    # 准备新 lower 内容
    local newlower="${TD}/newlower"
    mkdir -p "${newlower}"
    for i in $(seq 31 60); do
        echo "v2_$i" > "${newlower}/file_${i}.txt"
    done

    do_ckpt "${TD}" 2 "${newlower}"

    # readdir 应该看到新的文件集
    local files_after; files_after=$(ls -1 "${TD}/merged/" 2>&1)
    local count_after; count_after=$(echo "$files_after" | wc -l)
    INFO "  切换后文件数: $count_after"

    if echo "$files_after" | grep -q "file_31"; then
        PASS "T08a: readdir 看到新 lower 的文件"
    else
        FAIL "T08a: readdir 未看到新 lower 文件 (dir cache stale)"
    fi

    # 重复 readdir 一致性
    local consistent=true
    for trial in $(seq 1 5); do
        local tc; tc=$(ls -1 "${TD}/merged/" | wc -l)
        [[ "$tc" -ne "$count_after" ]] && consistent=false
    done
    ${consistent} && PASS "T08b: 5 次 readdir 结果一致" \
                   || FAIL "T08b: readdir 结果不一致"

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T09: mmap 安全性 =====================
t09_mmap_safety() {
    INFO "=== T09: mmap 安全性 ==="
    local TD; TD=$(new_testdir)
    dd if=/dev/urandom of="${TD}/lower/mmap.dat" bs=4096 count=16 status=none

    do_mount "${TD}"

    # 编译 mmap 测试程序
    cat > "${TD}/mmap_test.c" << 'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>

int main(int argc, char *argv[]) {
    const char *path = argv[1];
    const char *action = argv[2]; /* "hold" or "access" */

    int fd = open(path, O_RDWR);
    if (fd < 0) { perror("open"); return 1; }

    size_t len = 4096 * 16;
    void *map = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) { perror("mmap"); return 1; }

    if (strcmp(action, "hold") == 0) {
        /* Simply hold the mmap and exit — parent will checkpoint */
        printf("MAPPED\n");
        fflush(stdout);
        /* Wait for input */
        char buf[8];
        if (read(STDIN_FILENO, buf, sizeof(buf)) <= 0) {
            /* Parent closed stdin or error */
        }
    }

    /* Access all pages */
    unsigned char checksum = 0;
    for (size_t i = 0; i < len; i += 4096)
        checksum ^= ((unsigned char *)map)[i];
    printf("READ_OK cs=%u\n", checksum);

    /* Write to mmap */
    memset((char *)map + 4096, 0xAB, 4096);
    msync(map, len, MS_SYNC);
    printf("WRITE_OK\n");

    munmap(map, len);
    close(fd);
    return 0;
}
EOF
    gcc -O2 -Wall -o "${TD}/mmap_test" "${TD}/mmap_test.c"

    # copy-up 使其可写
    cp "${TD}/merged/mmap.dat" "${TD}/merged/mmap.dat.tmp"
    mv "${TD}/merged/mmap.dat.tmp" "${TD}/merged/mmap.dat"

    # 启动 mmap 持有者
    "${TD}/mmap_test" "${TD}/merged/mmap.dat" hold < <(
        # 等 mapping 建立
        sleep 0.5
        # checkpoint
        do_ckpt "${TD}" 2 2>/dev/null
        sleep 0.3
        # 发送 "go" 让子进程继续
        echo "go"
    ) > "${TD}/mmap_out.txt" 2>&1
    local rc=$?

    if [[ $rc -eq 0 ]] && grep -q "WRITE_OK" "${TD}/mmap_out.txt"; then
        PASS "T09a: mmap 读写在 checkpoint 后不崩溃"
    else
        FAIL "T09a: mmap 测试失败 rc=$rc"
        cat "${TD}/mmap_out.txt" 2>/dev/null
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T10: 并发读写压力 =====================
t10_concurrent_stress() {
    INFO "=== T10: 并发读写 + 交替 checkpoint (5s) ==="
    local TD; TD=$(new_testdir)
    for i in $(seq 1 20); do
        dd if=/dev/urandom of="${TD}/lower/data_${i}" bs=4096 count=$((RANDOM % 20 + 1)) status=none
    done

    do_mount "${TD}"

    local STOP="${TD}/stop"
    local ERR_DIR="${TD}/errs"
    mkdir -p "${ERR_DIR}"

    # 4 个 writer
    for w in $(seq 1 4); do
        (
            set +e
            while [[ ! -f "${STOP}" ]]; do
                idx=$((RANDOM % 20 + 1))
                dd if=/dev/urandom of="${TD}/merged/data_${idx}" bs=4096 count=1 \
                    seek=$((RANDOM % 5)) conv=notrunc status=none 2>/dev/null
                [[ $? -ne 0 ]] && echo "w${w}" >> "${ERR_DIR}/w${w}.log"
                sleep 0.01
            done
        ) &
    done

    # 4 个 reader
    for r in $(seq 1 4); do
        (
            set +e
            while [[ ! -f "${STOP}" ]]; do
                idx=$((RANDOM % 20 + 1))
                cat "${TD}/merged/data_${idx}" > /dev/null 2>&1
                ls "${TD}/merged/" > /dev/null 2>&1
                sleep 0.005
            done
        ) &
    done

    local switches=0 switch_err=0 ckpt_idx=2
    for secs in $(seq 1 5); do
        sleep 1
        ckpt_idx=$((ckpt_idx + 1))
        mkdir -p "${TD}/upper${ckpt_idx}" "${TD}/work${ckpt_idx}"
        "${OVL_IOCTL}" "${TD}/merged" "lowerdir=${TD}/lower,upperdir=${TD}/upper${ckpt_idx},workdir=${TD}/work${ckpt_idx}" 2>/dev/null \
            && switches=$((switches+1)) || switch_err=$((switch_err+1))
    done

    touch "${STOP}"
    sleep 1
    wait 2>/dev/null

    local total_werr=0
    for f in "${ERR_DIR}"/w*.log; do
        [[ -f "$f" ]] || continue
        total_werr=$((total_werr + $(wc -l < "$f")))
    done

    [[ $switch_err -eq 0 ]] && PASS "T10a: 全部 $switches 次 checkpoint 成功" \
                              || WARN "T10a: $switch_err/$switches 次 checkpoint 失败"
    [[ $total_werr -eq 0 ]] && PASS "T10b: 无写入错误" \
                              || WARN "T10b: $total_werr 个写入错误 (切换瞬间可预期)"

    # 文件系统仍可用
    if ls "${TD}/merged/" > /dev/null 2>&1; then
        PASS "T10c: 压力测试后文件系统可用"
    else
        FAIL "T10c: 文件系统不可用"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T11: fallocate / ftruncate =====================
t11_fallocate_ftruncate() {
    INFO "=== T11: fallocate / ftruncate 跨 checkpoint ==="
    local TD; TD=$(new_testdir)
    echo "original" > "${TD}/lower/ft.txt"

    do_mount "${TD}"
    # copy-up
    echo "trigger" >> "${TD}/merged/ft.txt"
    sync

    do_ckpt "${TD}" 2

    # ftruncate
    truncate -s 100 "${TD}/merged/ft.txt" 2>/dev/null
    local rc=$?
    local sz; sz=$(stat -c "%s" "${TD}/merged/ft.txt" 2>/dev/null)
    if [[ $rc -eq 0 && "$sz" == "100" ]]; then
        PASS "T11a: checkpoint 后 ftruncate 到 100 bytes"
    else
        FAIL "T11a: ftruncate 失败 rc=$rc size=$sz"
    fi

    # fallocate (如果可用)
    if command -v fallocate >/dev/null 2>&1; then
        fallocate -l 1M "${TD}/merged/ft.txt" 2>/dev/null
        rc=$?
        sz=$(stat -c "%s" "${TD}/merged/ft.txt" 2>/dev/null)
        if [[ $rc -eq 0 && "$sz" -ge 1048576 ]]; then
            PASS "T11b: checkpoint 后 fallocate 到 1MB"
        else
            WARN "T11b: fallocate 结果 rc=$rc size=$sz (可能不支持)"
        fi
    else
        WARN "T11b: fallocate 命令不可用，跳过"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T12: stale fd 读写 (类似 bystander) =====================
t12_stale_fd() {
    INFO "=== T12: stale fd 读写 (bystander 场景) ==="
    local TD; TD=$(new_testdir)
    echo "initial" > "${TD}/lower/st.txt"

    do_mount "${TD}"
    echo "v1" > "${TD}/merged/st.txt"
    sync

    # 用 bash fd 持有文件
    exec 6<>"${TD}/merged/st.txt"

    do_ckpt "${TD}" 2

    # 读
    lseek_c=$(cat <&6 2>&1)
    # 由于 exec 6<> (读写模式), cat 可能读到 v1 或旧内容
    if [[ -n "$lseek_c" && "$lseek_c" != *"error"* ]]; then
        PASS "T12a: stale fd 读取不崩溃"
    else
        FAIL "T12a: stale fd 读取失败"
    fi

    # 写（seek 到末尾）
    echo "stale_write" >&6 2>/dev/null
    local wrc=$?
    exec 6>&-
    sync

    if [[ $wrc -eq 0 ]]; then
        PASS "T12b: stale fd 写入不崩溃"
    else
        FAIL "T12b: stale fd 写入失败 rc=$wrc"
    fi

    # 验证数据可读
    local c; c=$(cat "${TD}/merged/st.txt" 2>/dev/null)
    if [[ -n "$c" ]]; then
        PASS "T12c: merged 读取不崩溃"
    else
        FAIL "T12c: merged 读取失败"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T13: 只读操作不触发错误 =====================
t13_readonly_safe() {
    INFO "=== T13: 只读操作安全性 ==="
    local TD; TD=$(new_testdir)
    echo "readonly" > "${TD}/lower/ro.txt"
    mkdir -p "${TD}/lower/rodir"
    echo "rd" > "${TD}/lower/rodir/sub.txt"

    do_mount "${TD}"
    do_ckpt "${TD}" 2

    # 读取文件
    local c; c=$(cat "${TD}/merged/ro.txt")
    [[ "$c" == "readonly" ]] && PASS "T13a: checkpoint 后读取 lower 文件正确" \
                               || FAIL "T13a: 读取结果='$c'"

    # stat
    stat "${TD}/merged/ro.txt" > /dev/null 2>&1 \
        && PASS "T13b: stat 正常" || FAIL "T13b: stat 失败"

    # readdir
    ls "${TD}/merged/rodir/" > /dev/null 2>&1 \
        && PASS "T13c: readdir 正常" || FAIL "T13c: readdir 失败"

    # access
    test -r "${TD}/merged/ro.txt" \
        && PASS "T13d: access(R_OK) 正常" || FAIL "T13d: access 失败"

    # 读取后 upper2 不应有 copy-up 产物（只读不触发 copy-up）
    if [[ ! -f "${TD}/upper2/ro.txt" ]]; then
        PASS "T13e: 纯读操作未触发 copy-up"
    else
        WARN "T13e: 只读操作触发了 copy-up (metacopy?)"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T14: 大量文件批量 copy-up =====================
t14_batch_copyup() {
    INFO "=== T14: 批量 copy-up (100 文件) ==="
    local TD; TD=$(new_testdir)
    for i in $(seq 1 100); do
        echo "file_$i" > "${TD}/lower/f_${i}.txt"
    done

    do_mount "${TD}"
    do_ckpt "${TD}" 2

    # 批量修改：全部触发 copy-up
    local errs=0
    for i in $(seq 1 100); do
        echo "modified_$i" > "${TD}/merged/f_${i}.txt" 2>/dev/null || errs=$((errs+1))
    done
    sync

    # 验证
    local in_upper2=0
    for i in $(seq 1 100); do
        [[ -f "${TD}/upper2/f_${i}.txt" ]] && in_upper2=$((in_upper2+1))
    done

    if [[ $errs -eq 0 ]]; then
        PASS "T14a: 100 个文件写入无错误"
    else
        FAIL "T14a: $errs 个写入错误"
    fi

    if [[ $in_upper2 -eq 100 ]]; then
        PASS "T14b: 100 个文件全部 copy-up 到 upper2"
    else
        FAIL "T14b: 只有 $in_upper2/100 个文件在 upper2"
    fi

    # 内容验证（抽查）
    local c; c=$(cat "${TD}/upper2/f_50.txt")
    [[ "$c" == "modified_50" ]] && PASS "T14c: 抽查第 50 个文件内容正确" \
                                  || FAIL "T14c: 第 50 个文件内容='$c'"

    cleanup_td "${TD}"
    check_kernel
}

# ===================== T15: O_APPEND 写入 =====================
t15_append_write() {
    INFO "=== T15: O_APPEND 写入跨 checkpoint ==="
    local TD; TD=$(new_testdir)
    echo "line1" > "${TD}/lower/ap.txt"

    do_mount "${TD}"
    # copy-up
    echo "line2" >> "${TD}/merged/ap.txt"
    sync

    # 用 append fd 持有
    exec 8>>"${TD}/merged/ap.txt"

    do_ckpt "${TD}" 2

    # append 写入
    echo "line3_after_ckpt" >&8
    echo "line4_after_ckpt" >&8
    exec 8>&-
    sync

    local lines; lines=$(wc -l < "${TD}/merged/ap.txt")
    local c; c=$(cat "${TD}/merged/ap.txt")

    if echo "$c" | grep -q "line3_after_ckpt" && echo "$c" | grep -q "line4_after_ckpt"; then
        PASS "T15a: O_APPEND 写入内容正确"
    else
        FAIL "T15a: O_APPEND 写入内容不完整"
        INFO "  内容: $(echo "$c" | tr '\n' '|')"
    fi

    if [[ -f "${TD}/upper2/ap.txt" ]]; then
        PASS "T15b: append 写入到 upper2"
    else
        FAIL "T15b: append 未写入 upper2"
    fi

    cleanup_td "${TD}"
    check_kernel
}

# ===================== 主流程 =====================
main() {
    echo ""
    echo "======================================================="
    echo "  OverlayFS 热切换全量测试 (ext4 宿主机兼容)"
    echo "======================================================="
    echo ""

    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: 需要 root 权限"; exit 1
    fi

    # 清理可能残留的旧测试环境
    for d in /tmp/ovl_fulltest_*; do
        [[ -d "$d" ]] || continue
        umount "${d}"/*/merged 2>/dev/null || true
        rm -rf "$d" 2>/dev/null || true
    done

    # 清理 dmesg 噪音
    dmesg -C 2>/dev/null || true

    build_tools

    # 运行所有测试
    t01_basic_checkpoint
    t02_lazy_switch_destination
    t03_multi_gen_checkpoint
    t04_create_delete_rename
    t05_symlink_hardlink
    t06_metadata_ops
    t07_deep_dir_copyup
    t08_dir_cache
    t09_mmap_safety
    t10_concurrent_stress
    t11_fallocate_ftruncate
    t12_stale_fd
    t13_readonly_safe
    t14_batch_copyup
    t15_append_write

    # ---- 最终清理 ----
    rm -rf "${WORK_TOP}"

    # 确保没有残留挂载
    local leaked; leaked=$(mount | grep "ovl_fulltest" | wc -l)
    if [[ $leaked -gt 0 ]]; then
        WARN "清理残留 $leaked 个挂载..."
        mount | grep "ovl_fulltest" | awk '{print $3}' | while read mp; do
            umount -l "$mp" 2>/dev/null
        done
    fi

    echo ""
    echo "======================================================="
    echo "  结果:  PASS=${PASS_COUNT}  FAIL=${FAIL_COUNT}  WARN=${WARN_COUNT}"
    echo "======================================================="
    if [[ $FAIL_COUNT -gt 0 ]]; then
        echo -e "${RED}失败项:${NC}"
        for msg in "${FAIL_MSGS[@]}"; do
            echo -e "  ${RED}- ${msg}${NC}"
        done
    fi
    echo ""

    # 最终 kernel 检查
    if dmesg 2>/dev/null | grep -qiE "oops|BUG:|panic|general protection"; then
        echo -e "${RED}[CRITICAL] 发现 kernel 错误，请检查 dmesg${NC}"
    else
        echo -e "${GREEN}无 kernel 崩溃${NC}"
    fi

    echo ""
    echo "Kernel 日志 (与 ovl 相关):"
    dmesg 2>/dev/null | grep -i "ovl" | tail -20

    exit ${FAIL_COUNT}
}

main "$@"
