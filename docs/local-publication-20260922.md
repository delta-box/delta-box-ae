# 本地工作发布清单（2026-09-22）

本次盘点覆盖 deltabox-runtime 的全部工作树，以及 d-overlayfs 的本地改动。目标是让有价值的代码与证据在 GitHub 有可恢复的版本。运行中的任务保留自己的工作树；快照不改变其 HEAD、索引或文件。

镜像构建中独立可验收的自动下载、选定输入校验及配套测试已整理进 main，验证与范围见[发布审查](https://github.com/delta-box/deltabox-runtime/blob/main/code-review-image-build-publication-v1.md)。其余工作按下表保留在远程分支。

末轮按 Git blob SHA-1 和文件模式逐路径核对：runtime 工作树中 5 + 16 + 3,086 = **3,107 处**本地变更均有已发布的对应内容，未发现仅存本地的变更路径。核对时的远程提交与各工作树计数见[远程覆盖记录](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/image-build-publication-20260922/remote-coverage.json)。这说明内容已保存，不表示所有 WIP 都已通过运行验收或合入 main。

## 已保存到 GitHub 的工作

| 内容 | 远程分支 | 快照 / 提交 | 状态 |
|---|---|---|---|
| 旧 dump-lifetime 工作树中的文档、镜像构建及辅助脚本 | [wip/local-ae-images-20260922](https://github.com/delta-box/deltabox-runtime/tree/wip/local-ae-images-20260922) | `4561413` | 按当时文件字节保存；需要的镜像构建代码另行移入 main |
| AE 分析、绘图、覆盖统计和入口改动 | [wip/local-ae-plots-20260922](https://github.com/delta-box/deltabox-runtime/tree/wip/local-ae-plots-20260922) | `5f39ae0` | 未验收工作快照，保留原源码锁 |
| 托管入口、E2B/Cube fan-out 与 SDK 环境修复 | [wip/local-hosted-fanout-20260922](https://github.com/delta-box/deltabox-runtime/tree/wip/local-hosted-fanout-20260922) | `39ef83d`，源 HEAD `1456741` | 原任务仍在验证；此处保存当时已提交状态 |
| 托管 CPU 候选的后续提交 | [fix/ae-hosted-complete](https://github.com/delta-box/deltabox-runtime/tree/fix/ae-hosted-complete) | `b480aa0` | 包含合并当前主线后的候选，原任务继续完成环境与实验验证 |
| LangGraph 集成 | [feat/langgraph-integration](https://github.com/delta-box/deltabox-runtime/tree/feat/langgraph-integration) | `00abd58` | 独立功能分支，未直接合入当前 main |
| 本地 runtime guard 和四个 proof 示例 | [wip/runtime-proof-snapshot-20260921](https://github.com/delta-box/deltabox-runtime/tree/wip/runtime-proof-snapshot-20260921) | `e4e6482` | 五个本地修改文件均与该已保存提交完全相同；不是新验收版本 |
| 早期 restore-wait 优化与对照数据 | [archive/restore-wait-20260920](https://github.com/delta-box/deltabox-runtime/tree/archive/restore-wait-20260920) | `8d47a61` | 历史版本保留 |
| 早期 paper-reproduction 文档与实验记录 | [archive/paper-reproduction-20260922](https://github.com/delta-box/deltabox-runtime/tree/archive/paper-reproduction-20260922) | `52a23db` | 历史版本保留 |

WIP 快照记录的是一个时间点的内容，不是通过全量实验的发布版本。不要用旧工作树整目录覆盖 main。后续主线集成应对比具体改动、验证接口和实验来源，再更新所需源码锁。

## 为什么旧工作树仍可能显示未跟踪

每个 worktree 有自己的分支、HEAD 和索引。main 已跟踪的文件，在仍停留于旧提交的另一个工作树里可能只是后来复制进来的文件，因此显示 `??`。例如 main 已跟踪 `ae/run_all.sh` 和 `ae/scripts/run_review.py`；旧 `fix/async-dump-lifetime@ffa0aa8` 的索引尚无这两个路径。

旧 dump-lifetime 工作树的 3,072 个未跟踪文件中，有 3,064 个当时已与 main 完全相同，另外 5 个与 main 不同、3 个在 main 中不存在。本次全部保存到对应 WIP 快照，文件由该快照提交跟踪；原工作树仍保留原索引和未提交状态，方便正在进行的任务继续工作。需要运行已合并版本时使用 main checkout。

## 没有作为有效改动上传的项目

d-overlayfs 在默认 macOS 大小写不敏感文件系统中有 13 处表面修改。逐一比对确认，其内容等于 Git 中仅大小写不同的另一个路径，例如 `xt_CONNMARK.h` 被工作区映射成了 `xt_connmark.h` 的内容。这些是检出路径冲突，不是待发布的内核修复。GitHub 中的源码分别保存了正确的大小写路径，应在 Linux 或大小写敏感卷上检出。

这些分支保存源文件和必要的小型证据，没有把大磁盘镜像、下载的安装包、凭据或构建缓存加入 Git。活动任务在快照之后的新增修改需由其后续提交继续发布。
