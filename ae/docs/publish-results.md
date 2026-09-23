# 现有测量的图表、结果归档与 README 发布

本指南用于选定一次已完成运行后的归档与发布；首输入验收必须保留限量范围，不称为全量复现。当前已发布批次见[结果目录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/README.md)。旧诊断结果保留自己的版本和目录，不能改名为新锁测量。发布过程不重写 raw manifest、不修改论文原图。

## 中英文全文

根目录 `README-zh.md` 保存中文全文，`README.md` 保存据此翻译的英文全文；两者都不是索引页。更新复现说明或输出示例时，先更新中文，再同步英文的对应内容、图注和 alt 文本。保持命令、实验项、图片路径和判断依据一致，两版顶部保留语言切换链接。`ae/README.md` 只保留双语入口与旧锚点导航。

## 统一偏差记录

所有实验与论文的数值、配置、计时定义、输入语义和覆盖差异，以及超过 50% 偏差的调查、未确认原因和后续验证，统一维护在 [ae/report/README.md](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)。这是唯一持续更新的偏差总账；不得按日期、批次或少数图号另建当前偏差说明。结果目录继续保存原始证据，旧报告保留历史身份并指回总账。每次发布先更新总账对应实验，再同步覆盖页与根目录双语 README 的结果图和链接；根 README 以论文实验为导航，不写作者补测进度、样本完成比例或批次历史；图片只作为脚本输出示例，不在图下添加来源、样本数或覆盖缺项链接。偏差调查和实际数据范围写入统一总账。

## 绘图格式来源

后续新图先核对 spr4numa 的 `rollbackable-sandbox-paper/plot/paperstyle.py` 和对应图号脚本；路径、版本、逐图映射与历史数据注意事项见 [论文绘图格式来源](paper-plotting-reference.md)。沿用格式时，仍显式接入本轮 fresh analysis。

## 选择实验与 attempt

运行代码以该轮 `review.json` 和各 job `run.json` 中记录的 release 为准。当前待测锁和后续修复锁可能不同；不要硬编码这里提到过的旧 commit，也不要仅用发布时的 `git rev-parse HEAD` 标识实验版本。

1. 检查 `SUMMARY.md`、顶层 `review.json` 的结束状态及覆盖列表；中断或仍 running 的目录需保留原件并另写终止说明，不能伪改为成功。
2. 选择 `review.json.outputs` 指向的一次 `attempt-NNN`。确认 analysis、plots、comparison 和冻结的 coverage 都来自同一 attempt。
3. 核对原始成功 job 的事件数、完整/限量标记和计时配置。缺项或失败必须与成功图一起公开；Figure 8(b) 使用独立 GPU 计时入口，(c) 使用 CPU 理论计算入口，分别记录验证范围与输入来源。
4. 从测量源码锁读取完整 `source_sha256`。统计量不能跨 source、profile、zero/recorded 等口径混合。Table 2 的系统列、Table 3 的 fast/slow 列可以在原布局中并列，必须保留各自身份与样本数；同一格存在冲突来源时不隐式选择或平均。

## 需要保留的产物

| 产物 | 用途 |
|---|---|
| 正式测量的源码锁、`review.json`、`SUMMARY.md` | commit、完整源码 SHA、选择范围及结束状态 |
| `plans/`、`configs/`、各组 suite / 各 job `run.json` | 有效配置、计划、输入和测量条件 |
| `runs/` 中 analysis 使用的原始文件 | JSON/JSONL、CSV、schedule、mock audit、后台 dump 和失败证据；不能只归档均值 |
| `environment/attempt-NNN/` | NUMA、CPU 频率采样及配置恢复证据 |
| `analysis/attempt-NNN/` | summary、metrics、series及选入/排除的来源哈希 |
| `plots/attempt-NNN/` | 全部 PNG/PDF 和 `plots.json`，保留独立 population；论文布局通过 `populations` 和 `data_mapping` 记录各列/柱的来源 |
| `coverage/attempt-NNN/review.json` | 生成图片时冻结的覆盖快照，之后不要覆盖为最终顶层状态 |
| `comparison/attempt-NNN/` | 七项 AE 图片、并排图、PDF、`manifest.json`，以及互相链接的英文 `README.md` 与中文 `README-zh.md` |
| `logs/`、旧失败/续跑记录 | 失败原因与恢复过程；可能较大的 staging/磁盘另行记录取舍 |

复制后保持相对目录结构。原 manifest 内的远程绝对路径是历史来源，不要批量替换；分析器和发布 helper 使用复制到本地的对应文件并核对原 SHA-256。原始测量可以另行压缩，但必须保留可重新展开并分析的文件及压缩包哈希。若省略可重建的 staging 镜像、源码包等，应说明未归档项，而不是改写原始清单。

## 手动生成同一 attempt 的对比图

一键入口已经自动执行这些步骤，通常无需重复。需要重画时优先用 `--analyze-existing` 写到一个新输出目录，它不会启动实验：

```bash
bash ae/run_all.sh --analyze-existing /absolute/path/to/formal-review \
  --output /absolute/path/to/formal-review-replot
```

如果仅需对已有同一 attempt 的 analysis/plots 生成另一份并排图：

```bash
export AE_REVIEW=/absolute/path/to/formal-review
export AE_ATTEMPT=attempt-001

.venv/bin/python ae/scripts/build_review_comparison.py \
  --analysis "$AE_REVIEW/analysis/$AE_ATTEMPT/summary.json" \
  --plots "$AE_REVIEW/plots/$AE_ATTEMPT/plots.json" \
  --coverage "$AE_REVIEW/coverage/$AE_ATTEMPT/review.json" \
  --output "$AE_REVIEW/comparison-regenerated/$AE_ATTEMPT"
```

此命令写入独立的 `comparison-regenerated/` 目录；该目录应尚不存在。已有发布图片需保留，后续重画继续选择独立的新目录或新 replot 输出。只有 analysis 和 plot 均成功才传入这两个参数；只传 coverage 可生成缺测说明面板，但这种无 fresh 数据的结果不能通过正式图片发布检查。

论文原图来自 `ae/reference/figures/`，PDF 源哈希与 crop 哈希已经记录。不要手工重画、改数字或替换为网络上另一版论文图片。

## 为主页七项图表生成发布片段

结果统一在 `ae/results/<测量源码>/` 内维护，重绘放在 `rendering/<绘图源码>/`。当前发布采用下列路径；换批次时须同时替换测量 source SHA、输出路径及覆盖说明。不要复制整套工作目录或改变原始 manifest。

```bash
export AE_REVIEW="$PWD/ae/results/c775a9215718/rendering/87550c0"
export AE_ATTEMPT=attempt-001
export AE_SOURCE_SHA256=e3fa3c4d57933967d33b6067cfa6a4bd195433714207ab11eb4abeb4e8084474

python3 ae/docs/publish_comparison.py \
  --manifest "$AE_REVIEW/comparison/$AE_ATTEMPT/manifest.json" \
  --readme "$PWD/README-zh.md" \
  --expected-source-sha256 "$AE_SOURCE_SHA256" \
  --output "$AE_REVIEW/readme-snippets-$AE_ATTEMPT.md"
```

`--readme` 直接指定要更新的根 README：中文用 `README-zh.md`，英文用 `README.md`，分别指定新的片段输出文件。helper 自动生成对应语言和相对路径；无需手工增加 `ae/` 前缀。两版全文均在各 `AE-RESULT:<图号>` 标记之间更新，图注只说明“脚本输出示例”并链接并排大图。

helper 使用标准库，并在写入前检查 coverage、summary、plots、论文裁剪、所有 comparison 图片/PDF 的哈希，以及统计样本的 release source 身份。它生成相对链接和简短的输出示例图注，**不会自动修改 README**。输出文件必须尚不存在；跨版本、身份缺失或图片被更改时明确报错。

| README 项 | 右栏图片 |
|---|---|
| Table 2 | `table-02-ae.png` |
| Table 3 | `table-03-ae.png` |
| Figure 2 | `figure-02-ae.png` |
| Figure 6(a)(b) | `figure-06-ae.png`，各 population 单独展示 |
| Figure 7 | `figure-07-ae.png`；组件模型定义统一记录在偏差总账及 manifest |
| Figure 8(a) | `figure-08-cpu-ae.png`；(b) GPU 时延和 (c) 理论计算另由 [Figure 8 入口](../paper/figure-08/README.md)生成 |
| Figure 9 | `figure-09-ae.png` |

核验通过后，将七项图片链接和简短图注同步替换到根目录两版 README 对应右栏，保留左栏论文原图。每张缩略图宽 300，可以点击原尺寸；图下仅提供并排大图入口；完整执行覆盖、样本数与缺项放在 manifest / 覆盖快照中。Table 2、Table 3 和 Figure 2、6、7 使用紧凑论文版式，不把这些审计内容拼成长图。Figure 2 的两个数据域、Figure 6 的两项实验按原图的面板并列，Figure 7 固定保留四组工作负载和两个系统的位置。统一偏差总账记录实际范围和差异，不能把 快速检查 或部分 cohort 标成全量复现。

## 历史本地文档镜像（可选）

当前工作只在 spr4numa 固定主仓库进行；不需要同步旧工作副本。只有另外明确要求时，如需让 `/Users/bytedance/Desktop/delta/deltabox-runtime-dump-lifetime/ae/README.md` 展示相同文档：

- 只同步明确选择的 README、文档、论文原图及正式报告产物；先检查目标同名文件是否自上次读取后变化。
- 不对整个旧工作树执行覆盖式同步，不覆盖另一个进程正在维护的 `ae/images/`、`ae/build_images.sh`、配置或运行时代码。
- README 的相对图片与文档链接需在目标目录存在；复制后重新检查这些链接。保留原始证据目录，不能用新文件覆盖旧测量；当前偏差结论只更新统一总账。
- 文档同步不选择运行源码。旧入口需要运行新版本时，显式使用 `bash ae/run_all.sh --runtime-repo /path/to/complete-checkout --config /absolute/config.json ...`；入口会打印所选 checkout，不猜测目录。macOS 上只分析/绘图，VM 实验仍在 Linux 上运行。

## 原始数据归档与 Git 可用性

当前批次在 `cpu/evidence.tar.gz` 和 `checks/deltabox-inputs/evidence.tar.gz` 分别发布完整轨迹与短程检查。每份 `evidence-manifest.json` 包含归档 SHA-256、逐文件 SHA-256 和省略的大型可重建文件说明。不能只上传图片和均值。

发布前，从归档解包到仓库内新的 `ae/work/` 子目录，独立重新分析；检查原始来源、统计选择、全部指标与序列和原分析一致。再使用图片发布 helper 校验论文原图、analysis、plots、coverage 及图片哈希，检查两版 README 各自的七组链接实际位于 Git 提交中。`ae/results/` 默认忽略，因此仅显式加入选定归档、索引和重绘产物，不将整个原始 staging 目录强制加入 Git。
