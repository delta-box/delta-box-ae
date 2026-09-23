# 暖模板死亡后的真实 CRIU fallback 诊断

`run_probe.py` 复用 `run_instance.py` 的标准 guest archive、schedule、环境、私有 rootfs、VM 生命周期、结果采集和严格验证。host 只使用 CPU 0–1、NUMA 0。`guest_probe.py` 作为单独文件上传，不替换归档内的生产文件；标准 `entry.py` 的源码校验、环境清理和只读数据盘准备仍会执行。

第一次 restore 前，诊断 wrapper 从 `template_pool.templates` 读取目标模板，只通过 pidfd 发送 SIGKILL 并等待其退出，保留字典登记。注入拒绝 active、namespace init、僵尸、跨 namespace 或 starttime 变化的 PID，不使用数字 PID kill。随后直接调用原始 `restore_action`，记录真实返回路径；不强制 CRIU、不重写结果、不绕过状态验证。

通过要求：恰好注入一次、实际路径为 `criu`/`criu-lazy`、标准结果检查全部通过、恢复后的 worker 状态匹配目标 checkpoint，并完成后续 worker execution 和 checkpoint。

以下命令在 spr4numa 隔离 checkout 执行；输出目录必须不存在：

```sh
sudo -n python3 replay/diagnostics/fallback/run_probe.py \
  --instance django__django-14997 \
  --trace-dir ae/paper/figure-07/data/inputs/deltabox/django__django-14997 \
  --kernel /mnt/disk2/dyp/overlay-stale-fd-20260921/vmlinux-fixed \
  --base-xfs /mnt/disk2/dyp/d-overlayfs/base.xfs \
  --data-xfs /mnt/disk2/dyp/d-overlayfs/data-django.xfs \
  --mode fast --checkpoint-profile runtime-default --max-events 5 \
  --vcpus 2 --mem-mib 8192 --timeout 900 \
  --out ae/report/replay-fixes-20260921/fallback/run
```

标准环境显式启用 `DELTABOX_ALLOW_COLD_RESTORE_POOL_CLEAR=1` 和 `DELTABOX_REPLAY_STRICT_EPOCH=1`，并保持 `DELTABOX_FORCE_CRIU_RESTORE=0`。环境、源码和镜像哈希保存在 `run.json`；额外记录 wrapper/driver 哈希，`fallback-injection.jsonl` 记录注入和真实返回，`fallback-validation.json` 只在所有验证成功后生成。

故障注入发生在 restore 调用内部，所有耗时均为诊断数据，不能用于论文性能比较。实机证据与本次结果放在 `ae/report/replay-fixes-20260921/fallback/`，不写入 replay 源码锁。
