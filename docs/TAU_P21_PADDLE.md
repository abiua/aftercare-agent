# P2.1 中文 OCR 引擎复现

在 macOS arm64 上使用 CPython 3.11.16、PaddlePaddle 3.2.0、PaddleOCR 3.3.1、PaddleX 3.3.10 和 Tesseract 5.5.3。PDF 单页光栅化使用 Poppler 26.05.0。Python 依赖及模型缓存位于项目内的 `.runtime/p21-paddle`。

- [PaddlePaddle CPU 安装说明](https://www.paddleocr.ai/v3.5.0/en/version3.x/paddlepaddle_installation.html)
- [PaddleOCR 3.3.1 官方快速入门](https://www.paddleocr.ai/v3.3.1/en/quick_start.html)
- [Tesseract tessdata_fast 固定版本](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd)

从仓库根目录执行以下命令。系统需提供 `uv`、`curl`、`tesseract` 和 `pdftoppm`。

```sh
uv venv --python 3.11 .runtime/p21-paddle
uv pip install --python .runtime/p21-paddle/bin/python --only-binary :all: \
  -r scripts/p21_paddle_requirements.txt
mkdir -p .runtime/p21-paddle/tessdata
for lang in chi_sim eng; do
  curl -fLsS "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/87416418657359cb625c412a48b6e1d6d41c29bd/$lang.traineddata" \
    -o ".runtime/p21-paddle/tessdata/$lang.traineddata" || exit
done

for engine in tesseract-eng tesseract-chi_sim-eng; do
  .runtime/p21-paddle/bin/python scripts/evaluate_tau_ocr.py tesseract \
    --manifest data/tau-ocr/manifest.json --engine "$engine" \
    --tessdata-dir .runtime/p21-paddle/tessdata \
    --output "reports/tau-p21/$engine.json" || exit
done
.runtime/p21-paddle/bin/python scripts/p21_paddle.py \
  data/tau-ocr/{policy_clear,policy_low,table_clear,table_low}.png \
  --out reports/tau-p21/paddle.json
```

`chi_sim.traineddata` SHA-256 为 `a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730`，`eng.traineddata` 为 `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`。两条 Tesseract 对照使用相同固定版本的语言包和相同默认整页分割设置，分别载入 `eng` 和 `chi_sim+eng`。两者保留原始 TSV 及坐标。历史 P2 输出另行保留。

Paddle 使用 PP-StructureV3、PP-DocLayout-M 和 PP-OCRv5 移动版中文检测与识别模型，在 CPU 上以四线程运行。表格由整页布局检测和 PP-StructureV3 自带表格子流程生成。报告保留 Paddle 原始识别、布局、坐标及表格结果，并从模型的 HTML 展开单元格锚点和合并范围。公式、印章、图表、区域检测和页面预处理关闭。PaddleX 3.3.10 初始化阶段仍会加载关闭预测的 PP-Chart2Table。首次运行从 Paddle 官方 BOS 下载权重，报告记录本次模型缓存的推理文件 SHA-256。

同一命令可接收 PDF，默认只渲染第 1 页，使用 `--page N --dpi 150` 可更换页码和分辨率。报告同时记录输入文件与实际识别图片的 SHA-256。样本 ID 为文件名去掉扩展名，一次调用请使用不同文件名。整页 PNG 是同输入比较使用的样本。输出中的 `startup_ms` 包含引擎初始化，冷启动时也包含下载。Paddle 单页 `elapsed_ms` 包含识别和结果转换，Tesseract 的每页计时包含启动其进程。PDF 渲染耗时在单页 OCR 计时之外。启动失败或单页失败写入 JSON 并返回非零退出码。

## 本机执行记录

四个 PNG 输入按 `policy_clear`、`policy_low`、`table_clear`、`table_low` 顺序执行。以下报告路径均位于 `reports/tau-p21/`。

| 引擎 | 报告 | 初始化（ms） | 逐页（ms） |
| --- | --- | ---: | --- |
| Paddle PP-StructureV3 | `paddle-cached.json` | 9879.59 | 3201.93 / 2132.29 / 5037.70 / 3756.98 |
| Tesseract 英文 | `tesseract-eng-serial.json` | 随每页进程计入 | 204.406 / 168.064 / 204.869 / 181.197 |
| Tesseract 中英 | `tesseract-chi_sim-eng-serial.json` | 随每页进程计入 | 367.186 / 334.521 / 378.620 / 360.469 |

三个串行捕获均成功处理四页。Paddle 冷首推的 `paddle.json` 保留全部原始输出，第一张页因按需下载表格方向和文本行方向权重用时 30289.01 ms。与缓存复跑逐页比较，输入 SHA-256、正文、表格 HTML 和原始预测 JSON 一致。两条 Tesseract 串行捕获各自与前一次成功捕获的逐页 TSV SHA-256 一致。时延来自本机单次串行复跑，Paddle 含独立的初始化时间，Tesseract 将每页进程启动计入该页。

两张表格样本均得到单表 5×4、16 个锚点的结构。低质表出现单元格内容错配，表格维度和跨度相同仍需查看单元格核验结果。`paddle-pdf-smoke.json` 记录同一 CLI 将扫描 PDF 的第一页按 150 DPI 转为图片并得到单表 HTML 的功能检查，四图评分沿用 PNG 输入。

`paddle-model-files.sha256` 包含缓存中 11 个模型的全部 44 个文件，含 PP-Chart2Table 的 `model_state.pdparams`。安装结果可见 `.runtime/p21-paddle/install.log`，下载预检与执行日志分别为 `structure-init.log`、`benchmark.log`、`benchmark-cached.log`、`benchmark-pdf.log`。最初的 Tesseract 试跑因隔离语言目录中缺少 TSV 格式配置，命令输出了纯文本并触发解析失败。失败 JSON 仍在报告目录，正式命令通过显式 TSV 配置重新执行。
