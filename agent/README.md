# 真实 LLM agent

本目录保存真实接入所需的入口和实现；本轮仅整理代码并验证不依赖 AK 的契约，**没有运行真实 LLM 任务**。
论文复测使用旁边的 [`replay/`](../replay/README.md)，不使用本目录产生的推理结果。

```text
run.py                 参数、AK 环境变量读取、工作目录检查、统一 checkpoint profile
host/decoupled_mcts.py  host 侧 UCT 搜索、分支 checkpoint/restore、验证与补丁导出
host/worker_client.py   host ↔ worker 的 FIFO 控制通道
worker/agent_worker.py 单线程 ReAct worker；内存中的上下文随 C/R 回滚
worker/worker_actions.py 查看、搜索、编辑、测试等动作
npd/npd.py             进程外 HTTP 代理；持有网络连接，不进入 worker 快照
npd/npd_client.py      兼容调用客户端
sandbox_driver.py     共享 runtime core 的 live-agent 适配器
```

支持 **OpenAI-compatible `/chat/completions`**；不声明原生 Anthropic Messages 协议兼容。
认证等非暂时性 HTTP 错误立即失败；暂时错误默认最多 3 次，每次 HTTP timeout 60 秒。
可通过 `NPD_MAX_ATTEMPTS`（1–10）和 `NPD_HTTP_TIMEOUT_S`（0–180 秒）配置。错误日志不写 AK 或响应正文。

## 使用

需要 Linux、打补丁的 DeltaBox OverlayFS 内核、CRIU、Python 3.10+、protobuf、git、sudo。
每个独立 VM/环境同一时间只运行一个 agent：FIFO 和 NPD 通道仍使用固定 `/tmp` 路径。
默认仅回滚任务仓库 overlay，不应将仓库外 `apt`/系统修改视为同一回滚范围。

```bash
# 由你在运行环境设置 LLM_AK；不要把密钥放入命令、README 或 git。
sudo --preserve-env=LLM_AK python3 agent/run.py \
  --api-key-env LLM_AK --api-base https://YOUR-ENDPOINT/v1 --model YOUR-MODEL \
  --task '修复目标问题并运行测试' \
  --base-lower /path/to/task-checkout --workdir /tmp/deltabox-new-run \
  --max-iter 8 --max-expansions 2 \
  --verify-cmd 'python3 -m pytest -q' --out /tmp/deltabox-fix.patch
```

工作目录必须与任务源码、runtime 仓库和 home 目录分离，默认拒绝清空已有数据。
`--out` 是补丁文件；标准输出为 JSON 摘要。`ok` 表示执行无 step/dump 错误，
`solved` 只有 finish 节点经过 `--verify-cmd` 返回 0 才为 true，不能把“循环结束”当作已解决任务。
`warm_forks` 按实际 restore 路径统计。worker 超时或返回失败会终止本次运行，
不会继续发送下一步并接收迟到动作；退出前等待 dump 完成，再关闭本次 namespace 中全部进程。

切换分支后，host 重开 FIFO，非阻塞清空已有回复，然后用带唯一 `ctrl_id` 的
`state` 请求等待恢复后的 worker 就绪；不再固定等待 200ms 和 50ms 的空闲窗口。
连接、请求发送和就绪回复共享默认 10 秒的超时预算，失败会关闭本次连接。
无 `ctrl_id` 或 ID 不匹配的回复会被丢弃。

live MCTS 摘要中的 `restore_ms_p50` 继续表示 runtime restore API 耗时。
`worker_reopen_ms_p50` 表示重连及就绪握手耗时，`branch_resume_ms_p50` 表示从
调用 restore 到通道就绪的总耗时。后两项只统计恢复后继续执行 worker 的分支切换，
不包含最终仅为导出补丁而进行的 restore。论文 replay 使用独立控制通道，未采用这些计时字段。

默认 profile 是 `runtime-default`；入口把共享协议参数显式写入环境，避免继承历史 async-full 配置。
`--no-warm-template`、`--mode moatless`、whole-root 等兼容路径保留，但不属于这次统一 replay 的验收范围。
