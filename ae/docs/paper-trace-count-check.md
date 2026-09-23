# 投稿 PDF 的 trace 数量声明核对

2026-09-20；基准为 `atc26-paper158.pdf` 的正文及图表说明。核对对象为已保存到 AE 仓库的输入和选定运行记录。

**结论：没有发现现有 trace 少于正文明确声明的独立轨迹数量。正文实验部分明确写出的独立轨迹数是 RL fork primitive 的 9 条，已找到 9 条输入和 9 份逐实例结果。正文中的 30 指 MCTS 迭代轮数，并不是独立轨迹数量。**

| PDF 位置 | 正文声明 | 现存证据 | 数量结论 |
|---|---|---|---|
| p.9 §6.1 | 四类 workload：Django、SymPy、Scientific、Tools/small repos | 主对比各 backend 的现存 cohort 都覆盖四类 | 类别数量齐全；不等于实例集合相同 |
| p.10 §6.2.1 与 Fig.7 | `30-iteration trajectory replays` / `30-iteration ... MCTS trajectories` | DeltaBox 12 条、E2B 对应普通 ms 8 条现存输入均有 `max_iterations=30`；各 JSON 树含根节点共 30 个节点 | 未声明必须有 30 条轨迹，没有“缺到只剩 5 条”的数量证据；配置与 30 轮预算相符 |
| p.10 §6.2.2 | `across 9 SWE-bench MCTS trajectories` | 明确 manifest 的 9 条输入均存在；VM overlay 下 9 份逐实例结果均引用对应输入路径，并包含 N=1/4/16/64 | 9/9 齐全 |
| p.4 §2.4 与 Fig.2 | 轨迹的逐步状态变化，未给出独立轨迹总数 | 文件系统 profiling 的 30 个实例、内存 profiling 的另一批 5 个实例均有输入及汇总 | 没有声明两部分都测 30 条，因此内存 5 条不构成少了 25 条 |
| p.8 Fig.6 | 四种保留策略的内存曲线，及 lightweight-skip 对比；未给出轨迹总数 | 内存曲线 1 条 SymPy 输入、4 种配置；adaptive 来源 12 个实例 | 无明确轨迹数量缺口；4 种配置不是 4 条轨迹 |
| p.11 Table2/Table3 | SWE-bench MCTS replay 的 per-event 均值；未规定每个 backend 的轨迹数相同 | DeltaBox/Cube 12，E2B 原表 8，FC 238，CRIU 241 条标为 ok，Replay 汇总 244 | 没有“声明 N 条、实际少于 N”的直接冲突；跨 backend 输入和统计范围仍需披露 |
| p.11–12 §6.3.2 与 Fig.9 | 真实 SWE-Search edits、三个文件系统、分 bin 中位数；未给出轨迹总数 | 185 个 pool+instance 输入、136 个不同实例、555 份三个文件系统的结果文件 | 未见明确声明数量缺口；edit 数、运行数不能当作独立轨迹数 |

### 不应与 trace 数量混淆的数字

- §6.2.2 的 N≤64 是每次 fork 的子 sandbox 数量，不要求提供 64 条独立 SWE-bench 轨迹。
- §6.3.3 的 53 是 δFS integration suite 的测试检查数量，不是 SWE-bench trace 数。本次数量检查不宣称这 53 项已经逐项验收。
- 迭代预算、JSON 树节点数量、模型调用数量、checkpoint/restore 事件数量和重复运行次数是不同单位。尤其树节点计数含 root，不能直接当作执行了 30 次 checkpoint 的证明。

### 本结论的边界

这次排除的是“正文明确承诺的独立轨迹数量不足”这一具体问题，并不等于全部实验声明已验证。仍有以下已知项：

1. DeltaBox/Cube 的 12 条与 E2B 原表的 8 条不是相同 cohort。
2. 同名实例在当前 ms 和旧 portable 包中的内容不同；部分历史运行缺少输入文件哈希。当前输入数量齐全，尚不能证明就是历史运行时的同一版本。
3. 正文“所有 replays 零 mismatch”需要完整运行日志支持；现有 baseline 还包含失败/尾部异常的尝试，不能用输入文件齐全替代成功运行证据。
4. 论文数值与已恢复结果是否完全一致属于另一项核对，参见 [来源报告](trace-audit.md)。

机器可读证据见 [paper-trace-count-check.json](../provenance/79-20260920/paper-trace-count-check.json)。在 AE 仓库根目录执行 `python3 scripts/check_paper_trace_counts.py` 可复核已保存输入的哈希、迭代配置、四类覆盖和 9 份 RL 结果；该脚本不运行新实验。
