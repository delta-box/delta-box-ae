# Figure 8：fan-out、GPU 时延与理论占用率

完整一键命令 `bash ae/run_all.sh` 或 `bash ae/run_all.sh --group figure-08` 会依次完成 CPU fan-out、GPU 生成/训练和理论计算，并汇入同一份中英文对比页。下面保留各阶段的底层入口，供自建环境与定向诊断使用：

| 面板 | 含义 | 资源与入口 |
|---|---|---|
| (a) | sandbox fan-out 的实际就绪时间 | 原有 CPU/KVM/Cube/E2B 入口 |
| (b) | Qwen2.5-7B 的生成与 LoRA 训练时延 | 需要真实 GPU；`ae/runners/gpu_timing.py` |
| (c) | 按论文 Equation 1 计算的预期同步占用率 | 仅需 CPU；`ae/repro/gpu_occupation.py` |

**CPU (a) 已完成重测：**`c775a9215718` 批次的 DeltaBox、Cube、E2B 在 N=1/4/16/64 均通过内容校验，图表和原始归档见[统一结果目录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/c775a9215718/README.md)。GPU (b) 已在 allinai2plus 的 H20 完成单卡生成及 B1/B4 训练，B16/B64 四卡训练仍缺失；最新测量与协议偏差见[统一总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md#figure-8b-gpu)。

以下命令均从仓库根目录执行。GPU 设备、模型与 Python 环境通过主配置的 `gpu.config` 指定，配置方法见[自建环境指南](../../docs/self-hosting-zh.md#gpu-setup)。GPU 资源不足或繁忙时，联系作者分配资源后续跑。

## 先运行理论计算，不需要 GPU

```sh
python3 ae/repro/gpu_occupation.py \
  --input ae/configs/figure08-theory-paper.json \
  --output ae/results/figure08-theory-reference
```

安装 `ae/requirements-analysis.txt` 中的绘图依赖后，加 `--plot` 可生成 PNG/PDF。输出目录必须是新目录。

输入文件使用随仓库归档的原始记录，逐项保留 SHA-256。它计算：

```text
U_sync = (T_gen + T_train) / (T_sandbox + T_gen + T_train)
S = (T_sandbox + T_gen) / T_train
```

其中 S 是论文相同输入推导的 policy-version staleness。计算结果是理论模型，不是新测的 GPU utilization，也不是端到端 RL 实测。模型按阶段时间计算，没有根据各阶段 GPU 卡数加权。历史 E2B N64 仍标为由两次 N16 测量推导的估计值。

## GPU 测量脚本

先在任意 CPU 机器上查看计划：

```sh
python3 ae/runners/gpu_timing.py plan
python3 ae/runners/gpu_timing.py plan --test
```

默认生成八个任务：

| 阶段 | Batch | GPU 数 | 原始实验配置 |
|---|---|---:|---|
| generation | 1 / 4 / 16 / 64 | 1 | vLLM、BF16、3 次测量 |
| training | 1 / 4 | 1 | LoRA-r16、序列长 768、5 次测量 |
| training | 16 / 64 | 4 | FSDP FULL_SHARD、每卡 microbatch 4、累积 1 / 4、5 次测量 |

训练包括 forward、backward、同步和 AdamW 更新，加载及 warmup 不计入时延。四张卡需在同一节点。训练使用 q/k/v/o LoRA、alpha32、gradient checkpointing 和学习率 1e-4。

**生成长度：**原脚本用文本重复方式构造“nominal 256”输入，并将 model context 设为 832。虽然请求 max output 512，归档记录每请求实际只输出约 307–308 tokens。默认 `paper-template` 保留原模板，并明确按剩余上下文限制输出、记录真实 token 数。`--prompt-mode fixed-tokens` 则生成精确 256 输入 / 512 输出；这是不同协议，不能与旧结果混作相同实验。

**前缀缓存：**默认配置显式设置 `generation.enable_prefix_caching=true`，对应历史 vLLM 的开启状态；实际值写入 `config.json` 和逐任务 `generation_protocol.prefix_caching`。这些 prompts 共享长前缀，重复测量又使用同一批输入，因此缓存开关会显著影响大 batch 耗时。禁用缓存的诊断应另建配置，显式设为 `false` 并使用独立输出目录。旧版本未记录该配置项，复跑旧的禁缓存协议时必须显式补上 `false`。

生成保留历史的最多两个请求预热；它不保证覆盖大 batch 的全部首次 JIT 开销，正式样本不剔除慢点。当前仍显式设置请求 seed，与历史未设置请求 seed 有差异。软件版本、配置差异和缓存对照证据统一记录在[总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md#figure-8b-gpu)。

## GPU 机器需要提供什么

- 单节点四张可独占的完整 NVIDIA GPU，支持 BF16 和 NCCL；论文使用 H20、96 GB/卡。其他型号也可记录为新硬件条件。
- 原记录的单卡训练 peak allocated 约 38 GiB，四卡 FSDP 约 34 GiB/卡；generation 在 96 GB 卡上配置了 0.5 显存利用率。这些不含所有驱动/缓存保留空间，不能当作最低显存保证。建议提供 80/96 GB 卡；小卡先运行 B1 快速检查，不自动缩小正式参数。
- 本地 `Qwen2.5-7B-Instruct` Hugging Face 权重、config 与 tokenizer 文件。脚本不自动下载模型。
- Linux、Python 3.10+ 和匹配驱动的 CUDA 环境。generation 需要 vLLM；training 需要 torch、transformers、peft、accelerate。可使用两个独立 Python 环境。
- 建议主机 RAM 至少 128 GB：FSDP 初始化时每个 rank 会加载模型。另需模型所在磁盘和可写结果目录。GPU 机器不需要部署 sandbox/KVM 服务来测量 (b)。

`ae/requirements-figure08-generation.txt`、`ae/requirements-figure08-training.txt` 是环境准备约束，不是找回的历史依赖锁。实际包版本、CUDA、GPU 身份、模型文件哈希和源码身份会随运行记录保存。GPU 环境验证后再固定实际环境。

## 获得 GPU 后的命令

将模型路径、分配的卡号和 Python 路径替换为实际值。设备也可用完整 GPU UUID，脚本不支持 MIG 切片。不要选择其他任务正在使用的卡。

```sh
python3 ae/runners/gpu_timing.py check \
  --model /models/Qwen2.5-7B-Instruct --devices 0,1,2,3 \
  --generation-python /envs/vllm/bin/python \
  --training-python /envs/lora/bin/python

# 先验证单卡完整调用链，不代表八个正式任务完成。
python3 ae/runners/gpu_timing.py run --test \
  --model /models/Qwen2.5-7B-Instruct --devices 0 \
  --generation-python /envs/vllm/bin/python \
  --training-python /envs/lora/bin/python \
  --output ae/results/figure08-gpu-check

# 完整矩阵；每个任务单独进程，串行运行以释放模型显存。
python3 ae/runners/gpu_timing.py run \
  --model /models/Qwen2.5-7B-Instruct --devices 0,1,2,3 \
  --generation-python /envs/vllm/bin/python \
  --training-python /envs/lora/bin/python \
  --output ae/results/figure08-gpu-full

python3 ae/runners/gpu_timing.py plot \
  --input ae/results/figure08-gpu-full/summary.json \
  --output ae/results/figure08-gpu-full-plots
```

`check` 只检查文件、包元数据和 NVIDIA 状态，不加载模型。实际执行才验证 CUDA/BF16。OOM、超时、输出缺失或卡上遗留进程均记录为失败，不把小 batch 外推成大 batch。默认遇到失败停止；`--keep-going` 可保留其它独立任务结果，但整轮仍失败。

运行器自动把所选 Python 的 `bin` 目录加入子进程 PATH，使 `ninja` 等 JIT 工具可用；仍需根据机器设置正确的 CUDA 工具链（例如 `CUDA_HOME=/usr/local/cuda-13.0`）。allinai2plus 已验证的生成环境为 `py312`：vLLM 0.21.0、torch 2.11.0、transformers 5.8.1。依赖约束允许这一组合，但不是历史完整软件锁。

输出包括 `summary.json`、固定的 `config.json`、模型文件哈希、每个任务的 `stdout.log` / `process.json` / `result.json`。运行器保存物理 GPU UUID；为兼容 vLLM，子进程使用数字卡号和 PCI 顺序，并在加载模型前核对 CUDA 实际 UUID。完整 case 参数、软件版本、实际设备和重复编号均校验后才采纳计时。模型哈希在运行前后核对，开销不计入每个 case 的 GPU 时延。复制完整结果目录到分析机以保留原始证据。

CPU/NUMA 绑定可由 GPU 机器的调度器或外层 numactl 设置。输出保存主进程 CPU affinity 与允许内存节点；实际内存分配策略可另保存 `numactl --show`。GPU 机器应按本机拓扑选择 CPU 编号。本次 spr4numa 的 CPU 脚本验证统一在 `/mnt/disk2/dyp/deltabox-runtime` 内执行，使用 NUMA3 / CPU 72–75 / 内存 bind:3，结果保存在该仓库的 `ae/results/`。

## 用新测量计算 (c)

先由现有 CPU 实验生成包含 `figure-08` fanout series 的分析 `summary.json`，然后在 CPU 机器执行：

```sh
python3 ae/repro/gpu_occupation.py \
  --gpu-results ae/results/figure08-gpu-full/summary.json \
  --fanout-summary /path/to/cpu-analysis/summary.json \
  --output ae/results/figure08-theory-new --plot
```

缺失或重复的输入不会被补成零，失败的 GPU 任务不会成为有效时延。历史输入、新测输入和混合输入分别标记；(c) 始终标为理论推导。

原有九输入 fork primitive 与 synthetic fanout 数据保持独立。`data/` 是不进入 Git 的解包目录，每个记录的来源与哈希见 `files.jsonl`。需要解包原始数据时运行 `python3 ae/reproduce.py prepare`。
