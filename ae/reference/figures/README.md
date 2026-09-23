# 论文原图

这里保存 ATC 2026 论文 `atc26-paper158.pdf` 的 9 张图和 3 张表，包含原始图注，供 [AE README](../../README.md) 引用。仓库中的同一份 PDF 为 [paper158.pdf](../paper158.pdf)。这些图片是论文原图，不是 AE 实测结果。

[manifest.json](manifest.json) 记录 PDF 的 SHA-256、页码、裁剪位置、图片尺寸和图片哈希。所有裁剪坐标以 PDF 页面左上角为原点，单位为 point；输出分辨率为 288 DPI。

维护文档时，可在仓库根目录重新提取；评审者使用已提供的图片，无需安装提取依赖：

```bash
python3 -m pip install pypdfium2 Pillow
python3 ae/scripts/extract_paper_figures.py
```

也可通过 `--pdf PATH` 指定原始 PDF。脚本会核对来源哈希；论文版本改变时，需要先重新确认页码和裁剪范围，再更新脚本。
