# 工作目录和结果组织

spr4numa 只使用 `/mnt/disk2/dyp/deltabox-runtime` 作为当前开发、调试和 AE 工作目录。

## 当前实验结果

所有新测结果放在仓库的 `ae/results/`，按源码锁中的 `source_commit` 前 12 位分组：

```text
ae/results/
  README.md
  <源码版本>/
    README.md
    cpu/                 # 同一次 CPU 运行的各实验、原始记录、分析和对比图
    checks/              # 最小或截短事件检查，不混入完整轨迹统计
    rendering/           # 使用该版本测量数据重绘，另记绘图代码来源
```

默认一键命令使用 `<源码版本>/full/`。仅减少输入数量仍保留完整轨迹，结果也放在 `full/`；`bash ae/run_test.sh` 和 `--max-events` 放在 `checks/`。已有目录不会被覆盖，同版本、配置和范围通过 `--resume` 继续。重试日志可以保留内部历史，但不会把各张图的数据拆成新的顶层编号目录。

目前主要的新测数据是 `c775a9215718`：17 个入口、23 个作业的首输入验收，以及独立的 12 输入短程检查。`bf1dcbeb19ff` 是后续绘图代码版本；它的重绘仍放在原测量版本内，保留两者的来源。当前各右图的缺项见[覆盖核查](current-result-coverage.md)。本次发布以绘图代码 `87550c0` 重新分析这批数据，输出放在 `rendering/87550c0/`；没有启动新的性能实验，也没有改变原始运行及源码身份记录。

`ae/results/hosted`、`ae/results/consolidated-20260922` 等旧路径只保留兼容链接，实际数据已移到源码版本目录中。优先从 `ae/results/README.md` 查看。

## 历史归档

不再使用的工作副本、实验中间数据、传输包和散落文件集中在 `/mnt/disk2/dyp/archive/`，入口为该目录的 `README.md` 和 `index.json`。系统盘上的旧 E2B 快照压缩后迁入 `archive/legacy-e2b/`，通过完整性与全数据比对后才移除原副本。

历史 Git worktree 和对象库引用已随迁移修复；原提交与未提交状态保留。部分历史数据副本共享硬链接，应保持只读，需要修改时先复制为独立文件。

2026-09-23 整理完成：75 项历史目录及传输文件、2 个 home 顶层散落文件移入归档；26 个 Git 工作区校验通过。系统盘的三批旧 E2B 数据共 97,115,844,608 字节，压缩归档为 23,435,802,321 字节；系统盘占用由 95% 降到 73%。已有结果归为 8 个测量版本，326 份运行和分析控制文件的哈希保持不变。

当前镜像、内核、工作负载仓库、编译缓存和在线服务仍在使用的路径继续保留。新临时文件和维护记录放在仓库的 `ae/work/`，不再在 `/mnt/disk2/dyp/` 顶层新建平行实验仓库。

## Git 中的可复查结果

[结果总索引](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/README.md)及当前测量版本的图片、分析、绘图映射与对比清单随 Git 发布。`cpu/evidence.tar.gz` 保存分析用的原始测量、run/suite、配置、计划、运行日志、测量环境及原分析记录；`checks/deltabox-inputs/evidence.tar.gz` 单独保存 12 输入短程检查。两份 `evidence-manifest.json` 记录归档及每个文件的 SHA-256，解包到新的目录即可重新分析。

服务器上原有完整目录继续保留，包括临时 payload 和工作负载副本；这些可重建的大文件不加入 Git。历史版本索引注明服务器路径和已有 Git 报告，不把未发布的本地目录伪装成可下载结果。主页只链接当前已发布的文件。
