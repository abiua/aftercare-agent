# 中文 OCR 合成样本

这套材料只有两个单页源文档，即退货政策和运费补偿表。每页提供三个版本，共六个单页 PDF。扫描版本另附送入 OCR 的 PNG。清晰与低质版本是同页的配对样本，样本中没有真实商家政策或客户数据。

| 源页 | 原生 PDF | 清晰扫描 | 低质扫描 |
| --- | --- | --- | --- |
| 退货政策 | `policy_native.pdf` | `policy_clear.png`、`policy_clear.pdf` | `policy_low.png`、`policy_low.pdf` |
| 运费补偿表 | `table_native.pdf` | `table_clear.png`、`table_clear.pdf` | `table_low.png`、`table_low.pdf` |

`ground_truth.json` 由 Agent 在 OCR 运行前编写。它同时定义合成文档内容和评估参考，未经过真人金标审核。政策按视觉行从上到下读取。表格按单元格左上角的起始行和起始列排序，合并单元格只计一次。表格坐标从 0 开始，跨度从 1 开始。审核时可打开 PNG，逐项核对金额、小数点、否定句、日期、例外及表格合并边界。

`manifest.json` 记录样本和参考文件的 SHA-256、图像尺寸、字体来源与降质参数。清晰扫描为 150 DPI 灰度渲染。低质扫描先按 92 DPI 灰度渲染，再逆时针旋转 1.65°、高斯模糊 0.7 像素、加入种子固定的高斯噪声，并经质量 42 的 JPEG 中间压缩后保存为 PNG。扫描 PDF 封装对应 PNG 的像素。

在仓库根目录运行以下命令。生成器下载并校验 Noto Sans SC 的公开上游文件，固定字体轴为 400。已有同名文件将做字节校验。生成依赖由脚本内的版本声明固定。

```sh
uv run --script scripts/build_tau_ocr_fixtures.py
```

如果已有相同源字体文件，可通过 `--font /absolute/path/NotoSansSC-VF.ttf` 复用。字体授权见 [Noto CJK 的 OFL 1.1 文件](https://github.com/notofonts/noto-cjk/blob/523d033d6cb47f4a80c58a35753646f5c3608a78/LICENSE)。
