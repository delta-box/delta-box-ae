# 论文 trace replay 入口

本目录是精简的论文测量入口：输入仅为已记录的 trace/schedule；不接收 AK、不进行真实 LLM 推理。
NPD 使用 `guest/mock_npd.py`，guest 包只包含本目录 helper、共享 runtime core 和 pycriu，
不包含 `agent/` 的真实网络代理或搜索策略。DeltaBox worker 保留 trace 中的真实源码索引、文件动作、测试命令和状态校验，
这些是论文 workload 的组成部分，不能为缩短时延而删除。

冻结候选版本复测优先使用 `python3 replay/run_release.py --help`；详见 [源码锁及状态](../release/README.md)。
旧 `ae/runners/deltabox` 是本目录的兼容链接。以下独立 runner 用于开发诊断，
正式 release run 经 `run_release.py` 传递源码锁并统一绑定 NUMA/频率。

This runner executes recorded MCTS actions in a real checkpointed worker and runs
`SandboxController.checkpoint_action` / `restore_action` from the **current
checkout**. Each guest archive is assembled from `backends/deltabox/gsd/*.py`,
`pycriu/**/*.py`, and the replay helpers in `guest/`. No controller, template-fork,
namespace launcher or other runtime fork is stored in `guest/`.

The helper provenance and original file hashes are in [ORIGIN.json](ORIGIN.json).
They were ported from the captured `benchmarks/table4` harness, whose historical
Table 4 naming corresponds to this artifact's paper Table 2 inputs. The real
worker retains a Python source index and executes recorded filesystem/search/test
actions; this is trace replay with recorded LLM waits, not fresh model inference.

## 本地 trace 与依赖资源

trace、录制响应和 RTT 均为本地文件。Table 2 的 Replay/CRIU/FC-Diff baseline 还加载
Moatless、LlamaIndex、NLTK 和 LiteLLM；这些库默认可能联网取数据或模型元信息。
`ae/runners/baseline.py` 现从配置 `nltk_data` 复制真实的 `punkt`、`punkt_tab`、`stopwords`
到每次运行的私有目录，逐文件记录 hash，并启用 LiteLLM 随包提供的本地模型表。
缺文件会在测量前失败，不以空目录伪装资源。FC-Diff 将同一份资源复制入私有 guest。
本地 mock HTTP 请求仍正常保留，它服务录制响应，不需要 AK。

`ae/configs/spr4numa-replay-fixes.json` 显式选择修复后的 guest kernel、离线资源和私有
host CRIU；详见[修复验收报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/replay-fixes-20260921/README.md)。
控制通道使用 epoch 和请求 ID 拒绝 restore 前的旧消息；cold restore 的 ready 确认计入
完整 API 时间，warm 路径保留已有 fork 握手。失败时只清理本次拥有的进程与 namespace。replay 显式允许 cold restore 清空本次 VM 的
暖模板池以释放固定 PID；这不会修改其他运行的资源，随后由 durable 镜像恢复。
已知 cold restore 在切换文件系统或杀 active 前检查 dump 完成状态和清池权限。
这些修复没有将默认 checkpoint 改为真正异步增量；跨 restore 的父页复用仍需单独解决。

## baseline 消息差异与日志

Moatless baseline 默认采用 `replay_message_policy: "audit"`，请求消息不同不会终止本地响应 replay；`"strict"` 用于一致性调试。结果明确记录差异，不能据此宣称真实测试已执行或 prompt 完全一致。已知 c14 缺失两条测试历史不会通过填充空 runtime 或复制期望消息来隐藏，见[根因说明](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/replay-audit-20260921/runtime-contract.md)。这项策略不改变 DeltaBox worker 的状态校验和真实动作执行。

mock 请求路径以有界内存保存差异，无诊断 print、hash 或文件写入；历史 span 顺序适配器只更新计数。CRIU/FC/E2B 在基准操作计时结束后导出，Replay 在父进程结束整个 replay 子进程计时后导出，再停止 mock。导出失败会有明确错误，正常完成输出 `*mock_audit.json` 并在 `run.json` 汇总。外层记录的整轮运行耗时仍包含收尾；必要的协议 JSON 解析、序列化和消息比较仍计入相应时间。

Figure 2 的独立入口通过 `--defer-audit` 将导出交给父 profiler，待 RSS 采样停止后再写文件。当前功能验收、17 次历史差异分类及图见[消息审计报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/replay-audit-20260921/README.md)。

## Run one instance

From the repository root, with images available on a Linux KVM host:

```sh
sudo python3 replay/run_instance.py \
  --instance psf__requests-863 \
  --trace-dir ae/paper/table-02/data/inputs/deltabox/psf__requests-863 \
  --kernel /path/to/vmlinux \
  --base-xfs /path/to/base.xfs \
  --data-xfs /path/to/data-tools.xfs \
  --mode fast \
  --out /path/to/new-results \
  --image-hash-cache /path/to/image-hashes.json
```

`--mode slow` forces CRIU restore (lazy restore for the older profiles; eager
restore for `async-incremental`). `--testbed NAME` selects a directory under
`/testbeds` in the data disk; otherwise the runner selects a clone containing the
recorded repository commit and checks out that commit. Images must provide the
patched overlayfs kernel, CRIU, Python 3.9+, protobuf for the bundled pycriu,
OpenSSH, and the benchmark repository/test environments. Host tools are
Firecracker, curl, OpenSSH/scp, iproute2, mount/umount and unshare.

Use `--schedule FILE` instead of `--trace-dir` to replay a prepared schedule with
a sibling `FILE.with_suffix('.meta.json')`. Metadata must contain `instance` and
a full 40-digit `repository_commit`. The default conversion is the original
all-standard policy; recorded RTTs, event order, IDs and worker actions are
preserved. For the 12 supplied Table 2 traces this produces 317 checkpoints and
334 restores per mode, matching the supplied original schedules.

`--max-events N` selects a prefix for quick check and marks `run_purpose=quick-check`.
It retains the original RTTs and does not produce a full-cohort measurement.
`--dry-run` prepares schedules, hashes, archives and manifests without requiring
root or starting a VM. Real image files are required and fully hashed even for a
dry run. Existing output directories are refused.

## Batch interface

```sh
sudo python3 replay/run_batch.py \
  --all --inputs-root ae/paper/table-02/data/inputs/deltabox \
  --kernel /path/to/vmlinux --base-xfs /path/to/base.xfs \
  --images-dir /path/to/images --mode both --out /path/to/new-batch
```

`--instance ID` selects one instance. With a single instance, `--data-xfs` can
override group selection. Group images are `data-django.xfs`, `data-sympy.xfs`,
`data-sci.xfs` (Astropy/Matplotlib), and `data-tools.xfs` (Pylint/Requests). Batches
run sequentially and fail on the first unsuccessful run. Host CPU/NUMA binding
can be applied to the parent command; the runner records inherited affinity and
Firecracker NUMA maps. It never changes host CPU frequency.

## Checkpoint policy and measurement meaning

`--checkpoint-profile runtime-default` is the default: fixed namespace PID 100,
async-full disabled, incremental dumping enabled. Live agent and replay obtain
these protocol settings from the same `common/runtime_profile.py`.
`--checkpoint-profile historical-async-full` explicitly selects the historical
full-dump comparison arm. Its successful runs do not validate incremental chains.
Both profiles inject the same runtime sources. Profile, effective flags, source
hashes, image hashes, and full/prefix scope are recorded; never pool the profiles.
Prewarm defaults to off; optional read-only prefetch is a separate configuration.

`--adaptive` retains the converter's action-derived standard/lightweight strategy
instead of forcing all-standard, and enables controller adaptive support. The
strategy tags are explicit replay decisions, not fresh LLM decisions. A supplied
schedule may also use `predump` with this flag. `--guest-env-json FILE` supplies
string-valued policy environment overrides, for example:

```json
{"DELTABOX_MEMCURVE":"1","DELTABOX_MEMCURVE_SKIP":"1","DELTABOX_MEMCURVE_GC":"1"}
```

The exact JSON input and resulting environment are recorded. Required worker,
source-selection, mode and checkpoint-profile settings cannot be overridden.
Guest memory/GC policies operate inside the disposable VM. Every checkpoint and
restore retains the helper's worker-index and filesystem/memory footprint data;
`DELTABOX_MEMCURVE=1` adds per-event PSS, template pool and tmpfs measurements.

`checkpoint_api_wall_ms` and `restore_api_wall_ms` measure the complete runtime
method call with `perf_counter_ns`, outside runtime code. They exclude replay
validation and include whatever the actual API call waits for. Runtime-reported
critical-path timings remain separate. Historical `ckpt_wall_ms` also includes
checkpoint-side bookkeeping, while `restore_wall_ms` adds lightweight replay to
the observed API latency. Summaries are event-weighted means by mode and metric.

## 可选异步增量 profile：使用与边界

`--checkpoint-profile async-incremental` 显式启用独立 dump 副本和逐页父内容比较，
不改变默认 profile。需要指定支持 `exact-parent-v1` 的 guest 兼容 CRIU，普通 stock
二进制不会因设置环境变量就获得此能力。新 profile 的 dump/restore 使用同一固定 ELF，避免不同构建的 KDAT magic 反复失效内核能力缓存；精确父页开关只传给 dump。stock 4.2 restore 兼容性另行验证。构建方法见 [CRIU 扩展说明](criu/README.md)。

```sh
sudo numactl --membind=2 --physcpubind=52-55 \
  python3 replay/run_instance.py \
  --instance psf__requests-863 \
  --trace-dir ae/paper/table-02/data/inputs/deltabox/psf__requests-863 \
  --kernel /path/to/vmlinux --base-xfs /path/to/base.xfs \
  --data-xfs /path/to/data-tools.xfs \
  --checkpoint-profile async-incremental \
  --criu-dump-binary /private/exact-parent-build/source/criu/criu \
  --mode fast --prewarm-policy off \
  --out /path/to/new-async-results
```

上例的 CPU 编号适用于本实验服务器；其他机器须选择 NUMA 2 内实际存在的 CPU。
该命令不设置 CPU 频率，性能复测仍需完成 release 的频率前置检查。
首次调试可加 `--max-events N`，但结果会标为 快速检查；只有完整事件与状态校验成功的
运行才能参与完整 trace 分析。当前只支持 standard checkpoint，不允许同时使用
`--adaptive` 或 `--memory-policy`。`--mode slow` 强制 cold restore，当前此 profile
关闭 lazy restore；不能把只通过 fast 的结果当作 cold 路径验证。

checkpoint 返回表示 **WARM_READY**：warm template 和文件系统代际已经存在，后台镜像
可能仍在写入。warm restore 使用存活模板，不等待 CRIU；后台成功发布完整镜像并确认
dump 副本退出后，才有可供 cold restore 消费的 **DURABLE_READY**。后台父失败会让依赖的
子镜像失败，不会改成成功的全量结果。cold restore 必须等目标镜像完成，并在销毁旧
namespace 前等待其下仍有进程的 dump writer 完成清理；这些等待计入实际 API 调用耗时。

pending writer 有界，默认最多 4 个。配额耗尽时 checkpoint 等待，超时明确失败；
不能从计时中扣掉这一等待。可通过 `--guest-env-json` 设置并记录
`DELTABOX_ASYNC_MAX_PENDING`（1–64）与 `DELTABOX_ASYNC_DUMP_TIMEOUT`（正秒数，默认 300）。
逐页比较仍读取当前候选页与父镜像，减少的是重复页写入；未测量前不能据此推断吞吐
或尾延迟改善。

此实现是受限 replay 资源合同：单线程 agent、action 子进程已经结束、无持久应用
文件/socket/匿名 pipe FD、无可写共享映射。cwd 与文件映射不得引用可变的任务
OverlayFS；只允许不可变 guest image 的库映射，包括只读 `r--s` 库 cache。
Conda 的 `/mnt/data/opt` 来源是只读 data drive。系统库不变是使用前提，不能用这些
检查宣称任意修改系统根目录的 agent 都受支持；多 root overlay 也明确拒绝。
详细依据见[资源与生命周期审查](diagnostics/lineage/architecture.md)。

协议 FIFO 与 append-only 诊断 stdout/stderr 是外部通道，不回滚历史字节或日志 offset。
新 profile 将 agent 输出写入 `/tmp/replay-agent.log`；cold restore 继承当前通道，
并继续使用 epoch/请求 ID 拒绝过期消息。`diagnostics.tar.gz` 收集该日志、
`template_fork.log`、`dump_lifecycle.jsonl`、`async-checkpoints.json` 与可用的 CRIU
诊断；失败运行同样保留部分结果，不能并入成功延迟统计。

runtime 与 CRIU 独立锁定：`run.json` 保存 runtime 源码与 guest archive hash、输入与
镜像 hash、profile/环境及指定 CRIU 文件 hash；上传前后检查二进制内容，controller
启动时再次检查能力并记录实际 binary 身份。CRIU 的 `build.json` 另存固定上游
commit/tree、补丁和 ELF hash。源码相同不保证不同构建目录生成相同 ELF，因此报告
必须使用实际运行的 binary hash。

当前机制证据与验收边界见[异步增量报告](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/async-incremental-20260922/README.md)。
独立页链、FIFO 与单元测试通过，不等于完整 VM workload 或论文性能已经验收。

## Prewarm ablation and API profiling

New runs default to `--prewarm-policy off`, matching the paper's default
retention policy. `--prewarm-policy read` enables safe read-only page prefetch;
it does **not** pre-pay CoW write faults. The old read-then-write-back operation
can overwrite concurrent agent mutations and is rejected. The legacy
`historical` flag selector only accepts an explicit safe read-mode override;
it does not make that run equivalent to historical CoW warming.

`run.json` records both `prewarm_requested` and the effective `prewarm_mode`;
fresh analysis separates off/read/historical write populations. A conflicting
`DELTABOX_DISABLE_PREWARM=1` override is rejected. The suite entry accepts
`prewarm_policy` in its JSON configuration and also defaults to off.
Figure 6's CoW `warm` arm is currently **unavailable**: substituting read-only
prefetch would change the mechanism and cannot validate that paper result.
The other retention policies remain available. See the
[expanded audit](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/restore-cohort-20260921/README.md).

`--guest-env-json ae/configs/diagnostics/api-profile.json` enables a diagnostic
cProfile around checkpoint/restore calls. The profile is saved inside
`diagnostics.tar.gz` as `api_profile.jsonl`; export occurs after timing.
Instrumentation still perturbs the call itself, so fresh performance analysis
rejects these diagnostic runs. Run again without profiling for latency claims.

## Evidence and isolation

Each `<out>/<mode>/<instance>/` contains `run.json`, `guest.tar`,
`<instance>.results.jsonl`, and VM/host/guest logs. The manifest records current
Git commit, source-file SHA256 values, upload-archive SHA256, input and schedule
hashes, full image hashes, flags, resource settings and status. The guest verifies
all injected file hashes before execution. Every scheduled event must have the
correct index/order/ID, finite timings, real-worker evidence and a final success
summary. Async dump futures are joined and checked even when no later restore
would observe their failure. Partial output remains available after failure.

A hash cache is optional and only reuses a full-file digest when absolute path,
device, inode, size, mtime and ctime all match. The cache uses a lock and atomic
replacement. It is a local performance hint, not independent authenticity proof;
delete it to force rereading all image bytes. Files changing during hashing or
between preparation and launch are rejected.

Every run uses its own mount/network namespace, random temporary rootfs/socket
paths and tap name. `--work-dir` selects the parent of temporary runtime copies.
The base image and data image are kept read-only from the runner's perspective;
SSH keys are injected only into the disposable rootfs. Cleanup targets only the
owned process group, tap, socket and temporary directory. No global process kill,
network cleanup, NAT change or host frequency change is performed.

Local regression checks:

```sh
python3 -m unittest discover -s tests/paper -p 'test_deltabox*.py' -v
```

## 录制轨迹中的预期动作失败

轨迹可能包含 LLM 自己发出的无效动作，例如 Django-14672 原始 observation 已明确记录
`Files not found: django/tests`。转换器保留这类 `no_test_files` 观测作为 `expected_outcome`，
worker 仍真实执行同一个 pytest 命令；仅当 rc=4、目标确实不存在、stderr 精确对应录制的缺失文件，
才记 `expected_failure_matched=true` 并继续。`test_passed` 仍是 false。
没有该录制证据、不同缺失路径、其他 stderr 错误或 command override 都仍然失败。
这不是接受所有 rc=4，也没有删除动作或缩短计时。manifest 记录 `recorded_expected_failures`。
