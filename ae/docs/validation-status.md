# 验证进度、运行条件与版本

更新日期：**2026-09-23**。当前发布入口位于 GitHub **`main`**，被测实现字节由[源码锁](../../release/candidate-lock.json)标识。入口和示例图片见 [AE README](../README.md)，当前图片、分析及原始数据见[统一结果目录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/c775a9215718/README.md)。下列测量保留各自的源码版本与覆盖范围。

**托管 CPU 环境：**当前固定目录为 `spr4numa:/mnt/disk2/dyp/deltabox-runtime`，来源是 GitHub `delta-box/deltabox-runtime`。发布源码 `bf1dcbe` 的全目录预检确认 **17/17 个入口、1,404/1,404 个计划作业的依赖与输入可用，0 缺项**。真实评审账号已完成 **17 个入口、23 个作业**的首输入验收，以及 **12/12 个 DeltaBox 输入**各两次 checkpoint、一次 restore；两批测量源码为 `c775a92`，后续仅修改两个绘图文件的留白。155 项入口、清理及绘图检查，以及留白修正后的 36 项绘图检查全部通过、0 跳过，发布版也通过了真实账号的最小 C/R 与出图检查。来源与覆盖见[托管环境报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/hosted-environment-20260922/README.md)。完整 1,404 作业性能评测尚未执行，下面的历史缺依赖记录不代表当前环境。

**主页图片已更新：**Table 3 采用 `9bbc1ba366ad` 的完整 24 作业；Figure 6、7 采用 `figure167-completed` 的已完成数据；Figure 9 采用 `6728d7c47a22` 全输入；其余图片保留 `c775a9215718`。当前偏差与历史结论更正统一见 [偏差总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)。c775 历史批次由绘图代码 `87550c0` 重绘，17 个入口、23 个作业全部成功；六个 Table 2 backend、Cube 阶段采集入口、三个系统的 N=1/4/16/64 fan-out 都已运行。阶段计量、分析接入及全量输入的剩余缺项见[逐图覆盖](current-result-coverage.md)。各批次的图片、清单和原始测量压缩归档见[统一结果目录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/README.md)。12 输入短程检查独立保存，不并入这些图。

**Figure 8 脚本准备：**(b) 已提供可独立执行的 GPU 生成 / 训练计时入口，GPU 环境和真实执行仍待资源就绪。(c) 是 CPU 上的理论计算，已用有来源哈希的历史输入复核六个结果；E2B N=64 保留历史估算标记。源码 `1bc367d` 在 spr4numa 固定目录、NUMA3 上完成 **139/139 项 CPU 检查，0 跳过**，前后源码锁一致；没有新增 GPU 测量。原有 17 组 CPU 默认实验保持不变。命令与资源条件见 [Figure 8](../paper/figure-08/README.md)，验证证据及历史输入预览见[脚本准备报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/figure08-script-preparation-20260922/README.md)。

**最新 live 通道验证：**源码 `9e44c85` 将 worker 恢复后的固定等待改为就绪握手。spr4numa 上绑定 **NUMA3 / CPU 72–75 / 内存 bind:3**，205 项单测全部通过；3 轮共完成 72 次 warm restore、24 次 cold restore 及后续 Bash 动作。恢复到通道就绪的中位数分别从 256.827ms 降到 7.620ms、从 285.394ms 降到 35.385ms。该验证使用本地固定 LLM 回复，与下文论文 replay 的历史图片分别记录，详见 [worker reopen 报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/worker-reopen-20260922/README.md)。

<details>
<summary>历史首轮与补测记录（d59dab3 / e0b07eb，保留当时状态）</summary>

**绘图格式更新：**Table 2、Table 3、Figure 1 已按论文原版式重绘并替换 README 图片。只重绘 d59dab3/e0b07eb 的既有数据，缺项显示 `—`/`N/A`；图像中的长审计面板移至 manifest。新绘图源码为 `8f66e1e`（综合锁更新至 `47b410c`），未修改 runtime 或实验配置，未新增性能样本。详见 [重绘报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/paper-layout-20260922/README.md)。

**历史 Table 2 补测（e0b07eb）：**一键任务 `baseline-recovery-001` **2/2 job 成功、0 失败、0 缺项**。Cube Django-14997 完整 29C/28R；E2B Django-10914 完整 29 轮搜索、25 对真实 C/R。命令仅用 `--limit 1`，没有截短事件，不表示两者完整 12/8 输入 cohort 已验收。两项实际 VM 都已核验 CPU 52–55、内存 `bind:2`；最高 P-state 请求与结束恢复均成功。

该轮 baseline 补测的源码锁为 `e0b07eb2d356d3c3ed45611d6e3c5f5a9712f69b` / SHA-256 `741d16fc9c7bd03008e99ecd4e5581cbace31760245638b172fcb579e97bdd51`，锁提交 `50d9cdc`。Linux **288/288 检查通过、0 跳过**；21 个 E2B 父链/执行输入前后未变，退出无遗留 VM/netns/NBD。新 [Table 2 对比图、原始数据与命令](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/baseline-recheck-20260922/README.md) 独立于下面 d59dab3 的八组图片，未混合不同源码样本。

首轮 d59dab3 在 spr4numa 上按 **NUMA 2 / CPU 52–55 / 最高 P-state 请求**验收。被测源码固定为 **`d59dab30b8a436a62540a6561d5f7cb14149d991`**，源码集合 SHA-256 为 `839f065aa19f30d4e06c251e6ee78989d56a2f04e693e300e9bb63286220ef19`，源码锁提交为 `dec1d71`。仍是 `candidate-not-final`，不把首输入验证等同于完整论文 cohort。

`review-publish-001 / attempt-001` 已完成全部 **17 组入口**的首输入验收：**12 组可用、18/18 个 job 成功、0 个失败、5 组 Cube/E2B 因运行配置或服务未就绪而未运行**。命令使用 `--limit 1`，没有 `--max-events`：Figure 6(a) 四策略、6(b) 两策略、fan-out 四个 N 和 Figure 9 三种文件系统均保留。输出为 `ok-with-unavailable`；GPU 与真实 AK 按要求跳过。103 项声明产物全部通过 SHA-256 复核，18 份 manifest 均绑定同一个 release，analysis 无排除项。

**后续修复已经完成：**Cube 的 reflink storage、节点地址、SDK 与模板配置已恢复；E2B 已修复 local 运行入口、warm worker 管道悬挂和 tokenizer 遮蔽，并使用独立父镜像闭包。Table 2 两个 backend 的完整首条 trace 均已通过，见 [补测报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/baseline-recheck-20260922/README.md)。以下首轮覆盖保留原始状态。

| 验收项 | 实测结果与边界 |
| --- | --- |
| DeltaBox fast / slow | Django-14997 各完整 29C/28R，后台各 29 dump 成功；fast 完整 API 12.51863 / 3.04966 ms，slow 7.33205 / 129.86521 ms |
| Figure 6(a) | 四臂各完整 28C/28R；warm 1 completed / 27 cancelled / 0 failed，是 best-effort 预热 |
| Figure 6(b) | standard/adaptive 各 82C/36R（含 bootstrap）；正式统计排除 bootstrap，dump 分别 82/19 次成功 |
| Figure 2/8/9 | Figure 2 两输入各完整 29 步；fan-out N=1/4/16/64 内容验证通过；Figure 9 三文件系统各 2 edits，只覆盖首个文件大小区间 |
| CRIU / FC-Diff / Replay | CRIU/FC 各 30C/28R；Replay 完整 28R，全部成功；性能配置 test runtime 为 none，不声称执行全部项目测试 |
| 正确性 | 恢复出的三套脚本通过，包含跨 checkpoint 旧 FD 路径；不等于论文 53-case 全清单 |
| 测量约束 | 12 组均绑定 NUMA 2 / CPU 52–55，请求 4 GHz 最高 P-state；忙碌样本频率中位约 2.9 GHz，结束均恢复原设置，无 restoration error |

本轮 Linux 论文/入口回归 **262/262 通过、0 跳过**。首次测试捕获 SIGKILL 异步退出导致的测试竞态；仅改有界终止确认，单项 20/20 与完整回归通过，生产代码未因此修改。首次失败日志、复测与源码锁见 [validation-d59dab3](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/oneclick-20260922/validation-d59dab3)。此前 `4c6bfba` 的 195 项核心检查、255 项论文检查及 prepare 后补查档案的日志单独保留在 [validation-4c6bfba](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/oneclick-20260922/validation-4c6bfba)。

同范围的真实 `--resume` 验证复用了全部 18 个成功 job，原始 manifest 未变化，103 项产物仍匹配；[续跑证据](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/oneclick-20260922/resume-validation.json)记录了该结果。`attempt-002` 只校验和重画，该历史批次图片取 `attempt-001`，不增加性能样本数。归档在本地也通过完整重新分析与出图。

全目录预检为 **1404 个计划 job、1370 个输入可用、34 个依赖缺失、0 个预检错误**，发生在前一版 `4c6bfba`，没有执行这些 job。完整 CPU cohort 尚未验收；Django-14672 的真实测试环境与正确性清单仍有缺项。Cube/E2B 首轮未运行记录保留，后续 Table 2 成功样本单独归档。

此前 `1593cfc`、`cc49b390`、`4c6bfba` 都是独立诊断批次，不混入本轮图片。镜像哈希缓存、resume 权限、producer 发布锁绑定、Figure 6 等待策略、Figure 9 离线输入与 staging 空间清理的修复及证据见本轮报告。清理发生在 producer 结束之后，并验证测量产物前后哈希；失败目录和原始计时保留。


</details>

## 1. 演示图片范围与准备进度

<a id="demo-figure-coverage"></a>

### 1.1 README 展示图片的覆盖范围

当前图片按不同实验分别选择已经完成且通过原始产物校验的测量，详见[逐图覆盖](current-result-coverage.md)。本次更新只重新分析已有数据，没有重新启动暂停的测量队列。

| 论文项 | 当前图片采用的数据 | 仍缺什么 |
| --- | --- | --- |
| Table 2 | c775a9215718：六个 backend 各一个完整输入 | 本图尚未更新为所有 backend 的全部输入 |
| Table 3 | 9bbc1ba366ad：12/12 fast + 12/12 slow；每臂 317C/334R | 预定字段和 24 个作业均完整 |
| Figure 2 | c775a9215718：filesystem / memory 各一个输入 | 全部 30/5 输入 |
| Figure 6 | (a) 四策略各 28 checkpoint；(b) 12 输入 × 两策略全部 24 作业，1081 Std、831 LW、250 adaptive standard | 预定输入、策略和事件均完整 |
| Figure 7 | DeltaBox 12/12 完整轨迹 + E2B 7/8；四组、两个系统的八个柱位都有值 | E2B Tools/Small 仅一条，Sphinx 尚未完成 |
| Figure 8(a) | 三系统各 N=1/4/16/64，全部 12 点 | 重复测量；(b) GPU 实测；(c) 独立输入的理论计算 |
| Figure 9 | 6728d7c47a22：185 输入 × 三文件系统、六个大小桶；每组 603 有效 edit | 每组排除 43 edit；输入映射限制见统一偏差总账 |


Table 3 使用已合入 main 的 24 作业完整结果；Figure 1、6、7 新发布选取 49 份成功 run.json，逐份验证所声明产物。Figure 7 采用完整 12 条 DeltaBox fast，未混入本地暂停批次的五条成功完整轨迹。E2B 的失败输入仍排除。细节见[测量与偏差报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)。

历史 c775a9215718 的 17 入口、23 作业验收记录保持原样，不能当作全部 1,404 作业已经测完。性能 replay 的 C/R 成功也不能推导全部 workload 测试通过。

<a id="demo-environment-progress"></a>

### 1.2 当前环境准备与剩余验证

| 项目 | 状态与待办 |
|---|---|
| 托管账号 | 已完成真实 SSH 登录、环境变量、固定仓库及实验权限配置；账号已由作者授权可写，见[账号说明](hosted-access.md) |
| 全量耗时与空间 | 完整 cohort 总耗时、全套实验峰值空间尚未测定；README 的 80 GiB 是镜像构建预留量，实验临时盘、dump、baseline 和构建缓存另计 |
| host payload / venv | 分发包和全新机器自动安装链尚未随 Git 提供；`ae/vendor/spr_payload/` 只是部分 driver 来源，自建用户需取得配套环境及服务材料 |
| guest 内核来源 | 当前源码、配置、编译器与历史二进制的完整对应关系尚未冻结；`--kernel` 仅记录输入、输出哈希，不表示从源码重建出该二进制 |
| 已有母盘的构建验证 | 已在 spr4numa 构建 base + Django，并完成三事件短轨迹的 warm / CRIU lazy restore；其他数据组及完整 cohort 尚未在该构建批次重建验证 |
| 无母盘的 OCI 配方 | `build_master.sh --from-ubuntu` 可下载并校验固定的 Miniconda 安装包；原安装包与 SHA-256 入口仍保留。随后使用 `build_images.sh --source-image` 生成 base/data，也可由 `--miniconda` 串联手动输入。全量 Docker 构建和 boot/replay 尚未完成验证 |
| Cube / E2B | Table 2 首条完整 trace、Cube Figure 1 阶段采集、两系统 Figure 8(a) 的四个规模均已通过 c775a9215718 批次验收；完整 cohort 与阶段指标缺项分别列在上表 |

母盘来自 Ubuntu 24.04 bootstrap 和按 repo/version 安装的 conda/testbed，再拆分为 XFS；没有证据表明这套 base/data 盘直接转换自官方 SWE-bench 单实例 Docker 镜像。来源、构建批次和验证证据见[镜像来源核查](../images/source-audit-20260921.md)及[镜像说明](../images/README.md)。

**镜像获取与校验：**母盘是构建中间产物，不是必须单独分发的工件。可选择提供 base/data 盘与内核，或完成从公开基础镜像、CRIU 和工作负载源码构建的路径；后者已有 `Dockerfile.master` 与 `build_master.sh` 配方，但尚未完成全量环境及 boot/replay 验证，部分依赖未锁版本。普通 Ubuntu 镜像没有预装这些工作负载环境；我们修改的内核源码/补丁也需随工件提供。

**公开镜像构建入口：**当前 `main` 支持 `ubuntu:24.04 → 多环境 Docker 镜像 → base/data XFS` 配方。`build_master.sh --from-ubuntu` 从官方渠道下载固定的 Linux x86-64 Miniconda 安装包，校验发布 SHA-256 后才供构建使用；也可继续手动提供安装包及校验值。已具备多环境 Docker 镜像时使用 `--source-image`，已有选定 XFS 母盘时使用 `--master-xfs`。完整参数见[README 的构建章节](../README.md#a2-构建镜像)和[镜像说明](../images/README.md)。

**已有输入的校验：**[输入清单](../images/inputs-20260922.sha256)记录选定母盘和修复版内核的 SHA-256。母盘来源为 [2026-09-21 构建记录](../images/evidence/20260921/disks-build.json) 的 `source_sha256`，内核身份见 [d59dab3 的运行记录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/oneclick-20260922/raw/review-publish-001/runs/table-02-deltabox/table-02-deltabox____django__django-14997__fast/fast/django__django-14997/run.json)。`--master-xfs` 实际构建前会校验母盘及选用的预编译内核；源码编译模式只校验母盘，`--plan` 不扫描整盘。重新构建的 OCI 输入和内核保留自己的构建记录，不套用历史二进制的校验值。

spr4numa 母盘为 `/mnt/disk2/dyp/d-overlayfs/ubuntu-24.04.xfs`（实际位于 disk1）；本轮配置使用 `/mnt/disk2/dyp/overlay-stale-fd-20260921/vmlinux-fixed`，与历史 `/mnt/disk2/dyp/d-overlayfs/linux-6.8/vmlinux` 是不同产物，应以配置和 SHA-256 确认身份。

**运行目录：**请使用完整的 GitHub `main` checkout，并从仓库根目录执行。旧目录只保存文档或不完整源码时，用 `--runtime-repo /path/to/complete-checkout` 显式选择实际执行版本；复制 README 或图片不会切换运行时代码。macOS 可用于文档、分析和绘图，VM 实验在 Linux 上执行。

### 1.3 从 README 移入的历史对照与诊断

- **Table 2 的旧批次：**`4253d79 / historical-async-full` 的 Django-14997 三轮完整 API 均值为 **11.901 / 4.046 ms**，单实例完整轨迹曾用时约 65–91 秒。它们不代表当前源码或全 cohort，见[受控复测](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/numa2-max-20260921/README.md)和[生命周期修复报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/dump-lifetime-20260921/README.md)。
- **Figure 6 的可选等待诊断：**`recorded-wall` 按相邻 transition 时间戳间隔等待，该间隔含原动作时间，不等于独立 LLM RTT。12 输入 × 2 策略会增加约 70.7 小时；默认论文配置为 `paper-zero`。核查见[时序对照](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/workload-environment-20260922/figure6-timing-review.md)。
- **Figure 9 的旧首输入：**三种文件系统的 2 次 edits 最终 upper 私有数据均为 20,480 bytes。按 4 KiB 块计算，两次编辑覆盖全部 5 个块，可解释数据量相同，但旧批次没有逐 extent / clone 事件记录，不能据此直接证明具体 CoW 过程或 reflink 未生效。详见[历史计量核查][candidate-war]。
- **输入与计时口径：**Figure 9 的 AE 输入是 trace base commit 的 Git 文件与 4 GiB sparse loop 文件系统，未重建历史完整 OCI rootfs；Figure 1 当前使用 E2B 实际阶段探针，Cube/Replay 使用各自已归档的阶段记录；具体边界和全部实验的差异统一维护在 [偏差总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)，根 README 只列实际覆盖。
- **旧图入口：**先前统一候选的[完整报告][candidate-first-round]、[Figure 2][candidate-figure02]、[Figure 8(a)][candidate-figure08]、[Figure 9][candidate-figure09] 固定到 `893d079`，不混入当前演示图。

README 图片下的重复源码哈希、成功/失败计数和缺项列表已集中到本节及关联清单。后续发布图片时，同步更新本节的源码、attempt 与样本范围；发布脚本继续校验来源和产物哈希，原始测量与图内标注保持不变。

以下 §2 起保留此前分支、版本和实验的历史记录；其中“本分支”“最新”只指该段注明的版本，不代表本轮一键入口或新的全量验收结果。

## 2. Artifact 内容与版本

**历史生命周期验证：**`4253d79` 在 spr4numa 上完成三轮完整 Django-14997 轨迹，使用 `historical-async-full`；87 次后台 dump 成功、84 次 restore 状态校验通过。该批次其他图表的结果主要为入口检查或部分实例测量。详细证据见[生命周期修复报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/dump-lifetime-20260921/README.md)。后续统一候选扩大了验收范围，进度单列如下。

### 2.1 异步增量候选 a69fdb7：已修复与实测范围

该批次使用历史分支 **`fix/async-incremental-replay`**。被测源码冻结为
`a69fdb75a58f10d85ddaa828a8b84918c8a5274e`，源码集合 SHA-256 为
`1fd3548b62ea1ad6c3b1e9529ff1bf657aac29229481bc7acaad64b076f1c786`；
源码锁提交为 `ada236d705623b537630c6053779fa572dd5436a`，报告归档提交为
`072c4594a44c2b810dbfcd6012a10b77ec18f2e4`。状态仍为 **`candidate-not-final`**，
不能把后续报告提交当作新实现，也不能把旧版本的全量轨迹自动算作本版验收。

| 问题 / 范围 | 本轮实现及真实验证 | 尚未覆盖的边界 |
|---|---|---|
| 跨 restore 的父页复用退化 | CRIU 4.2 增加显式 `exact-parent-v1`：在首次输出页前精确比较父页，不绕过 PID reuse 保护。Django warm、Requests cold 的恢复后增量均保持父页引用 | 后台仍读取候选页与父页，不能把镜像缩小等同于所有内存读取消失 |
| 真正异步 dump | 独立、停止的 dump 副本，有界队列、父提交依赖、失败传播、原子发布及副本清理；两条冻结版轨迹各 29 次 checkpoint 均先于后台 dump 完成返回 | 当前只支持受限资源合约下的 standard trace replay；默认 profile 未切换；不支持该模式的 lazy restore |
| Django-14997 warm | 完整 29 checkpoint / 28 restore，29 dump 成功；恢复后的索引、文件摘要与目标一致 | 仅该 workload 一次冻结版完整轨迹，不是 Table 2 全 cohort |
| Requests-863 cold | 完整 29 checkpoint / 28 CRIU restore，29 dump 成功；控制恢复、索引和文件校验全部通过，之后可继续增量 checkpoint | eager cold 路径；不是全部 Table 3 slow 输入通过 |
| baseline 测试 runtime 绑定 | Replay、CRIU copytree、FC-Diff 的重建树显式绑定 `local-pytest`。Replay 完成 29 expansions、37/37 响应；CRIU/FC 各完成 30 checkpoint / 28 restore，均真实执行 pytest | Requests-2674 共收集 161 项，本轮显式选中 2 项且均 PASSED；不是完整测试集。Figure 2 已改绑定但本轮未实测，Cube/E2B 对应环境尚未验收 |
| 回归测试 | 冻结版 Linux **184 + 148 = 332 项通过，无跳过**；另外保存 baseline 专项及页链机制证据 | 单元测试不能替代全部特权环境、workload 和论文图表运行 |

冻结版测量如下；MB 使用十进制，增量大小只统计实际 pages 数据，父页比例来自 CRIU pagemap：

| 轨迹 | seed / 后续增量页数据 | 后续增量父页复用率 | checkpoint API 均值 | restore API / 内部 critical 均值 |
|---|---|---|---|---|
| Django-14997 warm | 98.57 MB / 0.97–3.49 MB | 96.46%–99.02% | 12.50 ms | 3.08 / 1.59 ms |
| Requests-863 cold | 20.33 MB / 0.94–1.91 MB | 90.60%–95.37% | 5.45 ms | 54.30 / 18.06 ms |

两次冻结实测均绑定 NUMA **2**、CPU **52–55**，请求最高 P-state，结束后恢复调频策略且无错误。
全轮 busy 加权实际频率分别约 **2888 MHz、2833 MHz**，不声称实际恒定 4 GHz。
完整 API 包含实际等待；不能用内部 critical 区间、warm 延迟或旧论文值替代 cold API。

另有冻结前的同源码 Requests 单轮对照：checkpoint API **43.05 → 8.78 ms**，
warm restore API **2.97 → 2.09 ms**。每臂仅一次完整轨迹，且不是上述冻结源码实测；
用于诊断改善，不能作为全部 workload 的收益、置信区间或论文差距百分比。

完整原始证据、复算脚本和对比图见[异步增量报告][async-report]、[冻结版汇总][async-summary]、
[baseline runtime 报告][baseline-runtime-report]。`DURABLE_READY` 表示完整 CRIU 镜像已发布且
副本已清理；本轮镜像位于 tmpfs，没有新增 fsync 或掉电持久化保证。后台记录和归档不替代
真实成本计量；资源检查、fork、配额等待等前台成本保留在 API 时间内。

重现该批次应按对应的[候选运行指南][async-replay]和[源码锁][async-lock]选择冻结版本。
当前 `main` 已包含 `async-incremental` 实现，当前运行仍应使用自己的有效配置和源码锁，不能重标为该历史批次。
该批次没有运行真实 AK 任务或 GPU 实验。

### 2.2 先前统一候选的首轮验收（历史记录）

以下结果来自 **`feat/unified-release`，不是本 checkout，也不是最新异步增量候选**。被测源码冻结为 `5d6ef73a848e085382573b89442a52105cf54ad4`；报告归档提交为 `893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a`。表中失败描述保留首轮观测；后续已修复项目以上节及维护记录为准，不回写历史结果。

| 论文项 | 首轮统一候选已执行范围 | 该轮尚未完成 / 未通过 |
|---|---|---|
| Table 2 DeltaBox / Table 3 fast | 12 条完整轨迹全部尝试，10 通过、2 环境失败 | 未达到 12/12；跨 restore 的父镜像页复用仍退化 |
| Table 3 slow、Table 2 baselines、Figure 1 | cold restore 和 Replay / CRIU / FC-Diff 首输入已尝试；Cube / E2B 已检查依赖 | cold 控制响应、baseline 重放或恢复失败；服务依赖阻塞，没有完整新 phase 数据 |
| Figure 2 | 文件系统、内存各 1 个输入通过，各 29 步 | 分别只覆盖 1/30、1/5 输入 |
| Figure 6 | 6(a) 四策略均尝试；6(b) standard/adaptive 各前 4 事件通过 | 6(a) 适配器守卫 / 不支持的 warm 臂；6(b) 未覆盖完整搜索及 adaptive 的标准 checkpoint 分支 |
| Figure 7 | 可由 10 条成功 DeltaBox 轨迹绘制组件模型 | 不是新测的真实 LLM 搜索耗时；E2B 缺匹配输入 |
| Figure 8(a) | DeltaBox 的 N=1/4/16/64 均通过内容校验 | 每个 N 仅一次；Cube / E2B 依赖阻塞；九轨迹 fork primitive 无独立 runner |
| Figure 9 | 首个输入的三种文件系统均通过，各 2 次 edits | 仅一个 16–32 KiB 文件，不能代表全图趋势 |
| §6.3.3 正确性 | 3 个脚本中 2 个通过 | checkpoint → unlink → 写旧 FD 报 `EBADF`；不是论文 53-case 全通过 |

首轮新增的 17 项计划内检查为 **8 通过、9 未通过**，另有 1 次调度中止；连同之前 Table 2 的 12 次尝试，共 30 条实际记录。数量指实际尝试，不是全部 CPU cohort 的完成比例。原始日志、失败归因、覆盖图和对比图见[首轮报告][candidate-first-round]。

统一候选将真实 LLM 入口放在 `agent/`、论文 trace replay 入口放在 `replay/`，共享同一份 runtime 核心；该批次没有运行真实 AK 任务或 GPU 实验。`release/lock.py verify` 与 `replay/run_release.py` 用于验证冻结源码并运行实验。**这些入口已集成到当前 main**；重现该历史批次仍应在独立 checkout 按[候选指南][candidate-release]选择对应版本与配置。

### 2.3 仓库内容与历史版本

```text
deltabox-runtime/
├── backends/deltabox/gsd/     DeltaBox 用户态 C/R 实现
├── pycriu/                   CRIU Python 绑定
└── ae/
    ├── reproduce.py          prepare / doctor / plan / run / analyze / plot
    ├── configs/              主机、镜像路径和实验配置
    ├── datasets/             随仓库提供的压缩 trace 与历史数据包
    ├── paper/                按论文图表组织的 cohort、输入和来源清单
    ├── runners/              当前 runtime 和各 baseline 的实验入口
    ├── vendor/               已恢复的历史 driver 及其来源、移植校验
    ├── images/               镜像清单、构建配方和构建验证范围
    ├── reference/            本指南对应的论文 PDF 和文本
    ├── report/               已有结果、对比图和部分压缩原始证据
    └── results/              新运行输出，不进入 Git
```

压缩数据包约 **20.5 MB**，展开对象约 **922 MB**，包含 2500 个去重对象和 4378 个图表引用。`prepare` 校验压缩包和逐对象 SHA-256，并建立各图表的输入链接。文件来源见 [paper/README.md](../paper/README.md)，历史 driver 来源见 [vendor/README.md](../vendor/README.md)。

**Git 中不包含**可启动的 VM 内核、XFS 镜像、完整工作负载依赖环境、模型权重、SSH 私钥或外部服务凭据。重跑系统实验还需 [README 的环境配置](../README.md#环境准备)；数据包本身足以进行历史分析。镜像构建状态见 [images/review.md](../images/review.md)，构建说明见 [images/README.md](../images/README.md)。

| 版本 | 用途 |
|---|---|
| `main@c74c8f5` | 本轮复现开始时的生产 runtime 基线；仓库默认分支名为 `main` |
| `feat/paper-reproduction` | CPU 实验入口、trace、报告；生产 runtime 保持上述基线 |
| `fix/async-dump-lifetime` | 历史生命周期验证分支；用于复跑 pidfd 等待优化及后台 dump 生命周期修复 |
| `4253d79` | 上述三轮完整轨迹实际测量的干净提交；不是论文历史运行的源码身份 |
| `feat/unified-release` / 源码 `5d6ef73` | 先前统一候选，含 `agent/`、`replay/` 和源码锁；首轮验收见历史表 |
| `fix/replay-acceptance` / `4f8ba06` | 后续 replay 修复候选，包含控制通道、baseline 诊断及消息差异审计等修复；保留独立证据 |
| **`fix/async-incremental-replay` / 源码 `a69fdb7`** | 最新异步增量及 baseline runtime 候选；锁与报告归档至 `072c459`，范围见 §2.1，尚未定为全量最终版 |

运行器会打包**当前 checkout** 的 `backends/deltabox/gsd` 和 `pycriu`，将 commit、源码哈希和镜像哈希写入 `run.json`。切换版本会改变被测实现；不要将不同提交的结果合并成同一组。历史数据与当前 runtime 的新测量分别分析。

<a id="hosted-environment"></a>

### 3.1 已配置的 spr4numa

登录方式和 checkout 见[初步检查](../README.md#快速检查)。当前托管配置为 `/etc/deltabox-ae/review.json`，公开路径说明见 [`spr4numa-review.json`](../configs/spr4numa-review.json)，最新运行、权限和服务记录见[托管环境报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/hosted-environment-20260922/README.md)。下表记录历史受控复测的环境，供性能测量对齐条件。当前一键入口按有效配置控制绑核与调频；历史 live 通道验证使用 NUMA3，其余记录保持各自的测量位置。异步增量批次的 kernel / CRIU 身份另列于下方。

| 项目 | 该历史批次的受控复测配置 |
|---|---|
| CPU | Intel Xeon Gold 6418H，4 sockets × 24 个物理核；当前 SMT 关闭 |
| CPU / 内存位置 | NUMA **2**，物理 CPU **52–55**，内存策略 `bind:2` |
| 调频 | 四核 `performance`，min=max=`cpuinfo_max_freq`（4.0 GHz 请求值） |
| 实际频率 | 已保存的忙时测量约 2.9 GHz；最高 P-state 请求不保证物理频率恒为 4 GHz |
| DeltaBox VM | 4 vCPU、8192 MiB、修改后的 Linux 6.8、XFS base + 只读 data 盘 |
| Host 工具记录 | Linux 6.8.0-124-generic、Firecracker 1.6.0、host CRIU 3.16.1 |

Host CRIU 与 guest CRIU 是不同依赖；历史 DeltaBox VM 使用镜像内的版本，新 profile 显式注入固定二进制。准确版本、镜像身份、实际 NUMA 分配和调频证据以该次输出为准。当前主机有其他任务，绑核不等于独占隔离。

**d-overlayfs 在哪里：**上述实测使用修改后的 guest 内核 `/mnt/disk2/dyp/d-overlayfs/linux-6.8/vmlinux`，配置为 `CONFIG_OVERLAY_FS=y`，改造版 OverlayFS 直接编入内核。Firecracker 分别加载该内核和 `base.xfs` rootfs；仅取得 rootfs 不能替代这份内核，也不需要靠 guest 中的 `modprobe overlay` 启用本次改造。二进制含 `ovl_ioctl_checkpoint`、`ovl_ensure_upper_and_switch`、`ovl_rehome_or_anon_cow`，自定义 checkpoint ioctl 已实际执行。

本分支[三轮完整轨迹之一的镜像记录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/dump-lifetime-20260921/raw/lifetime-full-001/run.json)与后续首轮验收使用同一 kernel SHA-256：`d51d169089c3d3b9dfe3cb6b2e6ffb3a9bceeab52adf58e2ae1dfe5fc2681d89`。这是 Linux 6.8.0 `#37`、2026-07-09 的构建；路径相同不保证文件永远不变，每轮仍需核对哈希。当前尚缺源码、配置、编译器与产物的完整构建对应记录，不能仅凭源码目录 HEAD 宣称它包含后续所有修改。见[内核与正确性核查][candidate-correctness]及[镜像构建说明](../images/README.md#guest-内核)。

**最新异步增量实测使用另一份独立产物：**

- kernel：`/mnt/disk2/dyp/overlay-stale-fd-20260921/vmlinux-fixed`，SHA-256 `fa09edb1891d893dbad6136b0dcfb4deac9c5126513df3b6312c278b0d521808`。在独立源码副本修复旧 FD 的匿名 backing，增量重编译；原 kernel、rootfs 未改。构建位置、补丁与语义用例见[OverlayFS 修复证据][overlay-fix]，不声称 clean build 或论文 53-case 全通过。
- CRIU：基于 4.2 commit `2cf8f13ca1f11a0491977e438b262e646137256c` 的 `exact-parent-v1` 扩展；实测 ELF SHA-256 `ce39d625bef154708270f92b8dad37b1002b2f7183f474995dabd0674bdb6d6f`。新 profile 的 dump / restore 使用同一固定 ELF，精确比较开关仅给 dump；stock 4.2 restore 兼容性另有独立探针和 Requests cold 快速检查。见[构建锁与使用边界][async-criu]。
- host baseline 的私有原版 CRIU 4.2、上述 guest 扩展和系统 3.16.1 分别记录，不能混称“同一个 CRIU”；没有覆盖系统二进制。该历史批次使用 `spr4numa:/mnt/disk2/dyp/async-incremental-frozen-20260922`；当前开发和验收统一使用 `/mnt/disk2/dyp/deltabox-runtime`。


## Checkpoint profile

历史 `fix/async-dump-lifetime` 分支默认 `checkpoint_profile=historical-async-full`。当前 `main` 的共享 runtime 默认 `runtime-default`；AE review 配置可按实验选择 `async-incremental` 等 profile，以该次有效配置和 `run.json` 为准。应按实际配置解释结果：

| Profile | 协议 / 当前证据 | 使用范围 |
|---|---|---|
| `historical-async-full` | 独立 dump 副本后台做全量 CRIU dump，warm template 供恢复 | 本分支三轮 Django 修复验证；不能证明异步增量链有效 |
| `runtime-default` | 增量配置；统一候选实测同进程可复用父页，但 restore 后新 active 触发 CRIU PID-reuse 保护；创建 warm template 前仍等待 dump 完成 | 后续统一候选的验收配置；尚未满足跨 restore 的真正异步增量目标 |
| `async-incremental` | 独立 dump 副本、后台精确父页比较及增量提交；已验证跨 warm / cold restore 后继续复用 | 最新分支显式 opt-in，仅 standard replay，eager cold；有界队列可产生背压，不适用于任意多线程 agent 或持久可变文件 FD |

先前统一候选 Requests-863 的 dump 大小为首张 20.89 MB、同进程增量 2.98 MB、restore 后再次 20.89 MB。这是 CRIU 镜像父页引用退化，不是 warm fork 的物理内存 CoW 消失。该 profile 等待的是 `criu dump` 的 future；仅把任务提交线程池不等于 API 已异步。那次机制核查未修改 CRIU，原始证据见[增量机制核查][candidate-incremental]。最新修复另增 CRIU 扩展并验证真实父页内容，未删除必要的 cold 等待、伪造 inventory 时间戳或关闭 PID 保护；§2.1 的 pages-only 大小也不能与旧轮总目录大小直接相除计算收益。

切换 profile 或源码版本时，应建立独立配置和输出目录，不能把 `historical-async-full` 的成功补记为 `runtime-default` 的通过。


## 维护与诊断记录（更新至 2026-09-22）

以下保存修复历史、后续候选分支调查及详细排查方法，供维护和核查证据使用。各项记录的源码版本与验证日期应分别解读，不代表评审会在当前工件中遇到所有列出的问题。

| 项目 | 当前状态 / 处理方式 |
|---|---|
| 原后台 CRIU dump 失败 | 已复现 restore 误杀未完成 dump 的临时进程；修复在 `4253d79`。同样注入修复后通过，三轮自然完整轨迹通过。见[日志与复跑命令](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/dump-lifetime-20260921/README.md) |
| 增量链与异步 dump | 旧 `runtime-default` 仍保留原等待和 PID-reuse 行为；新 `async-incremental` 已在冻结版 Django warm / Requests cold 完整单轨迹验证，见 §2.1 和[新报告][async-report]。不能给旧 profile 回填成功 |
| cold restore | FIFO 帧、epoch / ready、PID namespace 回收和继承 FD 等修复后，源码 `426a02c` 的完整 Django warm/cold 各 29 checkpoint / 28 restore 通过；新源码另完成 Requests 28 次 eager cold。见[前轮修复证据][replay-fix-report]和[新报告][async-report]，不再泛列为未解决失联 |
| Figure 6 适配器 | restore 已委托当前 core，checkpoint AST 守卫保留；Sympy 前 5 事件的 none/skip/gc 通过。write-warm 仍因并发覆盖风险禁用，未完成全图；该 fork-only 适配器明确拒绝新 async profile。见[修复验收][replay-fix-report] |
| CRIU+cp baseline | 原大型 worker 是单线程；3.16.1 / glibc rseq 恢复不兼容有诊断证据。私有原版 4.2 在单/四线程科学进程均完成两轮恢复执行，后续 baseline 有完整轨迹证据；旧失败不计性能。见[host CRIU 诊断][host-criu-report] |
| Replay / FC-Diff 消息差异 | 先修复 synthetic ViewCode span 顺序；随后 AE 默认改为 `audit`，差异有界保存在内存，计时结束后导出，热路径无差异诊断 print/hash/写盘。`strict` 保留排错；协议、cursor、worker 错误仍失败。见[消息审计报告][audit-report]；不能把 audit 完成称为消息完全一致 |
| baseline 测试 runtime | 最新代码修复 Replay/CRIU/FC/Figure 2 的重建绑定；Replay/CRIU/FC 实机执行 Requests 的显式 2 用例子集。默认 `none` 仍记为 `tests_not_executed`；Cube/E2B 对应模式未验收，全部测试集环境尚未补齐。见[绑定报告][baseline-runtime-report] |
| Cube / E2B | 2026-09-21 检查时 Cubelet 的 ext4 存储不支持 FICLONE，SDK / Table 2 模板缺失；E2B L1/API 未就绪，配置的 3000 端口实际属于 Cube。此为历史检查；2026-09-22 已完成两者 Table 2 首输入补测，见本页顶部。原检查见[服务检查][candidate-services] |
| 文件系统正确性 | 独立新内核已修复旧 FD `EBADF` 及污染同名新路径：4/4 严格语义用例、3 套 recovered 脚本、额外 seek/read/write 校验通过。未恢复论文最终 53-case 清单，也未作内核专门性能 A/B，见[补丁与证据][overlay-fix] |
| 历史记录与论文 | DeltaBox fast 历史聚合与发表值不同；slow 完整成功仅 8/12；部分历史 E2B 图含模型或缩放；均保留标记 |
| 全量覆盖 | 全部 1404-job CPU 计划尚未完整执行；Figure 8(b) 脚本待 GPU 实测，(c) 已能在 CPU 上计算，历史参考计算不计作新测量 |

详细入口验证记录见 [docs/runtime-validation.md](runtime-validation.md)。常见运行问题：

| 现象 | 检查位置 / 处理方式 |
|---|---|
| 输入不存在或哈希不匹配 | 重新执行 `prepare`、`paper_data.py verify`，核对数据包；不要跳过校验 |
| `/dev/kvm`、工具或镜像缺失 | 查看 `doctor` 的失败项和当前 `AE_CONFIG`；macOS 仅用于分析，VM 实验在 Linux 上执行 |
| `OverlaySwitchUnsupportedError` | 核对 `run.json` 的 kernel 路径、哈希与 guest 启动内核；只替换 `base.xfs` 不会启用自定义 ioctl。本次为 `CONFIG_OVERLAY_FS=y`，不是未执行 `modprobe` |
| wrapper 无终端进度 | 查看终端打印的 `logs/<step>/stdout.log`；启用绑核时还有 `environment/<experiment>/command.log`、频率采样与恢复状态 |
| sudo 非交互检查失败 | 托管机器联系作者修复账号权限；自建环境检查 `sudo -v` 与所需环境变量是否保留 |
| 频率锁定被拒绝 | 核对 CPU 是否位于节点内、cpufreq policy 是否跨出所选核；不要扩大到整机来绕过检查 |
| CRIU 报 EPERM / ESRCH | 查看 `guest.log`、`diagnostics.tar.gz` 和具体 CRIU 日志；目标提前退出也会产生这些错误，不能仅凭 EPERM 断言权限不足 |
| 目录已存在 | 使用新运行编号；脚本不会覆盖已有测量证据 |
| 分析后缺图或样本变少 | 查看 `summary.json` 的排除原因及 `plots.json` 的 unavailable 项，保留缺测 |

终止实验时只操作自己启动的进程、VM 或 sandbox。运行器会清理自己拥有的临时资源，不要用按名称批量 kill 的方式清理共享主机。


[candidate-release]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/release/README.md
[candidate-first-round]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/README.md
[candidate-correctness]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/correctness-audit.md
[candidate-incremental]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/incremental-audit.md
[candidate-war]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/war-metric-audit.md
[candidate-cold]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/cold-restore-diagnosis.md
[candidate-memory]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/memory-adapter-audit.md
[candidate-baselines]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/local-baseline-failures.md
[candidate-services]: https://github.com/delta-box/deltabox-runtime/blob/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/baseline-prerequisites.md
[candidate-figure02]: https://github.com/delta-box/deltabox-runtime/raw/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/comparison/figure-02-comparison.png
[candidate-figure08]: https://github.com/delta-box/deltabox-runtime/raw/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/comparison/figure-08a-comparison.png
[candidate-figure09]: https://github.com/delta-box/deltabox-runtime/raw/893d079b13e3d1f7e338ccd4c8c4bf52527b6d8a/ae/report/first-round-20260921/comparison/figure-09-comparison.png
[async-report]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/ae/report/async-incremental-20260922/README.md
[async-summary]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/ae/report/async-incremental-20260922/frozen-summary.json
[baseline-runtime-report]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/ae/report/baseline-runtime-20260922/README.md
[async-replay]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/replay/README.md
[async-lock]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/release/candidate-lock.json
[async-criu]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/replay/criu/README.md
[overlay-fix]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/replay/diagnostics/overlayfs/README.md
[replay-fix-report]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/ae/report/replay-fixes-20260921/README.md
[host-criu-report]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/ae/report/replay-fixes-20260921/criu-host/README.md
[audit-report]: https://github.com/delta-box/deltabox-runtime/blob/072c4594a44c2b810dbfcd6012a10b77ec18f2e4/ae/report/replay-audit-20260921/README.md

Figure 1 作为论文的问题引入，已移出独立 AE 任务和主页。上文涉及它的旧批次验收是历史记录，保留原始证据，不构成补测要求；当前范围以[统一总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)为准。
