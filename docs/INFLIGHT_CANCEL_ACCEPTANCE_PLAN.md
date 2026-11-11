# 在途模型请求、取消与晚到用量验收计划

首次真实模型路径已于 2026-11-10 执行并通过，见[实际报告](../reports/inflight-cancel-http.json)与[同标识独立读回](../reports/inflight-cancel-http-readback.json)。受控租约交错的两分支也已在真实PG验收（晚到分支额外终止专属数据库持锁连接）；真实提供方与强制失租的组合未测。详见文末实际报告。以下保留实施前的证据边界：已有 HTTP 取消报告使用 PostgreSQL 和 inline 受控门禁，模型尝试数为 0。Worker 强杀报告验证真实租约接管，模型同样关闭。真实流式复验的四次调用都正常完成。三份证据各有覆盖范围。[现有取消](../reports/evaluation-load-cancel-http.json) · [Worker 强杀](../reports/worker-crash.json) · [真实流式复验](../reports/streaming-compatibility-wire-recheck.json)

## 现有接口与约束

| 路径 | 当前行为 |
| --- | --- |
| 评测取消 | tester 通过 `POST /api/evaluation/experiments/{id}/cancel` 提交 `expected_version` 与 `Idempotency-Key`。事务标记实验 `CANCELLED`，未启动结果转 `NOT_RUN`，运行中的结果等待同一 Worker 收尾。队列仍将有 RUNNING 结果的取消实验视为可收尾。 |
| 单独运行取消 | 当前创建者或 support 通过 `POST /api/cases/{case_id}/runs/{run_id}/cancel` 提交案件版本和幂等键，运行转为 `CANCELED / USER_CANCELED`。本次选择评测入口，独立 run HTTP 取消另计。 |
| 模型与任务围栏 | 审计先提交 `ModelAttempt STARTED`，再发起真实模型请求。入站模型响应先记录已发生的用量和估算成本，再复验任务租约、运行状态和评测取消状态。后续模型尝试、工具／结果发布受围栏约束。Worker 的租约为 60 秒，约每 20 秒续租。 |
| 晚到与失租 | 旧 Worker 即使已失租，`finish_attempt` 仍可完成同一已存在尝试的用量审计，且不写旧 Worker 的运行事件。后续图状态和评测结果发布由有效租约约束。若接管者先恢复评测快照，旧响应稍后写入原始隔离审计，现有代码无自动回填已发布报告的路径。验收须分别保留两份读数。 |

[评测接口](../backend/src/aftercare/evaluation/api.py) · [评测取消和准入](../backend/src/aftercare/evaluation/service.py) · [运行接口](../backend/src/aftercare/intelligence/api.py) · [尝试审计](../backend/src/aftercare/intelligence/service.py) · [模型响应流程](../backend/src/aftercare/intelligence/models.py) · [队列交付](../backend/src/aftercare/queue_delivery.py) · [心跳](../backend/src/aftercare/delivery_worker.py)

## 最小真实路径

在专属 test API、Redis 与 Celery Worker 上创建单个 queued 评测，使用真实 test PostgreSQL、S3 与已授权模型配置。选开发案例 `AC02-C081`，单 Agent，固定 `analysis`，1 次重复，1 个并发，`source=live`，`probe_intent=true`。预期第一逻辑尝试是 `evaluation:intent_probe`，通过真实 Responses 调用，随后本可进入售后图。当前 `ModelRunner` 对探针也使用注册表的原生流设置。两种传输各自记录响应交付边界，结论不外推到真实 SSE 断流。案例事实及输入为模拟，模型请求为真实。

请求体 `max_calls=2`、`budget_usd=0.10`。在启动前核对冻结价格、候选、配额和首个尝试的预留。2 次的业务配额保留下一次尝试的空间，避免把零新增请求归因于配额已用尽。私有探针另设 1 次真实出站硬上限。若生产代码尝试第二次出站，探针在网络前拒绝并标为失败。总量低于单 run 12 次和 0.10 美元估算预算。只创建一个付费实验，固定的评测输入和取消请求各有一个幂等键。

### 触发与证据

1. 默认只做预检。确认测试目标、代码／模型快照、真实队列唯一私有 Worker、无其他待执行任务、已有报告及状态文件不会覆盖。先以不联网替身验证探针接在 OpenAI SDK 的实际 HTTPX2 传输入口，真实执行须单独显式授权。
2. 显式执行时先写权限 0600 的状态文件和报告，再发实验创建请求。逐步保存实验 ID、结果 ID、`eval_<result UUID hex>`、案例／run／thread ID、任务 ID 和创建及取消幂等键。创建响应丢失时按原键读回或查持久变更记录，状态不明时只读核对。
3. 等到同一租约的任务为 `RUNNING`，该评测唯一的结果为 `RUNNING`，隔离库的首条尝试为 `STARTED`，且有同一请求的真实官方 HTTPS 响应头或等价的已送达证据。记录请求方法、模型、发送的输出上限、序号和 HTTP 状态，不保存 Authorization、原始请求体或私有端点。只有 `STARTED` 或客户端准备发送都不足以触发取消。
4. 首次验收使用单次短门禁。非流式在真实 HTTP 200 响应头到应用读取响应体之间暂停。原生流等待同一真实 `response.completed` 到达，在交给适配器转换前暂停。此时记录真实传输已发生、应用仍在等待、原租约有效和终态用量尚未写入。门禁最多 8 秒，到期自动释放。门禁可证明应用在途及晚到审计，服务方开始或结束生成的时刻仍不可知。
5. 门禁保持期间，从最新实验读回取版本，发送一次取消并等待 HTTP 返回 `CANCELLED`。确认原任务仍持有同一有效租约和同一模型尝试，立即释放门禁。若门禁先超时、用量先写入、取消未确认或缺少实际出站证明，记录 `NOT_OBSERVED` 或 `UNKNOWN`。本轮不新建第二个实验追结果。
6. 继续轮询同一实验、结果、原始模型尝试及任务，直到结果离开 `RUNNING` 且任务终态或达到固定时限。网络响应慢或取消请求结果不确定时，继续等同一个 handle，核对实验与审计记录。Worker 仍在心跳时保持原进程。只有独立的强制失租试验才改变租约。脚本不得重启 Worker、重新提交模型运行、换用另一个模型或追加新付费实验。

### 判定

- 必须观察到取消事务先提交、同一真实尝试随后完成审计。该尝试可为 `SUCCEEDED`。如果原生流在发布缓冲文本时察觉取消，也可为 `UNKNOWN` 且保留真实终态用量。内层运行应为 `CANCELED / EXPERIMENT_CANCELLED` 且输出为空。外层实验保持 `CANCELLED`，原结果保留取消原因。不得出现后续模型尝试、工具执行或业务写入。
- 比对取消前后任务的 owner 与 generation，记录租约到期时间、claim 数和终态。有效租约下的正常收尾预期原任务仅一次领取，晚到审计不能将取消结果恢复为成功。
- 对比隔离 schema 的原始 `ModelAttempt` 与评测报告同一 `run/cycle/logical_id/ordinal` 的状态、用量和费用。终态用量未到、连接中断或本次审计只见 `STARTED` 时，保留 `UNKNOWN`、`usage=null` 和 `estimated_cost_usd=null`，另列已预留金额与已观测调用数。已知估算成本与未知尝试分开记录。
- 截止后仍在运行则报告未观测终态，并提供只读继续命令和持久 ID。不能用等待超时推断没有费用。报告区分应用收到的真实响应、受控交付时间和取消是否自然撞上模型生成。

## 强制失租另测

当前 60 秒租约与约 20 秒心跳会主动续期。短时自然取消无法可靠覆盖旧 owner 被替换。先在独立 PostgreSQL／Celery 验收中用受控响应和只对本次任务生效的短租约或停心跳钩子，让接管顺序可重现。旧 Worker 必须存活且其原响应保持在门禁中，禁止用 SIGKILL 代替晚到响应。接管者运行同一个 job generation 与 experiment/result ID。此阶段使用替身响应，真实付费预算维持一个 run。

分别取两种顺序的证据。旧尝试先写入审计时，接管者应能读到已知用量。接管者先收尾时，评测快照可能保留 `UNKNOWN`，之后旧 Worker 只能更新原始尝试；报告注明快照与原始审计不一致，并关联晚到读数。若要求前台自动反映晚到用量，需另立回填需求和验收。两种顺序都检查旧 owner 无新尝试、无图／事件或评测结果改写，实验保持取消，接管未重放原请求。该测试只验证受控租约交错。真实提供方与强制失租的组合属于单独试验范围。

## 拟交付脚本

- `scripts/verify_inflight_cancel_http.py` 负责只读预检、真实评测创建与取消、按 ID 观察和报告。默认只读，实际执行要求 `--execute-paid` 及新报告路径。独立私有状态先占用，后续只允许读回。
- 同一脚本可提供私有 Worker 入口或配套探针，只拦截本次评测的首个请求和响应。它使用生产队列、模型适配与租约路径，记录可放行的门禁标志和超时，拒绝其他业务流量。门禁对照与可控租约试验分别报告。首次实施前核对 Worker 中的模型冻结配置和 test 服务隔离。

首条路径已实现。传输探针可通过 `--fixture-check` 运行零网络检查，真实验收需显式选择执行模式和新报告路径。强制失租留作单独试验。

## 首次实际执行结果（2026-11-10）

实验 `cca67581-c261-4067-b8cc-225ebb31ac82`，开发案 AC02-C081。真实 Sol Responses 原生流仅一次出站，HTTP 200。提供方终态事件到达后，在适配器转换前门禁暂停；取消于 11:25:08.991592 UTC 提交，11:25:09.019799 放行，11:25:09.043603 原尝试完成审计。七项检查均通过，另通过 HTTP/原始数据库只读回查同一标识。

外层 CANCELLED，结果 ERROR/EXPERIMENT_CANCELLED，运行 CANCELED、output=null。原尝试状态 UNKNOWN/EXPERIMENT_CANCELLED，但真实用量完整（输入64、输出32、总计96），估算费用0.000448美元，与发布快照一致，未知费用尝试0。UNKNOWN 表示此次尝试因取消未正常发布，不表示这条用量缺失。无第二次出站或工具执行；方案、审批、执行账本计数均保持0。这不是全库行内容不变的证明。

同一 owner/generation，仅一次领取，任务 DONE，无租约失效。专用 Worker 确认终态后退出；同标识回读确认终态后恢复常规测试 Worker。实际暂停约87毫秒，门禁上限8秒。此结果证明受控应用响应交付顺序下的取消与晚到记账，不证明自然断流、服务方生成中断或强制失租。

恢复约束：如创建响应丢失或专用 Worker 退出，必须先通过原幂等记录/同标识读回确认实验终态或取消，再恢复常规 Worker。常规 Worker 不携带本次探针的一次出站限制，不得接手状态不明的测试实验。


### 2026-11-10 失租晚到用量与补充审计

新增测试员专用、仅测试环境只读接口 GET /api/evaluation/results/{result_id}/supplemental-usage。以完整run/cycle/logical/ordinal匹配冻结快照与隔离原始审计，仅返回状态、数值用量/费用、获取及结束时间。原始回复、密钥、模型原文不返回。两侧未匹配记录分别计数；找不到schema/账本/可用快照明确不可用，不将缺失记零。原actual/trace/metrics/gate保持冻结。前端按需展开并逐次对照，保留0.00002美元精度，不将部分晚到费用当实验总额。

root完整后端纯回归471通过、1跳过、11未选；前端23项纯测试、lint/TS/build通过。真实PG两种交错通过，旧owner不能继续图/事件/检查点/报告写入，同一请求未重放。用量先到：恢复快照已知；恢复先到：快照UNKNOWN/null保留，原审计及补充视图显示受控20tokens/0.00002美元。后者明确额外终止本实验精确PID+advisory key的数据库连接，不能称单纯租约过期；两者使用受控租约时钟、内存检查点及模型替身，不涉及模型费用。隔离内层run保持RUNNING，是既有恢复策略的保留状态，外层CANCELLED、结果ERROR/WORKER_INTERRUPTED；不声称内层也取消。两次随机schema/用户/任务均清理验证。证据 reports/inflight-lease-pg.json、inflight-lease-pg.log。

测试API/Worker/Beat已更新恢复（PID43426/43438/43450；session93282/90526/96418），实际HTTP tester200且no-store、匿名401、客服403、未知result404，readyz200/Worker响应正常。测试UI5187新资源index-GDp-4_pu.js已读回SHA验证。真实浏览器只读展开既有fixture单案补充视图，显示获取时间与未发现差异，实际GET200、0写请求；晚到非空行的正例是PG服务测试，未冒称浏览器正例。捕获保存私有0600且会话zyif已结束。体验UI/API5186/8020仍是旧citations镜像，本轮未更新。[综合证据](../reports/deployment/supplemental-usage-test.json)。独立人工质量与其他待验项不变。
