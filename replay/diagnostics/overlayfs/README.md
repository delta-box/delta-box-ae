# 跨 checkpoint 的旧 FD 修复（2026-09-21）

已在 spr4numa 的独立 Firecracker VM 中确认并修复两个相关错误：checkpoint 后删除路径时，旧读写 FD 写入返回 `EBADF`；checkpoint 后同名路径已有新 upper 时，旧 FD 写入会污染新文件。修复位于 guest 内核，不需要修改 replay 的业务语义，也没有修改 CRIU。

## 源码和实际构建位置

原镜像使用 `spr4numa:/mnt/disk2/dyp/d-overlayfs/linux-6.8/vmlinux`。对应源码在同目录 `fs/overlayfs/`；上层 Git 仓库 HEAD 为 `6819771a572094191bd3ab594d3466cad9123e6f`，kernel 子树为 `36cf0511858127bc33ffa921d4cbc64fdbf62246`。

`include/generated/compile.h` 记录编译主机 `spr4numa`、用户 `dyp`、GCC 11.4.0；`.version` 为 37。仓库 README 记录 `cd linux-6.8 && make -j$(nproc)`，`.vmlinux.cmd` 保留链接命令。配置为 `CONFIG_OVERLAY_FS=y`，因此修改内置于 guest kernel，不是 rootfs 里单独加载的 `.ko`。这些是原构建位置的证据，不是完整的历史构建可复现证明。

本次复制完整源码和构建目录到 `/mnt/disk2/dyp/overlay-stale-fd-20260921/linux-6.8`，只在副本修改 `fs/overlayfs/file.c` 并执行 `make -j16 vmlinux`。这是保留原编译对象的增量重编译，未声称 clean build。原源码、原 `vmlinux`、共享 base/data 镜像均未改写；原 Linux tracked status 仍为空，原文件和内核 hash 均保持不变。

| 产物 | SHA256 |
| --- | --- |
| 原内核 `d-overlayfs/linux-6.8/vmlinux` | `d51d169089c3d3b9dfe3cb6b2e6ffb3a9bceeab52adf58e2ae1dfe5fc2681d89` |
| 修复候选 `/mnt/disk2/dyp/overlay-stale-fd-20260921/vmlinux-fixed` | `fa09edb1891d893dbad6136b0dcfb4deac9c5126513df3b6312c278b0d521808` |
| 原/新 `.config` | `a030e626590ce76e4c06c8a31a468c0c5f62b257cc07479031b8c5f03b04c916` |

完整记录见 [build-manifest.json](evidence/build-manifest.json)。正式 AE 默认配置仍指向原内核；后续评测应使用独立配置选用新内核，并记录上述 hash，不能把新旧内核结果混在一起。

## 已实证的错误路径

`stale_fd_probe.py` 直接执行 Python `os.open(O_RDWR)`、checkpoint ioctl、`os.unlink()`、`os.pwrite()` 和 `os.pread()`，不经过 shell 重定向。旧内核的失败 FD 仍通过 `fstat`、`F_GETFL`，读写标志为 32770；错误来自真正的 write 操作。

旧内核 kretprobe 显示匿名 CoW 成功返回 0，接着 `ovl_real_fdget_meta()` 再次打开按路径解析的文件，最终 `backing_file_write_iter()` 返回 -9。为排除其他返回路径，在独立诊断内核中记录 backing inode、flags、nlink 和 mount：

- 删除后的旧 FD：CoW 生成 inode 3068105、`nlink=0`、`flags=0x48002` 的可写匿名 upper；紧接着重新打开 lower inode 102057，`flags=0x48000`（只读），write 返回 -9。
- 同名新 upper：CoW 生成匿名 inode 6915618；随后错误重绑定到新文件 inode 6915617，旧 FD 的 write 返回成功，但污染了新路径。

证据见 [逐阶段内核日志](evidence/instrumented-probe-v2/probe-dmesg.log)、[旧内核 syscall 结果](evidence/old-kernel-probe-v2/probe.log)。诊断日志中的 inode 是该次 VM 的具体值，不是固定预期值。

修复 [preserve-anonymous-backing.patch](preserve-anonymous-backing.patch) 只给 `ovl_real_fdget_meta()` 加一个身份保护：当 fd 私有 backing 的 `i_nlink==0` 时，保留该已打开文件并继续原 flags 同步，不再根据 inode 全局路径把它替换成 lower 或同名的新 upper。这样 read、write、seek 都使用匿名 CoW 对象。修复内核不包含 `diagnostic-instrumentation.patch` 的打印代码。

## 实机结果

| 独立 Python 语义用例 | 原内核 | 修复候选 |
| --- | --- | --- |
| checkpoint 后仍有路径，旧 FD 正常 rehome | 通过 | 通过 |
| 先 unlink，再 checkpoint | 通过 | 通过 |
| 先 checkpoint，再 unlink | `EBADF` | 通过；可写可读，路径不复活，父快照未改变 |
| checkpoint 后同名新 upper | 新路径被覆盖 | 通过；匿名写可读回，新路径和父快照未改变 |

同时原样重跑三套历史脚本：`test_full.sh`、`test_cross_checkpoint_fd_cow.sh`、`test_deleted_open_resurrect.sh` 全部返回 0；跨 checkpoint 脚本原先未执行到的四条断言这次执行通过。结果与原始日志位于 [fixed-probe](evidence/fixed-probe/results.json)。各阶段内核日志未检出 BUG、Oops、panic 或 WARNING。

随后使用同一修复内核，补测非零 `lseek`、普通 `read/write`、文件偏移变化，并与 `pread` 交叉检查，四个用例全部通过。全部写入后再次核验旧父快照和同名新路径没有被污染；这轮没有重跑历史脚本。证据见 [fixed-seek-probe](evidence/fixed-seek-probe/probe.log) 和 [VM manifest](evidence/fixed-seek-probe/vm.json)。实际 probe SHA256 为 `680be198faeedd8a891a7b3574e3dc18489a360f5df324f0817fab5de3899305`；外层绑定 CPU 0–1、NUMA node 0，manifest 记录 `physcpubind: 0 1`、`membind: 0`。Firecracker PID 3475960 已退出，私有 rootfs 已删除，没有占用 NUMA2 的性能测试核心。

这是 2 vCPU、2 GiB 的语义诊断，未作性能对比，也不等于论文未恢复出的 53-case 清单全部通过。最初一次诊断启动遗漏 `/dev/vdb`，guest fstab 等待后进入 emergency mode，未执行语义测试；补上只读 data 镜像后完成上述测试。另一轮诊断发现历史脚本会 `dmesg -c`，因此最终采集器在每套脚本后立刻保存日志；本文引用完整保存的 `*-v2` 记录。所有诊断 VM 和其私有 rootfs 已清理，原始失败尝试保留在远端诊断目录。

## 重跑

将本目录脚本部署到服务器，确保路径指向包含 `ae/runners/vm.py` 和原测试脚本的 runtime checkout。以下命令要求尚不存在的输出目录，并在私有 network/mount namespace 中运行；`base.xfs` 复制为独立可写盘，data 盘只读。

```bash
sudo -n numactl --physcpubind=0-1 --membind=0 \
  unshare --mount --net --propagation private \
  python3 replay/diagnostics/overlayfs/run_vm_probe.py \
  --runtime-repo "$PWD" \
  --kernel /mnt/disk2/dyp/overlay-stale-fd-20260921/vmlinux-fixed \
  --base-xfs /mnt/disk2/dyp/d-overlayfs/base.xfs \
  --data-xfs /mnt/disk2/dyp/d-overlayfs/data-tools.xfs \
  --out /mnt/disk2/dyp/overlay-stale-fd-rerun
```

脚本记录 Firecracker PID、实际 kernel 路径及 hash、私有 rootfs 路径、每项返回码和各阶段日志，并在结束时清理自建 VM。使用 `results.json` 判断测试结果；历史证据来自新增最终 exit-code/hash 字段之前的采集器，已有每项返回码且内核 hash 由构建 manifest 补齐。

仅重跑严格 Python 用例时添加 `--probe-only`。当前采集器也记录 probe 文件 hash、host CPU affinity 和 `numactl --show` 的实际内存策略；默认完整模式仍运行 Python 用例和三套历史脚本。

若需重新编译，在**新的独立 Linux 源码/构建副本**中应用 `patch -p1 < preserve-anonymous-backing.patch`，核对 `.config` hash 后执行 `make -j16 vmlinux`。`diagnostic-instrumentation.patch` 仅用于对原源码增加诊断日志，不应混入正式性能内核。
