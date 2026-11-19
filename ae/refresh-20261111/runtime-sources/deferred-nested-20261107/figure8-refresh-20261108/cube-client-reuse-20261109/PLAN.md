# Cube Figure 8：客户端CA上下文复用对照与三点重跑

用户授权：重做Cube实验。N64保持暂停；先N1/N16小对照，正确性通过后N1/4/16各四轮，论文不改。

## 唯一被测变化

- 不修改任何SDK文件、服务、VMM、模板、页大小、prefault、存储/刷盘、CPU预算或验证器。
- 在测试驱动进程内替换SDK引用的build_client函数：复用同一CA证书SSLContext，所有原始HTTP client/transport对象仍按原位置新建，原TLS验证保持CERT_REQUIRED/check_hostname=True。
- SSLContext第一次在源沙箱原有准备操作中惰性创建，后续子沙箱复用。不是在计时前创建子沙箱的HTTP客户端，也不提前访问子VM或验证内存。
- 原/复用两种模式均记录客户端构造跨度及commands.run跨度，数据仅追加内存、在每次试验结束后落盘；不将这些跨度从连续挂钟中扣除。原始bench_fanout.py和验证器二进制不改。

## 对照和正式点

- control排除的warmup：[original,N1]、[reuse,N1]。
- control正式顺序：original1、reuse1、reuse16、original16、reuse1、original1、original16、reuse16；每条件每N两次，全部保留。
- control共8点68子VM，含warmup10点70子VM。不混入正式均值。正确性/环境/模式校验通过后自动接正式批次，不以“足够快”作为筛点条件。
- 正式reuse四轮：[1,4,16]、[4,16,1]、[16,1,4]、[1,16,4]。每轮额外N1warmup。12正式点84子VM，含warmup16点88子VM。
- 每点45秒静置，所有成功/失败保留；任何失败停止并恢复，不用成功重跑替换失败。不执行N64。

## 固定条件及计时

同一L1/QEMU/KVM、4vCPU/8GiB模板cube-agent-full-4c8g-l1e、NVMe、VM池0-3,12-15,24-27,36-39、控制面32-35,44-47、驱动4-7；相关宿主CPU锁2.1GHz后逐项恢复。原VMM及服务哈希复核。

完整official clone（含快照、并发创建N个子VM、正常临时快照删除）到全部验证返回；源准备和最终销毁在窗口外。验证器不改：64MiB全部16384页marker、token/PID/requests/checksum；每VM4读线程、最多4VM同时验证，所有子VM保留到验证结束。guest缺页/等待/命令调用/剩余客户端构造均计时。

N64之前故障不参与本任务；孤立CubeMaster记录的既有修复只读校验，不再修改数据库。只调整既有Cube实验CPU放置并恢复；不触碰E2B。

## 资源和恢复

宿主NVMe当前约39GiB空闲，旧Cube完整N1/4/16/64批次采样空间波动约0.295GiB。本轮最大N16，启动门槛30GiB（旧N64脚本为40GiB）、运行下限15GiB，L1空闲内存/磁盘各8GiB下限；不删除任何现有产物。guard在运行时持续检查。

环境恢复校验将Docker挂载按Source/Destination排序，避免上轮仅顺序不同造成误报。所有服务二进制/SDK文件/依赖身份、CPU/cgroup/docker放置、频率、原始日志、逐点结果与失败保留。结果放本目录，持续结论只更新ae/report/README.md。
