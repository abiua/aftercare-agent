# 智能工作流、模型与政策检索

2026-11-10。实现位于 `backend/src/aftercare/intelligence/`。本页区分受控验证和需由集成环境验证的能力。

## 业务入口和控制边界

- `POST /api/cases/{case_id}/runs` 创建持久运行，参数含 `expected_version`、`mode`（`single` 或 `multi`）、`strategy`（`routed`、`economy` 或 `analysis`）以及可选 `message`。`GET` 返回该工单运行列表。
- `GET /api/cases/{case_id}/runs/{run_id}` 返回运行与草案。 `POST /resume` 在业务版本推进且运行暂停时恢复，`POST /cancel` 撤销待运行、运行中或暂停的运行。三个变更接口都要求 `Idempotency-Key`。
- `GET /events?after=N` 补读 JSON，`GET /stream?after=N` 实时补读 SSE。SSE 优先采用 `Last-Event-ID`，断线重连仅查询事件，并在长连接内重复验证身份和案件访问权。
- 多角色的协调、政策和核验分别是 LangGraph 节点，有各自的上下文、模型调用、结果和审计。规则模式逐节点运行，轨迹明确标出来源为 `rules`。单 Agent 基线使用同一组只读工具、模型和预算上限；流程依次处理案件理解、政策与方案核验。
- 角色意见冲突、引用缺失或业务规则拒绝可返工一轮。仍有冲突时转人工，候选不会成为可用草案。草案调用业务服务的只读 `validate_draft`。客服提交方案、审批、执行走独立业务 API。
- 用户补证后显式恢复。材料齐备且仍需人工审核时，原因是 `MATERIAL_REVIEW_REQUIRED`，审核并推进业务版本后可再次显式恢复。同一运行和检查点线程可跨多个处理周期，人工等待不计入下一周期的主动模型预算。

## 部署与恢复

根装配提供 `AgentService(engine, settings, business, checkpointer, dispatch=None)`，注入 `app.state.agent`，挂载 `aftercare.intelligence.api.router`。工作进程持有 `PostgresSaver`，部署时调用其 `setup()`。Celery 处理函数调用 `AgentService.process(run_id)`，扫描 `pending(limit=100)` 可找回入队失败、工作进程中断及重复投递的运行。运行状态和事件序号同事务提交，单运行使用 PostgreSQL advisory lock 防止两个 Worker 同时推进。对外模型调用期间不持有数据库事务。

模型尝试单独审计。工作进程在完整响应已经入库后重启，可以从已校验结果继续。只有开始记录、无完整响应的尝试变为 `UNKNOWN`，保留未知用量并转人工。执行中取消可能仍收到已经在途的模型响应，尝试记录继续保留，晚到结果无法改变已取消状态。挂起状态和角色轨迹仍属运行证据。

## 模型配置

`Settings.model_enabled=false` 时使用规则模式。开启时从 `model_config_path` 读取 JSON，凭据由 `api_key_env` 引用受控进程环境。模型项包括 `alias/provider/model/endpoint/interface`（`chat` 或 `responses`）、工具和结构化输出能力、上下文、价格与数据范围。必须提供实际能力证据、版本和日期才可登记为 `VERIFIED`。显式验收用 `allow_unverified=true` 仍保留 `UNVERIFIED`。两别名指向同一实际模型配置会被拒绝。

路由先过滤能力、数据范围、上下文和成本配置。固定策略只用指定别名。规则路由可按任务选择并切换，单逻辑请求最多三次尝试，全周期最多十二次，默认最长一百二十秒。SDK 重试关闭。同模型技术重试和语义修复各最多一次，可切换一次，共用尝试上限。429 冷却在同运行处理周期内生效。超时且无用量时保存未知状态。计价按配置日期与每百万 token 单价估算，缺价格或 usage 时费用为 null。可用 `max_usd` 开启单处理周期估算成本上限。批量评测可额外设置 `max_run_attempts` 和 `max_run_usd`，同一运行补证后沿用总上限。缺 usage 时，以该次预留估算占用有上限的额度，原始用量和费用仍保留未知。

工具执行前校验名称、参数、调用编号与重复编号，只提供当前工单的已核验摘要和本次检索出的适用条款。适配器使用完整响应，工具回传保留调用编号。兼容 Chat 服务必须支持适配器使用的 Chat Completions 请求字段。

## 检索与政策

调用 `AgentService.index_policies()` 使用 B 的当前目录建索引。PostgreSQL 需预先安装 vector 扩展，索引表为 `policy_vectors`。设置 `embedding_enabled=true` 和固定 `embedding_model_path` 指向本机 BAAI/bge-small-zh-v1.5 快照。512 维归一化向量通过 pgvector 精确余弦排序，中文 Jieba＋BM25 产出词法排名，再用 RRF 融合。检索轨迹分别存词法、语义及融合结果和政策快照。向量未完整覆盖时显示 `INCOMPLETE_VECTOR_INDEX` 并以 BM25 降级。关闭嵌入时只运行 BM25。政策按时效、意图和版本过滤，模型引用需匹配编号、版本和原文片段；业务规则所需条款同时从适用快照补齐。

## 受控验证

- `pytest -q backend/tests/intelligence` 验证同线程暂停恢复、三角色及单 Agent 轨迹、候选过滤、预算、有界返工、拒绝越界草案、幂等和取消、未完成尝试的恢复语义、词法和融合排序。
- SQLite 与 LangGraph InMemorySaver 验证生命周期。Responses 与 Chat Completions 线协议由本地 HTTP 替身验证，包含同一工具调用编号的请求及回传，以及 SDK 用量解析。
- PostgreSQL 进程强杀恢复、实际 BGE 下载和向量索引、两个远程模型的真实适用性、浏览器及跨服务验收由根集成负责。受控结果不能代替这些验收。

面试复盘题：如果模型请求已发出，Worker 在写入完整响应前退出，为什么要将尝试标为 `UNKNOWN`，系统依据什么决定能否继续？

## 2026-11-10 追加实现

缺照片与待人工材料审核现由确定性节点先处理。同一运行补证到材料审核通过前，无需模型调用。已核验政策版本由 `resolve_policies` 按商品、意图、时基和优先级计算，结果包含各商品适用版本及统一哈希。业务层将该结果作为 `context.policy_resolution` 提供给图，检索仅搜索已解析的候选。引用同时绑定版本与原文。

启用原生流式需设置注册表 `native_streaming=true`，并为候选设置经验证的 `streaming`、`streaming_tools`、`streaming_usage`、`streaming_checked_at` 与 `streaming_compatibility_version`。Chat 与 Responses 的 SSE 通过本地 HTTP 替身验证。前端按 `logical_id/ordinal` 处理 `model_draft_delta`、`model_draft_committed` 和 `model_draft_discarded`。只有完整 provider 终态后的结构化输出才能触发工具。流中断且无最终用量时保留 UNKNOWN，部分文本作废。

启用共享限流前迁移需导入 `aftercare.intelligence.model_limits` 并创建 `provider_rate_scopes` 和 `provider_rate_leases`。注册表的 `provider_limits` 按 provider 指定无秘密的 `scope`、`max_in_flight` 和 `requests_per_minute`。同一数据库内运行及评测共享 public 账本。若两个部署使用同一供应商配额，须注入连接同一受控限流库的 `ProviderLimiter`，两边的作用域和上限保持相同。实际 PostgreSQL 双进程测试由集成方在完成迁移后设置 `AFTERCARE_INTELLIGENCE_PG=1` 运行。

客户自然语言入口由 `aftercare.intake.router` 提供，挂到 `/api`。`/intake/preview` 只浏览当前客户的订单并提示待确认字段，`/intake/confirm` 要求显式布尔确认、选定意图与订单版本，调用现有幂等业务服务。`/intake/capabilities` 报告规则与模型分类是否配置。结构化工单入口继续可用。

真实分类启用前迁移需导入 `aftercare.intake_audit` 并创建 `intake_model_audits`。装配方在有模型注册和供应商限流时注入 `app.state.intake_classifier = BoundedIntakeClassifier(agent.model, IntakeAuditFactory(engine, provider_limiter=agent.provider_limiter), max_usd=Decimal("0.02"))`，费用为可调的明确上限。用户必须显式选择模型分类并附请求键。模型只接收该段文字，独立审计最多登记一次请求。相同请求读回原结果，已发出且结果未知的请求不会自动重发。预览和分类均不创建业务工单。

旧冻结对照的七个 C001 失败保留。新受控用例验证前两个补证周期零模型调用，终态审核周期才调用角色。动作与订单行字段不一致现在返回具体修复字段，缺失引用时记录确切条款及版本。实际新配置质量仍由同预算复验确认。
