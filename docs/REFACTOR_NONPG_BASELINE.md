# 产品接入前的非数据库回归基线

2026-11-13，E1保留集运行期间只做离线回归，未改动冻结模型代码与提示。

命令：在backend目录运行 `.venv/bin/python -m pytest -m 'not pg' -q`。

结果：1003 passed、5 failed、5 skipped、40 deselected，41.60秒。完整日志：`reports/refactor-nonpg-baseline.log`。

五项失败与原工程记录的类别一致：

| 测试 | 当前失败 | 后续验收要求 |
|---|---|---|
| test_actual_frozen_business_candidate_across_checkpointed_arms[strong_single] | 强单输入预算路径与预期不一致 | 分别测量提示/工具开销，不能简单删工具或改断言 |
| test_material_review_queues_new_native_candidate_without_customer_message[None] | 未生成预期PENDING_SUPPORT快照 | 复核关闭模型时的失败接管及版本变化；保留实际失败原因 |
| 同上[PENDING] | 同上 | 同上 |
| 同上[RUNNING] | 同上 | 同上 |
| test_confirmed_new_issue_keeps_only_its_own_photos_in_input | 新事项照片集合不符 | 明确pending_scope及确认后的附件归属，不混入旧事项材料 |

这些问题仍未修复。此次结果作为阶段6改动前基线，不等于产品验收通过，也不证明数据库、浏览器或真实模型链路可用。
