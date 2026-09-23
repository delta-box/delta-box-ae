# Table 3 重测口径与一键入口

## 论文方法与本轮测量配置

参考文件是仓库内 `ae/reference/paper158.pdf`（SHA-256 `1cc012a6ba4afdd127372a236333983ad6d17934ac63680b37faa3e6f65bb95e`），PDF 第 11 页 Table 3、§6.1 和 §4.2。论文配置为 4 vCPU、8 GiB guest RAM、XFS rootfs + 只读 data image，关闭可选 skip/prewarm；快恢复使用 warm-template，慢恢复使用 CRIU lazy-pages。论文的 checkpoint 0 ms 是被推理窗口隐藏的时间模型，不是 API 实测为零。

论文 §6.1、§6.3.1 和 Table 3 图注明确描述默认**异步增量** CRIU dump，正文存储配置为 NVMe。不能根据单份保存配置推断论文全部实验实际采用了其他模式。

本轮按用户要求改用内存盘，并设置两个可区分的测量配置：`async-incremental-lazy` 为当前增量实现；`historical-async-full` 是代码已有的兼容配置名，本轮用它检查 full dump + lazy restore 的组件计时。后者仅是诊断对照，不能称为论文默认增量路径的精确复现。保存的运行配置只是选择此诊断配置的线索，不能据此给论文实验定性。

当前 `async-incremental-lazy` 的 exact-parent 协议在后台比较当前页与父镜像字节，只写变化页；本次补齐父引用页的 lazy 标记及后台 reader 的父链保留，支持 lazy cold restore。论文写的是 soft-dirty 跟踪，本实现仍读取待比较页，不能称为相同的脏页选择算法。旧 README 右图的 125.45 ms 因而不是同一 lazy 路径；其底行还使用了完整 API + ready/replay 等待，而论文记录使用内部计时。另一个实质缺陷是汇总只枚举 `FAST_COMPONENTS`，丢弃 replay 已保存的 `restore_slow_ioctl_ms`、`restore_slow_criu_ms` 等字段。

## 运行

在工作仓库执行：

```bash
bash ae/run_table3.sh --output "$PWD/ae/results/table3-incremental"
```

默认测量 12 条完整输入 × fast/slow 两臂，使用 `spr4numa-table3.json` 的当前异步增量实现。该配置的冷恢复使用 lazy-pages；dump 和 restore 使用同一构建 CRIU，避免内核探测缓存版本来回失效。没有默认 `--limit` 或 `--max-events`；短程验证须显式指定，不能称为完整复现。

需要诊断 lazy 组件计时时，可显式选择另一配置：

```bash
AE_CONFIG="$PWD/ae/configs/spr4numa-table3-historical.json" bash ae/run_table3.sh \
  --output "$PWD/ae/results/table3-full-lazy-diagnostic"
```

该诊断配置使用 full dump，不能代表论文所述增量路径。
两组结果不能混成同一测量总体。当前环境默认仍使用 `async-incremental-lazy`，没有为对齐论文数字而改成全量 dump。

VM runner 在自身私有 mount namespace 中，把 rootfs 和 data image 放到 `ae/work/vm/` 下的独立 `noswap tmpfs` 挂载；复制与 SHA-256 校验发生在 VM 启动和 C/R 计时前。guest snapshot store 同样必须成功挂载 tmpfs，失败即停止。挂载点虽然位于 disk2 仓库路径下，实际块设备 I/O 由内存承载；原始镜像只在准备阶段读取。每条轨迹完成后释放自己创建的 RAM 挂载，永久日志和结果保存在仓库内。

两个 Table 3 配置均绑定 NUMA 2、CPU 48–51。频率包装器逐 CPU 设置 performance、min=max=cpuinfo_max_freq，并记录 turbostat 有效频率，退出恢复原策略。最高 P-state 请求与实际运行频率分别记录。不要让两组测量或其他实验争用这些 CPU。

## 每个数字的定义

- Overlay、fork、CRIU：事件原始计时；slow 字段现在和 fast 字段一起进入分析，按实际事件数加权。
- Coordination：直接计时的串行前/后处理。fast 使用 fork 分派前和 fork 响应后的两个不重叠区间；slow 使用 CRIU 调用前、后的两个区间。lazy daemon 启动已包含在 slow 前处理内，不能重复相加。
- `restore_table3_total_ms`：active teardown 之后，组件恢复开始到 controller bookkeeping 结束的直接窗口。fast ioctl 与 fork 重叠；本轮 lazy 配置为串行 ioctl + CRIU。
- 新图底行明确写 component window / checkpoint overlap model。完整 `restore_api_wall_ms`、`restore_wall_ms`、原 `restore_critical_ms` 同时保留，不能把组件窗口当作完整 API 阻塞，也不能宣称它与历史论文未解析的 coordination 定义完全相同。
- checkpoint 重叠模型逐事件计算 `max(0, checkpoint_api_wall_ms - latency_ms)`，在 manifest 标成 `derived-model`。本 replay 没有直接测量并发在线 LLM 推理，因此模型值不代表实测端到端阻塞。
- 三个结构上不适用的格子为 `not-applicable`；缺少应有计时的格子仍为 `unavailable`。两者不能混称数据缺失。

源码、输入、stage 镜像 SHA、storage.json、NUMA maps、频率策略与采样、每条轨迹是否完整以及每格样本数都随本次结果保存。论文原图不改写，旧失败记录不删除，数值不以论文常数补齐。

准备阶段先检查绑定节点的空闲内存加可回收的干净 active/inactive 文件缓存是否足以容纳data image、将转为可写 rootfs 的 base image、8 GiB guest RAM 和 2 GiB 余量；不足会停止并记录缺口，不依赖 swap 或跨节点分配。Django 4.0 轨迹使用项目原生 `tests/runtests.py --settings=test_sqlite --parallel=1`，预先验证设置与应用加载。测试代码自身的失败保留原退出码和完整日志。原轨迹生成的模型缺少 app_label 时，记录 `workload-collection-error`、未通过测试和原始报错，继续回放后续动作；这不代表任何测试成功。未知环境启动错误和无报错的零测试退出仍不能通过。

RAM 中经过 SHA-256 验证的私有 base image 在启动前重命名为新建 rootfs 路径，不重复分配第二份镜像；共享原始镜像不修改。

原工具在执行测试前会拒绝纯目录参数。本轮只对原轨迹明确记录的同类错误重做目录存在性检查；匹配时记录 `invalid-test-selection`、`test_subprocess_started=false` 和 `test_passed=false`，文件状态不匹配则失败。不会把目录扩大为整个项目测试集。

旧 `async-incremental` 配置继续保留 eager 恢复以兼容已有二进制与历史运行；Table 3 一键入口默认选用新增的 `async-incremental-lazy`。lazy 模式要求 dump 和 restore 均提供 `exact-parent-lazy-v1` 能力，旧二进制被明确拒绝。

RAM 容量检查分两步：先确保能安全复制镜像，再按 tmpfs 实际分配页检查完整 8 GiB guest、可写 rootfs 剩余最大增长及 2 GiB 余量。只读 data image 的零洞不占 RAM，但逻辑大小与 SHA256 不变；任一步不足都在 VM 启动前停止并释放挂载。不会通过降低 guest RAM 绕过容量限制。

异步 checkpoint 的 `checkpoint_overlay_ms` 从层切换调用前到返回后计时，包含调用包装而不含 mkdir/rename。目录与层列表准备单列 `checkpoint_overlay_preparation_ms`，仍计入完整 checkpoint API。2026-09-23 首次短测及随后中止的完整批次曾使用更宽的 filesystem-sink 区间；它们的 checkpoint Overlay 行不作为最终校正口径数据，原结果保留。

两类文件缓存均扣除 Dirty、Writeback 和 Mapped；Shmem/anon 不计入可回收量。此前仅计 inactive 会把本轮镜像读取后提升到 active LRU 的干净缓存误当作不可用内存，造成启动前假性不足；对应未启动任务保留原记录，不计作性能样本。

交付配置按当前仓库相对路径定位 CRIU：`ae/work/runtime-deps/criu/61b978b4c7c03577e9a122e5d23845950304f84abec93ef95ef54a917d494940/criu`。spr4numa 主仓库与本 worktree 均已安装同一个验证过的 ELF（SHA256 `61b978b4c7c03577e9a122e5d23845950304f84abec93ef95ef54a917d494940`），合并后入口不依赖临时 worktree。需要重新构建时使用 `replay/criu/build_exact_parent.py`；新 ELF 必须重新记录构建身份，不能冒用这次二进制 SHA。完整测量 9bbc1ba 的旧绝对路径保存在原始配置里，没有重写；路径交付调整不改变二进制内容。
