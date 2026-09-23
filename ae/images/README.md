# 论文实验镜像与生成脚本

范围：`atc26-paper158.pdf`，以 2026-09-20 从 79 采集的脚本和历史运行配置为证据。
这里提供**构建配方**，镜像二进制不进 Git。已绑定的主要 DeltaBox cohort 对应基础盘加四组数据盘，逻辑容量为 **49 GiB**；第五组 Sphinx 是可选历史盘，加上后为 59 GiB。用于拆分的旧母盘另有 20 GiB，不必与拆分产物一起分发。

**先读 [镜像范围复核](review.md)**：上一版混合了历史配方、重建候选、构建中间产物和可选环境，不能把目录中的全部配方视为必须分发、已经验证的论文镜像。Figure 8 的 Cube/E2B 模板也不能直接由 Table 2 的构建入口替代。

`historical/` 保存找到的原始配方和 SHA-256；**执行入口在 `scripts/`**。
新脚本拒绝覆盖已有输出，源母盘只读挂载，生成的磁盘会记录完整 SHA-256。
不要直接执行历史脚本：其中存在固定路径、删除旧输出、复用已有 mount 等旧逻辑。

## 统一构建入口

先看[2026-09-21 来源核查与构建验证](source-audit-20260921.md)。spr4numa 的现有 CPU AE 磁盘采用 `Ubuntu bootstrap → 多环境母盘 → base/data 拆分`，不是官方 SWE-bench 单实例 OCI 的直接导出。旧 runtime README 的 `docker export → tar 到 XFS → 启动 VM` 示例不完整，也没有证明是这些历史盘的生成记录。

所有命令均从 **deltabox-runtime 仓库根目录**执行。托管评审可直接使用已配置镜像；已有历史母盘时，推荐复用已验证内核：

```bash
bash ae/build_images.sh \
  --master-xfs /path/to/ubuntu-24.04.xfs \
  --kernel /path/to/verified/vmlinux \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" \
  --output /scratch/deltabox-images
```

`--kernel` 原样复制并记录哈希；需要自行编译时改用 `--kernel-source /path/to/clean-linux-6.8`，可附 `--kernel-config FILE`。这避免使用已做 in-tree build 的历史源码目录触发 `O=` 构建失败，也避免把当前源码误当成历史二进制的准确来源。

默认构建四组 data 盘。`--groups django` 可先构建 base + Django 用于最小验证，不能用该子集运行完整 cohort。`--plan` 只校验输入并输出命令，`--help` 查看参数。成功后生成 `bundle.json`、分阶段日志和 `config.json`：

```bash
bash ae/run_test.sh --config /scratch/deltabox-images/config.json
```

新配置默认关闭 host 绑核，需要性能测量时按 [AE 指南](../README.md#测量环境)设置本机 NUMA/CPU。完整构建示例与 baseline 配置见 [详细命令行指南](../README.md#环境准备)。`--miniconda` / `--source-image` 仍保留为实验性多环境 OCI 入口，未验证为历史盘的等价重建；不能把官方 `swebench/sweb.eval.*` 当成这里的 master。构建器不部署 Cube/E2B 服务。

## 论文需要哪些镜像

| 用途 / 图表 | 镜像或模板 | 找到的配方 / 本次补写 |
|---|---|---|
| DeltaBox；Table 2/3、Figure 1/2/6/7/8 的对应 DeltaBox 实验 | Ubuntu 24.04 `base.xfs`，XFS reflink=1，3 GiB；修改后的 Linux 6.8 `vmlinux` | 原始 `build_base.sh`；补写 `build_xfs.py`、`build_kernel.sh`，保存 79 的 kernel config |
| 按实际 cohort 选择工作负载 | `data-django.xfs` 9 GiB；`data-sympy.xfs` 9 GiB；`data-sci.xfs` 18 GiB；`data-tools.xfs` 10 GiB；Sphinx 10 GiB 仅按需 | 原始 `build_data.sh`、`install_envs.py` 和 **80 组历史规格**；规格数量不是 AE 必须构建的环境数量 |
| FC-Diff+dm；Table 2、Figure 1 | 在 `base.xfs` 上扩容到 8 GiB，注入 Python 3.11、replay venv、单实例 repo/index/trace、controller service | 从 `finalbench/fc_diff_dm/fc_dm_pilot.py:prepare_rootfs` 重建 `build_fc_dm.py` |
| replay+cp、CRIU+cp；Table 2、Figure 1 | 在 host 上使用 replay venv/repo/index，不需要额外 VM 镜像 | 保留为 host 环境需求；CRIU baseline 不能拿 FC VM 镜像冒充 |
| CubeSandbox；Table 2 | `ghcr.io/tencentcloud/cubesandbox-base:2026.16` + Python；模板为 4 vCPU / 4096 MiB / 8 GiB writable layer | 找到实验 Dockerfile；补写 build 和注册脚本。Cube v0.3.0 (`a7b099d`)，官方 kernel `6.6.1199-0009-03_2.0.1`。Figure 7 没有 Cube 分支 |
| CubeSandbox；Figure 8a | `cube-official-fork-bench-20260605_210553` | 与 Table 2 模板 ID 不同；原始构建请求/OCI digest 未找回，不能宣称现有 `build_cube.sh` 已覆盖 |
| E2B；Table 2、Figure 7 | E2B `rootfs.ext4` + local build snapshot；部分记录在 Ubuntu 24.04 QEMU L1 内运行 | 找到 `prepare_base_build.sh`、`run_e2b_in_l1.py` 和 infra create-build；补写 L1 / local build 入口，完整部署仍是前置条件 |
| E2B；Figure 8a | 官方 SDK 访问本地服务注册的 `base` 模板 | `build_e2b.sh` 仅生成 local build；未恢复 API 服务的模板注册链，不能视为即用的 Figure 8 模板 |
| Figure 9 write amplification | ext4、XFS(reflink=0)、XFS(reflink=1) 三种临时文件系统；SWE-bench OCI `/testbed` | `build_xfs.py filesystems`；已记录 **185 个 input 引用 / 136 个 OCI 名称**，`pull_figure09.py` 按需下载并记录 digest |
| Figure 8(b) GPU profiling / RL inference | GPU 机器上的 CUDA/PyTorch/transformers/peft，模型为 Qwen2.5-7B-Instruct profiling / Qwen3-Coder-30B inference | 79 的记录没有给出完整 GPU 容器/依赖锁。[Figure 8(b) 新脚本](../paper/figure-08/README.md)可使用本地 Python 环境；可选 `Dockerfile.gpu` 要求显式给 digest 和依赖 lock。Figure 8(c) 是 CPU 理论计算 |

`sci` 包含 Astropy、Matplotlib、scikit-learn、Xarray；`tools` 包含 pytest、pylint、requests、flask、seaborn。目录名为 `<repo>__<version>`，不是每个 instance 一份镜像。母盘 `/testbed/<spec>` 被复制为数据盘 `/testbeds/<spec>`，guest 从只读数据盘按实例 checkout。五组对应关系见 `historical/image_groups.py`。

Figure 3/4/5 是设计示意，不额外构建镜像。Figure 8 的 synthetic fan-out 使用该实验记录的各后端模板，64 MiB source 在运行时生成；CRIU dump、FC VM snapshot 和 E2B/Cube 内存快照是运行期产物，不应逐次打包分发。模板初始化所需的快照与每次 benchmark 产生的快照需要区分。

## 路径 A：从历史母盘重新生成（候选重建路径）

当前 AE 应先冻结**被选定 run 实际使用的已知可运行镜像**。母盘拆分脚本有历史依据，但尚未证明当前母盘可重建相同版本的 base/data；它不是已经验证的替代品。记录中的实际路径还包含 disk1 与 `/dev/shm`，见复核说明。

Linux x86-64，root 权限；需要 `python3` (3.9+)、`xfsprogs`、`e2fsprogs`、`rsync`、`util-linux`。推荐 Ubuntu 22.04/24.04 的 xfsprogs，避免较新版本默认开启 Linux 6.8 不支持的磁盘格式特性。预留至少 80 GiB；源母盘必须没有并发写入。

从仓库根目录运行，输出路径必须尚不存在：

```sh
sudo python3 ae/images/scripts/build_xfs.py split \
  --source /mnt/disk2/dyp/d-overlayfs/ubuntu-24.04.xfs \
  --out /mnt/disk2/dyp/ae-images-new \
  --ssh-pubkey "$HOME/.ssh/id_rsa.pub"
```

默认 `--groups django sympy sci tools`，对应已绑定的主要 DeltaBox workload；显式传 `--groups django sympy sci tools sphinx` 才加入历史 Sphinx 数据盘。其它后端的 cohort 含 Sphinx，并不自动意味着需要 DeltaBox 的 Sphinx 盘。Cube 使用自身模板和上传的 payload，不使用这些 XFS 数据盘。`base.xfs` 总会生成。新增脚本会剔除历史 `/root`、`/home` 内容，重置 guest SSH 身份，安装指定公钥、禁止 guest 密码登录。这只用于 runner 与 guest 的内部通信；托管评审通过密码登录 spr4numa 的 `atc-ae` 账号，内部密钥由作者预配置。它保留实验的数据布局，**不承诺重建后镜像与旧盘逐字节相同**。

生成目录的 `build.json` 保存输入母盘完整 SHA-256、各组 spec 清单和输出盘完整 SHA-256。每张 data 盘内部还有 `MANIFEST.json`。使用时 rootfs 作为 `/dev/vda` 可写盘，数据盘作为 `/dev/vdb` 只读盘；rootfs 的 `/opt` 指向 `/mnt/data/opt`。运行器仍须注入 guest daemon、δCR 等代码。

## 路径 B：母盘不可获取时的实验性重建

历史 README 明确记录了 `debootstrap noble` 和手动安装依赖；母盘中的 bootstrap 日志、repo/version 目录也与此相符，但完整的版本锁和一键生成脚本没有找回。本目录后来补写 `Dockerfile.master`，它采用 Ubuntu OCI 重新安装依赖，不是官方 SWE-bench 镜像转换器，也不是已恢复的原始配方。找到的环境规格中部分 pip 包不固定版本、原安装器允许部分依赖失败，因此从头构建需要重新检查成功日志和测试运行。这条路径还没有完成全量构建验证。

`build_master.sh --from-ubuntu` 自动从 Anaconda 官方渠道下载固定的 Linux x86-64 Miniconda 安装包，再按已有 Dockerfile 构建多环境镜像。此入口无需已有母盘，也无需手动准备安装包：

```sh
bash ae/images/scripts/build_master.sh --from-ubuntu \
  /scratch/deltabox-master deltabox-ae-master:local
bash ae/build_images.sh \
  --source-image deltabox-ae-master:local \
  --kernel-source /path/to/clean-d-overlayfs/linux-6.8 \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" \
  --output /scratch/deltabox-images
```

下载地址和校验值固定在 `scripts/fetch_miniconda.py`，来源为 [Anaconda 官方发布目录](https://repo.anaconda.com/miniconda/)的 `Miniconda3-py311_24.5.0-0-Linux-x86_64.sh`；这是重建配方选定的依赖，不是历史母盘的版本锁。第二条命令直接从 OCI 文件树生成 base/data，不额外生成母盘文件。

使用已有母盘的 `ae/build_images.sh --master-xfs` 路径会在构建前按 [inputs-20260922.sha256](inputs-20260922.sha256) 校验选定输入，不匹配时不创建构建输出。该清单是这一批 AE 输入的身份，不是所有自行重建镜像的通用校验值；OCI 重建和源码编译的产物另行记录。`--plan` 不扫描母盘整盘哈希。

需要离线提供安装包或选择其他构建输入时，保留以下手动方式：

1. 准备固定版本的 Linux x86-64 Miniconda installer 和发布者提供的 SHA-256；不要使用未经核验的 `latest` installer。新建 conda 环境默认使用 conda-forge，可通过 `CONDA_CHANNEL` 指定获准使用的 channel。
2. 建议用 `UBUNTU_IMAGE=ubuntu:24.04@sha256:...` 固定基础镜像。默认 tag 只是可用入口，不是历史 digest。CRIU 默认 `30acbabcd`，对应保存的 `v4.2-36-g30acbabcd` 条件记录；可用 `CRIU_REF` 覆盖。
3. 执行：

```sh
bash ae/images/scripts/build_master.sh \
  /path/to/Miniconda3-Linux-x86_64.sh VERIFIED_SHA256 \
  /scratch/ae-master-build ae/deltabox-master:ae1

sudo python3 ae/images/scripts/build_xfs.py oci \
  --source ae/deltabox-master:ae1 --out /scratch/ae-xfs \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub"
```

构建目录、tag 必须是新的。可传第五个参数使用 `env_specs.json` 的子集，导出时相应选择 `--groups`。严格包装器让依赖失败和缺失 spec 导致构建失败；不会把失败的环境标记为成功。共享 `testbed` shell 环境用 Python 3.11 新建，历史 lock 未找回。conda channel、Ubuntu apt 包、共享环境与原镜像可能不同，不能据此宣称重现原始耗时。

## guest 内核

需要含 DeltaBox 修改的 **Linux 6.8 源码**。从私有 [GitHub d-overlayfs 源码仓库](https://github.com/delta-box/d-overlayfs)获取，先由维护者授予访问权限。该仓库保留原 `6819771a572094191bd3ab594d3466cad9123e6f` 的 Linux 源码树，并在独立提交中加入 2026-09-21 验证的旧 FD 修复；原提交本身不含这项修复。来源与历史增量构建记录见仓库的 `source-provenance.json`，不保证重新构建得到字节相同的内核。脚本会检查 `OVL_IOCTL_CHECKPOINT`、记录 Git revision 和实际 overlayfs 源文件哈希。

```sh
bash ae/images/scripts/build_kernel.sh /src/clean-d-overlayfs/linux-6.8 /scratch/ae-kernel
```

请使用新的、没有 in-tree build 产物的源码 checkout；脚本采用 `O=` 编译，不清理现有工作树。输出为 `vmlinux`、`arch/x86/boot/bzImage` 以及启用模块的 `.ko`。采集的 config 为 `CONFIG_OVERLAY_FS=y`（编入内核），而正文描述 loadable module；需要 module profile 时提供单独 config，并在 guest 启动前安装匹配的 `overlay.ko`，不能把两者写成同一次验证。

Ubuntu build host 需要 `build-essential flex bison bc libssl-dev libelf-dev`，部分 config 还需 `dwarves`。编译器、`.config`、补丁源码与镜像构建记录应一起归档。

正文默认 4 vCPU / 8 GiB；部分条件记录为 4 vCPU / 4 GiB。VM 资源是 runner 参数，构建镜像时不统一改写历史配置。DeltaBox 条件记录中的 Firecracker 为 1.13.1；Firecracker 可执行文件是 host 工具，不在 rootfs 中。

## FC-Diff+dm

以下输入来自已经准备好的 finalbench/replay 环境。Python 前缀必须与 guest 用户态 ABI 相容；此处不会把 host 私钥或用户 home 打进镜像。

```sh
sudo python3 ae/images/scripts/build_fc_dm.py \
  --base /scratch/ae-xfs/base.xfs --out /scratch/ae-fc-dm \
  --payload /path/to/spr_payload --venv /path/to/moatless_det_venv \
  --driver /path/to/finalbench/fc_diff_dm/guest_controller_driver.py \
  --python-root /path/to/ubuntu-python311-sysroot \
  --instance django__django-14997
```

需要 payload 的 `repos/swe-bench_<instance>`、`index_store/<instance>`、`det_traces/ms/<instance>`、`moatless-det-src` 和四个 controller 模块。这里固定的是**guest 内部**历史路径 `/mnt/disk2/dyp/...`，host 输入路径可变。脚本会 chroot 检查 replay interpreter；dm-thin、TAP、启动和 Diff snapshot 仍由 benchmark runner 配置。原 `fc_dm_pilot.py` 对所有实例固定挂 `data-django.xfs` 来满足 `/dev/vdb` 启动依赖，实际 replay payload 在 rootfs；不能据其它实例的 repo 名要求 FC 分发五组 data 盘。每实例生成的 `fc-dm.xfs` 应现场构建，不逐个预打包。

## CubeSandbox

```sh
bash ae/images/scripts/build_cube.sh YOUR_REGISTRY/ae-cube-python:ae1
# 将上一步镜像发布到 Cube 节点能访问的 registry 后：
CUBEMASTER_ADDR=YOUR_CUBE_MASTER \
  bash ae/images/scripts/register_cube.sh \
  YOUR_REGISTRY/ae-cube-python:ae1 ae-cube-new /scratch/ae-cube-template
```

构建脚本只生成本地 OCI image，不自动发布。注册脚本要求已部署 Cube v0.3.0 和 `cubemastercli`、`jq`；等待 READY 后保存实际 template request/状态，默认 4000 millicores、4096 MiB、8G writable layer。基础 image 的 envd `/health` 端口是 **49983**。命令和 JSON 结构已对照 [v0.3.0 CLI 源码](https://github.com/TencentCloud/CubeSandbox/blob/v0.3.0/CubeMaster/cmd/cubemastercli/commands/cubebox/template.go)。CubeCoW 存储需要 XFS reflink，这属于 host storage 配置；新建模板本身不会启用 host reflink。

## E2B

原记录使用 self-hosted infra，部分基线在 Ubuntu 24.04 / Linux 6.8 的 L1 中执行。`build_l1.sh` 只生成磁盘和 cloud-init seed，不启动或重配现有机器：

```sh
bash ae/images/scripts/build_l1.sh /path/to/noble-server-cloudimg-amd64.img \
  VERIFIED_SHA256 "$HOME/.ssh/id_ed25519.pub" /scratch/ae-e2b-l1
```

需要 `qemu-img`、`cloud-localds`；宿主机提供 nested KVM。镜像用独立 qcow2 保存，默认扩到 120G。原始记录下载 Ubuntu `noble/current`，没有固定 release digest，所以这里要求显式输入和 SHA-256。启动 L1 时使用 `-enable-kvm -cpu host`，磁盘附加 `l1.qcow2` 和 `seed.img`；安装 E2B 所需 Go、Firecracker、kernel、NBD/网络等依赖后，再执行模板生成。

```sh
# 在独立 infra checkout 或 L1 中：
git -C /src/infra checkout f9a52875167889d130c9f505b1315fa56a01d749
git -C /src/infra apply /path/to/ae/images/patches/e2b-create-build.patch
sudo -E bash ae/images/scripts/build_e2b.sh /src/infra /scratch/ae-e2b-storage \
  11111111-2222-4333-8444-555555555555 e2bdev/base:latest
```

优先将 `e2bdev/base:latest` 替换为发布时锁定的 digest；历史 digest 未找回。默认匹配找到的 prepare_base 配方：kernel `vmlinux-6.1.158`、Firecracker `v1.14.1_458ca91`、1 vCPU / 1024 MiB / 10240 MB / hugepages=false。参数可用 `E2B_*` 环境变量覆盖。

保存的 patch 仅恢复 create-build 的 `-image` 参数。79 当前 infra 另有 resume/provision 等本地修改，不在这份镜像补丁中；需要运行实验 runner 时还应独立冻结这些修改。镜像生成脚本不会把不同 E2B 测试阶段的嵌套层级或资源参数自动统一成论文默认值。

## Figure 9 / GPU

```sh
# 默认只列出所需 OCI；加 --pull 才下载，失败不会用别的实例替换。
python3 ae/images/scripts/pull_figure09.py
python3 ae/images/scripts/pull_figure09.py --instance astropy__astropy-14309 \
  --pull --out /scratch/ae-war-pulled.json
sudo python3 ae/images/scripts/build_xfs.py filesystems --out /scratch/ae-war-fs
```

三个空文件系统默认各 4 GiB，与找到的 `swesearch_replay.sh` 的 `LOOP_MB=4096` 一致，可用 `--size-mib` 调整。此前 512 MiB 的 快速检查 只检查 mkfs/mount/reflink，不验证论文中的物理 I/O 数值。实际实验应让原 runner 现场生成并测量 loopback FS，不分发三张空盘。实验仍需从 OCI 复制 `/testbed`，并使用 `paper/figure-09` 内各自 trace 的 base_commit/编辑序列。

GPU 部分未找到能证明历史版本的镜像或完整依赖 lock。若决定发布一个经过重新验证的替代容器，可使用：

```sh
bash ae/images/scripts/build_gpu.sh 'CUDA_TORCH_IMAGE@sha256:DIGEST' \
  /path/to/requirements-gpu.lock /scratch/ae-gpu-build ae/gpu:ae1
```

lock 必须含完整依赖及 pip hash（含 transformers/peft），模型权重单独只读挂载。该脚本没有给未知历史版本编造默认值；完整 GPU benchmark 仍需有 GPU 的环境验证。[Figure 8(b) 计时脚本](../paper/figure-08/README.md)也支持分别指定 vLLM 和训练环境的 Python 路径，当前提供的是依赖约束，不是已验证的容器锁；(c) 的理论计算只需 CPU。

## 来源与验证边界

- [provenance.json](provenance.json)：原始脚本、env specs、kernel config 的 SHA-256，及重建脚本对应的源文件。
- [inventory79.json](inventory79.json)：79 当前镜像大小和**前 4 MiB** hash，不能当作整盘校验值，也不是论文提交时冻结清单。当前 base 的前缀 hash 与部分旧 conditions 中记录的值不同。
- [validation.json](validation.json)：本次实际执行的检查及未执行项。完整系统 benchmark 不属于这次脚本验证结果。

2026-09-21 已完成 `split` 路径的 base + Django 构建、Firecracker 启动、短轨迹 warm restore 与 CRIU lazy restore；[原始记录与范围](source-audit-20260921.md#构建与运行验证)单独保存。其它数据组、完整 cohort、OCI 从零构建、FC 注入均未因此获得整套 boot/replay 验证；master 的 80 个环境、完整 kernel 编译、Cube/E2B service 构建及 GPU 容器也没有宣称通过。当前可优先用历史盘推进 AE，同时冻结实际发布盘的整盘 hash，再跑最小 boot、δFS/CRIU、每组一个 replay 的验收。

`images/out/`、下载的 source cache、磁盘/快照二进制已被忽略；这里只 push 小型脚本、配置、原始配方、来源清单。
