# 异步增量 checkpoint：资源与生命周期审查

本文针对 `fix/async-incremental-replay` 的受限 replay profile。它说明实现边界与独立探针证据，不表示所有论文 workload 的完整恢复测试已经通过，也不把机制探针的耗时作为论文性能结果。

## 进程与增量链

活动 agent 在命令边界暂停接收新 workload。控制器先封存任务目录的 OverlayFS 代际，再产生 warm template 和独立的 dump 副本。warm restore 从 warm template fork；后台 CRIU 只接触 dump 副本。增量写者可以等待物理父镜像，但这个等待应留在后台；pending 配额耗尽的 admission 等待必须计入真实 checkpoint API。

dump 副本使用新的 PID namespace，成为虚拟 PID 1，使每个镜像的页映射文件使用一致的任务标识。PID 一致不能证明页面可以复用：CRIU 补丁必须在首次落盘前比较当前页与物理父链中的真实字节，并保留原有 PID reuse 检查。不能把父 inventory 的时间戳改写当成有效的页面来源证明。

PID namespace 不隔离 FIFO、文件描述符或挂载。PID 1 的自发 SIGSTOP 也不能作为完成冻结的证明。当前协议由祖先进程发 SIGSTOP，并等待子进程确实停止；匿名握手 pipe 必须在停止前关闭。控制器必须使用其自身 `/proc` 视图验证实际 PID，而不能直接相信子进程报告的第一项 NSpid。

冷恢复如果杀死旧 PID namespace 的 init，会连带杀死嵌套 namespace 内的 dump 副本。因此销毁 namespace 前必须等待所有仍拥有进程的 writer 完成处置。warm restore 不应等待这些后台任务。冷恢复得到 PID 1 后，下一次 checkpoint 前要重新将活动 agent 移到普通子进程，否则下一次 warm rollback 杀 active 会杀掉整个 namespace。

## 当前支持的资源合同

`async_resources.validate_replay_resources()` 要求控制器持有串行命令门，并检查：

- agent 单线程；没有尚未结束的应用子进程。已归属当前 pipeline 的 dump 副本可继续存在，已退出的 zombie 不作为活跃资源拥有者。
- cwd、可执行文件和私有文件映射不引用可变的任务 OverlayFS。
- 文件映射仅来自声明为不可变的 guest image；可写共享映射、非不可变来源的共享映射、已删除文件映射和不在合同内的路径被拒绝。不可变库文件的 `r--s` 映射（例如 glibc 的 gconv cache）可保留，因为其 backing file 受同一不可变前提约束。
- 不保留应用文件、目录、socket、匿名 pipe 等持久 FD。只允许明确列出的 RDWR|NONBLOCK 协议 FIFO、`/dev/null` stdio，以及列出的 append-only 诊断 stdout/stderr。

`freeze_dump_view()` 仅在 disposable child 内重新打开这些外部通道，取得独立的 open file description。它不执行 mount namespace 隔离，不重挂 OverlayFS，返回的证据明确为 `isolated_overlay=False`、`mutable_task_file_references=False`、`immutable_image_required=True`。控制器仍须保留文件系统代际的 layer lease。

不重挂的原因是继承的文件 VMA 仍可引用旧挂载对象；未经验证的 mount namespace 复制和重挂会制造 mount ID / 设备不一致。当前实现通过拒绝任务文件引用缩小支持范围，而不是宣称处理了任意持久文件 FD、共享 mmap、删除后仍打开的文件、文件锁或任意系统根目录变化。

不可变 guest image 是使用前提：仅根据 `/proc/PID/maps` 的路径检查不能阻止另一个进程修改 `/usr` 等根目录。此 profile 的 replay worker 在 action 边界已经等待测试子进程结束，应用代码在任务目录执行；若具体 trace 会修改映射中的系统文件，必须先提供额外隔离或拒绝该 workload，不能沿用这一合同声称正确。

replay 使用的 Conda 可执行文件在 `/proc` 中可显示为 `/mnt/data/opt/...`，所以白名单包含这个精确子目录；VM runner 将 data drive 设为只读，guest 的 `/opt` bind 到该源目录。该例外不包含 `/mnt/data/testbeds` 或 `/mnt/data/opt-extra`。新 profile 的 agent 启动时将 cwd 改为 `/`，任务 subprocess 仍显式使用任务根目录作为 cwd。

详细 maps/process 证据保留在 registry。传给子进程的 `dump_child_contract()` 只包含设置 FD 所需的字段，避免把数千条 maps 放入 FIFO。协议仍需要带截止时间的 write-all，精简 payload 本身不保证一次 `write()` 完整。

## FIFO 与诊断日志不回滚

fork 得到的 FIFO 仍共享内核 buffer，即使重新打开获得独立 OFD 也不改变这一点。这些通道是外部传输设施，不是需要回滚的应用状态。冷恢复使用当前打开的 FIFO 和 append-only 诊断日志，通过 `--inherit-fd` 传给 CRIU；日志不恢复旧 offset，FIFO 不重放旧 buffer。控制器仍须串行命令、过滤失效 epoch，并负责重新激活 worker。

基于固定源码 CRIU `2cf8f13ca` 的检查：`fifo.c` 通过 `open_path(reg_d, do_open_fifo, ...)` 打开 FIFO；`files-reg.c:open_path()` 首先检查 inherited descriptor，命中时绕过 `do_open_fifo()` 中的 `restore_pipe_data()`。普通文件名存储为根目录相对名称，所以 `/tmp/agent.in` 的继承 key 是 `tmp/agent.in`。

真实 Linux 探针已在 spr4numa、内核 `6.8.0-124-generic`、NUMA 2 / CPU 52–55 上完成：

| 条件 | 当前 FIFO 内容 | 冷恢复后实测 |
| --- | --- | --- |
| 镜像内历史 buffer 为 A，普通 restore | B | BA：发生历史回灌 |
| 相同镜像，restore 继承当前 FIFO | B | B：历史 buffer 未回灌 |

结果和 CRIU 二进制 SHA-256 见 [fifo-probe-001/result.json](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/async-incremental-20260922/lineage/fifo-probe-001/result.json)，完整 dump/两次 restore 日志在同目录。探针源码为 [fifo_restore_probe.py](fifo_restore_probe.py) 和 [fifo_worker.c](fifo_worker.c)。两次恢复的 worker 均保持 stopped；观察者从当前 FIFO 读取结果，所以没有依赖被测 worker 自行消费或清空历史数据。

这个探针证明上述固定 CRIU 对命名 FIFO 的继承行为；它不替代完整 replay 的内存、文件、epoch、warm→cold→checkpoint 连续恢复检查。独立的 exact-parent 补丁若不改 FIFO/文件恢复代码，可以保留同一资源处理机制，但仍需集成测试。

## 提交、失败与 GC 必须维持的条件

- 镜像写入 staging 目录；CRIU 成功关闭输出后，原子 rename 才发布完整目录。`WARM_READY` 不等于 `DURABLE_READY`。
- public dump Future 在成功、CRIU 失败、父失败和 executor 拒绝提交等路径都必须完成。失败不得通过空镜像或全量回退伪装成功。
- dump 的 pidfd 由一个明确的所有者持有到进程退出，再释放 pending 配额；失败清理不得双重关闭 pidfd 或覆盖最初错误。
- `dispose_done` 与镜像成功状态分开记录。cold teardown 可以经过已确认清理完毕的无关失败镜像；不能经过尚未完成或 `cleanup_error` 所指的清理失败。选中的目标镜像仍需单独通过可恢复性检查。
- GC 同时保留 logical parent、physical previous image、effective restore 和所有 pending writer 的传递依赖。只保留一张逻辑图不够。
- lazy restore 若启用，还必须保留正在提供缺页的镜像及祖先；当前 async profile 关闭 lazy restore，以免未实现的 page-server lease 扩大正确性边界。
- 删除 registry 条目同时需要处理相应的 warm template；不可终止当前 active、namespace init 或尚被 writer 使用的资源。

`tests/test_async_resources.py` 覆盖拒绝不支持资源、精简 payload 与外部 FD 生命周期。`tests/test_async_checkpoint.py` 使用真实 ThreadPool 和 Event 阻塞模拟 CRIU，覆盖前台早返回、物理父依赖、父失败、bounded admission、原子目录发布、Future 终结与 GC 闭包。这些单元测试不验证特权系统调用或 CRIU 页内容；实际 Linux 探针与端到端回放承担这一层验证。
