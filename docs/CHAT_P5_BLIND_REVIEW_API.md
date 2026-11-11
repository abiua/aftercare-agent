# P5 独立盲审 API v1

第一批交付盲审存储与权限。内部入口 `BlindReviewService.assign` 从持久原始 Agent 快照登记同一冻结输入、指定测试评审员并锁定候选集合。聊天评测运行器尚未接入。普通用户和评审员无法自行创建任务、提交候选或把复核升级成正式金标。服务仅在 test profile 开放，`GET /api/evaluation/capabilities` 提供 `chat_blind_review`。评审者只能读取分配给自己且仍启用的任务。所有响应禁止缓存。

## 读取

`GET /api/evaluation/chat-blind-reviews?after=<uuid>&limit=25` 返回 `{items,next_after}`。列表项是 `{id,case_label,input_sha256,status,version}`，阶段为 `BLIND`、`SEALED`、`REVEALED`、`LABELED`。游标为空时到达末页。

`GET /api/evaluation/chat-blind-reviews/{id}` 返回列表项及以下字段：

- `input` 含 `schema_version:"chat-blind-input-v1"`、`intent`、`messages:[{id,sequence,text}]`、`images:[{id,message_id,sha256,size,content_type}]`、`facts:[{id,kind,trust,data}]` 和 `policies:[{id,version,title,text,parameters,conditions}]`。事实 ID 形如 `ORDER_LINE:L01`，订单行在 `data.line_id`，`trust` 为 `VERIFIED_RECORD` 或 `UNVERIFIED_OBSERVATION`。同一任务始终返回同一冻结摘要。图片使用任务内资源，登记时核对字节与摘要，单任务原图总量上限 32 MiB。实际材料审核作为事实记录，包含材料范围、版本、状态、时间和是否有效，不显示审核者或备注。条款 `conditions` 只展示条款原有的时效、商品范围、优先级等字段，不展示程序算出的适用商品列表。候选、架构、模型、客服处理决定、存储地址和内部跟踪始终不进入 `input`。历史 P3 快照的 `policy_coverage` 为 `SCOPED_ONLY`，只归档当时选取的条款版本，`policy_context` 为空。具备完整目录的快照标为 `COMPLETE_CATALOGUE`，`policy_context` 提供冻结商品分类、时间与条款默认值。
- `initial` 为 null 或 `{sha256,created_at,payload}`。读回自己已经封存的初判。
- `exposure` 为 null 或 `{sha256,created_at,candidates}`。只有显式揭示提交成功后出现。每项候选为 `{id,status,options,unresolved}`，id 是盲审任务内的不透明标识，状态为 `AVAILABLE` 或 `UNAVAILABLE`。当前原始 Agent 快照入口只登记 `AVAILABLE`，未运行或未输出的行待评测运行器接入。选项为 `{id,action,lines:[{line_id,amount_cents?,quantity?}],required_evidence,evidence_ids,policy_ids,rationale,alternatives,evidence_references,policy_references}`。引用原文 ID 保留，辅助解析为 `{id,status,resolved_ids}`，状态为 `RESOLVED` 或 `UNRESOLVED`。含多个冻结版本的裸条款 ID 无法确定具体版本时仍标未解析。`effective-policy-snapshot` 指向该快照实际选中的条款版本。
- `labels` 为 null 或 `{sha256,created_at,payload}`。后置标注与初判分别保留。

`GET /api/evaluation/chat-blind-reviews/{id}/images/{image_id}` 通过任务权限读取原始图片，验证冻结大小及 SHA。任务或图片无权访问统一返回 404。

## 写入

各 POST 需要 `Idempotency-Key`，请求含整数 `expected_version`。返回同一详情形状。同键同体重放原响应，同键不同体、过期版本和错误阶段返回 409。

- `POST /{id}/initial` 接收 `{expected_version,judgment,acceptable_plans,required_fact_ids,policy_assessments,notes}`，`notes` 可省略。初判为 `DECIDABLE` 或 `UNDETERMINED`。每个可接受方案包含 `action`、`lines`、`required_evidence`，动作限 REFUND、REPLACE、REQUEST_EVIDENCE、NOT_SUPPORTED、HUMAN_HANDOFF、STATUS_ONLY。每条行包含 `line_id`，退款还须包含 `amount_min_cents,amount_max_cents`，换发须包含 `quantity`。非执行动作可提交空行数组，`required_evidence` 为需要补查内容的文本数组。方案可以有多条、多种动作。事实引用使用冻结 `facts[].id`，条款评定为 `{policy_id,version,status}`，状态为 APPLICABLE、NOT_APPLICABLE、UNKNOWN。初判一次性封存。
- `POST /{id}/reveal` 接收 `{expected_version}`。必须已封存初判且内部已登记候选集合。事务内先记首次曝光与两份摘要，再返回候选。常规 GET 无揭示副作用。
- `POST /{id}/labels` 接收 `{expected_version,items:[{candidate_id,decision,notes}]}`。结论为 ACCEPTABLE、UNACCEPTABLE、UNDETERMINED。覆盖本次揭示的全部候选，明确无可判输出的候选只能记 UNDETERMINED。该提交不会改写初判。

该审计记录能证明本应用指定入口的判断和曝光顺序。评审者是否真的独立、是否曾在应用外接触样本，仍需正式治理流程核对。本批不运行评测，也不填充人工金标。
