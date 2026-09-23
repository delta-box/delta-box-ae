# GitHub 私有仓库与源码来源

代码的下载和协作入口迁至 `delta-box` GitHub 组织：

- [deltabox-runtime](https://github.com/delta-box/deltabox-runtime)：Private；默认 `main` 为当前 AE 候选版，同时保留 `feat/ae-oneclick-validation`。
- [d-overlayfs](https://github.com/delta-box/d-overlayfs)：Private；提供 Linux 6.8 源码及独立的旧 FD 修复提交。

GitHub Free 组织资料页不能据此称为私密；私密范围是上述代码仓库。GitHub API 已确认 `private=true` / `visibility=private`。本次尝试禁止组织成员创建公开仓库时，GitHub 返回该组织不支持 private-only policy，因此没有声称已建立组织级强制私有策略。评审或协作者需先获得对应仓库访问权限；托管 AE 账号仍使用预配置 checkout。

## 版本与范围

运行库保留当前 AE 分支的完整可达历史，原 commit ID、源码锁和实验产物哈希不变。迁移只修改下载地址和文档，未重跑或重标历史测量。历史报告的链接使用 GitHub 上原 commit 的路径，已核对 34 个链接目标在迁移历史中存在。

其他工作分支未作 `--mirror`：`feat/langgraph-integration`、`feat/paper-reproduction`、`fix/async-dump-lifetime`、`perf/restore-wait` 和 `wip/runtime-proof-snapshot-20260921` 仍有当前 AE 分支不包含的提交。本地旧仓库和原实验数据保留，不自动发布这些分支。

内核原历史含一个约 267 MB 的压缩包，超过 GitHub 单文件限制，故以独立源码仓库发布：`ae-source-6819771` 保留原 `6819771a572094191bd3ab594d3466cad9123e6f` 的整个 `linux-6.8` Git tree，`main` 另加入已实测的 `fs/overlayfs/file.c` 10 行修复。新仓库 commit ID 与原仓库不同；发布的 `main` 为 `256fb32c4e78474ceec2ba9ac44deeeb7d97fbb5`，基线分支为 `ef0ea9ae6504f4c121e0e7394fc50bccf048ea9b`。原 Linux 子树为 `36cf0511858127bc33ffa921d4cbc64fdbf62246`，修复后为 `dba04dd930de1db48fd3289c85fc9ddf8e2891ca`；源码差异仅一个文件增加 10 行。子树来源、文件哈希和历史构建说明见内核仓库的 `source-provenance.json`。不包含原仓库的实验数据、论文文件及重复源码压缩包，也不宣称本次进行了新的内核构建。

## 维护者使用

运行库当前工作分支跟踪 `github/feat/ae-oneclick-validation`，推送目标也为 `github`。原 `origin` 保留，避免影响共用同一 Git 配置的其他 worktree；新的文档下载入口均使用 GitHub。新克隆的默认 `origin` 自然指向 GitHub。

```bash
gh auth login
gh repo clone delta-box/deltabox-runtime
gh repo clone delta-box/d-overlayfs
```

GitHub 私有仓库的网页、clone、源码和历史图片均需要已授权账号；不要把匿名下载失败误判为仓库丢失。历史来源记录中的旧远端地址仅作溯源，不是新的获取步骤。
