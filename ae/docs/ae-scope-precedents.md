# AE 范围、样本数与公开先例

核查日期：2026-09-20。此文说明当前数据的展示方式与可借鉴的公开做法，不是 AEC 对本 artifact 的预先认可。

## 先确认论文写的是什么

`atc26-paper158.pdf` 第 10 页 §6.2.1 与 Fig. 7 使用 `30-iteration`，指每条 MCTS 轨迹的迭代预算/轮数，并没有在这些位置声称存在 30 条独立轨迹。§6.2.2 明确写 9 条 SWE-bench MCTS trajectories；对应输入和逐条 fork primitive 记录已找到。

现存 Fig. 2 文件系统 profiling 汇总涉及 30 个实例，内存 profiling 涉及另一批 5 个实例。应分别展示其统计口径，不应把两者统称为同一批 30 条实验。

## 三种不同情况

| 实际情况 | 如实表述与处理 |
|---|---|
| 原实验确实跑了 30 条，AE 为节省时间选 5 条执行 | 明示原实验 N=30、AE 子集 N=5、具体选择规则及验证范围；保留已存在的原始结果与完整配置 |
| 原实验可能跑了 30 条，但现在只找到 5 条记录 | 写明目前可核实 5 条、其余记录未定位；不能直接断言其余没跑，也不能无依据称为有意抽样 |
| 论文明确写跑了 30 条，实际上仅跑了 5 条 | 将对应样本数和相关结论更正为实际证据支持的范围；不能通过把目录命名为 subset 来解决。涉及录用论文实质性结果变更时，应向 shepherd/PC 与 AE chair 说明并确认更正方式 |

如果真实样本数变化会改变均值、置信区间或加速比，应基于实际已有结果重新计算并清楚标注。这里的复算不要求重新采集实验，但也不能保证原结论保持不变。

## 官方规则和可核实先例

1. **ATC 2026** 的 Functional checklist 要求为论文每类实验提供示例输入与配置，鼓励但不强制提供全部实验的输入、配置和输出；同时要求相关材料、数据修改说明，以及代码与论文主张的对应关系。Reproduced 另有执行与生成结果要求。依据：[官方 badge checklist](https://sysartifacts.github.io/atc2026/badges)。这支持明确界定复现范围，并不授权夸大实际样本量。

2. **OSDI 2025 Nos / Stripeless Data Placement** 的作者 README 说明 AE 执行覆盖各图的实验子集，约 70 分钟，并明确不申请 Reproduced。官方结果列为 Available + Functional。依据：[作者 artifact](https://github.com/IcicleF/Nos#evaluating-the-artifact)、[OSDI 2025 官方结果](https://sysartifacts.github.io/osdi2025/results)。这是“缩小 AE 验证范围”的公开案例，不能推断其原论文少跑了数据。

3. **OSDI 2024 EPIC / Massively Parallel Multi-Versioned Transaction Processing** 提供约 6 分钟的实验子集检查，以及约 5 小时的完整运行，两者在文档中分开。依据：[作者 artifact](https://github.com/ShujianQian/epic-artifact#3-run-experiments)。这说明 quick/full 分开组织有先例；只执行 quick 不自动等于复现完整论文结果。

本次没有找到能够支持“实际只测 5 条但仍保留测过 30 条的描述可被接受”的公开依据。也不能将其他会议或其他 artifact 的 badge 结果当作 ATC 2026 的通过承诺。

ATC 2026 对 badge 组合有特定约束：软件 artifact 通常以 Available + Functional + Reproduced 为目标，若复现评估失败可只授予 Available + Functional；官方对直接申请后者列出了适用情形。不要仅因时间不足就假定软件 artifact 可以任意改变申请组合。[官方说明](https://sysartifacts.github.io/atc2026/badges)

## 当前采用的展示方法

- 按实验/图表分区，分开列出真实可定位的轨迹数、实例数、迭代预算、运行重复数、成功/失败数。
- 主对比再按 Django、SymPy、Scientific、Tools/Small 展示各 backend 实际数量；明确不同 cohort 和已知输入版本差异。
- 只从原始结果重绘的项目写明“已有结果复算”，可执行但尚未在 AE 环境重跑的项目写明状态，不预先声称已经独立复现。
- 不为补数量复制轨迹，不把 5 条 × 6 次重复写成 30 条独立轨迹，不把同实例不同录制批次无说明合并。

已落实到 [trace 分区入口](../traces/README.md)。此次仅整理已有证据与展示方式，没有启动新实验，没有修改投稿 PDF，也没有向会议人员发送消息。
