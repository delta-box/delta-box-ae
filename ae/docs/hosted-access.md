# 托管账号权限

当前 `atc-ae` 是作者授权的可写开发账号。它可以编辑 `/mnt/disk2/dyp/deltabox-runtime`，并通过 `bash ae/run_all.sh` 的固定入口以 root 执行仓库代码。这项授权包含运行其自行修改代码的权限，只适合受信任的开发者。

权限由 root 管理的 `/etc/deltabox-ae/launcher.json` 显式指定：

```json
{
  "allowed_user": "atc-ae",
  "trusted_maintainer": "dyp",
  "trusted_developer": "atc-ae"
}
```

`trusted_developer` 必须与 `allowed_user` 对应同一账号；调用方不能通过环境变量开启此权限。没有该字段时，入口继续按只读评审账号检查，拒绝评审账号可修改的代码。

root 策略、私有环境、启动程序和运行控制文件继续由 root 管理。工作目录和结果目录允许可信开发者的访问 ACL。源码锁、结果哈希和版本目录规则不变；修改被测源码后，需要提交并更新源码锁。

仅检查入口权限和可选实验名称，不执行实验：

```bash
cd ~/delta-box-ae
bash ae/run_all.sh --list
```


## 快速检查与完整实验入口

评审账号的 `~/delta-box-ae` 应指向策略中 `runtime_root` 配置的发布仓库。发布时一并更新 `ae/run_test.sh` 和 `ae/run_all.sh`。

作者更新部署时，同步安装仓库中的 `ae/scripts/hosted_launcher.py`，使托管入口接受 `--group gpu`、`--group cpu` 和 `--group figure-08-cpu`。默认完整运行输出到 `ae/results/<源码版本>/full/`；`bash ae/run_test.sh` 仍为最小 CPU 检查。

在 root 管理的 `/etc/deltabox-ae/review.json` 中设置 `gpu_remote_config`，指向作者维护的远端配置（默认 `ae/configs/figure08-remote.json`），删除旧的 `gpu.config` / `gpu.enabled`。本机无需 GPU，入口通过原调用用户的非交互 SSH 身份访问 allinai2plus；管理员应为该用户配置可用的主机别名和访问权限，不向评审者复制其他用户私钥。具体字段见[自建环境指南](self-hosting-zh.md#gpu-setup)。

仅 auto：GPU 全忙或 SSH/环境不可用则跳过，有 1–3 张空闲卡运行六案例，四张运行完整八案例。GPU 状态独立记录在 `result.md`，不使已完成的 CPU 实验失败；CPU 成功不表示 GPU 完整。
