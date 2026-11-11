# 冻结小样本真实模型对照结果

2026-11-10 从运行中 test API 实时读取四组终态。三种开发案例各重复3次，每组9次，共36次；并非36个独立案例，也不是独立人工金标评估。

| 条件 | 已结束 | 失败 | 待人审 | 记录调用 | 估算美元 | 门槛 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| single-economy | 9 | 0 | 9 | 30 | 0.0578977 | INCOMPLETE |
| single-analysis | 9 | 2 | 7 | 36 | 0.1559255 | FAILED |
| single-routed | 9 | 2 | 7 | 37 | 0.09755225 | FAILED |
| multi-routed | 9 | 3 | 6 | 55 | 0.19614415 | FAILED |

合计记录 158 次模型调用，估算 $0.50751960；这是调用账本估算，不是供应商账单。

7次失败均为 AC02-C001：single-analysis 2次、single-routed 2次出现 DRAFT_RULE_CONFLICT；multi-routed 1次 MODEL_COST_BUDGET_EXHAUSTED、2次 MODEL_RUN_BUDGET_EXHAUSTED。其余29次仍需人工审核。不得将 task_success=0 解读为全部业务失败，该指标目前要求独立人工通过。

两组 compare 报告均 compatible=true 且9/9成对完成，保存于 reports/compare-single-economy-vs-single-analysis.json 和 reports/compare-single-routed-vs-multi-routed.json。

后续变更：前置缺材料/材料待审判断，减少无效模型调用；校验空 action 与残留 lines 的一致性，明确遗漏引用与订单事实，再按原预算复验。原报告保持作为失败基线，不以加预算或改参考答案掩盖失败。

本轮不能证明多Agent优于单Agent；也不能从3个开发案例推广整体模型质量。
