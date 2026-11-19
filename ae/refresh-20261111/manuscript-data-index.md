<!-- CURRENT-PAPER-DATA:START -->
# 当前论文采用数据总表

核对时间：2026-11-10T16:20:39+08:00。以桌面 dddd 当前 LaTeX、绘图输入和已编译 PDF 为准。

此节是统一总账的当前入口；下文历史记录保留其原始时间和状态。本文档整理数据，不改论文正文、图表或实验原件。

当前审核稿：[final_version_dddd.pdf](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/final_version_dddd.pdf)。机器可读数据与证据哈希：[paper-data-current.json](/Users/bytedance/Desktop/delta/ae/report/paper-data-current.json)。

## 1. 已采用与待更新

| 系统/项目 | 当前论文 CK / RS（ms） | 状态 | 已更新位置 |
|---|---:|---|---|
| Replay+cp | 313.2 / 14221 | 新数据；12 条 | Table 2、Figure 1、Background、Restore costs |
| CRIU+cp | 556.2 / 613.2 | 新数据；criu-overlap-20261109 完整12条，317 CK/334 RS | Table 2与Table 3同批；Table 3 CK仍为91.1 ms剩余等待 |
| CubeSandbox | 839.8 / 643.9 | 新数据；cube-balance 12 条 | Table 1/2、Figure 1、相关获批正文 |
| E2B | 1072.0 / 424.7 | 新数据；phase6+phase7 合并 12 条 | Table 1/2、Figure 1、相关获批正文 |
| FC-Diff+dm | 647.9 / 2567.9 | 新数据；CoW基底克隆＋官方增量合并，完整12条连续计时已采用 | Table 2、Checkpoint/Restore costs |
| DeltaBox | 10.83 / 1.86 | 原值保留 | 全文不改 |
| Figure 8 Cube/E2B | 见第 4 节 | 最新客户端复用/2 MiB读页结果，逐点统计见下文 | (a)、(c) 及五处获批正文 |
| Figure 7 | E2B/Cube同批分段重算，DeltaBox原值 | 已对齐LLM+action+C/R公式 | 四组柱高、图注和正文；标签间距已调整 |
| Figure 9 | (a)/(b)新数据；(c)/(d)原数据 | 固定4 KiB覆盖的实测写入量＋修正真实编辑重放 | (a)/(b)、测量方法、结果正文与图注已同步 |

## 2. Table 2 完整当前值

单位 ms；每格顺序为 CK / RS。下表直接解析当前 source/eval.tex。

| 分组 | Replay+cp（新） | CRIU+cp（新） | Cube（新） | E2B（新） | FC-Diff（新） | DeltaBox（不变） |
|---|---:|---:|---:|---:|---:|---:|
| Django | 631.2 / 11336 | 596.9 / 974.6 | 928.3 / 758.8 | 1053 / 449.5 | 589.4 / 2222 | 12.12 / 2.23 |
| SymPy | 187.0 / 19887 | 713.7 / 528.6 | 1008 / 638.4 | 1308 / 439.8 | 767.0 / 3305 | 11.96 / 2.21 |
| Scientific | 303.2 / 15224 | 534.7 / 606.1 | 831.4 / 645.3 | 1090 / 427.3 | 672.8 / 2643 | 10.86 / 1.82 |
| Tools/Small repos | 201.8 / 10625 | 457.6 / 449.1 | 681.4 / 571.2 | 895.8 / 394.6 | 566.0 / 2173 | 9.16 / 1.46 |
| Event Avg | 313.2 / 14221 | 556.2 / 613.2 | 839.8 / 643.9 | 1072 / 424.7 | 647.9 / 2568 | 10.83 / 1.86 |

| 分组 | 轨迹 | 调度 CK | RS |
|---|---:|---:|---:|
| Django | 2 | 53 | 54 |
| SymPy | 2 | 54 | 56 |
| Scientific | 5 | 130 | 140 |
| Tools/Small repos | 3 | 80 | 84 |
| Event Avg | 12 | 317 | 334 |

Cube、E2B、CRIU、FC 原始 360 次 CK，按固定 DeltaBox 节点调度剔除 43 次，保留 317 次；RS 为 334 次。分组 2/2/5/3 是轨迹数量，均值按各操作事件计算。
Replay 不打进程快照：其 CK 列沿用每条初始 pristine-repo copy，12 次复制均值为 313.150194 ms；RS 为 334 次实测，均值 14220.584528 ms，Figure 1 显示 14220.6，正文写 14.2 s，Table 2 显示 14221。没有使用先前 5570 ms 的估算扣减。
Table2的1000–9999 ms数值从未舍入均值四舍五入为整数；五位数沿用既有显示。两个用户指定显示例外：Replay SymPy实测19887.764474显示19887，Tools/Small实测10624.380547显示10625。总均值仍为14221，原始数值、Figure1和14.2s正文不变。
CRIU 当前采用criu-overlap-20261109：沿用e7/e7c的lazy-pages和quiesce后FS/dump并行路径，按连续外层窗口计完整操作，包含lazy_settle。旧主批567.6/609.5及约0.0283 ms lazy_settle窗口问题保留历史身份，不属于当前Table2数值。补测最多两路并发，旧主批四路，运行条件差异保留于PLAN和总账。

## 3. Figure 1 当前阶段分解

单位 ms。图例保留 guest-ready；列为已记录的功能区间，process/VM 不是全部底层缺页时间的物理归因。

| 系统 | 操作 | filesystem | process/VM | guest-ready | other | action replay | 总计 |
|---|---|---:|---:|---:|---:|---:|---:|
| CubeSandbox | ck | 35.697 | 704.035 | 62.015 | 38.074 | 0.000 | 839.821 |
| CubeSandbox | rs | 71.724 | 194.878 | 350.288 | 26.977 | 0.000 | 643.867 |
| E2B | ck | 58.046 | 557.435 | 199.211 | 257.317 | 0.000 | 1072.009 |
| E2B | rs | 1.351 | 144.112 | 258.990 | 20.294 | 0.000 | 424.746 |
| copytree+replay | ck | 313.150 | 0.000 | 0.000 | 0.000 | 0.000 | 313.150 |
| copytree+replay | rs | 567.592 | 6350.192 | 0.000 | 0.000 | 7302.801 | 14220.585 |

Cube CK：filesystem = rootfs_dump（35.697 ms）；process/VM = memory_prepare + memory_dump（704.035 ms）。分类按状态对象，与 E2B 的内存快照导出一致；内存快照 backing file 的准备不再混入 guest 文件系统。RS 的 process/VM = shim_update_restore。guest-ready 为 API 返回后的首次成功 agent RPC 区间。所有 317 CK /334 RS 均有同批匹配日志。
E2B：envd-init 与旧 VM 同步终止归 process/VM；首次 agent RPC 保留在 guest-ready。按同事件 OTEL 区间去重，未用猜测比例分摊；总值包含旧 VM 的同步关键路径。
Replay RS：filesystem = rmtree + copytree；process = 子进程中 run_search 以外的工作；action replay = run_search。三个实测时段合计为总值。

## 4. Figure 8 当前值与正文派生数字

Cube/E2B均为L1、4 vCPU/8 GiB，源状态64 MiB，每个子沙箱全页读回校验。Cube N1/4/16为四次成功中位数，N64按成功一次即停规则采用单次成功；E2B N1/4为四次成功中位数，N16为三次成功中位数、另一次失败保留，N64按用户指定采用先前验收成功3843.726369ms。DeltaBox和GPU原始输入不变。

| N | Cube 就绪 ms | E2B 就绪 ms | DeltaBox 就绪 ms（原值） |
|---:|---:|---:|---:|
| 1 | 444.1 | 750.7 | 34.1 |
| 4 | 599.6 | 839.0 | 41.1 |
| 16 | 2251.8 | 1538.4 | 91.3 |
| 64 | 8943.3 | 3843.7 | 368.8 |

| 派生项 | Cube | E2B | DeltaBox（原值） |
|---|---:|---:|---:|
| 同步 GPU occupation，N=16 | 56.8% | 65.8% | 97.0% |
| 同步 GPU occupation，N=64 | 46.6% | 67.0% | 95.5% |
| 异步 staleness，N=16 | 3.56 | 2.94 | 1.67 |
| 异步 staleness，N=64 | 2.71 | 1.58 | 0.81 |

对应正文五处已获批并更新：Introduction与§6.2.2 fan-out加速范围 **10.4×–24.7×**；GPU occupation Cube **47–57%**、E2B **66–67%**；staleness N=16为Cube **3.56**、E2B **2.94**，N=64为Cube **2.71**、E2B **1.58**。
Figure8(b)像素及GPU输入未变。新E2B N64另一轮成功3890.011512ms另存证据，不与用户选择的3843.726369ms混算。各批失败/中断和旧批次均保留，不能将当前八个点统称为四轮均值或四轮中位数。

## 5. 其他已同步正文及尚未采用项

- Background：已获批替换为阶段分析。Cube checkpoint839.8 ms的84%为内存快照准备/导出；E2B1072.0/424.7 ms，restore首次响应61%（加envd-init为85%）；Cube restore643.9 ms的54%为首次响应；Replay14.2 s的FS/process/replay显示4/45/51%。数据和图表不变，逐句前后原文见build-info/background-analysis-adoption-20261110/。
- Checkpoint costs：VM基线范围0.57–1.31 s；下限来自FC Tools/Small的565.9798 ms。
- Restore costs：E2B 0.39–0.45 s、约230×；Cube 0.57–0.76 s、约350×；首次agent响应分别占61%/54%。Replay14.2 s、约7600×；FC2.17–3.31 s、约1400×。
- Introduction 官方 E2B 约 4 s/GiB 引用保留，已按授权限定for full snapshots；§6.1 request-to-return 原句保留。
- Figure7：按原分段公式更新，E2B1.64–1.86×、Cube1.74–2.04×；§6.2.1正文同步总占比41.5%/46.3%，删除旧的额外框架时间解释。Introduction同步E2B/Cube合并分组范围39–51%。图注按用户要求恢复30-iteration轨迹及LLM+action归一化表述；实际LLM+action+C/R公式不变。DeltaBox原值1.008/1.011/1.020/1.024不变。柱间距增加，字号、线宽、配色及全部绘图数据不变。
- Table 2、Table 3已统一采用criu-overlap-20261109完整12条：Table2完整操作556.2/613.2 ms；Table3仍保留剩余等待91.1 ms（305窗口）和restore613.2 ms，FS143.4/334.0、CRIU348.8/102.7、Coordination205.1/176.5 ms全部不变，不新增完整操作耗时行。Table3 CK的FS和dump并行，不能相加当作完整操作。DeltaBox全部不变。
- 更早的 Cube 173/254.8/272.7/903.3/981.3、E2B 822.6/365.6、Replay 12919/22124 等候选不属于当前 Table 2 采用版本。

FC 连续计时补跑已采用：12 条，317 CK /334 RS，新均值 **647.9 / 2567.9 ms**。四组结果和阶段见 [summary.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/fc-reflink-20261109/summary.json)；已替代论文的历史622.3 /3429，原始结果保留。

Figure7采用同一批12条的317个调度CK与334RS：各系统原始360个CK保留，额外43个CK不计入当前分段公式。原始连续挂钟及逐条窗口见 [summary.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/figure7-e2b-cube-20261109/summary.json)；当前筛选节点和复算见 [aligned317-summary.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/figure7-e2b-cube-20261109/aligned317-summary.json)，不是重新运行了317事件的整批挂钟。

| Figure7分组 | E2B | Cube | DeltaBox（原值） |
|---|---:|---:|---:|
| Django | 1.801085 | 1.740400 | 1.008000 |
| SymPy | 1.635549 | 1.828778 | 1.011000 |
| Scientific | 1.653268 | 1.856851 | 1.020000 |
| Tools/Small | 1.856877 | 2.041271 | 1.024000 |

当前图按(录制LLM+实测action+317个调度CK+334个RS)/(录制LLM+实测action)计算；原始连续挂钟仅留作审计，不作为柱高。43个额外CK原始记录保留，当前公式复算见figure7-e2b-cube-20261109/aligned317-summary.json。CK占C/R的比例为E2B70.85%、Cube54.53%，正文显示71%/55%。

## 6. 一处查到源数据

| 项目 | 汇总/证据入口 |
|---|---|
| CRIU Table 2/3当前同批 | [summary.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/criu-overlap-20261109/summary.json)；[checkpoint-events.csv](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/criu-overlap-20261109/checkpoint-events.csv)；[restore-events.csv](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/criu-overlap-20261109/restore-events.csv)；[verified-data.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/build-info/criu-table3-update-20261109/verified-data.json) |
| Replay当前、CRIU历史主批 | [replay-criu-provenance-summary.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/nested-full-audit-20261108/remaining-three/replay-criu-provenance-summary.json)；[update-data.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/build-info/criu-replay-update-cube-revert-20261108/update-data.json) |
| Cube balance | [cube-balance-summary.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/nested-full-audit-20261108/cube-balance-summary.json)；[cube-balance-fetch-manifest.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/nested-full-audit-20261108/cube-balance-fetch-manifest.json) |
| Cube Figure 1 | [cube-balance-manuscript-update.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/build-info/cube-balance-unification-20261108/cube-balance-manuscript-update.json) |
| E2B 当前 12 条 | [12 份原始结果](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/nested-full-audit-20261108/e2b-phase7/combined)；[phases.json](/Users/bytedance/Desktop/delta/ae/evidence/table2-unified12-20261107/nested-full-audit-20261108/e2b-phase7/analysis/phases.json) |
| E2B Figure 1 最新分类 | [classified-events.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/build-info/figure1-e2b-attribution-20261109/classified-events.json)；图例已恢复 guest-ready |
| Figure 8 当前逐点证据 | [figure8-provenance.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/plot/figure8-provenance.json)；[figure8-data.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/plot/figure8-data.json) |
| 全部当前绘图分段 | [figure1-data.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/plot/figure1-data.json) |
| Figure 8 获批正文前后 | [text-before-after.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/build-info/figure8-text-sync-20261109/text-before-after.json) |

服务器证据总根：`spr4numa:/mnt/disk2/dyp/deltabox-runtime/ae/work/deferred-nested-20261107/`。Cube 在 `results/nested-cube-host/cube-balance`；CRIU 当前同批在 `criu-overlap-20261109/`，旧主批在 `results/nested-criu/criu-e7` 与 `criu-e7c`；Replay 在 `results/nested-replay-host/replay12`。Figure8各当前点准确路径与哈希以上述逐点证据清单为准，早期fullpage-parallel-final保留历史身份。

本次检查：50 个新 Table 2 单元格与采用的未舍入汇总一致；CRIU317CK/334RS从逐事件CSV独立复算，24份原始JSONL哈希匹配；Figure1各段加总与同批总值一致；Figure8当前25份原始结果哈希匹配，6个中位数和2个单次成功点重算一致；当前PDF与最新审核稿SHA256一致。与原ZIP相比，变化的实验图件为Figure1、7、8、9。FC12份及Figure7的24份原始结果哈希通过核验。

## 7. 当前全文旧值与遗漏复核（2026-11-09）

Figure7相关正文已获批同步：Introduction E2B/Cube39–51%；§6.2.1 E2B1.64–1.86×、Cube1.74–2.04×，总占比41.5%/46.3%。原连续挂钟占比保留在历史证据中，不用于解释现图。
Table2 CRIU已于2026-11-10统一为与Table3同批的556.2/613.2，替代独立主批567.6/609.5；Table3剩余等待91.1及分段不变，不加完整操作行。FC现采用完整CoW重测647.9/2567.9，替代838.0/7727.2；测量边界与事件对齐不变。
仍用原数据：Table1上半五行泛化范围、Figure2状态特征、Figure6优化、Figure8(b)GPU、Figure9(c)/(d)，以及全部DeltaBox数值。Figure3/4/5是设计图。Figure9(a)/(b)及其正文已换为新结果。原数据不等于已证实错误。
遗留说明：§6.1统一NVMe描述未涵盖保留DeltaBox及Figure9内存loop实验；Figure8图注4GPUs概括了不同并行配置；Figure9(d)正文图注未细写排除finalization的日志区间。第6页worker restart0.4–17s仍是旧经验范围，未找到本轮对应替代表。
78.9保留指令已失效。此前已有另一批66.1的报告记录（53完成/60尝试）；当时缺当前12条同批测量，不是完全没数据。当前91.1已采用；工作约定已纠正。
本次审计证据：[audit.json](/Users/bytedance/Desktop/dddd/sigopsatc2026-9/build-info/remaining-data-audit-20261109/audit.json)。当前paper-local.pdf不含78.9；source/paper.pdf保留原稿，不能误当当前编译稿。


Figure9(a)/(b)已采用：(a)固定4 KiB原地覆盖，文件4 KiB至1 MiB，每点30次，纵轴采用loop实际写入均值，reflink为65.13–95.90 KiB（含数据/元数据/日志），线性轴；(b)185条输入、555任务、每组646次编辑的两级中位数。物理I/O为小文件136→27 KiB、大文件320.5→174 KiB。测法、结果正文及图注均已同步；(c)/(d)不变。证据：[summary.json](/Users/bytedance/Desktop/delta/ae/evidence/figure9-corrected-20261109/summary.json)。

<!-- CURRENT-PAPER-DATA:END -->

