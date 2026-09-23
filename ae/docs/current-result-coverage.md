# 当前重测结果：已完成与剩余缺项

本页按 README 当前采用的数据核对覆盖。2026-09-23 已把远端合入的 Table 3 完整结果与本分支完成的 Figure 6、E2B 数据接入；本轮已补齐 E2B 8 条与 Cube 12 条，并完成独立冷/暖 worker 和探针对照。各系统/实验臂独立统计，不将不同批次拼成同一均值。

| 论文项 | 当前图片采用的数据 | 仍缺什么 |
| --- | --- | --- |
| Table 2 | Replay：244/244 完整输入、244C/6,606R；其余五列仍为 c775a9215718 各自首输入 | CRIU、FC-Diff、Cube 四组单轮补测进行中；其余系统尚未接入完整列 |
| Table 3 | 9bbc1ba366ad：12/12 fast + 12/12 slow；每臂 317C/334R | 预定字段和 24 个作业均完整 |
| Figure 2 | c775a9215718：filesystem / memory 各一个输入 | 全部 30/5 输入 |
| Figure 6 | (a) 四策略各 28 checkpoint；(b) 12 输入 × 两策略全部 24 作业，1081 Std、831 LW、250 adaptive standard | 预定输入、策略和事件均完整 |
| Figure 7 | DeltaBox 12/12 + E2B 8/8；八柱和全部输入完整 | 预定覆盖、独立复算和归档验收通过 |
| Figure 8(a) | 三系统各 N=1/4/16/64，全部 12 点 | 重复测量；(c) 独立输入的理论计算 |
| Figure 8(b) | main 合入 H20 单卡：四组生成各 3 次、两组训练各 5 次 | 四卡训练 B16/B64 |
| Figure 9 | 6728d7c47a22：185 输入 × 三文件系统、六个大小桶；每组 603 有效 edit | 每组排除 43 edit；输入映射限制见统一偏差总账 |

- [Replay 全量结果、逐事件数据与配置](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/table2-replay-completed/README.md)
- [最新 Figure 6/7 与 Cube/E2B 完整数据](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/figure167-full/README.md)
- [此前 Figure 6/7 与背景阶段图归档](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/figure167-completed/README.md)
- [全部实验与论文偏差总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)
- [Table 3 完整结果](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/9bbc1ba366ad/table3/README.md)
- [Figure 9 完整结果与输入审计](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/6728d7c47a22/figure09-memory/README.md)
- [其余图片所用首输入批次](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/c775a9215718/README.md)

图上都有数据不等于全部输入已完成。E2B 失败轨迹、暂停的 DeltaBox 5/12 批次和短程 快速检查 均未混入所选统计。原始 run.json 保留真实测量源码、配置与状态；重新分析与绘图版本另行绑定。完整 1,392-job CPU 评测仍未全部完成。
