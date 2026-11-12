# 售后运行模型配置（2026-11-13）

客户聊天及任务路由的 economy 改为 `deepseek-flash`（DeepSeek Chat Completions，关闭 thinking）；analysis 保留 `gpt-6.1-sol`（OpenAI Responses）。当前启动入口为 `scripts/aftercare-eval.sh api|worker [--live]`，不带 `--live` 仍为离线模式。

配置位于 `.runtime/models.json`、`.runtime/p9-task-models.json`。切换前副本为同目录的 `*.before-deepseek-20261113.json`。密钥分别从 `deepseek-secret.json`、`model-secret.json` 读入环境变量，不进入配置或报告。

## 实际验证

通过项目 LangChain 适配器完成 3 次 DeepSeek 请求：工具调用参数正确；回填“无退款记录”后返回 `records_found: false`；输入原有模拟杯子、包裹图片，经项目图片预处理后识别裂纹和包裹破损。输入共 1316 tokens，输出共 155 tokens。证据为 `.runtime/deepseek-flash-compatibility-20261113.json`。这验证接口兼容，不代表完整售后任务正确率。

配置保留原有图片输入预算上界，不因这两张小图的用量降低预算。流式能力未验证，关闭原先属于 OpenAI 的流式能力标记。工具任务使用共同兼容组，允许低成本模型失败后按既有预算升级到 analysis；图片任务仍只有 economy 候选。

费用按 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/) 的高峰未命中缓存价估算：输入 $0.30/百万 tokens、输出 $1.20/百万 tokens；缓存命中也按未命中价保守预留，不伪造缓存 TTL。该估算不是账户实际账单。本次 3 次请求按此上界约 $0.000581。

## 范围

未重跑官方评测，历史报告、模型和成本原样保留。`run_tau_official_campaign.py` 是独立的冻结对比协议，仍使用原来的模拟客户与官方裁判模型，不随售后应用路由配置变更；后续评测要另建批次明确模型组合，不得覆盖旧结果。
