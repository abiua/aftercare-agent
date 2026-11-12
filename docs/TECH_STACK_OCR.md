# 文档解析与 OCR 技术选型

2026-11-12 已确认保留：原生文字优先直接解析；扫描件采用 PaddleOCR PP-StructureV3 候选路径，有长表格线且角度共识足够时先自动倾斜校正，再走原有 SLANeXt_wired + 单元格检测/识别流程。该方案是项目文档处理技术栈的一部分，目前为离线已验证组件，不等于客户聊天或可信政策入库已自动接线。

- 文本/原生PDF：复用 `scripts/tau_documents.py`，保留原文、来源版本、块位置和审阅状态。
- 中文扫描：PaddlePaddle 3.2.0 / PaddleOCR 3.3.1 / PaddleX 3.3.10，固定依赖及模型哈希。
- 倾斜校正：OpenCV Canny + Hough长线共识，根据图像自动估角，扩展画布避免裁切；证据不足则跳过。入口 `scripts/p21_table_optimization.py --configuration deskew-baseline`。
- 不采用本轮wired E2E替代路径：在已知两张表上导致空格与文字错配。保留全部负面结果。
- 不以OCR置信度或金额出现代替事实核验：保留原图、实际送入模型的校正图、仿射矩阵、哈希及模型原始输出。需要复核的OCR不能自动成为可信政策；P3索引入口应拒绝 `needs_review` 文档。

本轮合成低质表完整单元格匹配2/16→15/16，清晰表16/16不变。仍存在“并→井”，不是整表完全可靠，不能外推真实企业准确率。无框表格、复杂透视和跨页表格待验证。

证据：[优化验收](TAU_P212_OPTIMIZATION.md)、[独立复核](TAU_P212_REVIEW.md)、[复现命令](../reports/tau-p212/REPRODUCE.md)。保留旧默认入口与历史评测，不按答案改OCR结果。
