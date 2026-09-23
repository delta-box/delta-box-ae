# Runtime 分支验证记录

验证主机：本地 macOS + `spr4numa`（Linux `6.8.0-124-generic`，host CRIU `3.16.1`，Firecracker `1.6.0`）。新分支基于 `main@c74c8f58819b1665c192c5b03d35b8ad8464a751`。生产 runtime、CRIU 和论文正文均未改动。下面的运行是入口验证，并非整套论文性能重测。

**执行条件补充：本轮没有绑物理核/单一 NUMA 节点，也没有锁频。** 关键 run 的 CPU 亲和性为 `0–95`，内存允许节点 `0–5`；已保存的 Firecracker NUMA maps 使用 `default` 策略。CPU 0 调频记录是 `schedutil`、0.8–4.0 GHz 配置范围，不是固定频率或实测有效频率。证据与时延百分比见[报告第 0/2/5 节](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)。这些 快速检查 结果不能作为统一环境下的论文性能验收。

## 已执行

- 从提交 `c53ad9f` 做独立干净 clone 后，`prepare`、逐对象 verify、51 项测试、Python 编译和 1404-job 完整计划全部通过；没有依赖原工作目录中的未跟踪源码。

- 51 项 `tests/paper` 测试通过：当前源码打包、trace 转换、真实 worker 证据校验、失败传播、嵌套进程超时回收、API 计时、配置边界、移机分析、artifact 哈希、cohort 隔离、阶段计时残差、远程 sandbox 失败/取消清理等。
- 全部 Python 入口和 vendor 编译检查通过；80 个移植后的 vendor 文件 SHA-256 校验通过。
- 全 CPU `plan --all` 构建成功，共 **1404 个 job**；`--limit 1 --max-events 3` 构建 23 个 job。计划包含失败尝试 cohort，数量不是成功样本承诺。
- 输入包 import/verify 通过：2500 个去重对象、4378 个引用、922,062,239 字节展开对象；压缩包 20,496,905 字节。
- 8 个论文图表的 archived 分析完成，16 个 PDF/PNG 已渲染。三个真实 快速检查 分析结果另外生成 26 个 PDF/PNG，并检查 Figure 6 图像。输出明确标记历史/新测量、建模/缩放及 GPU 跳过。
- 独立 SPEC review 与后续 quality review 完成，已确认的问题均已修复并回归。

远程原始结果位于 `/mnt/disk2/dyp/ae-paper-reproduction-20260921/ae/results/`：

| 目录 | 实际执行 / 结果 |
|---|---|
| [第二次运行时检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) | requests-863，当前 runtime、历史 async-full profile，2 checkpoint + 1 restore 成功 |
| [第二次分支检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) | 实测 64 MiB donor，N=1/4/16/64，全部子进程继承状态验证通过 |
| `fanout-report-001` | 使用 `6818499` 为图表报告重跑；N=1/4/16/64 分别为 35.511/37.368/94.072/411.113 ms，继承内容验证通过；当前格式的 artifact 校验及 fresh 分析通过 |
| [第二次重放检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) | 真实 Moatless Replay，2 restore 成功；这是补充 zero-LLM 计时前的版本 |
| [第一次内存检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) | SymPy-22840，none/skip/gc/warm 四组各 2 checkpoint + 1 restore 成功 |
| `cpu-validation-001` | Figure 9 首个 Astropy 输入，ext4/XFS/XFS reflink 三组完整 edits 成功；Figure 6 首个 legacy trace 两组各前三事件成功；Figure 2 首个 filesystem profile 完整 replay 成功；DeltaBox、Replay prefix 成功 |
| `cpu-validation-002` | 新版 Replay 3 restore 成功，包含原始总耗时、mock 实际等待时间、zero-LLM 字段；新版 DeltaBox 3 events 成功，包含便于移机的 schedule artifact 和 快速检查 标记 |
| `cpu-validation-003` | 正确性套件与 FC-Diff 诊断，见下方未通过项 |
| `validation-analysis-001/002`、`memory-analysis-001` | 对真实 快速检查 产物运行 fresh 分析成功；失败 run 排除并附原因 |

较早失败的 probe 也保留：[第一次运行时检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) 的 protobuf 兼容问题已修复；[第一次分支检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) 的 guest fstab 缺辅助盘问题已修复；首轮 Replay 的 logical trace 路径问题已修复。不能将这些旧失败目录直接当作最新 runner 的行为。

论文编号与结果目录的完整对应、6 张对比图、8 张历史重绘图及重画命令见[图表报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)。旧 [第二次分支检查](https://github.com/delta-box/deltabox-runtime/blob/main/ae/docs/runtime-validation.md) manifest 缺少最终格式的 artifact 绑定，当前严格分析器会拒绝它；报告使用新跑的 `fanout-report-001`，未追补旧 manifest 来伪装成新格式。

## 当前环境中未通过或尚未运行的部分

1. **Host CRIU baseline**：Astropy-13033 在首次 restore 失败。保留日志明确为 `criu/cr-restore.c: ... killed by signal 11: Segmentation fault`。当前 host CRIU 3.16.1 与该进程/主机组合尚未通过恢复验证；不能把只完成 dump 当作复现成功。脚本保留其失败状态、过程和诊断日志。
2. **FC-Diff baseline**：输出路径过长导致 AF_UNIX bind 失败的问题已修复。随后真正执行了 root snapshot、diff checkpoint 和两次 restore，但第三次 expansion 的 recorded-response 校验出现 mismatch，guest 返回 HTTP 500。`cpu-validation-003` 保留完整错误：mock cursor=3、n_mismatch=6。现存 trace/dependency/driver 组合尚不能声称与历史成功 run 完全一致；未放宽请求哈希检查。
3. **正确性**：修正 XFS 根挂载信息探测后，`test_full.sh` 输出 44 条 PASS，`test_deleted_open_resurrect.sh` 输出 10 条 PASS（含汇总，不能当作 10 个独立 case）。`test_cross_checkpoint_fd_cow.sh` 在删除新分支路径后，向旧 FD 写入返回 `Bad file descriptor`；returncode 非零，整组失败。该额外 suite 与论文最终 53-case 清单的关系尚未完整恢复，既不宣称“53项通过”，也不修改断言来凑数。
4. **Cube/E2B**：未做新的服务基准。spr4numa 原 Cube SDK 路径不存在，检查时 Cubelet 尚未就绪；E2B L1 key/SSH/build/template 条件需重新指定。已提供真实 API/CLI 和 Figure 1 instrumentation 入口，缺依赖明确报错。
5. **全 cohort / 全链路**：未运行 1404-job 全量计划；未验证当前 runtime-default 配置的完整增量 restore/checkpoint 链；未将 prefix、legacy diff replay 或 fork-only 内存曲线冒充这些验证。
6. **GPU**：按作者要求跳过 Figure 8 的生成、训练以及依赖新 GPU 数据的派生指标。

以上环境/数据兼容性限制会导致相应 job 非零退出；`--keep-going` 可以继续其他独立工作。fresh 分析仅统计成功且哈希完整的产物，保留实际覆盖数，并明确 `paper_cohort_verified=false`，不会把 快速检查 显示为完整论文 cohort。

## 重复验证

在 runtime 根目录：

```sh
python3 ae/reproduce.py prepare
python3 -m unittest discover -s tests/paper -v
python3 -m compileall -q ae/reproduce.py ae/repro ae/runners ae/vendor
python3 ae/scripts/verify_runtime_sources.py
python3 ae/reproduce.py plan --all > /tmp/deltabox-plan.json
```

新测量需要匹配的镜像、依赖和服务。使用新目录保留每次结果；不要覆盖既有证据。
