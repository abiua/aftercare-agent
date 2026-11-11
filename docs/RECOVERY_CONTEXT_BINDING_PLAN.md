# 运行恢复的上下文绑定

## 触发条件

同一运行周期可能先由旧 worker 写入完整模型尝试或终态检查点，再由接管 worker 发布。开发验收复现了后一条路径。另一工单改变了同订单的退款执行记录，查询工单版本保持相同，接管 worker 发布了检查点里的旧账本。原有结束校验只比较本次进程开始与结束时读到的版本，因此无法识别在接管前发生的变化。执行记录转为失败时还可能保持订单版本不变。 [复现报告](../reports/recovery-context-drift-diagnostic.json) · [账本状态更新](../backend/src/aftercare/business/service.py#L1440)

同一上下文内的完整响应可以在接管后复用，以免重复向供应商发出请求。失租 worker 仍可保存已发生的用量和估算费用。它发布运行、草稿或检查点时仍受租约围栏。恢复后的工具调用继续按当前权限和证据校验。 [模型恢复要求](notion_snapshot/models.md#L80) · [既有同上下文复用回归](../backend/tests/intelligence/test_intelligence.py#L1368)

## 周期契约

- worker 在调用意图探针或运行图之前，从授权业务上下文计算摘要。在队列租约和运行行锁保护下，为每个周期写入一次 `context_binding` 事件。事件只记录 `cycle`、摘要协议 `version=agent-context-v1` 和 `digest`。既有事件用于比对，保留原摘要。
- 同周期接管先读取检查点元数据，以判断是否存在持久工作。绑定一致后才恢复图或使用已经保存的终态。绑定存在但当前业务事实不同，运行进入 `NEEDS_REVIEW`，原因 `RECOVERY_CONTEXT_CHANGED`，发布内容为空。同工单版本本身已过期仍由原版本冲突处理。
- 模型缓存保持原调用标识和尝试数。持久返回格式为 `{calls}` 或 `{result}`。恢复时分别把工具调用列表或结果对象传给当前校验器。当前工具、引用或结构校验拒绝时，用 `REPLAY_VALIDATION_FAILED` 结束到待复核。此路径保留已发生的调用审计。
- 最终发布之前重新读取业务上下文并核对同一摘要。周期内事实发生变化时沿用 `FACTS_CHANGED_DURING_RUN`。运行发布仍经过租约与周期校验，正式方案、财务审批和执行继续由业务服务控制。

## 摘要字段与时间

摘要输入限于 `BusinessService.context` 的当前业务字段，包含工单、订单、政策版本与解析结果、材料和审核、授权账本、版本、资格及已登记事实。只保存摘要。账本行内容和资格余额或库存都进入计算，以覆盖版本号未变化的状态转换。业务查询按稳定顺序返回列表，摘要规范化对象键顺序。 [业务快照](../backend/src/aftercare/business/service.py#L546)

策略解析使用当前时钟选择版本，但返回值只含选择结果、范围、冲突和其摘要。时钟推进本身不改变摘要；有效政策确实切换时才体现为业务变化。材料对象的访问签名链接由独立接口临时生成，业务快照只有登记元数据。请求追踪值、连接信息、运行计时和临时链接不参与摘要。 [版本解析返回](../backend/src/aftercare/intelligence/policy_versions.py#L185) · [材料元数据](../backend/src/aftercare/business/service.py#L99)

## 旧运行兼容

已结束运行仍通过原查询读取原输出和事件。对于仍活跃的旧周期，没有绑定且已经产生同周期检查点、模型尝试或运行工作事件时，进入 `NEEDS_REVIEW/RECOVERY_CONTEXT_UNBOUND`，发布内容为空。无持久工作的排队或刚领取周期可以首次绑定。等待补证的旧运行可以显式进入新周期，前提是检查点仍是相邻旧周期的补证暂停，且新周期尚无持久工作。新周期随后绑定其新业务事实。

## 回归矩阵

| 情况 | 预期 |
| --- | --- |
| 相同业务上下文下失租后返回完整响应 | 新 owner 复用一次调用，校验后结束，原 worker 保留用量 |
| 终态检查点已保存，其他工单改变账本 | 待复核且发布内容为空，保留旧检查点与尝试审计 |
| 同工单及订单版本未变，跨案账本状态或资格变化 | 摘要识别变化 |
| 业务上下文在周期开始后、发布前变化 | `FACTS_CHANGED_DURING_RUN`，发布内容为空 |
| 无绑定的旧活跃周期有检查点、尝试或工作事件 | `RECOVERY_CONTEXT_UNBOUND`，无新增调用 |
| 旧运行处于相邻周期补证暂停，且新周期尚未执行 | 新周期成功绑定并按新材料继续 |
| 模型缓存格式或当前工具、原文校验失败 | `REPLAY_VALIDATION_FAILED`，无自动重发 |
| 查询已结束的历史运行 | 原输出与事件逐项相同 |

实现见 [运行服务](../backend/src/aftercare/intelligence/service.py) 和 [模型缓存复核](../backend/src/aftercare/intelligence/models.py)。

## 实际验证与部署

完整后端纯回归482通过，前端24项通过；[真实PG租约组合复验](../reports/inflight-lease-context-recheck.json)两分支通过，[新周期HTTP/Celery绑定](../reports/context-binding-http.json)持久化确认且0模型请求。[体验部署](../reports/deployment/recovery-context-app.json)已更新至acceptance-20261110-context，37张public表摘要不变、历史run可读。兄弟退款漂移修复回归使用真实业务服务/SQLite/内存检查点/模型替身，不能外推为真实提供方中断质量实测。原失败报告保留。
