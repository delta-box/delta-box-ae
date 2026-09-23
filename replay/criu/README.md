# CRIU exact-parent 扩展

本目录将 CRIU 固定在 `2cf8f13ca1f11a0491977e438b262e646137256c`（4.2），用一个可选补丁支持跨 fork / restore 的父镜像页复用。普通模式行为不变；本扩展不修改 CRIU 镜像格式，产物可由未打补丁的同版本 CRIU 做 eager 恢复。增量父页 lazy restore 还需要下述 reader 完成回调修复。

## 安全机制

`DELTABOX_CRIU_EXACT_PARENT=1` 启用首次页输出时的精确比较：

1. 保留原 `detect_pid_reuse` 检查及警告，不更改进程 starttime，不重写父镜像时间。
2. 禁用当前 task 的 soft-dirty parent-hole 推断。即使当前脏位被清空，也必须采集当前页。
3. 从被冻结 dump 副本的页 pipe 读取当前页，使用 CRIU 自带递归 parent reader 读取相同虚拟地址的父页，逐字节比较。
4. 只有完整读取成功且 4 KiB 字节相等才生成 `PE_PARENT`（保留当前页原有 `PE_LAZY` 资格）；否则保存当前页数据。先比较、后写入页镜像，没有先写全量镜像再去重的步骤。
5. 已指定但缺失/损坏的父 pagemap、错误页文件长度、非法 pagemap / lazy-only parent 会报错，不静默伪装成成功的增量。父链读取错误同样导致 dump 失败。
6. 没有父镜像时创建全量 seed；本模式会保存本次真实 dump uptime，使后续原 PID-reuse 检查仍然可用。不需要 `--track-mem` 或 `clear_refs`。

本实现节省持久化页数据，仍读取当前完整待保存页集并读取父链做比较。它不宣称省掉所有内存读取、后台 CPU、I/O 或尾延迟。只有与独立 dump 副本、资源封存、有界队列、持久化完成状态一起使用，才能构成完整异步 checkpoint。

仅支持 regular local dump。允许 regular dump 镜像进行 lazy restore，包括跨多层父链的缺页读取；禁止与 pre-dump、lazy dump、page server、image streaming、auto-dedup 组合。共享内存的特殊镜像仍走 stock 路径，本补丁不解决可写共享内存或共享 FD / FIFO 的外部状态冻结。

父镜像和完整物理父链必须由 runtime 固定、保持不可变，并在子 dump / restore 使用期间禁止回收。固定 namespace task ID 只用于查找同名 pagemap，不作为页等价证明。当前 runtime 使用独立 PID namespace 的 PID 1，子进程还必须在自己的 namespace 中 `setsid()`。

## 构建与识别

```sh
python3 replay/criu/build_exact_parent.py \
  --source /path/to/upstream-criu-clone \
  --out /private/new-build-dir --jobs 2

DELTABOX_CRIU_CAPABILITIES=1 /private/new-build-dir/source/criu/criu --version
```

能力探测必须得到 `DeltaBox capabilities: exact-parent-v1 exact-parent-lazy-v1`；不能只设置环境变量然后假定 stock 二进制理解它。构建脚本只操作新的输出目录，不安装、覆盖或修改系统 CRIU。`build.json` 记录完整源码 commit/tree、补丁 SHA256 和二进制 SHA256。

CRIU 上游构建将时间写入 kernel-data 缓存版本常量，调试信息也可能包含构建路径；因此默认构建不承诺跨目录的 ELF 字节完全相同。需要逐字节可复现时还要固定 `SOURCE_DATE_EPOCH`、构建路径与工具链。无论是否固定，都以实际 `build.json` / binary SHA256 为运行身份。

源码仓库若由其他用户拥有，使用相同文件所有者执行构建，或提供自己拥有的 clone；不需要修改全局 Git safe.directory。

## 验证

[真实 fork、分叉增量链和冷恢复证据](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/async-incremental-20260922/lineage/README.md)覆盖 16 MiB C 任务、相同 namespace PID 不同进程身份、清空 soft-dirty、父分支独有写入、写回原值、VMA 地址复用、三层 parent 链和损坏父镜像拒绝。它是机制诊断，不是完整论文 workload 或性能验收。

## 增量父页的 lazy restore

复用页保留 `PE_LAZY` 标记。CRIU 4.2 的父 reader 没有顶层 UFFD reader 的 `io_complete`；仅开启 lazy 会读到父页却不完成缺页，进程挂起。补丁在有顶层回调时同步读完本次局部父页范围，再调用顶层完成回调一次。页装载发生在独立 lazy-pages 服务进程中，仍按 userfaultfd 缺页/后台预取推进，不把整个父镜像预读进 restore 临界区。没有回调的 eager/batched reader 保留原行为。

`exact-parent-lazy-v1` 表示同时具备 dump 侧 lazy 父页标记与 restore 侧父读取完成回调。runtime 会验证两侧能力，并在 reader 存活期间保留镜像祖先链；不允许 stock lazy reader 静默挂起。
