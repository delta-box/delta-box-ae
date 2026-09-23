# 79 机器 trace 核对记录（2026-09-20）

> 这是输入采集时的历史审计。后续已找到精确匹配 Table 2 的 DeltaBox legacy 批次，main 也恢复了 Replay portable 输入与 RTT 对应；当前结论统一见 [偏差总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md#conditions)，原采集证据保持不变。

论文基准：`atc26-paper158.pdf`。来源仅限 `dyp@192.168.12.79` 的 `/mnt/disk2/dyp`。本次保存输入、清单和选定结果，不运行远端实验，不修改远端文件。

**你的记忆是对的：不同实验用了不同录制批次，同一张对比表中也存在实例集合不同的情况。当前最需要处理的是输入与历史 run 的绑定，以及主对比实验的统一输入。** 不同机制实验可以选择不同且明确说明的子集；同一性能对比需要控制输入、运行语义与统计口径。

## 保存范围与校验

| 归档 | 来源内容 | 来源文件数 | 解包内容字节 |
|---|---|---:|---:|
| `mcts_trajectory_data_20260625_172222.tar.zst` | 79 上已有 MCTS 历史备份 | 26,796 | 7,650,081,741 |
| `spr4numa-paper-trace-supplement-20260920.tar.gz` | 当前工作目录中的补充 trace、manifest、schedule、选定结果与 Git 恢复记录 | 3,945 | 1,618,551,243 |
| `spr4numa-lw-trace-inputs-20260920.tar.gz` | 较早的自适应 checkpoint schedule 和来源说明 | 28 | 295,908 |

合计 **30,769 个来源文件、9,268,928,892 字节**，另有归档内采集索引。三个压缩包合计 444,045,397 字节。历史包 SHA-256 与远端已有 `.sha256` 相符；两份补充包全部 3,973 个来源文件的本地哈希均与采集时远端哈希相符。

详细证据：[下载记录](../provenance/79-20260920/downloads.json)、[归档校验值](../provenance/79-20260920/archive-checksums.sha256)、[主补充包校验](../provenance/79-20260920/supplement-verification.json)、[schedule 补充包校验](../provenance/79-20260920/lw-verification.json)。

两处可选来源目录不存在：Cube 的 `llm_rtt/`、DeltaBox 1x slow 的 `summaries/`。已在校验报告记录。它们不是缺失的原始 `trajectory.json`。此次建立的各 cohort 对应输入文件均能定位；对于部分历史 run，找到候选文件仍不等于证明它就是当时执行的版本。

历史 MCTS 包有 **1,491 个 trajectory 路径、1,147 个不同字节哈希**。加上补充池后为 **2,518 个路径、2,131 个不同字节哈希**。同一实例可以在不同模型、搜索配置、录制时间下出现多次；相同内容也有副本。不能用这些数字直接支撑论文的样本量声明。

## 各实验用的批次

表中链接指向可复算的输入清单，每条包含原路径、本地路径和 SHA-256。

| 实验/记录 | 实例或轨迹数量 | 来源与核对结论 |
|---|---:|---|
| [Table 2 DeltaBox](../provenance/79-20260920/cohorts/table2-deltabox-12.csv) | 12 | Qwen P-EAGLE `mcts-iter30`；manifest 共 317 checkpoint、334 restore |
| [Cube canonical 对齐运行](../provenance/79-20260920/cohorts/table2-cube-canonical-12.csv) | 12 | 与 DeltaBox 相同实例、相同 trace；12 份 replay schedule 的哈希也完全相同，且符合 selection 中记录的哈希 |
| [论文原表 E2B](../provenance/79-20260920/cohorts/table2-e2b-original-8.csv) | 8 | 普通 Qwen ms 批次；与上述 12 条**实例交集为 0**；8 份 pilot_result 和聚合已从 Git 找回 |
| [另一轮 E2B P-EAGLE](../provenance/79-20260920/cohorts/e2b-separate-peagle-12.csv) | 12 | 与 DeltaBox 的 12 个实例对齐，run_metadata 标注 12/12；这是另一轮运行，不能当作论文原表 E2B 的来源 |
| [FC-Diff 实际聚合](../provenance/79-20260920/cohorts/table2-fc-actual.csv) | 238 | 计划 244，实际聚合 238 条 OK |
| [CRIU 全部尝试](../provenance/79-20260920/cohorts/table2-criu-all-attempts.csv) | 244 尝试，241 标为 ok | 237 条 OK、4 条 TAIL_CRASH_OK、3 条 RC_1；不能统称 244 条成功 |
| [Replay 实际汇总](../provenance/79-20260920/cohorts/table2-replay-actual.csv) | 244 | 与 FC/CRIU 的计划实例清单相同；历史文件版本绑定仍需确认 |
| [RL fork primitive](../provenance/79-20260920/cohorts/rl-fanout-9.csv) | 9 | 找到明确的 9 条 manifest、输入和逐条运行记录；与主对比 12 条交集为 3 |
| [Fig. 2 文件系统](../provenance/79-20260920/cohorts/fig2-filesystem-30.csv) | 30 | `profile50-native-mcts30-20260609_015241`，目录名带 50，但汇总明确选 30 个实例 |
| [Fig. 2 内存](../provenance/79-20260920/cohorts/fig2-memory-5.csv) | 5 | `profile5-tree-rss-20260609_052851`，五个实例也出现在 DeltaBox 主对比中，但这五条 trace 的哈希均不同 |
| [Fig. 6 内存曲线](../provenance/79-20260920/cohorts/fig6-memory-1.csv) | 1 | SymPy-22840 P-EAGLE 输入，四种运行配置 |
| [Fig. 6 adaptive 来源](../provenance/79-20260920/cohorts/fig6-adaptive-source-12.csv) | 12 | 较早的 Claude/MiMo MCTS，Django/Xarray/SymPy 各 4；另有 Astropy probe，不计入此 12 条清单 |
| [Fig. 9 WAR 输入集](../provenance/79-20260920/cohorts/fig9-war-inputs.csv) | 185 个 pool+instance 输入，136 个不同实例 | Claude/MiMo linear 与 MCTS；185 份 edits 输入对应 555 个文件系统运行结果文件 |

完整统计及集合交集在 [cohort-summary.json](../provenance/79-20260920/cohort-summary.json)。CSV 的哈希交集比较的是**当前保存的候选输入文件**；若未找到历史输入哈希，不能由此推定当时 run 使用的文件未变。

WAR 每个文件系统有 646 条 edit 记录，其中 603 条 `applied_ok=true`。这是原始记录数量；复算论文曲线时还要复核绘图代码对失败应用、零分母等情况的筛选，不能将 646 当作 646 条成功独立轨迹。

## 同一实例还有不同版本的 trace

本次同时保存：

1. 当前目录：`d-overlayfs/traces/swe-search/qwen3-coder-30b-ms/<instance>/`，301 个实例。
2. 旧 portable 包：`spr_payload/det_traces/ms/<instance>/`，253 个实例。
3. 三个 baseline 的共同计划清单：244 个实例。

这 244 个实例在两个池中都存在，但 **244/244 的 trajectory 文件哈希不同，244/244 的树结构及 action 签名也不同**。签名只提取节点 ID、父子关系和 `action_steps[].action`，排除了 LLM 文本和时间元数据，因此差异不能仅解释成 JSON 排版变化。

例如 `django__django-14997`：当前普通 ms 的 `ms_trace.jsonl` 有 36 行，旧 portable 版本有 33 行；生成回复、搜索结果及动作记录也有差异。逐例结果见 [ordinary-vs-portable-244.csv](../provenance/79-20260920/ordinary-vs-portable-244.csv)。

检查当前 driver 后发现：FC、CRIU 和 `real_trace_runner.py` 默认从普通 ms 目录取输入，可被环境变量覆盖；旧的 `replay_copytree/trace_runner.py` 则直接读取 portable 包。证据为 [driver 输入路径摘录](../provenance/79-20260920/runner-input-references.json)，含文件哈希和行号。

**因此目前能确认“三个 baseline 的计划实例 ID 相同”，还不能确认“三个历史运行读到了逐字节相同的 trace”。** 当前文件、旧 portable 文件和历史结果全部保留。CSV 将当前 ms 作为与 real-run driver 默认路径一致的候选，同时提供 portable 候选路径；`input_binding` 明确标注缺少历史每条输入哈希。这也修正了此前仅凭 244 条清单可能产生的过强判断。

## 关于论文中的 9 条轨迹

找到了 `2026-06-10_rl_fanout_qwen_peagle_mcts30_table3/manifest_qwen_only_9.tsv`，9 个实例均有输入。`2026-06-10_rl_fanout_qwen_peagle_mcts30_vm_overlay` 另存逐实例、逐重复的结果，对应论文约 0.55–5.1 ms 的 fork 数值范围。

但 `bench_fanout_n_real.py` 的用法是将完整 trajectory JSON 加载进 donor heap 模拟状态，然后测 fork primitive，并非重放九条轨迹的全部动作。manifest 的 `actions_len=0` 需要结合这一实现解释，不能把它当作九条输入缺失，也不能把这一实验描述为完整 replay。

## 历史结果与 AE 新运行应分开

论文 Table 2 的 E2B 原始 8 条聚合给出 524.40/899.69 ms，与 PDF 的对应数字一致；另一轮同 12 实例的 E2B 虽然存在，但运行语义和结果不同。两套记录已分开放置，未替换原始来源。

DeltaBox 的历史说明写 317/334 个事件、10.83/1.86 ms；此前对当前候选完整结果复算得到约 8.32/1.40 ms，仍未完成对论文原始运行版本的定位。本次下载完成不代表所有论文数值已复现。

较早 adaptive 运行的 26 份 conditions 文件引用的 schedule 均已下载，26/26 的 schedule 哈希与 conditions 中记录一致。这种运行时留下的输入哈希，比目录名和实例名提供更强的绑定证据。

推进建议：

1. 从已定位的 P-EAGLE 12 条及其 schedule 冻结主对比输入；先审查另一轮 E2B 12 条的语义是否可直接复用。其他 backend 在同一 frozen cohort 上运行，完整记录失败与排除理由。
2. 每次新 run 保存 `instance_id + pool + trajectory_sha256 + schedule_sha256 + base_commit + runner_commit + config + seed`；LLM mock 响应及 RTT 输入也固定并哈希。结果文件引用该 manifest。
3. Fig. 2、adaptive、RL primitive、WAR 保留各自有目的的子集，明确为什么选这些输入、每个数字的计数单位，不强行要求所有机制实验都使用相同数量。
4. 保留论文原始结果与 AE 补充结果的对应表。无法绑定历史 trace 的地方如实列为待核查；新跑的结果单独标注，不倒填为当初已经运行的实验。

## 本地复查

在 AE 仓库根目录执行 `python3 scripts/audit_trace_cohorts.py` 重建所有 cohort 清单。该脚本读取本地保存的数据，不联系服务器，不运行下载下来的实验脚本。

原始 trace 目录加入 `.gitignore`，避免把约 9.3 GB 及未经检查的历史元数据直接推送到 Git；本地文件完整保留。清单、校验和、脚本和报告可进入仓库。这里尚未准备容器/VM 镜像、安装依赖或对外发布包。
