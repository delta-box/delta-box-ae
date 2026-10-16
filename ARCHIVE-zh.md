# DeltaBox 归档使用说明

[English](ARCHIVE.md) · [AE 指南](README-zh.md)

Zenodo 归档包含 DeltaBox 实现、实验脚本、录制输入与结果，以及编译好的 x86-64 guest 内核 `linux/vmlinux`。`linux/kernel.config` 记录内核编译配置，各组件的许可声明保留在原目录中。

`VERSION.json` 记录上游 AE 提交和内核身份；`FILE-MANIFEST.jsonl` 与 `SHA256SUMS` 列出本归档实际提供的文件。归档包含 Git 元数据，供实验运行器记录实际执行的源码版本。

## 准备 Linux 环境

解压后进入 `delta-box-ae` 目录。使用 Linux x86-64，并安装 Python 3.10+、Git 和 `zstd`：

```bash
cd delta-box-ae
python3 -m venv .venv
.venv/bin/pip install -r ae/requirements-analysis.txt
.venv/bin/python ae/reproduce.py prepare
```

在自己的机器上部署时，继续按照[自建环境指南](ae/docs/self-hosting-zh.md)准备 KVM、Firecracker、CRIU、guest 磁盘和 baseline 服务，跳过其中的 Git clone 步骤。托管评审机器已提供这些依赖，登录和运行命令见 [AE 指南](README-zh.md)。

## 使用编译好的内核

准备匹配的 XFS 母盘和自己的 SSH 公钥，然后将归档中的内核传给镜像构建入口：

```bash
bash ae/build_images.sh \
  --master-xfs /path/to/ubuntu-24.04.xfs \
  --kernel "$PWD/linux/vmlinux" \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" \
  --output "$PWD/ae/work/images-local"
```

输出目录必须是新目录。母盘及 baseline 环境的获取或准备方式见自建环境指南。配置完成后，按照 AE 指南运行快速检查，并从实验索引选择所需实验。
