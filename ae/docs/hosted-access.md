# 托管账号权限

当前 `atc-ae` 是作者授权的可写开发账号。它可以编辑 `/home/atc-ae/delta-box-ae`，并通过 `bash ae/run_all.sh` 的固定入口以 root 执行仓库代码。这项授权包含运行其自行修改代码的权限，只适合受信任的开发者。

权限由 root 管理的 `/etc/deltabox-ae/launcher.json` 显式指定：

```json
{
  "allowed_user": "atc-ae",
  "trusted_maintainer": "dyp",
  "trusted_developer": "atc-ae"
}
```

`trusted_developer` 必须与 `allowed_user` 对应同一账号；调用方不能通过环境变量开启此权限。没有该字段时，入口继续按只读评审账号检查，拒绝评审账号可修改的代码。

root 策略、私有环境、启动程序和运行控制文件继续由 root 管理。工作目录和结果目录允许可信开发者的访问 ACL。新运行记录实际源码提交和内容哈希，不要求先更新源码锁。显式续跑仍校验来源一致性，避免混合不同源码的结果。

仅检查入口权限和可选实验名称，不执行实验：

```bash
cd ~/delta-box-ae
bash ae/run_all.sh --list
```


## 快速检查与完整实验入口

评审账号的 `~/delta-box-ae` 应指向策略中 `runtime_root` 配置的发布仓库。发布时一并更新 `ae/run_test.sh` 和 `ae/run_all.sh`。

作者更新部署时，同步安装仓库中的 `ae/scripts/hosted_launcher.py`，使托管入口接受 `--group gpu`、`--group cpu` 和 `--group figure-08-cpu`。默认完整运行直接输出到 `ae/results/`；`bash ae/run_test.sh` 仍为最小 CPU 检查。

在 root 管理的 `/etc/deltabox-ae/review.json` 中设置 `gpu_remote_config`，指向作者维护的远端配置（默认 `ae/configs/figure08-remote.json`），删除旧的 `gpu.config` / `gpu.enabled`。本机无需 GPU，入口通过原调用用户的非交互 SSH 身份访问 allinai2plus；管理员应为该用户配置可用的主机别名和访问权限，不向评审者复制其他用户私钥。具体字段见[自建环境指南](self-hosting-zh.md#gpu-setup)。

仅 auto：GPU 全忙或 SSH/环境不可用则跳过，有 1–3 张空闲卡运行六案例，四张运行完整八案例。GPU 状态独立记录在 `result.md`，不使已完成的 CPU 实验失败；CPU 成功不表示 GPU 完整。

## 最新结果与历史备份

在 launcher.json 中设置根用户管理的 `results_backup_root`：
`/mnt/disk2/dyp/deltabox-runtime/ae/work/results-backups/public-ae`。
新的完整运行在持有运行锁时，先按时间戳复制全部旧结果，逐文件核验 SHA-256、元数据和链接，再替换工作目录。目标需要容纳现有结果并另外保留 10 GiB。复制失败、校验失败或活动引用均保留源结果，不启动实验。

每份备份包含 `results/` 和 `backup.json`，后者记录文件清单、校验结果及原始路径到归档路径的映射。历史 JSON 的来源身份和链接文本保持原样；恢复旧数据时按记录的原始路径还原，不能把旧数据算作新一轮结果。快速检查、指定实验和显式 `--output` 不触发完整目录轮换。启动审计和互斥锁放在结果目录之外，避免随归档丢失运行保护。
