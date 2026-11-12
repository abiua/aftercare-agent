# Tau P2.1 中文扫描评测协议

## 样本与采集

- 合成中文政策页与带合并单元格的表格页各一张。每张源页派生清晰、低质两种扫描，共四张 PNG。两份原生 PDF 供核对版式，未进入扫描分母。评测样本包含两个源页和四份相关扫描。
- 三组使用同一批 PNG 字节。Tesseract 使用现有 P2 英文 TSV 路径的语言设置作为历史参照，另用同样的 TSV 处理和默认分割配置运行 `chi_sim+eng`。两组都通过 `--tessdata-dir` 指向同一份固定语言数据。现有 P2 提取器从 PDF 按 300 DPI 转成 PNG，历史语言参照在此改为直接接收固定 PNG，便于与中文基线和 Paddle 比较。
- Paddle 的正文取实际识别输出，表格取结构模型预测的网格。OCR 输入仅为图片。Tesseract 采集用清单定位图片和核验字节，只有评分命令读取参考 JSON 内容。采集与评分输出使用不同文件。
- `manifest.json` 登记参考和各输入 SHA-256，评分前核对。原始采集文件也计算 SHA-256，评分报告记录清单、参考、评分脚本、采集文件和逐页输入哈希。复跑时保留冻结清单和原始采集文件，并使用已记录的 SHA-256 审核一致性。

## 评分

- **CER** 移除 Unicode 空白，保留原字符的大小写、标点、金额小数点、正负号、数字、中文和否定词。每页按参考阅读顺序拼接文字，对归一化后的两个字符串计算字符级 Levenshtein 距离。汇总 CER 是总编辑距离除以参考字符总数。阅读顺序差异会影响 CER。
- **关键文本保留率** 按金额、否定、日期／期限、例外分别计算。每条冻结的文本片段在归一化正文内完整出现算命中，金额与日期还检查紧邻的数字边界。分母是每类参考片段总数，金额出现在另一行仍可能命中。金额与记录的行列归属通过表格指标审核。
- **结构覆盖** 报告有表格参考的页数、引擎明确支持的页数、返回有效非空网格的页数和各状态。Tesseract 只产出 TSV 文字与坐标，其结构指标记为 `unsupported`，相应精度和召回为 `null`。旧采集缺少能力声明时记为 `unknown_support`。
- **表格结构与内容** 使用零基行列锚点。锚点及其 `rowspan`、`colspan` 一致计入 span recall，另报参考合并单元格的 span recall。再要求该位置文本完全匹配才算单元格内容命中。单元格 precision 的分母是有效预测单元格数，recall 的分母是明确支持引擎的参考单元格数。整表精确要求行列数、全部单元格和总单元格数均匹配。未产出结构、网格为空、跨度越界或单元格重叠都记明状态，相关参考单元格仍计入 recall 分母。
- **失败分母** OCR 异常、超时、缺页、空文本、输入哈希不符按空预测计入 CER 和关键文本保留率。表格支持的引擎在相应表格页保留结构失败分母。重复样本 ID、未知样本 ID、参考或输入文件与清单的哈希不一致会中止评分。
- **时间** Tesseract 每页启动一个新进程，记录启动、识别和 TSV 解析的合计用时。Paddle 分别记录初始化及每页推理时间，第一张包含初始化后首次推理的开销，后续张为同进程连续推理。两类单页计时边界不同。汇总记录计时页数和均值，逐页保留实际时间或空值。

## 命令

```bash
python3 scripts/evaluate_tau_ocr.py tesseract \
  --manifest data/tau-ocr/manifest.json \
  --engine tesseract-eng --tessdata-dir .runtime/p21-paddle/tessdata \
  --output /tmp/tau-p21-eng.json

python3 scripts/evaluate_tau_ocr.py tesseract \
  --manifest data/tau-ocr/manifest.json \
  --engine tesseract-chi_sim-eng --tessdata-dir .runtime/p21-paddle/tessdata \
  --output /tmp/tau-p21-chi-sim-eng.json

python3 scripts/evaluate_tau_ocr.py score \
  --manifest data/tau-ocr/manifest.json \
  --ground-truth data/tau-ocr/ground_truth.json \
  --result /tmp/tau-p21-eng.json --result /tmp/tau-p21-chi-sim-eng.json \
  --result /tmp/tau-p21-paddle.json --output /tmp/tau-p21-score.json
```

参考由合成样本作者冻结，未经过人工金标准标注。结果适用于这些固定样本，样本量不足以估计真实售后文档上的总体准确率。
