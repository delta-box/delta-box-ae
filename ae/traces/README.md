# Trace 分区与实际数量

按实验用途和原始 workload 分组展示已保存的数据。数量来自各实验清单或结果汇总；各行是独立 cohort，不能相加当作独立样本量。

**论文中的 `30-iteration` 指迭代轮数，不是 30 条独立轨迹。** 这是输入盘点，尚不代表所有历史运行已与现存文件版本逐一绑定或全部论文结果已复现。

## 主对比记录

| 系统 / 记录 | Django | SymPy | Scientific | Tools/Small | 合计 |
|---|---:|---:|---:|---:|---:|
| [DeltaBox](../provenance/79-20260920/cohorts/table2-deltabox-12.csv) | 2 | 2 | 5 | 3 | 12 |
| [Cube canonical](../provenance/79-20260920/cohorts/table2-cube-canonical-12.csv) | 2 | 2 | 5 | 3 | 12 |
| [E2B（论文原表）](../provenance/79-20260920/cohorts/table2-e2b-original-8.csv) | 2 | 2 | 2 | 2 | 8 |
| [FC-Diff（聚合 OK）](../provenance/79-20260920/cohorts/table2-fc-actual.csv) | 103 | 45 | 47 | 43 | 238 |
| [CRIU（聚合 ok，含 4 条 TAIL_CRASH_OK）](../provenance/79-20260920/cohorts/table2-criu-all-attempts.csv) | 105 | 46 | 47 | 43 | 241 |
| [Replay（汇总记录）](../provenance/79-20260920/cohorts/table2-replay-actual.csv) | 106 | 48 | 47 | 43 | 244 |
| [E2B（另一轮 P-EAGLE，非论文原表）](../provenance/79-20260920/cohorts/e2b-separate-peagle-12.csv) | 2 | 2 | 5 | 3 | 12 |

- DeltaBox/Cube 的 12 个实例及 schedule 对齐；E2B 原表 8 个实例与这 12 个没有交集。另一轮 E2B 12 个实例单独列出，未替换原表来源。
- FC/CRIU/Replay 的计划清单均为 244 个实例；FC 聚合 238，CRIU 237 条 OK + 4 条 TAIL_CRASH_OK，另 3 条失败，Replay 汇总 244。
- workload 映射取自保存的 `table2_family_aggregate_zero_llm.json`。该历史映射将 seaborn、flask、sphinx 等放入 Tools/Small；这里保留原始聚合口径。
- 相同实例名不足以证明 trace 相同。当前 ms 与旧 portable 包的 244 个共同实例动作树均不同；两份快照都保留，历史输入绑定限制见 [核对报告](../docs/trace-audit.md)。

## 机制实验记录

| 分区 | 轨迹 / 实例数 | 计数说明 |
|---|---:|---|
| [Fig. 2 文件系统](../provenance/79-20260920/cohorts/fig2-filesystem-30.csv) | 30 | 30 个实例的文件系统 profiling |
| [Fig. 2 内存](../provenance/79-20260920/cohorts/fig2-memory-5.csv) | 5 | 5 个实例的独立内存 profiling；不是从同一次 30 条实验任意截取的结果 |
| [Fig. 6 内存曲线](../provenance/79-20260920/cohorts/fig6-memory-1.csv) | 1 | 1 条 SymPy trace，4 种策略配置；配置数不是轨迹数 |
| [Fig. 6 adaptive](../provenance/79-20260920/cohorts/fig6-adaptive-source-12.csv) | 12 | Claude/MiMo MCTS，12 个实例 |
| [RL fork primitive](../provenance/79-20260920/cohorts/rl-fanout-9.csv) | 9 | 9 条 trajectory 作为 donor 状态；非完整动作 replay |
| [Fig. 9 WAR](../provenance/79-20260920/cohorts/fig9-war-inputs.csv) | 185 | 185 个 pool+instance 输入，136 个不同实例；三个文件系统的运行不重复计作轨迹 |

## 读取方法

每个分区链接到 CSV，`local` 列给出相对于 AE 仓库根目录的文件路径，`sha256` 给出文件校验值。`input_binding` 列说明历史版本绑定的限制。原始文件保留在 `raw/` 与 `supplement/` 中，分区清单引用原文件。

两种复现范围必须分别标注：已有结果的重绘/复算，以及真正重新执行的实验。仅从保存结果画图不等于重新运行系统。

AE 范围与公开先例见 [说明](../docs/ae-scope-precedents.md)。

在仓库根目录执行 `python3 scripts/build_trace_index.py` 可重建本页。
