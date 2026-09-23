# 跨 fork 父页复用诊断

本探针使用独立 C 进程、独立 PID namespace 和私有输出目录。它不改系统 CRIU、sysctl、共享镜像或 CPU 频率，诊断日志和校验不属于性能测试。

```sh
gcc -O2 -Wall -Werror replay/diagnostics/lineage/worker.c -o /private/worker
sudo numactl --membind=2 --physcpubind=52-55 \
  python3 replay/diagnostics/lineage/probe.py \
  --out /private/new-lineage-run \
  --criu /private/exact-parent-build/source/criu/criu \
  --stock-criu /path/to/unmodified-criu-4.2 \
  --worker /private/worker
```

`worker.c` 的 active 创建新的 PID namespace dump 副本，host PID 每次改变、内 PID 恒为 1。副本写就绪记录后等待，由宿主明确停止；CRIU dump 时使用 `--leave-stopped`。探针杀掉原副本后，仅用未修改的 stock CRIU 冷恢复，继续执行应用命令。

A 为 seed；B 是原分支稀疏修改后的新副本，分别做 stock 和 exact dump；恢复 A 后创建与 B 分叉的 C；冷恢复 C 后再创建 D，形成 D→C→B→A 的物理页链。每次恢复独立读取 `/proc/PID/mem` 的全部 16 MiB 并比较 SHA256，同时检查应用计算的摘要与已知页字节。C 覆盖父分支独有修改、写回原值和 munmap/mmap 地址复用。B dump 前故意清空自身 soft-dirty，验证模式不依赖它。三种损坏父镜像都必须拒绝。

`image_summary.py` 独立解码真实 pagemap 的 parent/present 页数，避免只根据选项或日志推断增量生效。`result.json` 保存命令、退出码、内外 PID/starttime、数据摘要和页数；各目录保留原始 CRIU 日志与镜像。

`vm_compat.py` 只用于在临时 rootfs 的 VM 内检查二进制 ABI / 动态库 / 能力标记；需要在独立 mount+network namespace 中调用。它不替代完整 VM checkpoint / restore 验证。

新增 `--lazy --restore-criu /path/to/patched-criu` 用同一套分叉、损坏父链及 16 MiB SHA256 校验验证 userfaultfd 恢复。此模式的 restore binary 必须含 lazy 父 reader 修复；原 `--stock-criu` 参数仍是别名。输出单列 `parent_lazy_pages`，必须大于零，避免把同步装入的父页误当 lazy 成功。
