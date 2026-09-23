# spr4numa 镜像来源核查与构建修复

## 结论

本次通过 SSH 以 `dyp` 登录 spr4numa，核对原始构建脚本、历史实验记录，并在独立 mount namespace 中只读检查现有母盘、base 盘和 Django 数据盘。

**当前 CPU trace-replay 使用的是 Ubuntu bootstrap + 多环境母盘拆分的双盘部署，未找到官方 SWE-bench 单实例 Docker 镜像直接转换为这些 base/data 盘的证据。** runtime 根 README 原来的 `docker export → tar 到 XFS → 启动 Firecracker` 是不完整的示例，不能当作现有实验盘的来源证明；本轮已移除该保证并链接到 AE 指南。

已有镜像与论文投稿批次的逐字节身份仍需对应 run manifest 和完整哈希核对。本次来源判断不把当前母盘、当前内核源码和全部历史运行自动视为同一版本。

## 核对到的实际链路

```text
Ubuntu Noble bootstrap
  → 安装 CRIU / Python / Miniconda
  → install_envs.py：按 repo/version 创建环境和仓库工作副本
  → ubuntu-24.04.xfs 多环境母盘
  → build_base.sh / build_data.sh
      ├─ base.xfs：OS + CRIU，/opt → /mnt/data/opt
      └─ data-<group>.xfs：/opt/miniconda3 + /testbeds/<repo>__<version>
  → Firecracker：补丁 vmlinux + 可写 base 副本 + 只读 data 盘
```

| 证据 | 内容 |
|---|---|
| spr4numa `/mnt/disk2/dyp/d-overlayfs/README-zh.md:65` | 明确给出 `debootstrap noble`、20 GiB XFS 母盘及 repo/version conda 目录 |
| [历史 build_base.sh](historical/build_base.sh)、[build_data.sh](historical/build_data.sh) | 都从 `ubuntu-24.04.xfs` 拆分，没有拉取或导出官方 SWE-bench OCI |
| [历史 install_envs.py](historical/install_envs.py) | clone 仓库、checkout `env_commit`、创建命名 conda 环境并安装依赖；不是从 Docker 导出环境 |
| [本轮只读盘内容](source-audit-20260921.json) | 母盘含多套 `django__*`、`sympy__*` 等目录；Django data 盘含九套 Django 环境和共享 `testbed` 环境 |
| 母盘 `/usr/lib/os-release` | `Ubuntu 24.04.4 LTS (Noble Numbat)` |
| 母盘 `/var/log/bootstrap.log` | 保留 2025-11-26 从 Ubuntu Noble 镜像站下载基础包到 `/tmp/my_rootfs` 的记录 |
| spr4numa `finalbench/deltabox_peagle_mcts30_2x_numa12_realrtt/images.env` | 历史 run 使用 `/dev/shm/.../lane0/base.xfs` 和同目录分组数据盘的副本 |

disk2 的 `base.xfs`、`data-*.xfs`、`ubuntu-24.04.xfs` 实际是指向 `/mnt/disk1/dyp/d-overlayfs_xfs/` 的软链接。母盘 20 GiB，base 3 GiB，Django data 9 GiB。

本轮核对的原始文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| 远端 `README-zh.md` | `d9ddecb98c82387cbb51e9291fe1c34fc5ca490fffc084387929f0e4673e7f72` |
| `scripts/build_base.sh` | `c0ec4d2ade648b0f7838f23a537e4d4986a91d155e7f7eff7eeb5be115b79207` |
| `scripts/build_data.sh` | `cdd304f8f634d2963c042fb20b2f88876a2a3a73871d4517416b30c19f45e09e` |
| `install_envs.py` | `2f2338640cb9df071d0a11d75a35c616325627c997480827eace8531a2967c74` |
| `env_specs.json` | `148f77f6d3826e10cfc4e1bef7d86eba4b2b580cde0a1bf47a1c764604e053ba` |
| 母盘内 `var/log/bootstrap.log` | `356f7f3a7568f3e28271ef6e8bd02385ac256981ff6b869b39f4631cf8b723b2` |

## 官方 SWE-bench OCI 用在哪里

远端 `d-overlayfs/benchmarks/replay/replay_trace.sh` 构造 `swebench/sweb.eval.x86_64.*` 名称，创建容器并通过 `docker cp ...:/testbed/.` 提取源码到 lower 层。该脚本 SHA-256 为 `f0ebed15175112965c705abc7bc36fa6ef16f619b9dfb94b54d84364271cefc1`。

`benchresults/2026-05-11_swesearch_war/README.md` 进一步注明：该轮 WAR 的 22 个实例中，2 个从本地 Docker 取目标源码，其余从 GitHub 的记录 commit 获取。这里使用的是 source tree，没有转换整套容器用户态为 Firecracker rootfs。

官方单实例 OCI 通常把工作仓库直接放在 `/testbed`，不符合现有 AE 导出器要求的 `/testbed/<repo>__<version>` 目录集合。若要建立官方 OCI → VM 的新路径，需要补齐 init、SSH/网络、CRIU、runner 挂载/路径约定并逐实例验收；不能仅改镜像名后复用当前多环境拆分器，也不能把新环境的数值补记为历史复现。

## 本轮构建修复

- `build_xfs.py` 将根盘 fstab 规范为 `/dev/vda / xfs`。只读检查发现母盘实际为 XFS，但保留 `/dev/vda / ext4 defaults 0 1`；旧导出器会继承该错误。
- `build_bundle.py` 在免密 sudo 可用时不再强制交互式 `sudo -v`。旧入口在本机 SSH 会话实测因此失败，记录保留在远端 `ae-image-source-check-20260921/bundle-01/`。
- 增加 `--kernel VMLINUX`：复制并校验已验证的内核，明确区别于 `--kernel-source` 编译；不会为了一次磁盘拆分强制重新编译历史工作树。
- 增加 `--groups`：允许先生成 base + Django 做最小验收，默认仍为四组。生成的配置记录实际数据组，子集不能执行其它组 workload。
- 官方 `swebench/sweb.eval.*` 作为 master 输入时提前报清楚错误；Ubuntu/Miniconda 的新建 master 配方仍标为实验性，未宣称通过完整重建。

## 构建与运行验证

本轮在独立目录 `/mnt/disk2/dyp/ae-image-source-check-20260921/` 操作，未修改原镜像。使用本文修复后的构建器生成 `bundle-02/`，成功产出 **base 3 GiB + Django data 9 GiB**，并复用既有 `vmlinux`。输入母盘完整 SHA-256 为 `dfe4d7bc0734fc3a3b5b8a695f686afce08509ba319c308b024f3bbc8c8e5c54`。

```bash
bash ae/build_images.sh \
  --master-xfs /mnt/disk2/dyp/d-overlayfs/ubuntu-24.04.xfs \
  --kernel /mnt/disk2/dyp/d-overlayfs/linux-6.8/vmlinux \
  --groups django \
  --ssh-pubkey /home/dyp/.ssh/id_ed25519.pub \
  --output /mnt/disk2/dyp/ae-image-source-check-20260921/bundle-02
```

该输出已存在，不能再次用相同目录构建。原始构建命令和状态见 [bundle.json](evidence/20260921/bundle.json)，磁盘整盘哈希见 [disks-build.json](evidence/20260921/disks-build.json)，构建日志见 [disks.log](evidence/20260921/disks.log)。构建 manifest 的 `built-not-boot-tested` 是生成时状态，后续启动验收单独记录，不回写原始构建证据。

随后使用远端相同分支的干净 runtime `ee77dd1f7c06db89c4b1419d39c21d40cd95488c`，以 4 vCPU / 8192 MiB、`historical-async-full` 对 `django__django-14997` 的前三个事件分别执行：

| 模式 | 实际事件 | 结果 | 原始证据 |
|---|---|---|---|
| fast | 2 checkpoint + 1 warm-template restore | `status=ok`，后台 dump 完成，索引恢复匹配 | [run](evidence/20260921/fast-run.json)、[events](evidence/20260921/fast-results.jsonl)、[guest log](evidence/20260921/fast-guest.log) |
| slow | 2 checkpoint + 1 CRIU lazy restore | `status=ok`，后台 dump 完成，索引恢复匹配 | [run](evidence/20260921/slow-run.json)、[events](evidence/20260921/slow-results.jsonl)、[guest log](evidence/20260921/slow-guest.log) |

两次均在真实 worker 内保留 6461 个源码文件的索引，`worker_exec_bad_n=0`、`error_n=0`。本地入口测试 12 项、配方检查 9 项、shell 语法与 diff 检查通过。结构化汇总及证据哈希见 [validation.json](evidence/20260921/validation.json)。

**验证范围：**本轮只构建 Django 一组并运行短轨迹，不覆盖其余三组磁盘、完整 cohort 或从零重建 80 套环境；没有重新编译内核，没有做性能复现。母盘携带 CRIU `v4.2-22-g2cf8f13ca`，与部分已记录现役 base 的 `v4.2-36-g30acbabcd` 不同，因此新盘通过 smoke 也不能证明与历史盘逐字节或依赖版本等价。
