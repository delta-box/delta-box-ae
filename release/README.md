# 源码冻结与验收范围

当前发布仓库是 [GitHub delta-box/deltabox-runtime](https://github.com/delta-box/deltabox-runtime)，评审入口在 `main` 和 `feat/ae-oneclick-validation`。日常 AE 运行记录当前 checkout 的提交及实际源码哈希，不再要求匹配 `candidate-lock.json`；旧锁仅保留为历史归档审计材料。当前 CPU 环境已配齐依赖，**完整 1,404 作业性能评测尚未执行**，源码锁保留 `candidate-not-final` 状态。

spr4numa 的开发和验收统一使用 `/mnt/disk2/dyp/deltabox-runtime`。托管配置是 `/etc/deltabox-ae/review.json`，可公开的路径说明见 [`spr4numa-review.json`](../ae/configs/spr4numa-review.json)。API 凭据留在机器的私有环境文件中，不进入仓库。评审账号登录后直接执行：

```bash
cd ~/delta-box-ae
bash ae/run_all.sh
```

该命令等价于 `--all`，严格运行 17 个 CPU 入口，自动分析、绘图并生成论文与本次结果的左右对比图。托管入口不接受 `--available`；缺依赖或运行失败会保留日志并返回非零。自建环境的准备和部分运行方式见 [AE README](../ae/README.md)。

2026-09-23，发布源码 `bf1dcbe` 的完整前置检查为 **17/17 个入口、1,404/1,404 个计划作业可用，0 缺项**。真实评审账号已在 `c775a92` 完成 **17 个入口、23 个作业**的首输入验收，以及 **12/12 个 DeltaBox 输入**各两次 checkpoint、一次 restore；`bf1dcbe` 仅修改两个绘图文件的留白，其余锁定源码逐字节未变，并通过了 36 项绘图检查和真实账号的最小 C/R 与出图检查。测量与绘图各自的来源见[托管环境报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/hosted-environment-20260922/README.md)。每组首输入和短事件检查不能代替全量性能评测。

异步增量和测试 runtime 的历史验证见[异步增量报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/async-incremental-20260922/README.md)、[baseline 报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/baseline-runtime-20260922/README.md)、[修复验收报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/replay-fixes-20260921/README.md)及[消息审计报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/replay-audit-20260921/README.md)。这些批次使用各自的配置和源码锁，旧版本 warm/cold 数据保持原测量身份。

上一轮 `feat/unified-release` 的被测源码为 `5d6ef73a848e085382573b89442a52105cf54ad4`；
[原源码锁](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/first-round-20260921/candidate-lock-original.json)、
[Table 2 复测](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/unified-release-20260921/README.md)和
[首轮各图表检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/first-round-20260921/README.md)保持历史身份，不能算作本次修复版结果。

论文 CPU 实验使用 trace replay，本轮不发起新的真实 LLM 调用。[Figure 8(b)](../ae/paper/figure-08/README.md) 已提供独立 GPU 计时脚本，实际 GPU 测量待资源就绪；Figure 8(c) 的理论计算可在 CPU 上执行，不计作 GPU 实测。

2026-09-22 的 live WorkerClient 优化将 restore 后的固定等待改为就绪握手，并增加分支恢复总耗时字段。
NUMA3 上的 205 项单测及 96 次真实 warm/cold 恢复对照通过，范围、原始数据和端到端性能见
[worker reopen 验证](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/worker-reopen-20260922/README.md)。这不重标历史论文 replay 数据。

## 2026-09-22 绘图格式更新

Table 2、Table 3、Figure 1 的默认 fresh 输出按论文布局生成，覆盖信息和逐格来源保存在 manifest。此次只改绘图、图像比较和验证测试，运行时与实验配置未变；综合源码锁包含绘图文件，因此更新了待测锁。新图仍使用原 d59dab3 / e0b07eb 测量身份，不重标为新版本实测，也没有重新执行实验。重绘说明见 [论文格式报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/paper-layout-20260922/README.md)。

## 两个入口、一份核心

`agent/` 保存真实 LLM 入口、NPD、ReAct worker、MCTS 和 driver；`replay/` 保存固定 trace/schedule
驱动和精简 guest。两者都调用同一份 `backends/deltabox/gsd/` 和 `pycriu/`。
默认 checkpoint 协议来自 `common/runtime_profile.py`，为固定 PID 100 的增量配置，prewarm off。
历史 async-full 是明确的独立对照 profile，不能用它补齐增量验证。

`candidate-lock.json`（生成后纳入 git）记录源代码提交、源树和每个源文件/兼容链接的身份。
文档和结果提交可以后续增加，但被锁定的实现文件必须保持一致。
镜像、kernel、trace/schedule 和实际配置的哈希由各 runner 记录；源码锁不能替代它们。

```bash
# 可选：审计历史锁；不是日常运行的前置条件。
python3 release/lock.py verify
```

开发者完成源码提交后才能生成新的源码锁；`python3 release/lock.py create --file PATH` 只接受尚不存在的目标文件。更换锁以后，必须用新结果目录验证改动涉及的实验。当前配置的完整一键入口见上文；下面保留历史 `spr4numa-replay-fixes.json` 的手动复测命令，不能把它的结果重标为当前托管配置。

```bash
python3 ae/reproduce.py prepare
python3 replay/run_release.py --config ae/configs/spr4numa-replay-fixes.json --plan --out ae/results/release-table2
sudo -n python3 replay/run_release.py --config ae/configs/spr4numa-replay-fixes.json --out ae/results/release-table2
```

`run_release.py` 每次启动及 suite 每个 job 前验证源码锁；实验结束再次验证。
默认跑 Table 2 DeltaBox 12 条完整轨迹（317 checkpoint / 334 restore），NUMA 2 / CPU 52–55，
请求最高 P-state 并保存实际频率。结果目录不可复用。`--limit`、`--max-events` 总是标为 快速检查。
`--plan` 只生成计划。本轮配置和镜像路径见 `ae/configs/spr4numa-replay-fixes.json`。

```bash
# Table 3 slow 使用同一份实现，强制选择 cold CRIU 路径
sudo -n python3 replay/run_release.py --config ae/configs/spr4numa-replay-fixes.json --experiment table-03-slow --out ae/results/release-slow
# 所有 CPU 项，包括 baseline；未满足的依赖不能记为通过
sudo -n python3 replay/run_release.py --config ae/configs/spr4numa-replay-fixes.json --all --out ae/results/release-all-cpu
# 从新结果生成分析和图，不混入历史结果
python3 ae/reproduce.py analyze --source fresh --input ae/results/release-table2/suite --output ae/results/release-table2/analysis
python3 ae/reproduce.py plot --input ae/results/release-table2/analysis/summary.json --output ae/results/release-table2/plots
```

论文图表与实验入口的完整映射见 [AE README](../ae/README.md#实验索引)。

## 已验证范围与完整评测要求

| 项目 | 要求 / 当前边界 |
|---|---|
| 源码统一 | live/replay 共享核心，源码锁校验；历史多版本结果不计为候选证据 |
| Table 2 / Table 3 fast | 新锁下完整 Django warm 通过（29 checkpoint / 28 restore）；仍需 12 条完整 replay 及 parent pages 复用验证 |
| Table 3 slow | 控制通道和 cold 回收屏障已修复；新锁下完整 Django 29 checkpoint / 28 cold restore 通过；其余 cohort 待验收 |
| 增量链与异步 dump | 默认旧 profile 保持历史语义；新 async-incremental profile 通过独立页链、真实 warm/cold 与有界队列检查，范围及源锁见本轮报告；完整 cohort 尚待验证 |
| Figure 2 / 6 / 7 / 8 CPU / 9 | `c775a92` 首输入验收通过，保留 Figure 6 全部策略、fan-out 四个 N 和 Figure 9 三种文件系统；Figure 7 从该轮有效轨迹派生，完整 cohort 未完成 |
| 文件系统正确性 | 独立内核修复旧 FD 重绑定；4 项严格语义和 3 套 recovered 脚本通过，仍非 53-case 全通过 |
| Figure 6 内存适配器 | restore 改为调用当前核心，初始化和运行失败清理已补齐；实机范围见本轮报告 |
| Figure 6 CoW warm | 历史 live-memory write-prewarm 的数据竞争不在环境准备范围内；当前实验使用记录的策略与 profile，不能据此宣称所有预热实现已验证 |
| baseline | Replay、CRIU、FC-Diff、Cube、E2B 已在 `c775a92` 完成各自首输入；Cube 流式 SDK 与 E2B 服务、容量配置已修复。完整 cohort 和所有工作负载测试仍需逐项执行 |
| 真实 LLM agent | 仅代码整理和无 AK 单元测试；按用户要求不做服务端到端验证 |
| GPU | Figure 8(b) 的脚本已提供，实际 GPU 测量待资源；Figure 8(c) CPU 理论计算和历史输入校验不计作新 GPU 数据 |

性能修复后应重跑受影响实验；新结果自动记录实际源码身份，不必重建源码锁，不能把旧结果重标成新结果。
测试通过、目录完成和短测通过都不足以声称“所有论文数据已复现”。
