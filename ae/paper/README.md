# 按论文图表组织的复现数据

各实验与论文的当前偏差结论统一见 [偏差总账](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)；本目录保存历史输入与来源清单。

以 `atc26-paper158.pdf` 为基准。这里按图表存放已定位的输入、schedule 和已有测量记录；CPU runner 与配置入口见 [ae/README](../README.md)；这些历史数据本身不代表当前 runtime 已完成全量重测。

| 论文项 | 目录 / 来源 | 输入范围 |
|---|---|---|
| Table 1 | [table-01](table-01/) | 文献及 Table 2/3 的测量，没有独立 trace |
| Table 2 | [table-02](table-02/) | 六个 backend 各自 cohort；12 / 12 / 8 / 238 / 244 尝试 / 244 |
| Table 3 | [table-03](table-03/) | DeltaBox 12 条，分别保留 fast 与 slow 运行 |
| Figure 1 | [figure-01](figure-01/) | phase 数据；轨迹引用 Table 2 |
| Figure 2 | [figure-02](figure-02/) | 文件系统 30、内存 5 |
| Figure 3/4/5 | 设计与架构图 | 不需要轨迹重放 |
| Figure 6 | [figure-06](figure-06/) | 内存策略 1 条，adaptive 12 条 |
| Figure 7 | [figure-07](figure-07/) | DeltaBox 12、E2B 原表 8，保留两套输入 |
| Figure 8 | [figure-08](figure-08/) | 9 条 fork primitive 输入；另含 synthetic fan-out、GPU 测量 |
| Figure 9 | [figure-09](figure-09/) | 185 个 pool+instance 输入，136 个不同实例 |

每个目录包含：

- `README.md`：用途、数量和已知复现限制。
- `cohort-*.csv`：具体实例与输入路径。
- `files.jsonl`：逐文件来源、目标路径、字节数、SHA-256。
- `data/`：本地实际可访问的数据文件。相同内容只在 `traces/objects/` 保存一份，各目录以相对链接引用。

## Git 与数据包

Git 保存清单、文档、整理脚本及 `datasets/` 中的精简压缩包。展开后的 `data/`、对象目录、完整历史归档及 `dist/` 全部忽略，避免将大量重复 trace 写进 Git 历史。当前数据包的精确大小和校验值见 [data-bundle.json](data-bundle.json)。

clone 仓库后，接收方在 runtime 仓库根目录执行以下命令，导入随仓库提供的精简数据包：

```sh
python3 ae/scripts/paper_data.py import
python3 ae/scripts/paper_data.py verify
```

需要 Python 3.9+ 和 `zstd` 命令。导入会核对整个压缩包、清单版本和每个对象的 SHA-256，并重建各图表目录。导入包不包含 VM/rootfs、内核、模型权重、依赖安装环境；已恢复的 benchmark driver 位于 `ae/vendor/`。不能仅凭此数据包声称已经完成端到端系统复现。

发布副本已清空 JSON 配置中的 API-key 字段，其余字节（包括 prompt、observation、action、测量值和 RTT）保持不变。每个文件保留原始 `source_sha256` 与发布副本 `sha256`，具体变换见 [data-transformations.json](data-transformations.json)。完整未修改原件仅保留在本地历史采集目录。

本地维护者可以从完整采集目录重新构建：

```sh
python3 ae/scripts/build_paper_layout.py
python3 ae/scripts/paper_data.py export datasets/deltabox-paper-data-NEW_VERSION.tar.zst
```

第二个命令拒绝覆盖已有包；生成新版本时使用新的文件名，保留原有版本。

## 仍需保留的口径

当前 ms 与旧 portable trace 的版本不同；本包选取与 real-run driver 默认路径对应的现存 ms 候选，旧 portable 和其他历史数据留在本地审计档案中。若不能证明历史 run 的确切输入版本，cohort 中仍保留该限制。E2B 的另一轮 12 实例实验没有混入论文原表的 8 实例来源。

统计范围、失败尝试、建模结果与实测结果必须按各图表说明解释；详见 [来源核对](../docs/trace-audit.md) 和 [正文数量核对](../docs/paper-trace-count-check.md)。
