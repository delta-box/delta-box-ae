# AE 新图的论文格式来源

后续 AE 实验产生新图时，优先参考原论文仓库的绘图脚本，保留对应的字体、颜色、尺寸、坐标轴与面板布局。新数据仍从所选 fresh analysis / manifest 读取；覆盖说明和各项数据来源保留在清单中。Table 2、Table 3 和 Figure 1、2、6、7 图片本身按论文紧凑版式输出。

2026-09-22 已只读核对：

- spr4numa：`/mnt/disk2/dyp/d-overlayfs/rollbackable-sandbox-paper`
- 远端 HEAD：`c3259eb83a1fcc6637c88ec7a6d543c4c0843c4b`
- 本地副本：`/Users/bytedance/Desktop/delta/rollbackable-sandbox-paper`
- 统一样式：`plot/paperstyle.py`；文件 SHA-256 及所查脚本见 [来源清单](paper-plotting-reference.json)。

本地副本的 Figure 2、Figure 9 脚本和 `eval.tex` 与远端不同；上述远端版本及哈希作为本次参考，统一 `paperstyle.py` 两端字节一致。Figure 2 的 v2 脚本还从 `plot/plot_fig_motiv_combo.py` 导入颜色与字号，该依赖也已记录。

以下对应关系按正文的 `includegraphics` 和表格 label 核对，不能仅凭旧脚本注释中的图号选择。

| 论文项 | 仓库内格式来源 | 布局与尺寸（英寸） |
| --- | --- | --- |
| Table 2 | `eval.tex`，`tab:overhead` | LaTeX 双栏表，scriptsize、列距 4pt、行距 1.15；没有独立 canonical Python 绘图器 |
| Table 3 | `eval.tex`，`tab:latency` | LaTeX 单栏表，small、列距 3pt |
| Figure 1 | `plot/plot_fig_e2b_cube_compare.py` | 3.4 × 1.9，checkpoint/restore 两面板；restore 对数轴 |
| Figure 2 | `plot/plot_fig_motiv_combo_v2.py` | 3.4 × 1.4，1 × 2；输出 `figs/fig_motiv_combo2.pdf` |
| Figure 6 | `plot/plot_fig_mem_combo.py` | 3.4 × 1.55，1 × 2，宽比 1.3:1；内存曲线 log-y、时延直方图 log-x |
| Figure 7 | `plot/plot_fig_end2end_2sys.py` | 3.4 × 1.62，两系统分组堆积柱、1.0× LLM+action 基线 |
| Figure 8 | `plot/plot_fig_rl_combo.py` | 8.0 × 1.3，跨栏三面板；当前 AE 只接入 (a) CPU fan-out |
| Figure 9 | `plot/plot_fig_war.py` | 3.3 × 2.3，2 × 1，共享 x；copy-up 与 physical I/O 的 log-log 图 |

统一样式使用 STIX serif / stix 数学字体，PDF/PS fonttype=42；单栏轴/刻度/面板约 9pt，图例 7.5pt，跨栏约 8pt、图例 6.8pt。网格通常为横向虚线，图例无框，隐藏上/右边框，子图标题位于轴下。默认 DeltaBox 柱为 `#228B22`、线为 `#006400`，baseline 为橙色；具体图的显式覆写优先，例如 Figure 7 使用 DeltaBox 橙色、E2B 天蓝色。通常输出矢量 PDF 与 200dpi PNG，Figure 6 使用固定画布与 180dpi PNG。

## 接入新测量时的处理

- Figure 1 含缺文件时的历史 fallback、固定 E2B 总量与阶段比例、硬编码 Replay 数值。只复用绘图部分，缺少新测量的项明确留空，不能直接运行原脚本充当本轮 AE 结果。
- Figure 2/6 的输入路径指向历史目录；显式替换为已校验的新数据。Figure 6 将小于 0.05ms 的值截到 0.05ms 的显示策略如需沿用，应注明，原始数据不能改写。
- Figure 7 有多个脚本写同一个 `figs/end2end.pdf`。当前正文对应两系统版本，不能用旧 `plot_fig_end2end.py` 或 `groups` 的模型替代当前计时定义。
- Figure 8 会自动挑选历史目录中的最新文件，且读取 GPU 历史结果。AE 应使用明确选定的 manifest；GPU 仍按要求跳过。
- Figure 9 可参考其输入参数接口，但黄色 12.1–66.4KB 参考带是历史 IQR。新图应注明其为论文参考带，或用新数据重新计算；聚合单位也要与新图说明一致。
- 格式调整属于重绘；测量、样本数、单位、统计和覆盖范围保持可追溯。Table 2、Table 3、Figure 1 的接入见 [重绘报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/paper-layout-20260922/README.md)；Figure 2、6、7 的双轴、面板与分组柱布局见 [本次版式更新](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/figure267-layout-20260922/README.md)。同一图中的不同数据域或策略独立统计，缺项保留位置和说明；同一指标的冲突来源不隐式合并。

发布前仍按 [图片发布指南](publish-results.md) 校验来源和图片哈希。
