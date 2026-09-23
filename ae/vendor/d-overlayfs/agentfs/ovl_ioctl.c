/*
 * ovl_ioctl.c - 用户态工具，调用 overlay checkpoint ioctl
 *
 * 用法: ovl_ioctl <overlay_mount_dir> "lowerdir=/path,upperdir=/path,workdir=/path"
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <errno.h>
#include <linux/types.h>

#define OVL_IOCTL_MAGIC 'O'

struct ovl_checkpoint_args {
    char *options;
    __u32 options_len;
};

#define OVL_IOCTL_CHECKPOINT _IOW(OVL_IOCTL_MAGIC, 1, struct ovl_checkpoint_args)

int main(int argc, char *argv[])
{
    int fd, ret;
    struct ovl_checkpoint_args args;

    if (argc != 3) {
        fprintf(stderr, "Usage: %s <overlay_mountpoint> <options_string>\n", argv[0]);
        fprintf(stderr, "Example: %s /mnt/merged \"lowerdir=/new_lower,upperdir=/new_upper,workdir=/new_work\"\n", argv[0]);
        return 1;
    }

    fd = open(argv[1], O_RDONLY | O_DIRECTORY);
    if (fd < 0) {
        perror("open overlay mountpoint");
        return 1;
    }

    args.options = argv[2];
    args.options_len = strlen(argv[2]);

    ret = ioctl(fd, OVL_IOCTL_CHECKPOINT, &args);
    if (ret < 0) {
        fprintf(stderr, "ioctl checkpoint failed: %s (errno=%d)\n", strerror(errno), errno);
        close(fd);
        return 1;
    }

    printf("Checkpoint succeeded.\n");
    close(fd);
    return 0;
}
