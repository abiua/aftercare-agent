# P2 离线文档处理

P2 读取 P1 已固定的公开政策。脚本校验仓库清单中的文件名、大小和 SHA-256，输出记录同时带上上游 commit、公开来源、许可证和文档版本。生效日期由来源单独提供，目前为 `null`。脚本也可处理仓库清单登记的合成格式样本，来源标为 `synthetic_format_fixture`。P1 的任务、用户场景和评分文件不在输入接口中。

## 复现

在仓库根目录运行。每次选择新的输出目录。Markdown 路径只需要 Python 3.11+。PDF 路径使用固定的 pdfplumber 版本，扫描页另外需要 PATH 中的 `pdftoppm`、`tesseract` 和 Tesseract 英语词库 `eng`。

```bash
python3 scripts/tau_documents.py --policy .runtime/tau-retail/source/policy.md \
  --output .runtime/tau-documents/policy-v1

uv run scripts/tau_documents.py --fixture synthetic_policy.pdf \
  --output .runtime/tau-documents/native-v1

uv run scripts/tau_documents.py --fixture synthetic_policy_scan.pdf \
  --output .runtime/tau-documents/scan-v1

uv run --no-project --python 3.11 --with 'pdfplumber==0.11.9' --with 'pytest>=8,<10' \
  pytest -q backend/tests/test_tau_documents.py
```

每个导出目录包含 `original/` 下的校验后原始字节、`document.json` 和 `preview.txt`。脚本在同级临时目录生成整套文件，并通过父目录的 `.tau-documents.lock` 串行检查目标与发布。CLI 的并发导出只会发布一次，已有目标会拒绝覆盖。三个文件随目录一起出现。发布使用 macOS/Linux 的文件锁。

## 结构与定位

- Markdown 支持 ATX 标题、连续条款段落、带缩进的列表和管道表格。块保留当前标题层级、原始文本、1 起始行号及 UTF-8 半开字节区间。派生文本统一行尾并移除独立空行，保留否定、条件、金额及行内标记。表格另存行列，空格修剪仅应用于派生单元格。解析器覆盖固定政策和合成样本使用的 Markdown 子集。
- 原生 PDF 逐页提取文字行和有边框的表格，坐标为左上角原点的 PDF 点。表格另存单元格边界。根据页内正文主字号计算的标题候选属于启发式，跨页规则仍保留在各自页。嵌入图片与文字并存的页标记待复核，因为图片可能带有遗漏内容。
- 扫描页以 300 DPI 栅格化，再运行英文 OCR。输出保留观察文本、页码、坐标、最小词置信度和工具版本。保留原始扫描 PDF 供复核。扫描表格输出为带坐标的 OCR 文字，列归属和行列配对需要人工核对。中文 OCR 尚未验证。
- 文档 ID 由来源类型、文档版本、文件名和文件哈希生成。块 ID 以文档 ID、块类型、定位和同位置序号生成。同一解析环境下重复运行得到相同 ID。更换 PDF 提取器版本可能改变坐标和块 ID。

## 状态与观察

| 样本 | 本地结果 |
| --- | --- |
| 固定 τ²-bench 政策 | 77 个 Markdown 块。来源哈希通过校验，版本固定到 P1 commit。 |
| 合成 Markdown | 8 个块。条件、否定、例外、两行金额和行号通过比对。 |
| 合成原生 PDF | 两页共 13 个块。第 1 页含条件与否定，第 2 页含例外及 3×2 表格。 |
| 合成扫描 PDF | 两页共 18 个 OCR 行。否定、例外及两个金额出现在 OCR 观察中。`Issue` 被误识别为 `lssue`。OCR 未输出表格行列关联，页面与文档均标为 `needs_review`。 |
| 合成空白 PDF | `empty`、`needs_review`，保存零块和页诊断，退出码 3。 |
| 合成损坏 PDF | PDF 解析报 Unexpected EOF，退出码 2。 |

`extraction_status` 为 `parsed`、`partial` 或 `empty`。只有 `parsed` 返回退出码 0，扫描得到文字也仍需核查 `review_status`。缺失 PDF 库、输入不符合清单或损坏 PDF 返回退出码 2。扫描页缺少外部工具、OCR 失败或没有文本时记录页诊断，整个输出按实际情况标为 `partial` 或 `empty`，返回 3。P2 的导出仅供离线查看与后续验收。

完整阶段结论见 [P2 验收](TAU_P2_ACCEPTANCE.md)，已知问题见 [bad case 分析](TAU_P2_BAD_CASES.md)。`source_verified` 仅表示来源字节校验，并不表示政策适用或业务事实已通过人工审核。
