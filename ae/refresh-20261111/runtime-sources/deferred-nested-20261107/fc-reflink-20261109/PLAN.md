# FC-Diff CoW 恢复重测计划

写定：2026-11-09。授权：用户要求使用更好的 FC-Diff 恢复方法，排在 Figure 7 后接力。旧数据与论文暂不修改，DeltaBox 不变。

## 目的及已知证据

上一轮 12 条 / 334 次 restore 的连续均值为 7727.237 ms，其中合并内存镜像 7170.016 ms：复制 8 GiB 基底约 4509.496 ms，差分覆盖与 fsync 约 2660.341 ms。12 次空差分链也生成了未使用的基底副本。官方 v1.6.0 合并器的小对照只减少约 7.1%，因此本轮主要改变基底重建方式，不以预期数值作为验收条件。

## 预先固定的方法

1. 内存 Full 基底、Diff 和本次恢复文件放在同一块 NVMe 上新建的私有 XFS（reflink=1）中。L1 原挂载为 ext4，不支持 FICLONE；用 L1 ext4 上的 40 GiB 私有 loop 镜像承载 XFS，loop 开启 direct I/O。禁止使用 tmpfs，禁止格式化现有磁盘或复用别人的文件系统。
2. 非空差分链：FICLONE 创建基底的独立 CoW 文件，使用官方 Firecracker v1.6.0 snapshot-editor 按祖先顺序覆盖差分页，最后执行一次 fsync。FICLONE 不支持即失败，不静默退回整文件复制。基底与历史差分不可原地改写。
3. 空差分链：直接使用不可变 Full 基底，不再生成最终没有使用的副本。仍终止旧 VM、恢复 dm、启动新 FC 并等待成功 RPC。
4. Firecracker、guest、dm-thin、调用逻辑和 50 ms socket 轮询保持上一轮版本；本轮不引入自定义 UFFD、不预先合并未来目标，也不把必要工作移出计时。

## 顺序与前置条件

Figure 7 `figure7-e2b-cube-20261109/finished.json` 的成功回执 → 取得共用 `figure8-refresh-20261108/host.lock` → 确认上一轮频率已恢复 → 存储/源码校验 → 稀疏差分与 CoW 正确性测试 → 单条完整 `sympy__sympy-17630` 验证 → 正式 12 条。

排队时冻结 Figure 7 回执 SHA256；不得把别的失败/未完成批次当作完成。本次检查时 Figure 7 已完成且无活动实验，因此队列可直接进入前置验证。队列锁避免重复启动，STOP 文件取消本批；失败时保留记录并停止，不自动删失败点重试。

## 不变的实验条件

- 同一 L1 QEMU/KVM、4 vCPU / 8192 MiB VM、NUMA 绑定与宿主 2.1 GHz。宿主 qcow2 在 NVMe、cache=none。此次增加 XFS/loop，作为明确的实现变更记录，不声称只有合并器不同。
- 同一 12 条输入，分组 2/2/5/3，输入哈希与已有 `inputs.json` 相同；原始 360 CK、334 RS。仅按事先固定的 DeltaBox 节点集合过滤 43 次扩展 CK，得到 317 CK / 334 RS。
- 四个固定 NUMA 队列，最多两条同时执行，沿用上一轮的空间保护；完整验证轨迹单路，不混入主均值。
- 同一冷缓存预处理与锁；fsync 保留。文件系统部分仍用原 dm-thin，不改其落盘与恢复方式。
- CK 使用原连续窗口。RS 连续计时从终止旧 VM开始，包含 CoW 克隆、官方合并、fsync、dm 恢复、新进程启动、snapshot/load、首次成功状态 RPC、同步收尾。细分只是该窗口内的归因，不用分段相加代替总值。

## 正确性门槛

- 合成测试覆盖全零脏页、重复覆盖、稀疏孔洞、不同分支顺序和尾页；与独立预期字节及旧复制合并器的完整哈希一致，基底/差分哈希不变。
- 单条完整真实轨迹：每次非空链同时生成参考复制合并结果，逐字节 SHA256 与 CoW 结果比对，并检查基底及差分未被修改；参考工作只在验证批中执行，该批全部不计入正式均值。
- 验证批与正式批均检查 restored node、原始事件顺序、317/334 的节点映射、4c8g、连续窗口、输入和源码哈希。没有新增恢复失败才进入正式批。消息差异与旧记录对照，不能把既有差异冒充全量一致性通过。

## 资源与收尾

宿主 NVMe 初始至少 60 GiB，L1 初始至少 70 GiB。40 GiB XFS 镜像只用于本批内存快照。运行中宿主/L1 至少保留 12 GiB、XFS 至少 4 GiB、L1 可用内存至少 8 GiB；触发即停止本批并保留日志。只删除本批可重建大产物；结果、输入、源码锁、逐事件与验证哈希永久保留。卸载本批 loop/XFS，恢复频率并记录核对结果。

## 交付

正式 12 条全部成功后生成四组及事件加权 CK/RS、连续阶段均值、与 7727.237 ms 的差值、源文件哈希和每条原始路径。失败则报告失败原因；不能把验证批和正式批拼接，也不按快慢挑选事件。更新唯一总账 `ae/report/README.md`，论文数据待复核后处理。

## 2026-11-09 用户授权修订：结束重复全量哈希验证，直接进入正式批

本修订覆盖上文“单条完整验证轨迹必须结束”的开跑门槛。用户明确要求优化或去掉长时间哈希，让正式 12 条尽快开始。原验证轨迹已完成 26 次恢复，其中 25 次非空链合并均与独立复制参考的完整 SHA256 一致、基底和差分未变，覆盖 1 至 13 层；另有 4 个合成分支、全零脏页、尾页检查。原协议在 13 层时每次读取约 240 GiB 逻辑字节：基底和 13 个差分各两遍，合并与参考文件各一遍。稀疏孔洞也逐字节进入哈希，造成数分钟额外等待。

保留已完成前缀及完整源码锁，显式中断剩余验证并完成原有设备清理。中断批保留失败/未完成状态，不伪造完整成功；其全部数据仍排除在论文均值之外。正式 12 条从头执行，不沿用验证事件或拼接旧性能结果。新门槛核验 26 个恢复状态、25 个完整对照哈希、源不可变性与最大链深 13，以及被验证的合并器、控制器、输入、配置与正式批的哈希一致性。小型合成检查再次运行，随后直接启动正式批。

正式测量源码和协议均不变：FC_MERGE_VALIDATE=0 原本就是正式批设置；FICLONE、官方合并器、fsync、4c8g、冷缓存、双并发和连续计时保持不变。不把原本需要的恢复工作移出窗口，不修改正式结果。

Figure 9 仅在交接期间暂停空闲队列监督；FC 重新进入正式批后恢复等待，仍须看到 12 条、317 CK/334 RS 及资源恢复的真实成功回执才能接力。
# NVMe interruption and continuation (2026-11-09)

The first formal attempt completed four traces and then hit the unchanged host
12 GiB free-space guard. Preserve those four raw results and their SHA256 values;
archive both interrupted traces and all failure receipts. Resume only the eight
unfinished inputs. The measured drivers, official merger, cold-cache procedure,
continuous timers, VM configuration, CPU placement, frequency, and fsync are
unchanged, as checked against the pre-interruption source lock.

Keep at most two concurrent traces on their original NUMA nodes. Between waves,
wait for all controllers to exit, remove only observed owned idle dm devices,
trim the private snapshot XFS and then the idle L1 root filesystem. Trimming is
outside every measured trajectory. This returns freed blocks through the loop
and qcow2 layers instead of letting allocation accumulate between trajectories.
Record every barrier. Do not combine partial failed trajectories with successes.
The scheduling barrier is a documented continuation change, not a hidden change
to the checkpoint/restore implementation. Figure 9 remains gated on all 12
successful traces, 317 aligned checkpoints and 334 restores.

