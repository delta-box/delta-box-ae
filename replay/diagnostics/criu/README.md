# 实际 guest CRIU 的源码和二进制身份

2026-09-21 通过只读挂载 spr4numa 的 `base.xfs` 与原始 `ubuntu-24.04.xfs` 核对，当前 replay 使用的是 **CRIU 4.2，GitID `v4.2-22-g2cf8f13ca`**。它不同于 host `/usr/sbin/criu` 的 3.16.1，也不等于纯 `v4.2` tag。

| 项目 | 实测值 |
| --- | --- |
| guest 实际命令路径 | `/usr/local/sbin/criu`；guest `/usr/sbin/criu` 不存在 |
| guest Git commit | `2cf8f13ca1f11a0491977e438b262e646137256c` |
| 二进制 SHA256 | `787295655db3cc6c3c4add01ba993f83e34e1ef6ab364e9fea6d09320bc8cce7` |
| ELF build ID | `25f7360216e68d7630641f2853ae0d1f7a288be3` |
| 编译目录、编译器 | DWARF 记录 `/root/criu`、GCC 13.2.0 |
| base 镜像 | `/mnt/disk2/dyp/d-overlayfs/base.xfs` → `/mnt/disk1/dyp/d-overlayfs_xfs/base.xfs` |
| 原始源码所在镜像 | `/mnt/disk2/dyp/d-overlayfs/ubuntu-24.04.xfs` → `/mnt/disk1/dyp/d-overlayfs_xfs/ubuntu-24.04.xfs` |
| 镜像内源码目录 | `/root/criu`，上述 commit，tracked status 干净 |

原镜像中的 `/root/criu/criu/criu` 构建产物、原镜像中的 `/usr/local/sbin/criu` 安装文件和 base 镜像中的安装文件，三个 SHA256 完全一致。原镜像源码的 GitID 与二进制输出一致；`criu/include/version.h` 也记录相同版本。`build_base.sh` 明确排除了 `/root/criu`，因此仅查看精简 base 镜像会找不到原源码。

replay 的 `run_instance.py` 显式设置 dump/restore 命令均为 `criu`，没有改写 PATH；在只读 guest rootfs 中执行 `command -v criu` 解析为 `/usr/local/sbin/criu`。后续若用自定义配置覆盖命令或 PATH，仍应在该次运行记录实际命令和 hash。

证据保存在 [原始取证输出](evidence/guest-identity.txt) 和 [身份 manifest](evidence/guest-identity-manifest.json)，包括源文件 hash、ELF notes、编译器信息及私有导出 hash。镜像通过私有 mount namespace，以 `loop,ro,norecovery,nouuid` 挂载，完成后卸载。未编译 CRIU、未修改原镜像、未更换系统二进制、未启动 VM 或性能实验。

## 后续格式适配的源码入口

服务器已有 Git 对象库 `/mnt/disk2/dyp/build/criu-v4.2-src`，包含 commit `2cf8f13ca1f11a0491977e438b262e646137256c`，但当前 checkout 是纯 `v4.2`（`3c7d4fa013297b431da48eff821db7f2e8b90c27`）。应从准确 commit 创建独立源码目录；本次未切换已有目录。

对增量链的分析应以该 commit 的 `criu/mem.c`、`images/pagemap.proto`、`images/inventory.proto` 为准，不能把旧审计中 `30acbabcd` 的参考源码当成当前 guest 源码身份。取证 manifest 已包含原镜像内这些文件的 SHA256，可以验证从对象库导出的版本。

## 给 host 诊断提供的私有副本

实际 guest 二进制已导出到：

```text
/mnt/disk2/dyp/criu-host-diagnostic-20260921/private-criu/criu
```

副本与 guest SHA256 一致。但 host 直接执行会报 `GLIBC_2.38 not found`，因为 guest 使用较新的用户态运行库。这个报错不表示 CRIU 镜像损坏，也不能据此改用 host 3.16.1 代替验证。可在匹配用户态环境中运行，或从准确 commit 做独立 host 构建，并单独记录新产物的 hash；后者属于源码版本一致对照，不再是原 guest 的同一二进制。
