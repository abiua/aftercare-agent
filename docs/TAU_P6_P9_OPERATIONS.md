# 评测与演示运行

旧自定义 `compare / ablate / counterexample / route / fault` 批次入口已经退役。官方评测操作以 `scripts/aftercare-eval.sh help` 为准；任务、模型及指标说明见 [官方评测](TAU_OFFICIAL_EVALUATION.md)。

测试中台：本地演示页 `http://127.0.0.1:5225`，选择测试员，查看官方 Retail 对比。显示的是已发布报告，不会仅因打开页面触发付费请求。

## 现有聊天运行

```sh
scripts/aftercare-eval.sh models
scripts/aftercare-eval.sh api
scripts/aftercare-eval.sh worker
```

API 和 worker 默认离线；真实模型调用显式增加 `--live`。沿用隔离 P5 数据库、缓存和图片存储，不自动启动或关闭其他项目的服务。业务执行仍为模拟。

模型路由能力继续保留：明确记录查询走程序，文本事实任务优先 economy，政策与复杂判断用 analysis，图片走已验证视觉配置。路由是否省钱需要另外的官方匹配对比，本次先固定模型完成三种架构比较。
