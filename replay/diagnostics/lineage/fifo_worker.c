#define _GNU_SOURCE
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

/* Own only a named transport FIFO; the controller observes its buffer while
 * every checkpoint/restored worker remains stopped. */
int main(int argc, char **argv) {
    if (argc != 3 || unshare(CLONE_NEWPID)) return 2;
    pid_t child = fork();
    if (child < 0) return 3;
    if (child) { int status; return waitpid(child, &status, 0) < 0 ? 4 : 0; }
    if (setsid() < 0) return 5;
    int fd = open(argv[1], O_RDWR | O_NONBLOCK);
    if (fd < 0) return 6;
    char hostpid[64]; ssize_t n = readlink("/proc/self", hostpid, sizeof(hostpid)-1);
    if (n < 0) return 7;
    hostpid[n] = 0;
    FILE *ready = fopen(argv[2], "w");
    if (!ready) return 8;
    fprintf(ready, "%s\n", hostpid); fclose(ready);
    for (;;) pause();
}
