# 售后评测数据 ac-eval-0.2

固定观察时间为 2026-11-10 12:00（UTC+08:00）。新案例使用合成客户、订单、库存和账本。现有 v0.1 作为商品和政策的只读来源，源文件哈希记录在 `data/v0.2/manifest.json`。

| 划分 | 破损 | 错发 | 退款进度 | 合计 |
| --- | ---: | ---: | ---: | ---: |
| 开发 | 20 | 20 | 20 | 60 |
| 验证 | 10 | 10 | 10 | 30 |
| 保留 | 10 | 10 | 10 | 30 |
| 核心合计 | 40 | 40 | 40 | 120 |
| 已知冲突回归 | 19 | 4 | 1 | 24 |

核心 120 案由 24 个场景族组成，每族 5 个案例。每族只进入一个划分，表达、订单商品和金额分配随案例变化。同一意图下的场景族分别改变材料顺序、人工意见和版本、支付或签收的可核验性、订单行额度、库存和账本状态。24 个已知冲突案例独立运行，涵盖越权、材料、政策、金额、库存、审批、重复或并发执行、未知支付和进程中断。其结果与核心案例分开汇总。

所有客户表述由本项目合成，场景共享同一套模拟业务和政策来源。保留集隔离了场景族和表述组，仍存在合成来源、作者与规则同源的限制。脚本中的人工材料意见为确定性测试输入。当前人工复核计数为零，所有参考均标记 `SYNTHETIC_REFERENCE` 和 `PENDING`。

## 文件与运行器接口

- `data/v0.2/agent/cases.<split>.json` 包含 `id`、`split`、`message`、`fixture` 和 `events`。快照内的 `business` 提供客户、产品、订单、物流和政策。 `inventory` 记录按商品的初始库存，`refund_history` 记录初始退款执行，少数回归的 `policy_overrides` 提供待插入的新政策版本。
- `data/v0.2/evaluation/private/references.json` 按案例 ID 索引，包含场景族、评分条件、允许动作、必需事实、政策及金额。评分检查指向事件 ID 和响应字段路径，数组使用数字路径段，例如 `output.ledger.0.status`。参考只给评分器。
- `data/v0.2/manifest.json` 固定源文件、案例文件、私有参考哈希、划分和事件计数。代码入口为 `aftercare.evaluation.dataset.load_cases()`、`load_references()` 和 `material_bytes(case_id, asset_id)`。

事件元素包含 `id/op/actor/args`。客户身份使用快照中的所有者，外部客户另行创建。 `create_case` 取订单版本，其余业务写操作取最新工单版本。 `start` 和 `resume` 使用实验配置的模式和路由策略。 `upload` 用 `material_bytes` 生成可解码、内容唯一的 8×8 模拟 PNG；同一 ID 可稳定重现同一内容，`invalid-bytes` 生成无效图片。图片只验证对象上传和材料流程。

退款进度历史在创建本案前注入隔离业务库。 `SUCCEEDED` 记录已计入订单行的 `refunded_cents`，运行器导入时保留金额，避免再增加一次。旧系统累计余额可存在而没有带时间与回执的明细。历史条目可含 `intent` 和 `case_line_ids`，用来构造跨案件重叠与已占用额度。历史案例、方案、审批和执行关联保持独立于本案。

冲突脚本的 `fault`、`parallel_execute`、`repeat_event` 需要相应故障或并发运行器。运行器缺相应能力时，记录该案例未执行并保留原因。对两种 Agent 或路由策略做成对实验时，复用相同的事件序列、模拟时钟、业务快照和初始状态。意图随工单创建参数提供，基于该入口运行的结果用于工作流评估，开放式意图分类需另行隔离标签测量。

## 数据验收

```bash
PYTHONPATH=backend/src backend/.venv/bin/python -m aftercare.evaluation.dataset verify
PYTHONPATH=backend/src backend/.venv/bin/pytest -q backend/tests/evaluation/test_dataset.py
```

2026-11-10 实际运行结果为 268 项检查通过。检查覆盖字节级复现、数量与来源分组、144 案金额与事件结构、120 案脚本各决策点对当前确定性业务规则的匹配、PNG 资产解码。结果对应数据和纯规则检查。真实 API、故障脚本、向量检索效果、模型能力及人工金标仍分别记录。

冻结前依据 AC02-R023 的中断结果契约修正一条参考。运行中断时返回的整个 `output` 为 null，因此检查该字段本身。案例输入、脚本及 `NEEDS_REVIEW / INTERRUPTED_MODEL_CALL` 的要求保持原值。
