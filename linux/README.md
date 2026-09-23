# Linux 6.8 for DeltaBox

Prebuilt x86-64 guest kernel with DeltaBox OverlayFS built in, including the anonymous-backing fix used by checkpoint/restore. Load `vmlinux` with Firecracker; `kernel.config` is the build configuration.

DeltaBox 使用的 x86-64 guest 内核，内置修改版 OverlayFS 及匿名 backing 修复。Firecracker 加载 `vmlinux`；编译配置为 `kernel.config`。

Built with `make -j8 vmlinux` from the existing Linux 6.8 build tree plus `overlayfs-fix.patch`. File hashes are in `SHA256SUMS`.

[Kernel source](https://github.com/delta-box/d-overlayfs) · [OverlayFS fix](overlayfs-fix.patch) · [License](LICENSE)
