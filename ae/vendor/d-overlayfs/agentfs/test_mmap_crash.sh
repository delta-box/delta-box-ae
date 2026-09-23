#!/bin/bash
# test_mmap_crash.sh - 验证热切换后 mmap 映射是否崩溃
#
# 核心问题: ovl_mmap() 将 backing_file_mmap(realfile, ...) 映射到用户空间
# 热切换后 realfile 变成旧层的文件，如果旧层被卸载或文件被替换，
# 用户空间 mmap 区域的页表仍指向旧后端文件 → page fault 时可能 crash
#
# 测试步骤:
# 1. 在 lower 层创建文件，overlay 挂载后 mmap 它
# 2. 执行热切换到新的 lower/upper
# 3. 访问 mmap 区域验证是否 crash
# 4. 写入 mmap 区域验证 lazy switch + mmap 交互
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

TEST_NAME="mmap_crash"
WORK_BASE="/tmp/ovl_test_mmap_$$"
trap_cleanup

main() {
    INFO "=== Test: mmap crash after hot switch ==="
    
    setup_xfs_image
    setup_dirs
    build_ioctl_tool
    
    # 准备 lower1 的测试文件 (4KB 对齐，方便观察 page fault)
    dd if=/dev/urandom of="${LOWER1}/testfile" bs=4096 count=16 status=none
    md5_orig=$(md5sum "${LOWER1}/testfile" | awk '{print $1}')
    
    mount_overlay "${LOWER1}" "${UPPER1}" "${WORK1}"
    
    # 用 C 程序做 mmap 测试，避免 shell 无法控制 mmap
    cat > "${WORK_BASE}/mmap_test.c" << 'CEOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <signal.h>

static volatile int got_signal = 0;

static void sig_handler(int sig) {
    got_signal = sig;
}

int main(int argc, char *argv[])
{
    if (argc < 3) {
        fprintf(stderr, "Usage: %s <file> <phase>\n", argv[0]);
        return 1;
    }
    
    const char *filepath = argv[1];
    int phase = atoi(argv[2]);
    
    if (phase == 1) {
        /* Phase 1: mmap the file, write PID, wait for signal */
        int fd = open(filepath, O_RDWR);
        if (fd < 0) { perror("open"); return 1; }
        
        size_t len = 4096 * 16;
        void *map = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (map == MAP_FAILED) { perror("mmap"); close(fd); return 1; }
        
        /* Write PID so parent knows we're ready */
        printf("READY %d\n", getpid());
        fflush(stdout);
        
        /* 等待 SIGUSR1 → 热切换已完成 */
        signal(SIGUSR1, sig_handler);
        while (!got_signal) {
            usleep(10000);
        }
        
        /* Phase 2: 切换后访问 mmap 区域 */
        printf("POST_SWITCH: reading mmap region...\n");
        
        /* 强制触发 page fault: 读取每个页 */
        unsigned char checksum = 0;
        for (size_t i = 0; i < len; i += 4096) {
            checksum ^= ((unsigned char *)map)[i];
        }
        printf("READ_OK checksum=%u\n", checksum);
        
        /* 尝试写入 mmap 区域 (这是最危险的操作) */
        printf("POST_SWITCH: writing mmap region...\n");
        memset((char *)map + 4096, 0xAB, 4096);
        msync(map, len, MS_SYNC);
        printf("WRITE_OK\n");
        
        munmap(map, len);
        close(fd);
        printf("DONE\n");
        return 0;
    }
    
    return 1;
}
CEOF
    
    gcc -O2 -Wall -o "${WORK_BASE}/mmap_test" "${WORK_BASE}/mmap_test.c"
    
    # 复制到 upper 层使文件可写
    cp "${MERGED}/testfile" "${MERGED}/testfile.tmp"
    mv "${MERGED}/testfile.tmp" "${MERGED}/testfile"
    
    # 启动 mmap 进程
    "${WORK_BASE}/mmap_test" "${MERGED}/testfile" 1 &
    MMAP_PID=$!
    
    # 等待 mmap 进程就绪
    sleep 1
    
    # 准备新的 lower 层内容
    cp -a "${LOWER1}/testfile" "${LOWER2}/testfile"
    
    # 执行热切换
    INFO "Performing hot switch with mmap'd file..."
    do_checkpoint "lowerdir=${LOWER2},upperdir=${UPPER2},workdir=${WORK2}" || {
        WARN "Checkpoint failed, checking if expected"
    }
    
    # 通知 mmap 进程继续
    kill -USR1 ${MMAP_PID} 2>/dev/null || true
    
    # 等待 mmap 进程完成
    wait ${MMAP_PID} 2>/dev/null
    mmap_exit=$?
    
    if [[ ${mmap_exit} -eq 0 ]]; then
        PASS "mmap access after hot switch did not crash"
    else
        FAIL "mmap access crashed or errored (exit=${mmap_exit})"
    fi
    
    # 检查 dmesg 是否有 kernel oops
    if dmesg | tail -20 | grep -qi "oops\|BUG\|panic\|page fault"; then
        FAIL "Kernel error detected after mmap hot switch!"
        dmesg | tail -20
    else
        PASS "No kernel errors detected"
    fi
    
    INFO "=== mmap crash test complete ==="
}

main "$@"
