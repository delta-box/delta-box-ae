# 镜像范围复核（2026-09-20）

结论：保存配方有用，但上一版**不是可直接发布的正确镜像集合**。它包含实验依赖、中间产物、可选部署环境和未经全量验证的重建候选。此前仅提交了约 330 KB 配方/配置，没有实际打包或 push 59 GiB 镜像。

## 已确认的错误与修正

1. **Cube 与 Figure 7 的映射错误。** 投稿 PDF §6.2.1 和 Figure 7 只有 DeltaBox / E2B。已从 README/catalog 中去掉 Cube → Figure 7。
2. **Table 2 与 Figure 8 的 Cube 模板被混为一份。** Table 2 使用 `cube-finalbench-python-4vcpu-4096m-20260604-cow`；Figure 8 原记录使用 `cube-official-fork-bench-20260605_210553`。名称差异本身不证明底层 OCI 一定不同，但没有构建请求、digest、资源配置的同一性证据，就不能把前者的配方算作后者已恢复。Figure 8 模板配方现标为缺失。
3. **E2B local build 不能直接等同于 Figure 8 的 API 模板。** Figure 8 用官方 SDK 访问本地服务，指定 `base` 模板；`build_e2b.sh` 调 local `create-build`，不完成服务部署和模板注册。两条入口现分开标注。
4. **FC+dm 不按 repo 分配五组 XFS 数据盘。** 原 `fc_dm_pilot.py` 固定使用 `data-django.xfs` 满足 `/dev/vdb` / systemd 的依赖，replay repo、index、venv、trace 已注入 rootfs。上一版“所需数据盘”的描述容易误导，已改明确。
5. **默认分发范围偏大。** 已绑定的 DeltaBox Table 2/3、Figure 6/7、Figure 8 fork primitive 等输入使用 Django/SymPy/Sci/Tools。Sphinx 数据盘不因此成为默认必需；其它后端的输入里出现 Sphinx 也不能推出 DeltaBox 需要该盘。默认构建缩至四组；五组原始配方仍保留。
6. **Figure 9 的容量默认值缺乏来源。** 上一版生成器默认 2 GiB，找到的 SWE-Search replay 脚本为 4096 MiB；已改默认值。512 MiB smoke 只能证明基本文件系统功能，不能验证 WAR 的具体数值、metadata/journal 几何或 benchmark 的物理 I/O 统计。

## 建议分发与省略

| 对象 | 处理 | 原因 |
|---|---|---|
| 与选定 run 对应的 DeltaBox `base.xfs`、`vmlinux` | 核心候选，先冻结整盘/整文件 hash，再做启动验收 | 名称、当前源码 HEAD、前 4 MiB hash 均不足以证明是投稿批次 |
| Django/SymPy/Sci/Tools data XFS | 每组只保存一份，在多个图表之间共享 | 四组加 base 逻辑容量 49 GiB；压缩后大小尚未测定 |
| Sphinx data XFS | 可选，不默认分发 | 需要明确 DeltaBox workload 与它绑定后才加入；额外 10 GiB |
| `ubuntu-24.04.xfs` 母盘和 master OCI | 构建来源/维护者备份；不与全部拆分盘同时列作默认下载 | 含重复 OS、conda、testbed 内容；母盘可以单独归档用于重建 |
| 80 组 env specs | 配方保留，运行按所选 cohort 取需要的 repo/version | 80 是历史环境目录范围，不是论文 AE 的必测数量；最小 repo/version 集尚未完全绑定 |
| FC 每实例 rootfs、run-rootfs、CRIU dump、运行期 snapshot | 由 runner 生成 | 不应按实例/图表预打包多份基础系统。后端初始化模板所需快照另论 |
| Cube Table 2 模板 | 单独基线包，复现该结果时必需 | 不能替换为 DeltaBox rootfs；Figure 8 的实际模板另找配方 |
| E2B local build | Table 2/7 对应基线的候选构建入口 | 缺完整部署与历史 digest；不是即用的 Figure 8 `base` API 模板 |
| E2B L1 qcow2 + seed | 特定历史 nested 配置的部署辅助项 | 复现选定 nested run 时要保留该层级；不应让所有 E2B 实验都额外增加 L1，改为非嵌套也不能宣称原配置 |
| Figure 9 三张空盘 | 只保留生成脚本，现场创建 | 空盘分发没有必要；原 runner 自带格式化/挂载逻辑，独立生成器主要用于 smoke |
| Figure 9 的 136 个 OCI 名称 | 清单/按需获取；不把全套 container layers 作为默认数据包 | 实验使用 `/testbed`；可以以后验证导出必要 source trees，但不能未验证就替换原输入 |
| 新补的 GPU Dockerfile | 可选重建草稿，不列入已恢复的历史镜像 | 没找到历史 CUDA/PyTorch/vLLM/transformers/peft 完整锁；Qwen3 inference endpoint 不必随离线 replay 默认分发 |
| `bzImage` | 其它启动方式的辅助产物 | 当前 Firecracker runner 使用 `vmlinux`，不必两者都作为默认下载 |

## 仍未证明正确的部分

`Dockerfile.master` 新选择 Ubuntu/conda channel、共享 Python 3.11 shell；包解算和用户态依赖可能与旧盘不同。新 base 配置还剔除历史 `/root`、`/home`、重置 SSH。它们有明确用途，但**不能凭语法检查或 XFS smoke 宣称可等价替代实验盘**。同理，保存当前 Linux config 不等于找回论文的 module build；采集值是 `CONFIG_OVERLAY_FS=y`。

本次只读检查证实，disk2 的五个核心 `.xfs` 路径都解析到 disk1 的对应文件，是同一份盘；不要对两个目录各打一次包。run config 中的旧 `/dev/shm/.../shared` 副本现在不存在。这里只记录路径/文件身份与前缀 hash，尚未以完整 hash 将当前镜像绑定到投稿 run。

实际推进应先选择图表对应的 run，确定所用 kernel/base/data/template 版本，再冻结当前可运行产物并做小规模启动/replay 验收。配方与二进制的同一性另行验证。不要为了让目录完整，先把所有重建候选都包装成“论文原始镜像”。

## 证据

- 投稿 PDF：§6.1、§6.2.1、Figure 7、Figure 8。
- `paper/table-02/cohort-deltabox.csv`、`paper/figure-06/cohort-*.csv`、`paper/figure-08/cohort-fork-primitive.csv`：已绑定的 workload 分组。
- 原 `finalbench/cube_cow_peagle_mcts30_2x_numa12_realrtt/REPORT.md`：Table 2 Cube 模板。
- `paper/figure-08` 保存的 `cube_official_fork_20260605_210626.json`、`e2b_official_fork_20260605_211829.json` 及 `e2b_official_fork16_c16_x4_20260605_215731.json`：实际模板 ID。论文 plotting source `plot_fig_rl_combo.py:load_official_fork` 读取这些记录。
- 原 `finalbench/fc_diff_dm/fc_dm_pilot.py:FirecrackerVM`：固定的 `/dev/vdb` 来源。
- 从 79 保存的 `benchmarks/replay/swesearch_replay.sh`：`LOOP_MB=4096`，先创建 loopback FS，再提取/获取 source。
- [review-bindings.json](review-bindings.json)：本次复核的模板名、源文件 hash 和路径身份记录。
