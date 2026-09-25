# 自建环境与定向运行

[English](self-hosting.md) · [返回 AE 指南](../../README-zh.md)

托管评审机器已提供实验环境。仅在自己的 Linux 机器上部署、选择特定配置或重新分析结果时使用本页；所有命令均从仓库根目录执行。

## 1. 准备主机与输入

需要 Linux x86-64、可用的 `/dev/kvm`、Firecracker、Python 3.10+、sudo 权限，以及 XFS/ext4、device-mapper、CRIU 和网络命名空间工具。从 OCI 构建镜像还需要 Docker。软件安装和各 backend 的要求见[镜像与模板指南](../images/README.md)。

DeltaBox 通常配置 4 vCPU、8 GiB guest RAM，宿主还需容纳运行器、镜像和临时状态。预留至少 80 GiB 用于镜像构建，实验工作盘、dump 和缓存另计。GPU 资源要求见[GPU 指南](../paper/figure-08/README.md)。

```bash
git clone --branch main https://github.com/delta-box/delta-box-ae.git
cd delta-box-ae
python3 -m venv .venv
.venv/bin/pip install -r ae/requirements-analysis.txt
.venv/bin/python ae/reproduce.py prepare
.venv/bin/python ae/scripts/paper_data.py verify

firecracker --version
sudo test -r /dev/kvm
sudo test -w /dev/kvm
```

先取得仓库访问权限。数据包位于 `ae/datasets/`，`prepare` 会校验并解包。修改版 guest 内核位于 `linux/vmlinux`。base/data 磁盘和完整 host 工作负载环境需从作者取得，或按镜像指南构建。

## 2. 构建 guest 镜像

<a id="images"></a>

使用配套 XFS 母盘与 DeltaBox guest 内核构建 base 和分组 data 盘。输出目录必须尚不存在：

```bash
bash ae/build_images.sh \
  --master-xfs /path/to/ubuntu-24.04.xfs \
  --kernel "$PWD/linux/vmlinux" \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" \
  --output "$PWD/ae/work/images-local"
```

`--ssh-pubkey` 是 host→guest 通信使用的公钥，对应私钥由运行实验的 host 账号保留。没有密钥时先生成自己的密钥；不要覆盖已有密钥。

构建器核对选定输入的哈希，输出 `bundle.json`、日志和 `config.json`。需要编译内核时，用 `--kernel-source /path/to/clean-linux-6.8` 替代 `--kernel`。从 OCI 或公开基础镜像构建的入口见[构建指南](../images/README.md#统一构建入口)；镜像来源和方法差异统一见[测量报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md#conditions)。

## 3. 配置 baseline 与测量资源

构建器不部署 Cube/E2B 服务。按所选实验准备：

| Backend | 需要配置 |
| --- | --- |
| DeltaBox | kernel、base/data 盘、guest SSH、CRIU |
| Replay / CRIU | host 工作负载 payload、Python 环境、repo/index；CRIU 另需 host CRIU 与 rsync |
| FC-Diff | device-mapper thin、loop device 与注入 replay 环境的 VM 镜像 |
| Cube | SDK、API、proxy 与对应工作负载模板；fan-out 使用独立模板 |
| E2B | self-hosted infra、父镜像、resume binary；fan-out 另需 SDK/API、模板和凭据 |

具体字段与构建入口见[镜像指南](../images/README.md)。在配置文件中填写本机路径；凭据通过环境提供。

在配置的 `measurement` 中填写同一 NUMA 节点的可用 CPU。例如下面的编号只是示例，须先用 `numactl --hardware` 核对：

```json
{
  "measurement": {"pin": true, "numa_node": 0, "cpus": "0-3"}
}
```

开启绑定需要 `numactl`、`turbostat` 和 cpufreq 控制权限。入口保存原配置、采样实际频率，并在正常退出或可处理的中断后恢复。选择 CPU 时避开其他任务；Cube/E2B 服务端也要使用对应资源配置。

```bash
export AE_PYTHON="$PWD/.venv/bin/python"
export AE_CONFIG="$PWD/ae/work/images-local/config.json"

sudo -v
sudo -E "$AE_PYTHON" ae/reproduce.py doctor --config "$AE_CONFIG" --all
"$AE_PYTHON" ae/reproduce.py plan --config "$AE_CONFIG" --all \
  > ae/work/local-cpu-plan.json
bash ae/run_test.sh --config "$AE_CONFIG"
```

`doctor` 检查依赖，`plan` 列出工作负载和命令，不启动实验。完成预检后，使用根 README 的单项命令；导出的 `AE_CONFIG` 会作为默认配置。

仅做功能检查时，可显式 `--no-pin`。自建机器允许用 `--available` 运行具备依赖的子集；完整实验使用默认严格模式。托管入口使用固定配置，不接受这些自建配置选项。

### 配置 GPU

<a id="gpu-setup"></a>

一键流程仅使用 auto 模式，通过 SSH 在 `allinai2plus` 探测 GPU 0–7；运行本机无需 GPU。复制并调整 `ae/configs/figure08-remote.json`，在主 `AE_CONFIG` 中设置 `"gpu_remote_config": "/absolute/path/to/remote.json"`。相对路径以主配置目录为基准。配置包含 SSH 主机、远端目录、模型和 Python 路径、空闲阈值及顶层版本约束；不再使用 `gpu.config` 或 `gpu.enabled`。

准备非交互 SSH、两端 rsync、本地绘图依赖和远端 GPU 环境。默认复用已部署的 py312，不自动安装软件；版本漂移会被预检记录。完整参数和准备说明见 [Figure 8 指南](../paper/figure-08/README.md)。

`--group cpu` 只选 CPU，`--group gpu` 自动运行远端生成/训练，`--group figure-08` 还包含 CPU fan-out，并在八个 GPU 案例与全部 CPU 输入齐全时推导 (c)。全忙跳过，1–3 张空闲卡运行六案例，四张运行八案例。GPU 失败不影响 CPU 退出码，状态与缺项写入 `result.md`；resume 使用新 GPU attempt，分析已有数据不启动 SSH 测量。

## 4. 专用配置与内存盘入口

<a id="specialized-runs"></a>

在自己管理、具备 sudo 权限的实验环境中，下面两个专用入口提供固定的内存盘配置。先检查配置中的内核、镜像、CPU/NUMA 和依赖路径：

```bash
# Table 3：异步增量与 lazy-pages 的 fast/slow 对照。
AE_CONFIG="$PWD/ae/configs/spr4numa-table3.json" \
  bash ae/run_table3.sh --output "$PWD/ae/results/table3-local"

# Figure 9：宿主活动盘与 guest loop 使用 noswap tmpfs。
bash ae/run_figure09.sh --config "$AE_CONFIG" \
  --output "$PWD/ae/results/figure09-local"
```

Table 3 的固定配置绑定 NUMA 2 / CPU 48–51；Figure 9 专用入口绑定 NUMA 2 / CPU 52–55。这些入口为 spr4numa 拓扑准备，不应原样用于拓扑不同的机器。临时测量 I/O 在内存盘中完成，永久结果仍写入指定目录。Table 3 的 profile 与计时定义见[方法说明](table3-method.md)。

## 5. 重新分析已有结果

<a id="reanalyze"></a>

在使用普通命令行入口的分析环境中，将已有运行目录与新的输出目录替换为实际路径：

```bash
bash ae/run_all.sh --analyze-existing /path/to/existing-run \
  --output "$PWD/ae/results/replot-local"
```

它只分析、绘图，不执行实验。托管的受限 launcher 不提供这个参数；评审者正常运行时已经自动出图，需要额外重绘可联系作者，或将完整结果目录带到分析环境中操作。

CPU VM 实验需要 Linux/KVM；已有结果的分析与绘图也可在 macOS 上进行。底层命令、图片发布方式见[发布指南](publish-results.md)和[绘图说明](paper-plotting-reference.md)。

自建机器请在配置中设置 `review.results_backup_root`，指定独立的备份文件系统。新的完整运行会先将旧 `ae/results` 复制并校验到该处，再清空工作目录；复制后目标至少须保留 10 GiB 可用空间。显式 `--output` 与 `--resume` 不触发整目录轮换。
