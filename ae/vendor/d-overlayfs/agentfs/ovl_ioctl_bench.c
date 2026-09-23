/*
 * ovl_ioctl_bench.c — In-process timed loop calling OVL_IOCTL_CHECKPOINT.
 *
 * Removes Python subprocess wrap overhead. Measures pure kernel ioctl
 * latency with CLOCK_MONOTONIC ns precision.
 *
 * Usage:
 *   ovl_ioctl_bench <merged_mount> <opt_A> <opt_B> <N_reps> <out_jsonl>
 *
 * Loop: alternates between opt_A and opt_B per rep so each call is a
 * real switch (not a same-target EBUSY).
 *
 * Output JSONL: {"rep": N, "ioctl_ns": X, "opt": "A"|"B"}
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <time.h>
#include <errno.h>
#include <linux/types.h>

#define OVL_IOCTL_MAGIC 'O'
struct ovl_checkpoint_args { char *options; __u32 options_len; };
#define OVL_IOCTL_CHECKPOINT _IOW(OVL_IOCTL_MAGIC, 1, struct ovl_checkpoint_args)

static long long ns_diff(struct timespec a, struct timespec b) {
    return (b.tv_sec - a.tv_sec) * 1000000000LL + (b.tv_nsec - a.tv_nsec);
}

int main(int argc, char **argv) {
    if (argc != 6) {
        fprintf(stderr, "Usage: %s <merged> <opt_A> <opt_B> <N> <out.jsonl>\n", argv[0]);
        return 1;
    }
    const char *merged = argv[1];
    const char *opt_A  = argv[2];
    const char *opt_B  = argv[3];
    int N = atoi(argv[4]);
    const char *out = argv[5];

    int fd = open(merged, O_RDONLY | O_DIRECTORY);
    if (fd < 0) { perror("open merged"); return 1; }

    FILE *fp = fopen(out, "w");
    if (!fp) { perror("fopen out"); return 1; }

    /* Warmup: 20 reps untimed */
    for (int i = 0; i < 20; i++) {
        const char *opt = (i % 2 == 0) ? opt_A : opt_B;
        struct ovl_checkpoint_args args = { .options = (char *)opt, .options_len = strlen(opt) };
        if (ioctl(fd, OVL_IOCTL_CHECKPOINT, &args) < 0) {
            fprintf(stderr, "warmup ioctl[%d] failed: %s\n", i, strerror(errno));
            return 2;
        }
    }

    /* Timed reps */
    struct timespec t0, t1;
    for (int i = 0; i < N; i++) {
        const char *opt = (i % 2 == 0) ? opt_A : opt_B;
        struct ovl_checkpoint_args args = { .options = (char *)opt, .options_len = strlen(opt) };
        clock_gettime(CLOCK_MONOTONIC, &t0);
        int r = ioctl(fd, OVL_IOCTL_CHECKPOINT, &args);
        clock_gettime(CLOCK_MONOTONIC, &t1);
        if (r < 0) {
            fprintf(stderr, "ioctl[%d] failed: %s\n", i, strerror(errno));
            fprintf(fp, "{\"rep\":%d,\"ioctl_ns\":-1,\"opt\":\"%c\",\"err\":\"%s\"}\n",
                    i, (i%2==0)?'A':'B', strerror(errno));
            continue;
        }
        long long dt = ns_diff(t0, t1);
        fprintf(fp, "{\"rep\":%d,\"ioctl_ns\":%lld,\"opt\":\"%c\"}\n",
                i, dt, (i%2==0)?'A':'B');
    }
    fclose(fp);
    close(fd);
    return 0;
}
