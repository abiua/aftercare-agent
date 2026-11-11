# P2 会话调查调度与调用审计

本阶段提供持久任务、预算和发布栅栏。视觉适配、HTTP/队列注册和迁移由集成层接入。模型调用按一次逻辑尝试登记，并在发出请求前提交预留。

- `AnalysisService.stage(db, conversation, input_from_seq=...)` 与消息最终提交共用事务。调用方先锁定会话行，完成消息序列和业务范围变更后登记。服务端快照保存 owner、revision、活动工单与订单及商品范围摘要、有序已提交附件及其摘要。调用方指定当前事项的起始序列，包含该事项允许分析的历史附件，旧事项不进入输入。首条待处理输入设硬截止，后续输入只重置静默期。相同 revision 和输入幂等返回原任务。
- `invalidate(db, conversation)` 用于换事项或接管，调用方先锁同一会话行。公开发布另检查当前 owner、revision、范围、分析代次、OPEN 状态、任务租约及期限。系统回执可以增加消息 sequence，但不应推进 revision。事务内的 `emit(db, conversation, observations)` 只写公开投影。
- `due()` 查询可领取的任务 ID。队列重复投递通过 `claim(turn_id)` 的会话/任务锁及随机 lease token 处理。到静默点且无可信待完成上传，或到硬截止，可以领取。已暂存但未公开的客户消息只在对应事项下等待，等待时间由服务端首次登记时间限制。未进入暂存的网络上传由静默期覆盖。未准入的过期租约可以续领，已开始的过期租约进入 UNKNOWN，保留预留，禁止重复发送。
- `reserve(turn_id, token, reserve_microusd, model_evidence)` 在锁住会话后核对当前任务及权限，并在同一事务持久记录一条 STARTED。按会话累计调用与整数微美元入账。未知保留原预留，若已知费用高于预留则按较高值计。只有成功取得 attempt ID 的调用者可以发起一次传输，传输层必须关闭 SDK 重试。
- `finish_attempt(attempt_id, status, usage, estimated_cost_microusd, response_evidence)` 记录实际响应或未知结果。迟到结果可更新审计，不能重新获得发布权限。`publish(turn_id, token, observations, emit)` 仅使用已完成调用，观察保存来源附件和谨慎表述，客户消息由固定公开投影生成。观察不写业务事实、退款、审核或材料类型。确定性回执和转人工保持独立。
- 建议部署参数静默 2–3 秒、普通聚合硬截止 10 秒、真实在途上传最长 30 秒。此模块的静默及最长等待由部署配置，计时使用服务端时钟；线上费用和调用次数另配。根代理的真实验收限制为合计 4 次、估算合计 0.10 美元，此限制由验收入口执行。

数据库表、调度状态和观察均需要随后加入正式迁移、备份与恢复范围。模块测试使用独立受控数据库及合成元数据。

## Worker 接线

1. 调用 `claim(turn_id)` 获取 `TurnLease(turn_id, token)`。随后用 `input_snapshot(lease)` 读取冻结来源和作用域。下载图片时沿用会话所属客户和原消息附件授权，并把实际读取的字节交给视觉预处理。
2. 对有照片的任务，构造 `ChatAnalysisAudit(service, lease)` 并传给现有 `vision.observe`。适配器复用 `ProviderLimiter`，将调用估算向上取整到整数微美元。持久调用记录唯一绑定一个任务。同一会话和 revision 也有数据库唯一约束，因此重新部署、模型注册表调整或转换版本变化不会重开旧的未知调用。真实费用高于预留时记 `budget_violation`，该会话之后拒绝模型准入。
3. `publish(turn_id, token, observation_result["observations"], emit)` 接收视觉适配器结构化结果，逐项核对实际发送图摘要、原图和逻辑调用。只接受 `MODEL_OBSERVATION/UNVERIFIED`，用固定词表从候选观察生成公开摘要。传给 `emit` 的数据仅含 `attachment_id, certainty, summary`。原图、变换摘要、候选类型和内部观察留在审计。程序补证可通过 `publish_status(lease, emit)` 在同一栅栏内写公开状态。
4. 失败时用 `fail_turn(lease, reason)` 记录当前任务失败，已有 STARTED 标为 UNKNOWN。Beat 可用 `expire_stale_attempts()` 清理过期的已开始尝试。旧任务可以记迟到用量，不能修改公开消息。若需要前端实时状态、自动补证、重试期限提示或故障转客服，集成层按公开契约投影并保留每个缺口的去重记录。

本模块验收涵盖受控时钟、独立 SQLite、一次实际 ModelRunner 的受控传输和原子回滚。PostgreSQL 并发语义、工作进程接线、照片对象读取及真实模型传输仍需集成验证。
