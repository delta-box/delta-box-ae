/*
 * bystander_test.c — 旁观进程 FD 行为测试
 *
 * 用法: bystander_test <overlay_file> <comm_dir> <mode>
 *   mode=hold    打开文件后等待信号，收到信号后读写并报告
 *   mode=write   打开文件后等待信号，收到信号后写入并报告
 *
 * 通信协议（文件轮询）:
 *   启动后写 <comm_dir>/ready  (内容: PID)
 *   等待 SIGUSR1
 *   收到信号后执行操作，写结果到 <comm_dir>/result
 *   等待 SIGUSR2（第二轮操作）
 *   执行第二轮，写结果到 <comm_dir>/result2
 *   退出
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>

static volatile sig_atomic_t phase = 0;
static void on_usr1(int sig) { (void)sig; phase = 1; }
static void on_usr2(int sig) { (void)sig; phase = 2; }

static void write_file(const char *path, const char *fmt, ...) {
    va_list ap;
    FILE *f = fopen(path, "w");
    if (!f) return;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fclose(f);
}

int main(int argc, char *argv[])
{
    if (argc < 3) {
        fprintf(stderr, "Usage: %s <overlay_file> <comm_dir>\n", argv[0]);
        return 1;
    }

    const char *ovl_file = argv[1];
    const char *comm_dir = argv[2];

    char ready_path[512], result_path[512], result2_path[512];
    snprintf(ready_path,  sizeof(ready_path),  "%s/ready",   comm_dir);
    snprintf(result_path, sizeof(result_path), "%s/result",  comm_dir);
    snprintf(result2_path, sizeof(result2_path), "%s/result2", comm_dir);

    /* Open the overlay file — this is the FD we're testing */
    int fd = open(ovl_file, O_RDWR);
    if (fd < 0) {
        perror("open overlay file");
        return 1;
    }

    /* Record initial state */
    struct stat st_before;
    fstat(fd, &st_before);

    char buf_before[4096] = {0};
    ssize_t n = read(fd, buf_before, sizeof(buf_before) - 1);
    if (n < 0) n = 0;
    /* trim newline */
    if (n > 0 && buf_before[n-1] == '\n') buf_before[n-1] = '\0';

    /* Setup signals */
    signal(SIGUSR1, on_usr1);
    signal(SIGUSR2, on_usr2);

    /* Signal ready */
    write_file(ready_path, "%d\n", getpid());
    fprintf(stderr, "[B] ready (pid=%d fd=%d dev=%lu:%lu ino=%lu)\n",
            getpid(), fd,
            (unsigned long)major(st_before.st_dev),
            (unsigned long)minor(st_before.st_dev),
            (unsigned long)st_before.st_ino);

    /* === Phase 1: wait for SIGUSR1 (sent after checkpoint) === */
    while (phase < 1)
        usleep(50000);

    fprintf(stderr, "[B] phase1: attempting read + write via original fd...\n");

    /* Try to read via original fd */
    lseek(fd, 0, SEEK_SET);
    char buf_phase1[4096] = {0};
    ssize_t r1 = read(fd, buf_phase1, sizeof(buf_phase1) - 1);
    int read_errno = (r1 < 0) ? errno : 0;
    if (r1 > 0 && buf_phase1[r1-1] == '\n') buf_phase1[r1-1] = '\0';

    /* Try to write via original fd */
    const char *write_data = "BYSTANDER_PHASE1_WRITE\n";
    lseek(fd, 0, SEEK_END);
    ssize_t w1 = write(fd, write_data, strlen(write_data));
    int write_errno = (w1 < 0) ? errno : 0;
    fsync(fd);

    /* Check state after */
    struct stat st_after;
    fstat(fd, &st_after);

    write_file(result_path,
        "phase=1\n"
        "initial_content=%s\n"
        "read_ok=%d\n"
        "read_content=%s\n"
        "read_errno=%d\n"
        "write_ok=%d\n"
        "write_bytes=%zd\n"
        "write_errno=%d\n"
        "dev_before=%lu:%lu\n"
        "dev_after=%lu:%lu\n"
        "ino_before=%lu\n"
        "ino_after=%lu\n",
        buf_before,
        (r1 >= 0) ? 1 : 0, buf_phase1, read_errno,
        (w1 >= 0) ? 1 : 0, w1, write_errno,
        (unsigned long)major(st_before.st_dev), (unsigned long)minor(st_before.st_dev),
        (unsigned long)major(st_after.st_dev), (unsigned long)minor(st_after.st_dev),
        (unsigned long)st_before.st_ino, (unsigned long)st_after.st_ino);

    fprintf(stderr, "[B] phase1 done: read=%zd write=%zd\n", r1, w1);

    /* === Phase 2: wait for SIGUSR2 (optional second checkpoint) === */
    while (phase < 2)
        usleep(50000);

    fprintf(stderr, "[B] phase2: re-read + re-write...\n");

    lseek(fd, 0, SEEK_SET);
    char buf_phase2[4096] = {0};
    ssize_t r2 = read(fd, buf_phase2, sizeof(buf_phase2) - 1);
    int read2_errno = (r2 < 0) ? errno : 0;

    const char *write_data2 = "BYSTANDER_PHASE2_WRITE\n";
    lseek(fd, 0, SEEK_END);
    ssize_t w2 = write(fd, write_data2, strlen(write_data2));
    int write2_errno = (w2 < 0) ? errno : 0;
    fsync(fd);

    struct stat st_phase2;
    fstat(fd, &st_phase2);

    write_file(result2_path,
        "phase=2\n"
        "read_ok=%d\n"
        "read_bytes=%zd\n"
        "read_errno=%d\n"
        "write_ok=%d\n"
        "write_bytes=%zd\n"
        "write_errno=%d\n"
        "dev_now=%lu:%lu\n"
        "ino_now=%lu\n",
        (r2 >= 0) ? 1 : 0, r2, read2_errno,
        (w2 >= 0) ? 1 : 0, w2, write2_errno,
        (unsigned long)major(st_phase2.st_dev), (unsigned long)minor(st_phase2.st_dev),
        (unsigned long)st_phase2.st_ino);

    fprintf(stderr, "[B] phase2 done: read=%zd write=%zd\n", r2, w2);

    close(fd);
    return 0;
}
