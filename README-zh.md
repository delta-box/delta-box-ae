# DeltaBox — ATC 2026 实验材料

> **发布状态（2026-11-19）：** 新增 DeltaBox 启动层已通过功能验收，但 Zenodo 限制了上传访问，v4 尚未发布。[已发布的 v3](https://zenodo.org/records/24136571) 仍可使用。下文的 v4 下载器和 DeltaBox 直接启动命令需要待发布的镜像层；当前基线镜像可用 `python3 ae/appliance/download_appliance.py --output-dir ../images --extract` 下载。

DeltaBox 为 agent 沙箱提供文件系统与进程状态的 checkpoint、restore 和分支功能。本仓库包含运行时代码、预编译 DeltaFS 内核、实验驱动和分析脚本。[Zenodo v4](https://zenodo.org/records/24358847) 提供实验镜像及录制输入、原始结果。

环境有两种启动配置。DeltaBox 使用随附的 DeltaFS 内核，在 4 vCPU / 8 GiB 虚拟机中直接运行。基线配置启动 E2B、CubeSandbox、FC-Diff+dm、CRIU+cp 和 copy+replay 所需的服务。两种配置共用下载的基础磁盘，小型 qcow2 补充层提供 DeltaBox 启动入口和功能检查。

[English](README.md) | **简体中文**

## 下载环境

宿主机需要 x86-64 Linux、KVM、QEMU/KVM、`qemu-utils`、Python 3.10+ 和 `zstd`。启动账号需有 `/dev/kvm` 访问权限。建议预留约 200 GiB 磁盘空间用于下载、解压和基线工作文件。

```sh
git clone https://github.com/delta-box/delta-box-ae.git
cd delta-box-ae
python3 ae/appliance/download.py --output-dir ../images
```

下载器验证 SHA-256，并复用已验证的本地文件。基础镜像和 `deltabox-direct.qcow2` 应保存在同一目录。

## 直接运行 DeltaBox

启动前选择空闲的宿主机 CPU。下例的 CPU 列表需按自己的机器调整。

```sh
taskset -c 0-3 python3 ae/appliance/deltabox.py check --images ../images --log check.log
taskset -c 0-3 python3 ae/appliance/deltabox.py fanout --images ../images --log fanout.log
python3 ae/appliance/deltabox.py shell --images ../images --log shell.log
```

`check` 使用随附 runtime 做文件系统与进程耦合 checkpoint/restore，检查保存的文件恢复、之后新建的文件消失，以及进程内计数器从 checkpoint 继续运行。`fanout` 在 N=1/4/16/64 下验证所有子进程继承的 64 MiB 状态的每个页。成功时打印 `DELTABOX_DIRECT_EXIT=0`，命令退出码为 0。

交互 shell 提供 `/repo` 下的运行时代码、CRIU、`/mnt/base` 下的只读 rootfs，以及 `deltabox-check` 和 `deltabox-fanout` 命令。例如执行 `deltabox-fanout --forks 1 4 --out /tmp/forks.json`。沙箱可写状态位于 noswap tmpfs。命令结束后，启动器关闭本次虚拟机。上述检查用于验收实验材料功能，论文测量数据见下方说明。

## 运行五个基线

基线配置需要 KVM 嵌套支持、OpenSSH、`genisoimage` 和 `util-linux`。完整配置使用四个 NUMA 节点上的 48 个宿主机 CPU 和 120 GiB guest 内存。短程验收可添加 `--functional-only --memory-gib 64`，并指定 12 个空闲宿主机 CPU。

```sh
python3 ae/appliance/appliance.py start --image ../images/DeltaBox-ATC2026-v3-l1.qcow2 --cpus 12-35,56-67,72-83
python3 ae/appliance/appliance.py ssh
```

在虚拟机内从公开仓库执行。

```sh
cd /home/atc-ae/delta-box-ae
sudo python3 ae/appliance/reproduce.py e2b --smoke --out /home/atc-ae/delta-box-ae/ae/results/e2b-smoke
```

可将 `e2b` 替换为 `cube`、`fc-diff`、`criu` 或 `replay`。`table3` 执行 CRIU 推理重叠实验。去掉 `--smoke` 运行录制的完整 12 条输入，也可用 `--instance` 选择输入。每次使用新的输出目录。适配器按记录的虚拟 NUMA 分配串行执行轨迹。详细放置、计时和原始编排见[数据来源](ae/refresh-20261111/README.md)。

在宿主机执行 `python3 ae/appliance/appliance.py stop` 可关闭基线配置。

## 原始结果与最终论文

归档包含线上最终稿采用的 11 月 baseline 原始记录、DeltaBox 历史记录和 Figure 1/7/8/9 输入。[数据索引](ae/refresh-20261111/README.md) 标明采用的批次及精确数值。线上 Table 2 的 CRIU 为 567.6/609.5 ms，overlap 批次提供 Table 3 的 91.1 ms 推理后剩余等待。

从 [Zenodo](https://zenodo.org/records/24358847) 下载 `DeltaBox-ATC2026-v3-source-data.tar.zst`，解压到独立目录。

```sh
mkdir recorded
zstd -d --stdout DeltaBox-ATC2026-v3-source-data.tar.zst | tar -xf - -C recorded
python3 recorded/delta-box-ae/ae/appliance/verify_results.py --evidence recorded/delta-box-ae/ae/refresh-20261111/evidence --out recorded/recomputed.json
```

安装 `numpy` 和 `matplotlib` 后，在本仓库执行 `python3 ae/appliance/draw_figures.py` 可绘制当前图件。

[镜像构建与验收](ae/appliance/direct/README.md) · [历史托管机器说明](README-zh-v2.md) · [实验总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)
