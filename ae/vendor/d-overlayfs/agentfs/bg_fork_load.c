/*
 * bg_fork_load.c — Tight fork+wait loop to simulate sandbox-template fork
 * competition during fast-path ioctl measurement.
 *
 * Usage: bg_fork_load <duration_sec>
 * Spawns a child that immediately exits, parent waits, repeats.
 * Reports total fork count to stderr on exit.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <time.h>
#include <signal.h>

static volatile int stop = 0;
static void sig_stop(int s) { (void)s; stop = 1; }

int main(int argc, char **argv) {
    int dur = (argc >= 2) ? atoi(argv[1]) : 60;
    signal(SIGTERM, sig_stop);
    signal(SIGINT,  sig_stop);

    struct timespec t0, tn;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    long count = 0;

    while (!stop) {
        pid_t p = fork();
        if (p == 0) {
            _exit(0);
        } else if (p > 0) {
            int s;
            waitpid(p, &s, 0);
            count++;
        } else {
            usleep(1000);
        }
        clock_gettime(CLOCK_MONOTONIC, &tn);
        if (tn.tv_sec - t0.tv_sec >= dur) break;
    }
    fprintf(stderr, "bg_fork_load: %ld forks in %d sec\n", count, dur);
    return 0;
}
