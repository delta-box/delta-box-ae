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

在 root 管理的 `/etc/deltabox-ae/review.json` 中，通过 `gpu.config` 指向作者配置的 GPU JSON，填写本机模型、Python 环境和分配的设备，删除旧的 `gpu.enabled=false` 设置。具体字段见[自建环境指南](self-hosting-zh.md#gpu-setup)。完整运行要求同节点四张空闲 GPU；评审者不通过命令行覆盖这些固定配置。

资源尚未分配或全部繁忙时，GPU 项返回失败，提示联系作者，并保留 CPU 结果及相关日志；不能将该轮标记为完整成功。
