# DeltaBox — ATC 2026 Artifact Evaluation

[English](README.md) | **简体中文**

**申请徽章：Available、Functional、Reproduced。**

DeltaBox 为智能体的树搜索提供文件系统与进程状态的 checkpoint、restore 和分支能力。本 artifact 包含运行时代码、录制的工作负载、实验驱动与绘图工具，用于评估状态管理开销、内存占用和写放大。CPU 实验重放录制的 LLM 响应，无需提供 LLM API key。

建议先完成约 **5 分钟的快速检查**，再运行完整实验，预留约 **10 小时**。也可按[实验索引](#experiments)选择单项。一键运行脚本中的 GPU 相关测试有可能因为 GPU 资源全部繁忙而失败，如有相关报错请联系作者为 AE 机器分配 GPU 资源。

[快速开始](#quick-start) · [实验索引](#experiments) · [查看结果](#results) · [自建环境](#self-hosting) · [运行问题](#troubleshooting)

## 1. 快速开始

<a id="quick-start"></a>
<a id="快速检查"></a>

### 登录并检查环境

请在 AE 提交系统的评论区提供 SSH 公钥。收到访问说明后，将 `HOST` 替换为分配的地址：

```bash
ssh atc-ae@HOST
cd ~/deltabox-runtime
bash ae/run_all.sh --smoke
```

托管机器提供 Linux x86-64、KVM、实验镜像、录制输入和 baseline 服务。以下命令均在该机器的 **deltabox-runtime 仓库根目录**执行；无需先构建镜像。自建部署见[环境指南](ae/docs/self-hosting-zh.md)。

快速检查会启动 DeltaBox，执行最小 checkpoint / restore 序列，并检查恢复状态、分析数据和生成图表。**成功标志**是退出码为 0，终端打印：

```text
ok: /mnt/disk2/dyp/deltabox-runtime/ae/results/<源码版本>/checks/smoke/SUMMARY.md
```

打开这个 `SUMMARY.md`，各步骤应为 `ok`。快速检查确认运行链路可用；论文结论由下面的完整实验评估。

### 运行完整实验

<a id="一键运行"></a>

```bash
bash ae/run_all.sh
```

该命令运行[索引](#experiments)中的 CPU 和 GPU 实验，并自动分析、绘图和生成中英文论文对比页。预计约 10 小时，实际时间受主机负载和 baseline 执行时间影响。Figure 7 从完整轨迹派生；Figure 8(c) 使用本次 CPU fan-out 与 GPU 生成、训练时延计算。GPU 失败时保留已完成的 CPU 结果，整轮返回非零退出码。

运行结束后，打开终端打印的 `SUMMARY.md`，再进入同一结果目录的 **`comparison/attempt-NNN/README-zh.md`（中文）**或同文件夹的 **`README.md`（英文）**。两页由一键脚本同时生成，顶部可以切换语言。具体路径记录在 `review.json` 的 `outputs.comparison`。默认完整运行的状态应为 `ok`，失败步骤会保留日志并返回非零退出码。

已有输出不会被覆盖。续跑或选择单项时，使用[运行问题](#troubleshooting)中的方法。

## 2. 实验索引与单项运行

<a id="experiments"></a>
<a id="实验索引"></a>
<a id="4-论文图表--实验步骤"></a>
<a id="逐项实验"></a>
<a id="5-逐项实验"></a>

完整实验命令已经包含下表中的 CPU 与 GPU 项目。单独评估某个结论时，按对应章节运行即可，无需先执行整套实验。

| 论文项 | 评估问题 | 单项选择 | 资源 |
| --- | --- | --- | --- |
| [Table 2](#table-02) | 各系统 checkpoint / restore 的开销是多少？ | `--group table-02` | CPU、KVM、baseline 服务 |
| [Table 3](#table-03) | DeltaBox 的开销来自哪些组件？fast 与 slow 路径有何区别？ | `--group table-03` | CPU、KVM |
| [Figure 2](#figure-02) | 每步修改的状态相对于总状态有多大？ | `--group figure-02` | CPU |
| [Figure 6](#figure-06) | 内存策略和 lightweight-skip 如何影响资源与时延？ | `--group figure-06` | CPU、KVM |
| [Figure 7](#figure-07) | 状态管理相对 LLM 与动作时间增加多少开销？ | DeltaBox + E2B 完整轨迹 | CPU、KVM、E2B 服务 |
| [Figure 8(a)](#figure-08) | 创建更多分支时，fan-out 时间如何增长？ | `--group figure-08-cpu` | CPU、KVM、Cube/E2B 服务 |
| [Figure 8(b)(c)](#figure-08-gpu) | GPU 阶段耗时如何影响理论占用率？ | `--group figure-08`：CPU + GPU + 理论计算 | 单卡与四卡 GPU；CPU fan-out |
| [Figure 9](#figure-09) | reflink 如何影响编辑时的 copy-up 和设备写入量？ | `--group figure-09` | CPU、KVM |
| [§6.3.3](#correctness) | checkpoint / restore 是否保持文件系统语义？ | `--experiment correctness` | CPU、KVM |

Table 1、Figure 1,3–5 是设计说明和问题引入，无独立测量任务。

下面的单项命令共用一个输出前缀。**在当前 Bash 终端定义一次**，把 `reviewer-A` 换成你的标识；每轮新实验使用不同的标识：

```bash
AE_VERSION=$(python3 -c 'import json; print(json.load(open("release/candidate-lock.json"))["source_commit"][:12])')
export AE_RUN="$(pwd -P)/ae/results/$AE_VERSION/reviewer-A"
```

无需预先创建各实验输出目录。托管入口使用作者提供的配置，管理 CPU/NUMA 绑定与频率采样；请顺序运行，避免同时争用实验资源。实验驱动负责选择输入，命令中的 `--limit` 只用于缩小检查范围。

下面按论文实验逐项给出验证目标、运行方式和判断依据。并排图片展示**一键脚本的输出示例**，用于说明生成图表的形式。评估时请查看自己运行生成的对比页。

### 2.1 Table 2：checkpoint / restore 开销

<a id="table-02"></a>
<a id="对比table2"></a>
<a id="步骤1"></a>
<a id="基线实验"></a>

**验证目标。** 比较 DeltaBox、Replay、CRIU、FC-Diff、CubeSandbox 和 E2B 的状态保存与恢复成本，评估 DeltaBox 是否减少智能体分支切换的开销。

**运行。**

```bash
bash ae/run_all.sh --group table-02 --output "$AE_RUN/table-02"
```

也可用 `--experiment table-02-e2b` 等单独选择一个 backend，并为它指定新的输出目录。

**输出与判断。** 查看 `table-02-comparison.png`。分别比较 checkpoint 和 restore，先看各工作负载组，再看事件加权的 `All` 汇总（图中标为 `Event Avg`）；单位为 ms，越低越好。论文结论关注 DeltaBox 相对 baseline 的开销优势。判断时核对各系统的配置和计时定义；`All` 不是四个组均值的简单平均。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/table-02.png"><img src="ae/reference/figures/table-02.png" alt="Table 2: 论文图表" width="300"></a></td>
<!-- AE-RESULT:table-02:start -->
<td align="center" valign="top"><a href="docs/images/table-02-ae.png"><img src="docs/images/table-02-ae.png" alt="Table 2: 脚本输出示例" width="300"></a><br><small><a href="docs/images/table-02-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:table-02:end -->
</tr>
</table>

### 2.2 Table 3：DeltaBox 组件时延

<a id="table-03"></a>
<a id="对比table3"></a>

**验证目标。** 分解 checkpoint 和 restore 的组件开销，对比基于保留模板的 fast restore 与通过 CRIU 重建进程的 slow restore。

**运行。**

```bash
bash ae/run_all.sh --group table-03 --output "$AE_RUN/table-03"
```

同一输入集合分别执行 fast / slow 两个实验臂。指定 lazy-pages 等运行配置的方法见[Table 3 实验说明](ae/docs/table3-method.md)。

**输出与判断。** 查看 `table-03-comparison.png` 中的 Overlay、fork/CRIU 和 coordination 行，识别主导开销，并比较两条恢复路径。fast 路径通过模板复用减少进程重建成本。组件时间用于解释总耗时；与 Table 2 对照时，应区分组件窗口和完整 API 时间。`—` 表示该路径不适用的组件。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/table-03.png"><img src="ae/reference/figures/table-03.png" alt="Table 3: 论文图表" width="300"></a></td>
<!-- AE-RESULT:table-03:start -->
<td align="center" valign="top"><a href="docs/images/table-03-ae.png"><img src="docs/images/table-03-ae.png" alt="Table 3: 脚本输出示例" width="300"></a><br><small><a href="docs/images/table-03-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:table-03:end -->
</tr>
</table>

### 2.3 Figure 2：每步状态变化量

<a id="figure-02"></a>
<a id="对比figure2"></a>
<a id="步骤3"></a>

**验证目标。** 检查智能体动作是否通常只修改少量文件系统和内存状态，为增量 checkpoint 提供依据。

**运行。**

```bash
bash ae/run_all.sh --group figure-02 --output "$AE_RUN/figure-02"
```

**输出与判断。** 查看 `figure-02-comparison.png`：面板 (a) 比较总状态与每步增量，面板 (b) 展示增量随搜索步骤的变化。关注增量相对总量的大小，以及少数大幅修改的步骤。文件系统与内存使用不同的横轴和单位，应在各自面板内比较。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-02.png"><img src="ae/reference/figures/figure-02.png" alt="Figure 2: 论文图表" width="300"></a></td>
<!-- AE-RESULT:figure-02:start -->
<td align="center" valign="top"><a href="docs/images/figure-02-ae.png"><img src="docs/images/figure-02-ae.png" alt="Figure 2: 脚本输出示例" width="300"></a><br><small><a href="docs/images/figure-02-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:figure-02:end -->
</tr>
</table>

### 2.4 Figure 6：内存策略与 lightweight-skip

<a id="figure-06"></a>
<a id="对比figure6"></a>
<a id="步骤4"></a>

**验证目标。** 观察搜索加深时不同保留策略的内存增长，并检验 lightweight checkpoint 是否降低可跳过步骤的保存成本。

**运行。**

```bash
bash ae/run_all.sh --group figure-06 --output "$AE_RUN/figure-06"
```

面板 (a) 对同一 SymPy 输入运行 `none / skip / gc / warm` 四种策略；面板 (b) 对同一输入集合分别运行 standard-only 与 adaptive。仅运行其中一项时，可选择 `figure-06-memory` 或 `figure-06-adaptive`。

**输出与判断。** 查看 `figure-06-comparison.png`。在 (a) 中比较曲线的增长速度及峰值，判断 LW-skip 是否限制保留状态带来的内存增长；在 (b) 中比较标准与 lightweight checkpoint 的时延分布，观察低时延事件及 adaptive 中仍需标准 checkpoint 的部分。横轴为对数时延，纵轴为事件数。两组实验分别统计，不能把两幅子图的策略或样本混为一组。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-06.png"><img src="ae/reference/figures/figure-06.png" alt="Figure 6: 论文图表" width="300"></a></td>
<!-- AE-RESULT:figure-06:start -->
<td align="center" valign="top"><a href="docs/images/figure-06-ae.png"><img src="docs/images/figure-06-ae.png" alt="Figure 6: 脚本输出示例" width="300"></a><br><small><a href="docs/images/figure-06-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:figure-06:end -->
</tr>
</table>

### 2.5 Figure 7：相对 LLM 与动作时间的开销

<a id="figure-07"></a>
<a id="对比figure7"></a>
<a id="figure7映射"></a>

**验证目标。** 评估状态管理成本相对于智能体执行时间的大小，观察 DeltaBox 是否使各工作负载的归一化时间接近 1.0×。

**运行。**

```bash
bash ae/run_all.sh \
  --experiment table-02-deltabox --experiment table-02-e2b \
  --output "$AE_RUN/figure-07"
```

Figure 7 由这些完整轨迹自动生成，没有独立的计时任务。若已运行完整 Table 2 或全部实验，直接查看已有输出即可。

**输出与判断。** 查看 `figure-07-comparison.png`。1.0× 是 LLM 与动作时间的参考线，柱高超出 1.0 的部分表示状态管理的相对开销。分别比较 Django、SymPy、Scientific、Tools/Small，避免仅凭绝对毫秒数判断其对执行时间的影响。柱高按各计时组件求和后归一化，组内采用总量之比。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-07.png"><img src="ae/reference/figures/figure-07.png" alt="Figure 7: 论文图表" width="300"></a></td>
<!-- AE-RESULT:figure-07:start -->
<td align="center" valign="top"><a href="docs/images/figure-07-ae.png"><img src="docs/images/figure-07-ae.png" alt="Figure 7: 脚本输出示例" width="300"></a><br><small><a href="docs/images/figure-07-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:figure-07:end -->
</tr>
</table>

### 2.6 Figure 8(a)：CPU fan-out

<a id="figure-08"></a>
<a id="对比figure8"></a>
<a id="步骤5"></a>

**验证目标。** 比较从同一冻结状态创建多个可用分支的成本，以及分支数增加时的扩展趋势。

**运行。**

```bash
bash ae/run_all.sh --group figure-08-cpu --output "$AE_RUN/figure-08-cpu"
```

DeltaBox、CubeSandbox 与 E2B 分别测试 N=1/4/16/64。每个子实例必须读回并验证源状态中的内容。

**输出与判断。** 查看 `figure-08-cpu-comparison.png`，比较相同 N 下的就绪时间和随 N 增长的曲线。论文关注低成本分支创建能否支撑更宽的搜索。内容校验与时延同样重要：只有状态继承正确的分支才是有效结果。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-08.png"><img src="ae/reference/figures/figure-08.png" alt="Figure 8: 论文图表" width="300"></a></td>
<!-- AE-RESULT:figure-08:start -->
<td align="center" valign="top"><a href="docs/images/figure-08-cpu-ae.png"><img src="docs/images/figure-08-cpu-ae.png" alt="Figure 8: 脚本输出示例" width="300"></a><br><small><a href="docs/images/figure-08-cpu-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:figure-08:end -->
</tr>
</table>

### 2.7 Figure 8(b)(c)：GPU 时延与理论占用率

<a id="figure-08-gpu"></a>
<a id="figure-8b-gpu"></a>
<a id="获得-gpu-后的命令"></a>

**验证目标。** 测量生成、训练阶段的时间，并结合 sandbox 时间计算论文中的同步 GPU 占用率与 policy staleness。

**运行。** 完整一键命令已包含这两项。只运行 GPU 生成与训练时使用：

```bash
bash ae/run_all.sh --group gpu --output "$AE_RUN/figure-08-gpu"
```

需要单独重跑 Figure 8 的全部面板时使用：

```bash
bash ae/run_all.sh --group figure-08 --output "$AE_RUN/figure-08"
```

模型、Python 环境和设备由作者在托管环境配置。生成 B=1/4/16/64 使用单卡，训练 B=1/4 使用单卡、B=16/64 使用同节点四卡。脚本在运行前检查设备，资源不可用或繁忙时报告失败；请联系作者分配 GPU，不会自动缩小正式参数。自建机器的配置方法见[环境指南](ae/docs/self-hosting-zh.md#gpu-setup)。

**输出与判断。** (b) 输出逐次生成/训练时延及 `gpu/attempt-NNN/plots/figure-08b.png`；在同一 batch 下比较对应阶段。(c) 在 CPU fan-out 和 GPU 测量均成功后，输出 `gpu/attempt-NNN/theory/occupation.json` 及图表。检查降低 sandbox 时间是否提高模型中的有效占用率、减少 staleness；(c) 是理论计算结果。两项结果自动进入本次中英文对比页。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-08.png"><img src="ae/reference/figures/figure-08.png" alt="Figure 8: 论文图表" width="300"></a></td>
<!-- AE-RESULT:figure-08-gpu:start -->
<td align="center" valign="top"><a href="docs/images/figure-08b.png"><img src="docs/images/figure-08b.png" alt="Figure 8(b): 脚本输出示例" width="300"></a><br><small><a href="docs/images/figure-08b-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:figure-08-gpu:end -->
</tr>
</table>

### 2.8 Figure 9：写放大

<a id="figure-09"></a>
<a id="对比figure9"></a>
<a id="步骤6"></a>

**验证目标。** 比较 ext4、XFS 与 XFS+reflink 在编辑文件时产生的 copy-up 数据和设备写入量，评估共享数据块能否减少写放大。

**运行。**

```bash
bash ae/run_all.sh --group figure-09 --output "$AE_RUN/figure-09"
```

内存盘专用入口及配置见[自建与定向运行指南](ae/docs/self-hosting-zh.md#specialized-runs)。

**输出与判断。** 查看 `figure-09-comparison.png` 的两个面板，在相同文件大小桶内比较三条曲线。关注 reflink 是否降低 copy-up 私有数据量，以及这种降低如何反映到设备 I/O；文件系统日志和元数据也会产生写入，因此两者不要求相等。纵轴为 bytes/edit，设备计量来自 loop 写入量。

<table>
<tr><th>论文图表</th><th>脚本输出示例</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-09.png"><img src="ae/reference/figures/figure-09.png" alt="Figure 9: 论文图表" width="300"></a></td>
<!-- AE-RESULT:figure-09:start -->
<td align="center" valign="top"><a href="docs/images/figure-09-ae.png"><img src="docs/images/figure-09-ae.png" alt="Figure 9: 脚本输出示例" width="300"></a><br><small><a href="docs/images/figure-09-comparison.png">查看并排大图</a></small></td>
<!-- AE-RESULT:figure-09:end -->
</tr>
</table>

### 2.9 文件系统正确性（§6.3.3）

<a id="correctness"></a>
<a id="步骤7"></a>

**验证目标。** 检查 checkpoint / restore 前后的文件内容、指向已删除文件的打开文件描述符的行为，以及跨 checkpoint 写入的隔离语义。

```bash
bash ae/run_all.sh --experiment correctness --output "$AE_RUN/correctness"
```

入口运行 `test_full.sh`、`test_deleted_open_resurrect.sh` 和 `test_cross_checkpoint_fd_cow.sh`。查看 `SUMMARY.md` 指向的断言日志：各脚本应退出 0，恢复后的内容与文件描述符行为应满足具名断言。

## 3. 查看、核验和重绘结果

<a id="results"></a>
<a id="实验结果"></a>

单项实验和整套实验使用相同的输出结构。以下路径相对于终端打印的结果目录；`attempt-NNN` 由 `review.json` 指定。

| 路径 | 用途 |
| --- | --- |
| `SUMMARY.md`、`review.json` | 确认运行状态，定位实际输出与失败步骤 |
| `comparison/attempt-NNN/README-zh.md`、`README.md` | 中文、英文对比页：查看论文原图与本次测量的并排图 |
| `analysis/attempt-NNN/metrics.csv`、`series.csv` | 查看汇总数值与曲线数据 |
| `gpu/attempt-NNN/` | GPU 测量、资源预检、图表及 Figure 8(c) 理论计算 |
| `runs/`、`logs/attempt-NNN/` | 追溯逐事件测量和执行日志 |
| `environment/attempt-NNN/` | 查看测量配置、CPU 频率及恢复记录 |

先确认所选实验成功，再按各节“输出与判断”检查论文结论。小规模检查用于验证流程；图中的 `N/A` / `—` 不能当作零。

<a id="绘图"></a>
<a id="发布图片"></a>
<a id="6-分析结果与绘图"></a>

需要从原始结果重新出图时，按[分析指南](ae/docs/self-hosting-zh.md#reanalyze)操作；无需重新测量。

## 4. 续跑与运行问题

<a id="troubleshooting"></a>
<a id="运行提示"></a>
<a id="6-运行提示"></a>
<a id="7-运行提示"></a>
<a id="当前边界"></a>

续跑时保持原来的源码、配置和实验选择，将 `--output` 换成 `--resume`。例如续跑上面的 Figure 6：

```bash
bash ae/run_all.sh --group figure-06 --resume "$AE_RUN/figure-06"
```

| 情况 | 处理方式 |
| --- | --- |
| 输出目录已存在 | 同一轮用 `--resume`；新一轮改用尚不存在的输出目录 |
| 长时间没有终端输出 | 查看 `SUMMARY.md` 指向的日志，或 `logs/attempt-NNN/<step>/stdout.log` |
| GPU 资源不可用或全部繁忙 | 联系作者为 AE 机器分配 GPU 资源；之后使用同一输出目录续跑 |
| 依赖、权限或模板预检失败 | 保存 `SUMMARY.md` 和相关日志，在 AE 提交系统中联系作者 |
| 只想检查一个输入 | 给单项命令加 `--limit 1`，并使用独立输出目录 |
| 想查看支持的实验名称 | 运行 `bash ae/run_all.sh --list` |

测量期间保持作者提供的资源配置；在共享机器上不要同时启动多个争用相同 CPU/NUMA 的性能任务。账号与权限说明见[托管访问指南](ae/docs/hosted-access.md)。

## 5. 自建环境与进一步阅读

<a id="self-hosting"></a>
<a id="环境准备"></a>
<a id="3-详细命令行指南"></a>
<a id="a2-构建镜像"></a>
<a id="baseline-配置"></a>
<a id="guest-内核"></a>
<a id="测量环境"></a>
<a id="路径-b母盘不可获取时的实验性重建"></a>

自建环境需要 Linux x86-64、KVM、DeltaBox guest 内核和磁盘、工作负载环境，以及所选 baseline 的服务。资源要求、构建命令、配置和预检步骤见[自建环境指南](ae/docs/self-hosting-zh.md)。GPU 环境独立于 CPU/KVM 环境。

- [实验输入与文件清单](ae/paper/README.md)
- [镜像构建与模板准备](ae/images/README.md)
