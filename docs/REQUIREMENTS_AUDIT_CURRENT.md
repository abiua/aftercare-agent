# 当前 U1–U6 需求与证据复核

2026-11-10。本次按[完整实施计划](FULL_IMPLEMENTATION_PLAN.md)和 Notion 的[首页](notion_snapshot/home.md)、[技术架构](notion_snapshot/architecture.md)、[阶段验收](notion_snapshot/stages.md)、[模型路由](notion_snapshot/models.md)、[数据与评测](notion_snapshot/evaluation.md)核对。旧项目报告用于解释要求，当前判断以重建后的代码和本轮证据为准。审计未启动共享服务、真实 API 或付费运行。

**已证明**表示有匹配该断言的实际证据。**局部验证**表示只有部分范围或受控替身证据。**实现待验**表示接口或代码已交付，仍缺相应环境的报告。**缺口**表示规定交付件或流程尚未闭合。人工判断另计。

## U1 最小 Web 底座

| 明确交付 | 当前代码与证据 | 判断 |
| --- | --- | --- |
| React/Ant Design、FastAPI、PG、锁定依赖和最小 Compose | [前端](../frontend/src)、[接口](../backend/src/aftercare/api.py)、[后端锁](../backend/uv.lock)、[前端锁](../frontend/package-lock.json)、[Compose](../infra/compose.application.yml)、[体验环境升级部署](../reports/deployment/app-acceptance-deployment.json)。 | 现有体验卷单独迁移到 head 后完成应用冷启动。API、Worker 和前端容器健康，Beat 正在运行。Nginx 同源 readyz 和静态资源均返回 200。较早的离线冷启动和启用 live-hybrid 的实际模型调用各有报告。随后上下文绑定版本部署后，同源 readyz 返回 200，应用 37 张 public 表全行摘要一致。前端使用 npm 锁。[在线部署验收](../reports/deployment/app-live.json) · [上下文绑定版本部署](../reports/deployment/recovery-context-app.json) |
| 客户、订单、工单、迁移与模拟初始化 | [初始化](../backend/src/aftercare/manage.py)、[迁移](../backend/migrations/versions)、[体验卷保留](../reports/deployment/app-upgrade-preservation.json)、[空卷验收](../reports/deployment/fresh-bootstrap-acceptance.json)。 | 体验升级后 30 张既有表原列行数和摘要一致，凭据保留，旧运行经 HTTP 回读仍为 WAITING_INPUT。另在隔离 Docker 资源验证空 PostgreSQL、空 S3 和无凭据文件时迁移到 head。生产 bootstrap 两次后表内容、凭据字节与权限及私有桶一致。生产 API 四角色鉴权成功，匿名返回 401，测试自有资源已清理。 |
| 身份认证、角色权限、本人归属、创建及刷新回读 | [鉴权](../backend/src/aftercare/auth.py)、[实际 PG](../reports/runtime-pg-tests.log)、[真实客户入口](../reports/intake-queue-http.json)、[真实客户浏览器](../reports/browser-intake.json)。 | 实际 PG 覆盖四角色、匿名和客户订单范围。真实 HTTP 证明订单澄清、本人确认、创建与启动幂等。真实客户浏览器也完成登录、本人订单选择、确认建单和详情回读。客户页面照片选择受工具限制，未发出材料上传请求。真实客服浏览器已显示合成照片，保存 NEEDS_MORE 材料审核并在刷新后回读相同意见、材料版本和有效标志；账本、方案和财务审批未变。独立的新案另在浏览器完成方案提交、审批和模拟执行，见 U3。客户通过浏览器上传文件仍待验收。[浏览器材料复验](../reports/browser-material-review.json) · [模拟财务闭环](../reports/browser-financial-flow-review.json) |
| 页面输入、列表、详情、异常和测试角色边界 | [工作台](../frontend/src)、[客户入口用例](../frontend/tests/intake.test.mjs)、[真实测试员浏览器](../reports/browser-evaluation.json)、[Playwright 用例](../frontend/tests/browser/specs/workbench.spec.mjs)。 | 真实测试员浏览器核对用例库及详情。4 项受控 API 的 Playwright 用例通过，覆盖登录错误及客服入口、客户澄清和同键重试、multipart 请求构造、测试员导出错误。受控上传验证的是浏览器请求构造，真实对象写入和全角色矩阵需分开验收。 |
| 基础 CI、格式类型、浏览器关键路径及调试 | [CI](../.github/workflows/ci.yml)、[本地检查](../scripts/check.sh)、[部署报告](AUDIT_DELIVERY.md)。 | 冻结安装、纯检查和独立 PG 入口已交付。历史宿主组合检查曾因按需加载依赖失败，干净 Darwin 与 Linux 前端通过证据独立。上下文绑定版本后端纯测试 482 项通过、1 项跳过、11 项未选中，前端 24 项纯测试及 lint、类型检查和构建通过。受控 API 的 Playwright 主路径通过。远程 CI 与 VS Code 断点仍待验收。[后端日志](../reports/backend-recovery-context-tests.log) · [前端日志](../reports/frontend-recovery-context-checks.log) |

## U2 单 Agent、补证恢复和事件

| 明确交付 | 当前代码与证据 | 判断 |
| --- | --- | --- |
| 案件、工单、运行和线程分离，LangGraph 真检查点 | [图](../backend/src/aftercare/intelligence/graph.py)、[运行服务](../backend/src/aftercare/intelligence/service.py)、[运行时](../backend/src/aftercare/runtime.py)、[检查点重启](../reports/process-restart.json)、[任务进程强杀](../reports/worker-crash.json)。 | 检查点报告证明原 case/run/thread 的 PostgreSQL 恢复，旧周期没有重做，并保留一次恢复期间轮询失败。真实 Worker 强杀后，同一 run/thread 恢复到 WAITING_INPUT，范围为关闭模型的缺证场景。原终态检查点发布旧账本的缺陷已在受控 SQLite、真实业务服务、内存检查点和模型替身回归中修复。真实 HTTP、Celery 和 PostgreSQL 的零模型 fixture 另证明新周期持久化一条上下文绑定。真实提供方在途失租并伴随业务漂移仍未验收。[原失败](../reports/recovery-context-drift-diagnostic.json) · [回归与部署](../reports/deployment/recovery-context-app.json) · [绑定写入](../reports/context-binding-http.json) |
| 缺证暂停、真实私有上传、同案恢复及权限 | [真业务闭环](../reports/business-full-flow.json)、[真 S3](../reports/s3-first-flow.json)、[最新入口](../reports/intake-queue-http.json)。 | 已证明缺证暂停、同一线程补证和人工审核后继续。最新真入口在缺材料前置门禁以零模型调用进入 WAITING_INPUT。旧版本、并发恢复及撤权按各自的后端和 SSE 证据核对。上下文漂移回归覆盖旧检查点、缓存复用和有持久工作的未绑定周期，范围为受控替身，见上一行。 |
| 自然语言入口、订单歧义澄清、确认及模型与规则边界 | [入口](../backend/src/aftercare/intake.py)、[入口审计](../backend/src/aftercare/intake_audit.py)、[真入口队列](../reports/intake-queue-http.json)、[在线意图分类](../reports/intake-live-classification.json)、[体验 Nginx 入口](../reports/deployment/app-acceptance-intake.json)。 | 实际 HTTP/Celery 记录了归属筛选、无效订单澄清、明确确认、重复确认同案与重复启动同 run。另一次真 HTTP 在线预判用 1 次模型请求识别货损，使用 106 tokens，估算 0.000146 美元。同键重放，未创建案件。升级后的体验 Nginx 入口另验证澄清、本人订单、确认和启动幂等，缺证进入 WAITING_INPUT，模型调用 0，队列任务领取 1 次并完成。分类质量仍需多案例评测。 |
| 单候选入口、能力和预算过滤、无候选停止 | [模型适配](../backend/src/aftercare/intelligence/models.py)、[真实兼容](../reports/model-compatibility.json)、[C001 单模型升级复验](../reports/post-upgrade-c001-single-analysis.json)。 | 已有固定单候选与可解释失败。当前实际候选为 Sol/Terra，历史首选 DeepSeek 的旧报告未计入。 |
| 持久事件、SSE 游标补发、刷新只读与终态 | [事件 API](../backend/src/aftercare/intelligence/api.py)、[页面协议测试](../frontend/tests/sse.test.mjs)、[真实业务运行 TCP SSE](../reports/sse-http.json)、[合成事件 TCP SSE](../reports/sse-active-synthetic.json)。 | 真实业务运行验证断连重连、顺序与限制、未来／无效游标、客户归属、令牌撤销后重连拒绝和只读回放零新增调用。该报告为 PARTIAL，已连接撤权和跨页边界各为 NOT_RUN。另以隔离 PostgreSQL 合成 405 条事件，在挂载生产路由和服务的最小应用中经真实 TCP 验证跨页续读到第 405 条及撤权后 1.004 秒干净闭合，重连返回 401。该验收范围限于 SSE，完整业务和浏览器断线补读仍待验证。 |
| 独立顺序 CLI、JSON 小样例、旧报告导入 | [CLI](../backend/src/aftercare/evaluation/cli.py)、[CLI 隔离验收](../reports/cli-inline-isolation.json)、[落盘报告](../reports/cli-inline-recheck.json)、[真实 HTTP 导入](../reports/import-retry-http.json)、[真实 HTTP 补跑](../reports/retry-http-fault-recheck.json)。 | 独立顺序 CLI 在真实 test PostgreSQL/S3 完成一例离线评测并落盘 JSON。Worker 和 Beat 活跃时，多个恢复周期后该实验仍无投递任务。真实 HTTP 导入验证回读、同键幂等、摘要错误 422 和客户越权 403。首次补跑的 409 保留，修复后由真实 HTTP/Celery 对历史 NOT_RUN 完成一例补跑，父实验及原结果链接正确，同键返回同子实验，父报告未变。真实测试员浏览器另完成一例开发 fixture 的 JSON 下载，文件与独立 HTTP 读回对象一致。浏览器导入受文件选择限制，未观察到导入 POST；CSV 的浏览器下载未在该报告验收。[浏览器 JSON 下载](../reports/browser-evaluation-create-export.json) · [浏览器导入受限](../reports/browser-evaluation-import-attempt.json) |

## U3 私有材料、审批和模拟执行

| 明确交付 | 当前代码与证据 | 判断 |
| --- | --- | --- |
| 私有 SeaweedFS/S3、格式与大小、归属、签名及版本 | [存储](../backend/src/aftercare/business/storage.py)、[业务用例](../backend/tests/business/test_business.py)、[test 签名自然过期](../reports/material-expiry-http.json)、[app 容器签名](../reports/deployment/container-material-link.json)。 | 真对象上传和字节一致回读通过。app 直接用外部 127.0.0.1:58350 签发，原始 URL 不改 Host 即可读取，签名和同源读取字节均匹配。app 与 test 匿名访问 403，配置 60 秒后约 65 秒实测过期 403。受控用例覆盖假图、超限、篡改及跨客户。合成图片经 HTTP 写入私有对象存储，客服已在真实浏览器读取照片。浏览器文件选择和上传提交仍待验收。[浏览器材料读取及审核](../reports/browser-material-review.json) |
| 人工材料意见独立，变更失效 | [业务服务](../backend/src/aftercare/business/service.py)、[完整闭环](../reports/business-full-flow.json)、[19 项正式回归](../reports/post-upgrade-fixture-regression.json)。 | 真闭环记录审核后继续。升级回归覆盖材料不足、同图重复及旧材料使方案失效，审核与财务批准分开。 |
| 订单行额度、冻结方案、独立审批、审批期限和版本重验 | [业务规则](../backend/src/aftercare/business/rules.py)、[账本模型](../backend/src/aftercare/business/models.py)、[真业务闭环](../reports/business-full-flow.json)、[回归](../reports/post-upgrade-fault-regression.json)。 | 已证明未批准拒绝、批准后模拟执行、额度与订单行关联。R016 注入时钟证明过期拒绝，受控用例补足订单与政策版本变化。真实测试浏览器由客服提交、独立审批人批准、客服执行，三次请求分别返回 201、201、200。独立读回确认 9,405 分整行模拟退款，方案、审批和账本各一条，账本 SUCCEEDED 并有模拟回执。材料及审核由 HTTP 预置。该浏览器证据仅覆盖单次成功路径，双击、并发与重放由独立后端测试覆盖。[浏览器验收](../reports/browser-financial-flow.json) · [独立审查](../reports/browser-financial-flow-review.json) |
| 不同键去重、同案并发、跨案余额、超时后核对 | [执行／核对](../backend/src/aftercare/business/service.py)、[真业务闭环](../reports/business-full-flow.json)、[5 项故障回归](../reports/post-upgrade-fault-regression.json)、[真实 PG 竞争](../reports/business-contention-pg-tests.log)。 | 同键重放与 R020 同一方案并发只产生一个执行 ID。真实 PG 三项独立测试通过，验证同一订单行两件合法案件争用退款额度、模拟支付回执丢失后 UNKNOWN 保持占用并按原键核对，以及两个订单争用同 SKU 最后一件库存。测试使用隔离 schema、内存材料和模拟支付，R021/R022 仍为受控故障注入。 |
| 统一程序保护及故障评分 | [隔离执行](../backend/src/aftercare/evaluation/runner.py)、[安全观测](../backend/src/aftercare/evaluation/observations.py)、[19 fixture](../reports/post-upgrade-fixture-regression.json)、[5 fault](../reports/post-upgrade-fault-regression.json)。 | 24 案实际完成，42 条确定性检查通过，0 条失败，24 案仍待人工复核。安全检查覆盖报告所列账本、行额度、库存、批准和已执行越权脚本。R023 保留一笔 UNKNOWN 模型尝试，费用仍未知。旧 24 案未运行报告另存。 |

## U4 三意图、语料和政策检索

| 明确交付 | 当前代码与证据 | 判断 |
| --- | --- | --- |
| 已下载资料、固定来源及许可 | [本地来源说明](</Users/mbw/Documents/简历/data/README.md>)、[固定清单](</Users/mbw/Documents/简历/data/manifest.json>)、[历史校验](</Users/mbw/Documents/简历/data/verification.json>)、[CDLA 存档](</Users/mbw/Documents/简历/data/licenses/CDLA-Sharing-1.0.html>)、[τ-bench MIT 文件](</Users/mbw/Documents/简历/data/raw/tau2_retail/LICENSE>)、[本次表达包校验](../data/corpus/verification.json)。 | 历史校验覆盖 12 文件、65,196,756 字节及 τ-bench 74/40 不相交划分。本次表达包重新核验所用 7 个来源文件的固定摘要，并存档 CDLA 与 MIT。Bitext 电商固定 revision 12dd624ddcd3057382b2faad661bcda1fa869491，44,884 条；τ-bench 固定 da0835f6c67baef58bdda18f2a3b037dfb3a1754，114 个任务；通用客服是可选来源。 |
| 使用资料筛选、中文改写、来源族划分和许可随附 | [来源要求](notion_snapshot/evaluation.md)、[公开表达包](../data/corpus/README.md)、[可复现核验](../data/corpus/verification.json)、[人审清单](../data/corpus/human-review.csv)。 | 已交付 Bitext 三意图各 40 条英文原句与标明 AI 的中文改写，保留逐条来源、固定 revision、许可及修改说明。9 个候选来源族按组划为 60/30/30。中英文归一化精确重复为 0，9,000 个跨组文本字段对未达到近重复阈值 0.68。120 条均待人工语义和分组复核，候选留出公开可见，正式封存待完成。表达包独立于 v0.2，τ-bench 只参考结构，公开答案和政策未纳入。 |
| 120 核心、三意图、60/30/30，另有至少 24 已知冲突 | [清单](../data/v0.2/manifest.json)、[数据测试](../backend/tests/evaluation/test_dataset.py)、[开发](../reports/dev-regression.json)、[验证](../reports/offline-validation.json)、[保留](../reports/offline-holdout.json)。 | 规格、可复现输入、隔离场景族及三类工作流已证明。核心 120 和升级后冲突 24 属独立批次。v0.2 人工金标数为 0，离线应用案例不能替代真模型质量评测。 |
| 条款化 BM25、BGE 中文 small、pgvector、RRF | [检索实现](../backend/src/aftercare/intelligence/policies.py)、[基线口径和未命中](POLICY_RETRIEVAL_BENCHMARK.md)、[真实 PG 配对报告](../reports/policy-retrieval-dev-v1.json)。 | 真实 PG 和本地 BGE 建立 15 个版本向量，完成 13 条开发查询的三基线配对和 1 条冲突范围检查。RRF 的 Recall@1 为 65.38%，高于语义的 61.54%。语义的 Recall@3／@5 为 96.15%／100%，高于 RRF 的 92.31%／92.31%。期望条款由开发者依据规则指定，独立人工检索金标及选型仍待验证。 |
| 发布状态、商品范围、时基、优先级、替代、来源与证据回指 | [政策解析](../backend/src/aftercare/intelligence/policy_versions.py)、[业务快照](../backend/src/aftercare/business/service.py)、[政策测试](../backend/tests/intelligence/test_policy_versions.py)、[升级回归](../reports/post-upgrade-fixture-regression.json)。 | 已接入共同解析器和业务版本绑定。政策未来生效、冲突、时点未知等有程序证据。不同商品版本冲突转人工。条件引用另按政策版本及文档摘要核对业务前提。真实空账本样本中 POL08 和 POL12 均标为前提未成立，未登记条件的 POL01 标为未评估；页面仍可查看原文，未满足的前提归为参考背景。其他正例、未知及旧报告兼容为纯回归证据。逐条人工引用支持与完整边界矩阵仍待完成。[真实条件引用](../reports/response-clarity-citation-http.json) · [浏览器读回](../reports/deployment/conditional-citations-deployment.json) |
| 错发、换发库存、退款进度和中台案例复核 | [业务规则](../backend/src/aftercare/business/rules.py)、[三意图队列](../reports/evaluation-smoke.json)、[中台浏览器](../reports/browser-evaluation.json)。 | 离线真实应用三意图跑通，浏览器能下钻用例、预期和实际检查。真实 HTTP 复验的错发缺证明确列出两类照片，模型调用为零；真实退款进度查询明确说明订单执行账本为空，历史累计单列。多意图真实模型理解、关键引用支持和独立人工复核仍待验收。[回复复验](../reports/response-clarity-http.json) |

## U5 路由、流式、任务和可恢复运行

| 明确交付 | 当前代码与证据 | 判断 |
| --- | --- | --- |
| 两个实际模型及兼容角色、规则路由、有限修复和备用 | [适配与注册](../backend/src/aftercare/intelligence/models.py)、[真实探针](../reports/model-compatibility.json)、[真实对照](LIVE_COMPARISON_RESULTS.md)。 | 两个实际模型已通过工具消息接口探针，均有实际业务调用。配置、错误字段、兼容候选、预算、缓存用量及计价入账。两模型来自同一提供方，证据范围限该部署。 |
| 故障分类、总预算、唯一重试层、限流和冷却 | [模型限流](../backend/src/aftercare/intelligence/model_limits.py)、[运行审计](../backend/src/aftercare/intelligence/service.py)、[智能测试](../backend/tests/intelligence)、[真实 PG 跨进程限流](../reports/provider-process-pg-tests.log)。 | 有按尝试与跨恢复配额、共享供应商作用域、备用兼容和未知用量。真实 PG 跨进程测试 1 项通过，两个 OS 进程以独立连接争用同一供应商并发名额，一方获准、一方被拒绝。体验 API 和 Worker 共享 app 数据库中的供应商限流，test 数据库独立。真实提供方 401/403、429、超时及服务异常的组合矩阵仍待核验。 |
| 原生流、工具参数拼接、断流时不跨尝试拼接 | [原生流](../backend/src/aftercare/intelligence/native_stream.py)、[首轮观测缺失](../reports/streaming-compatibility.json)、[真实传输复验](../reports/streaming-compatibility-wire-recheck.json)。 | 首轮完成原生文本、工具参数和用量检查，传输探针因监控了错误的 HTTP 客户端而保留 PARTIAL。复验在实际 HTTPX2 传输入口观测到 Terra 和 Sol 各两次请求，4/4 通过且均为 HTTP 200。每次序列化上限为 256 个输出 token，重试序号均为 0。估算合计 0.001689 美元，未知成本尝试为 0。另有一次真实 Sol 请求的在途取消验收。原生流终态到达后、适配器交付前受控暂停，HTTP 取消先提交，随后保留迟到用量，原尝试仍为 UNKNOWN/EXPERIMENT_CANCELLED。实际只发出 1 次请求，无工具或最终输出，估算 0.000448 美元。自然断流及真实请求叠加强制失租仍未验收。[在途取消](../reports/inflight-cancel-http.json) · [独立读回](../reports/inflight-cancel-http-readback.json) |
| Celery／Redis、事务任务、投递去重、租约及过期接管 | [持久投递](../backend/src/aftercare/dispatch.py)、[队列接线](../backend/src/aftercare/queue_delivery.py)、[真实 PG 15 项](../reports/queue-lease-pg-tests.log)、[升级后实际入口队列](../reports/intake-queue-http.json)、[真实 Worker 强杀](../reports/worker-crash.json)。 | 真实 PG 证明领取、过期接管与过期持有者围栏。真实 PostgreSQL、Redis 与 Celery 验收中，Worker 在 60 秒租约内被 SIGKILL，73.712 秒后另一进程恢复同一 run/thread 到 WAITING_INPUT，任务完成，旧投递重放无影响。模型关闭，尝试数为 0。取消与真实模型响应交错见上一行，当次租约保持有效。另以真实 PostgreSQL、受控时钟、内存检查点和模型替身验证两种失租与晚到用量顺序。快照先落库时保留 UNKNOWN 与空费用，晚到用量可由测试员只读查询；原报告、指标和发布门槛保持原状。该分支另终止指定 advisory lock 的数据库连接。真实浏览器仅验证补充审计空视图，非空证据来自 PostgreSQL 服务回归。[两种交错复验](../reports/inflight-lease-context-recheck.json) · [补充审计权限及页面](../reports/deployment/supplemental-usage-test.json) |
| 批量实验、预算与取消、负载和导出 | [评测服务](../backend/src/aftercare/evaluation/service.py)、[运行器](../backend/src/aftercare/evaluation/runner.py)、[负载与取消验收](../reports/evaluation-load-cancel-http.json)。 | 真实 HTTP 编排、Celery、PG 与 S3 完成两案并发，直接调用 BusinessService 的读取成功 10/10，P95 为 15 ms。两案均待人工复核，任务成功率 0/2。读时延统计范围为服务内部调用，HTTP 网络压力和浏览器负载仍待验证。独立真实 HTTP 取消使用 PostgreSQL 和 inline 受控门禁，首案 3 条持久审计保持一致，第二案未启动，同键取消幂等。该两案取消场景未经过 Celery 或真实模型。另有单一真实 Celery/模型请求受控取消证据，见原生流一行；两个报告各自保留执行范围。 |
| 备份恢复、Nginx、完整 Compose | [最新恢复摘要](../reports/backup-current-v2-summary.json)、[恢复明细](../reports/backup-current-v2-restore.json)、[创建初次失败](../reports/backup-current-create.log)、[备份测试](../reports/backup-current-tests.log)、[体验环境部署](../reports/deployment/app-acceptance-deployment.json)、[显式在线覆盖层](../infra/compose.live.yml)。 | 最新 v2 隔离恢复核验 4,261 张表和 217 个私有对象，覆盖入口审计、投递、导入、限流和两类迁移记录。首次一致性快照因 SQLSTATE 53200 失败，test 锁容量调整至 512 后完成。备份受控测试 31 项通过。体验环境先通过离线冷启动和 Nginx 验收，现已启用 live-hybrid。容器 API 调用 Terra 1 次，Worker 调用 Sol 2 次，估算合计 0.0060415 美元。Worker 一次领取，完成 STATUS_ONLY，检索为 HYBRID，案情、订单、账本、方案和审批快照不变。Documents 初次挂载读权重失败后，使用 34 个文件摘要一致的本机缓存，容器首次索引写入 14 条。[缓存核验](../reports/deployment/container-bge-cache.json) · [在线部署验收](../reports/deployment/app-live.json) |

## U6 对照、发布与可交付作品

| 明确交付 | 当前代码与证据 | 判断 |
| --- | --- | --- |
| 固定单 Agent 比较三策略，固定策略比较单／三 Agent | [冻结方案](LIVE_COMPARISON_PLAN.md)、[原始结果与汇总](LIVE_COMPARISON_RESULTS.md)、[单策略比较](../reports/compare-single-economy-vs-single-analysis.json)、[组织结构比较](../reports/compare-single-routed-vs-multi-routed.json)。 | 36 次均完成，3 个独立开发案例在四条件各重复三次。记录 7 次确定性失败、29 次待人工复核、158 次模型调用、估算 0.50751960 美元。四条件人工通过均为零，能比较工程观察，不能推出质量增益。 |
| 更新后复验、保留失败和未知 | [C001 单分析](../reports/post-upgrade-c001-single-analysis.json)、[C001 单路由](../reports/post-upgrade-c001-single-routed.json)、[C001 三路由](../reports/post-upgrade-c001-multi-routed.json)、[压缩三 Agent 复验](../reports/compact-multi-recheck.json)。 | 两个 single 各 12 条确定性检查通过，仍待人工复核。原 multi 8 项通过、2 项失败，第三周期 MODEL_COST_BUDGET_EXHAUSTED。随后压缩版 C001 复验 6 次真实模型调用均成功，10 条确定性检查通过，估算 0.022897 美元，结果 NEEDS_REVIEW、发布门槛 INCOMPLETE。各次复验独立保存。 |
| 验证选型、冻结后正式保留集与可重复性 | [评测规范](notion_snapshot/evaluation.md)、[离线验证](../reports/offline-validation.json)、[离线保留](../reports/offline-holdout.json)、[冻结方案](LIVE_COMPARISON_PLAN.md)。 | **正式质量闭环未完成。** 离线保留集完成 30 案、160 项检查通过、模型请求为零，全部待人工复核。真实对照使用开发样本。尚无独立金标、验证阶段正式选型与阈值、在同一独立保留集的两组真实对照及配置交错证据。 |
| 可回溯指标、失败／未知、导出、人工意见和可比性 | [指标](../backend/src/aftercare/evaluation/metrics.py)、[门槛](../backend/src/aftercare/evaluation/gates.py)、[测试](../backend/tests/evaluation/test_metrics_and_gate.py)、[中台浏览器](../reports/browser-evaluation.json)。 | 当前实现区分意图探针、工具选择与参数、澄清与接管、引用适用与支持、草稿风险／拦截／最终账本、恢复和路由、分阶段时延及成本。显示分母和未知。真实支持性、事实覆盖与工具业务正确仍依赖标注。案例可回读，未完成配对隐藏比较指标。真实测试员浏览器创建一例开发 fixture，计划、启动、结束均为 1，5 项确定性检查通过，结果仍为 NEEDS_REVIEW。实际下载 36,543 字节 JSON，与独立 HTTP 读回对象一致。真实浏览器另已读取9/9开发配对、下载CSV并与独立HTTP逐字节核对，9行结果与原实验一致。第3次左右详情及基线深链接重载可见，其中一次人工协助后继续；此前自动点击无效原因未定，不称全自动路径通过。浏览器取消、导入、人工复核写入仍待验收。[对照与CSV验收](../reports/browser-comparison.json)。[浏览器创建与下载](../reports/browser-evaluation-create-export.json) · [实际下载](../reports/browser-evaluation-export.json) · [独立读回](../reports/browser-evaluation-export-readback.json) |
| 质量／费用门槛和人工发布批准 | [门槛配置及独立金标](HANDOFF_EVALUATION.md)、[门槛单测](../backend/tests/evaluation/test_metrics_and_gate.py)。 | 支持版本化阈值、独立人工证据摘要与技术 PASS/FAILED/INCOMPLETE。正例来自受控测试；项目实际验证阈值、人工金标、引用支持与最终发布批准尚未取得。 |
| 干净运行、3–5 分钟演示、架构图、失败案例及学习复盘 | [运营与演示](OPERATIONS.md)、[架构图](ARCHITECTURE.md)、[真实失败](../reports/live-smoke.json)、[恢复失败](../reports/backup-v2-restore-attempt1.json)、[交接](HANDOFF_INTEGRATION.md)。 | 有启动、角色、演示路径、架构图、操作说明和真实故障记录。当前未见一套计时的完整 3–5 分钟正式演示、最新逐阶段预测／复盘材料及用户理解实证。复盘本身不阻塞开发。 |
| Notion 更新并读回、保留历史 | [集成交接](HANDOFF_INTEGRATION.md)、[首页读回](../reports/notion-progress-readback.json)、[四主线页面读回](../reports/notion-four-pages-readback.json)。 | 首页进度区和四个主线子页面均已同步并读回，保留原有内容和历史标题。 |

主要未闭环事项仍为来源表达包的独立人工复核、金标与正式保留集质量验收，以及各表所列的浏览器、自然断流、真实失租交错和交付演示证据。发布结论以冻结配置、匹配环境、实际证据和独立人工批准为准。


### 2026-11-10 回复清晰度复验补充

[修复与静态审查](REVIEW_RESPONSE_CLARITY.md)后的前后端已部署到体验环境。[部署与真实浏览器证据](../reports/deployment/response-clarity-deployment.json)证明新空账本文案可见，范围限定为订单退款执行账本，历史累计另列；浏览器只读旧案，无新模型运行。后端454项纯回归通过。材料审核类型/版本与未查询的财务审批已在工具字段中区分，缺照回复按实际缺项生成。新最终模型回复已通过独立的[HTTP/Celery复验](../reports/response-clarity-http.json)：错发缺证0调用，退款查询2次真实Sol调用、估算0.00546美元，公开回复明确空账本，业务摘要不变；独立人工意见仍PENDING。[条件引用设计](CONDITIONAL_CITATION_PLAN.md)尚未实施，不将原文匹配当作条款前提或语义支持通过。


### 2026-11-10 条件引用验收补充

[条件核验实现](../backend/src/aftercare/intelligence/citation_conditions.py)与前端已部署，见[部署及真实浏览器报告](../reports/deployment/conditional-citations-deployment.json)。一条新真实模型退款查询实际引用POL08/POL12，两条在空的授权账本中均标为前提未成立；POL01无登记规则，保留未评估。原文保留、未满足前提的引文归入参考背景。2次调用、估算0.006944美元、业务摘要不变，[真实HTTP/Celery报告](../reports/response-clarity-citation-http.json)。仅证明已登记两条精确文档的空账本否定条件路径；正例/未知/旧报告兼容目前经纯回归，不能代替独立人工语义支持和整体质量通过。全后端纯回归464通过，前端19项及lint/build通过。

### 2026-11-10 真实模型在途取消补充

[实际报告](../reports/inflight-cancel-http.json)及[独立同标识读回](../reports/inflight-cancel-http-readback.json)证明：专用测试 Celery Worker 发出一次真实 Sol Responses 请求，在真实原生流终态事件到达后、适配器转换前受控暂停；HTTP 取消先提交，晚到用量随后记账。七项检查通过，输入64/输出32，估算0.000448美元，未知费用尝试0。运行保持取消、无最终输出，无第二次出站、无工具执行，方案/审批/执行计数仍为0。一次领取、同租约正常收尾，专用 Worker 退出并已恢复常规测试 Worker。原尝试 UNKNOWN/EXPERIMENT_CANCELLED 与完整用量并存，未改写为成功。范围限受控响应交付门禁，不包含自然断流或强制失租组合；详细边界见[验收计划](INFLIGHT_CANCEL_ACCEPTANCE_PLAN.md)。


### 2026-11-10 真实浏览器创建与下载验收

测试UI5187（当前生产构建预览）连接真实测试API8022。BrowserSkill会话 jiei 在新捕获 d5e7e6f3b1636 中登录测试员，通过表单仅创建开发案 AC02-C081 一次 fixture 实验。POST201后任务正常完成，详情显示计划/启动/结束1/1/1，单案5项确定性检查通过，NEEDS_REVIEW/HUMAN_REFERENCE_PENDING，0模型调用和0费用，人工复核历史为空。随后浏览器点击导出JSON，实际下载36543字节文件，与独立HTTP读回完整对象一致，SHA256 c78d55862b6577142d8f51e7588a037a92c823461bbed507b212f736f6413c66。实验 b8fe7add-7fd7-4495-b7b3-13b91bfd7a6a。

[验收报告](../reports/browser-evaluation-create-export.json) · [实际下载文件](../reports/browser-evaluation-export.json) · [独立读回](../reports/browser-evaluation-export-readback.json)。捕获已保存私有目录并设0600，会话已停止。之前会话uqxi已超时关闭，其未导出历史不作为成功证据。此次未执行浏览器取消/导入/人工复核写入，未生成质量金标，不代表整体发布通过。


### 2026-11-10 导入重复记录提示

前端新增保守去重说明：仅当提交SHA与先前观察、返回记录的SHA一致且记录ID相同，显示“已复用已有导入记录，首次登记的来源保持不变”。列表未覆盖或未知情形使用中性保存提示，不把未命中当新建。提示保留在导入详情，关闭清除。后端去重/人工评分契约不变。root独立运行20项纯测试通过，构建通过；新静态产物已更新测试UI5187，HTTP读回index-Bios4sqU.js SHA256一致。[测试日志](../reports/frontend-import-notice-tests.log) · [构建日志](../reports/frontend-import-notice-checks.log) · [测试部署](../reports/deployment/import-notice-test-ui.json)。体验UI5186仍为先前版本；导入实际浏览器上传受文件选择工具限制未完成，见[受限记录](../reports/browser-evaluation-import-attempt.json)。不得将静态部署或纯匹配测试称为真实导入验收。


### 2026-11-10 失租晚到用量与补充审计

新增测试员专用、仅测试环境只读接口 GET /api/evaluation/results/{result_id}/supplemental-usage。以完整run/cycle/logical/ordinal匹配冻结快照与隔离原始审计，仅返回状态、数值用量/费用、获取及结束时间。原始回复、密钥、模型原文不返回。两侧未匹配记录分别计数；找不到schema/账本/可用快照明确不可用，不将缺失记零。原actual/trace/metrics/gate保持冻结。前端按需展开并逐次对照，保留0.00002美元精度，不将部分晚到费用当实验总额。

root完整后端纯回归471通过、1跳过、11未选；前端23项纯测试、lint/TS/build通过。真实PG两种交错通过，旧owner不能继续图/事件/检查点/报告写入，同一请求未重放。用量先到：恢复快照已知；恢复先到：快照UNKNOWN/null保留，原审计及补充视图显示受控20tokens/0.00002美元。后者明确额外终止本实验精确PID+advisory key的数据库连接，不能称单纯租约过期；两者使用受控租约时钟、内存检查点及模型替身，不涉及模型费用。隔离内层run保持RUNNING，是既有恢复策略的保留状态，外层CANCELLED、结果ERROR/WORKER_INTERRUPTED；不声称内层也取消。两次随机schema/用户/任务均清理验证。证据 reports/inflight-lease-pg.json、inflight-lease-pg.log。

测试API/Worker/Beat已更新恢复（PID43426/43438/43450；session93282/90526/96418），实际HTTP tester200且no-store、匿名401、客服403、未知result404，readyz200/Worker响应正常。测试UI5187新资源index-GDp-4_pu.js已读回SHA验证。真实浏览器只读展开既有fixture单案补充视图，显示获取时间与未发现差异，实际GET200、0写请求；晚到非空行的正例是PG服务测试，未冒称浏览器正例。捕获保存私有0600且会话zyif已结束。体验UI/API5186/8020仍是旧citations镜像，本轮未更新。[综合证据](../reports/deployment/supplemental-usage-test.json)。独立人工质量与其他待验项不变。


### 2026-11-10 补充审计版本同步体验栈

已使用allowlist构建并部署 acceptance-20261110-supplemental：后端ab39676fdc9b9ba4244cf03fed8c2fdd56338e7cd0779548682d57cc91a964e4，前端2a2e46f15f8b039a70942d5bb0f8d1ac9c80fe9560df53c344408a8ec34ffaa4，JS index-GDp-4_pu.js。升级前app活跃run/job为0，保留数据库和私有卷，无bootstrap/迁移。37张public表前后行数和全行内容摘要相同。API/Worker/前端healthy，Beat running；同源readyz200、匿名401、客服身份200，历史条件引用run69314c17-76ea-4a94-8143-c0c0e6c3edd9仍COMPLETED可读。补充审计在app即使tester也按test-only返回403。runtime仍live，新增付费0。测试栈不变。[部署证据](../reports/deployment/supplemental-app.json)，构建和部署日志同目录supplemental-build.log/supplemental-deploy.log。此项证明部署与只读HTTP，不替代新模型/浏览器写业务验收。


### 2026-11-10 恢复上下文漂移缺陷（已复现，修复中）

独立纯诊断使用临时SQLite、真实业务/Agent服务、内存检查点和模型替身：查询案旧owner在终态checkpoint后、_finish前失去权限；另一工单按既有审批流程完成39800分模拟退款，查询案版本仍1但订单版本1→2，真实当前账本有SUCCEEDED。新owner同run/cycle直接复用终态checkpoint，错误发布ledger为空/历史退款0的旧查询输出，总模型替身调用仍1。另途中attempt复用可保留旧笔记/旧政策引用，但该分支最终账本可能由新事实重算，不混淆两条路径。财务旧方案仍被STALE_PROPOSAL/实时草稿校验挡住，未复现重复支付。[原始诊断](../reports/recovery-context-drift-diagnostic.json)。这是受控纯诊断，非PG或真实模型证据。周期可信上下文绑定与独立回归已开始，未完成前不能把该恢复路径视为通过；历史部署验收仅证明升级和只读路径。


### 2026-11-10 恢复上下文漂移修复验收

上述已复现缺陷现已修复并部署 acceptance-20261110-context。每周期在授权事实读取后、图/意图探针开始前，于租约和run行锁内保存仅含cycle/version/digest的context_binding事件。恢复任何checkpoint/缓存前比较可信完整事实摘要，含账本内容/资格/政策而非仅版本；发布前再比较。漂移→NEEDS_REVIEW/RECOVERY_CONTEXT_CHANGED，无输出，不自动重发模型。legacy有持久工作无绑定→RECOVERY_CONTEXT_UNBOUND；尚无工作可绑定；补证新周期合法绑定。cached result再次经过当前工具/引用/格式校验，失败REPLAY_VALIDATION_FAILED。相同上下文继续一次调用复用，费用审计不丢，已完成历史不改。

root完整纯回归482passed/1skipped/11deselected，包含真实BusinessService SQLite+内存检查点的兄弟退款终态checkpoint、途中attempt、同版本账本/政策变化及legacy/稳定恢复回归。原失败 reports/recovery-context-drift-diagnostic.json保留。真实PG租约两交错在新代码下2passed，reports/inflight-lease-context-recheck.json；受控模型/时钟/内存checkpointer边界不变。新HTTP→Celery→PG fixture唯一开发案运行完成，实际持久一条agent-context-v1绑定、0模型尝试，reports/context-binding-http.json；不是新真实模型质量评测。前端24项及lint/TS/build通过，三个原因有明确人工核对提示。

体验栈前后37张public表全内容摘要一致，旧run可读，API/Worker/前端healthy、Beat running、同源readyz200。后端镜像390d1eb21b66eb5a780150cfdd3a78263cf120daa77693d11cd0bb152e2ca81a，前端6e37079532857a4259ff3f47a55b772781ed00a555c205ba0090d797299df918，JS index-2JB6BMtz.js HTTP读回SHA一致。[综合部署验收](../reports/deployment/recovery-context-app.json)。test API PID46372/session26468，worker46384/session57680，beat46396/session38462，UI5187同新静态产物。未新增付费调用；未将纯漂移案例称为真实提供方/PG漂移实验，中文新错误分支尚无单独真实浏览器截图。整体人工质量/正式留出验收继续未完成。[设计与兼容](RECOVERY_CONTEXT_BINDING_PLAN.md)。


### 2026-11-10 真实浏览器单笔模拟退款闭环

测试环境5187→8022，新案3c7af7f2-42a5-4477-85df-48ed91368895，订单O0030，整行9405分。准备脚本仅创建新工单、两张可区分合成图片及材料审核；真实BrowserSkill页面由客服提交方案，切换审批人批准，再切回客服勾选确认并模拟执行，展开回执。页面已办结/已执行/已完成，sim-pay回执可见。独立HTTP读回case RESOLVED、ledger SUCCEEDED、订单版本1→2、累计退款9405分，方案/审批/账本各1、Agent运行0。无付费模型、真实支付或外发消息。

证据：reports/browser-financial-case-prepared.json、reports/browser-financial-flow.json、reports/browser-financial-receipt.txt；私有原始浏览器capture dba6ead199a5c保存在.runtime/browser-financial-flow.json（600），会话wnmq已停止。控件布局变化造成的点击失败经重新观察后处理；无网络重放规则。此项仅证明单次页面成功路径，未进行连续双击/并发执行试验，不替代已有后端幂等测试，也不充当人工质量金标。整体质量与正式留出验收仍待完成。

独立Sol6.1审查：reports/browser-financial-flow-review.json。38条捕获请求中仅3次POST（方案201/审批201/执行200），各一次真实页面点击，角色顺序support→approver→support；方案摘要与审批及执行绑定一致，执行位于审批有效期，无网络规则或重放。


### 2026-11-10 财务页面未知结果与重复提交保护

已修复同案/方案存在执行记录时仍显示“模拟执行”的问题，UNKNOWN/SUBMITTING只保留核对原记录；打开的执行弹窗也使用相同判定阻止过期提交。useActions同步锁保留，HTTP408/429不删除当前表单幂等键，网络提示明确仅未关闭且内容相同的表单重试沿用编号。跨关闭/刷新没有持久请求编号，不声称已实现。

root检查25项纯测试、lint/TS/build及7项受控浏览器测试通过。新增3项包括UNKNOWN与SUBMITTING刷新后只有reconcile POST，以及同一DOM任务两次requestSubmit/在途再次submit只发一次，受控首次提交后断线→手动429→手动成功三次请求body/key保持相同。模拟API的这些检查不代替真实网络故障和后端幂等证明。日志reports/frontend-financial-retry-browser.log、frontend-financial-retry-pure.log；审查docs/FINANCIAL_RETRY_UI_REVIEW.md。

测试UI5187已同步新静态产物index-CJh7XIRu.js，独立HTTP字节SHA核验通过，reports/deployment/financial-retry-test-ui.json。体验容器5186仍acceptance-20261110-context，本项尚未部署到体验栈。无付费模型、真实支付或新增业务案件。


补充：独立审查发现执行已开始且审批过期时仍可能显示重新审批，现与模拟执行共用canActBeforeExecution判定，打开的审批表单也拦截该状态。最终25项纯测试、9项受控浏览器测试及lint/TS/build通过（reports/frontend-financial-retry-final-browser.log、frontend-financial-retry-final-pure.log）。新增审批员测试确认未执行时允许重新审批，已有UNKNOWN/SUBMITTING记录刷新后不再显示审批或新执行，0 POST。最终静态产物已更新5187并核验字节，reports/deployment/financial-retry-final-test-ui.json；前述7项为首次运行，保留不覆盖。体验5186仍待同步。


### 2026-11-10 财务页面保护已同步体验版

仅构建并重建frontend-app，当前前端aftercare-frontend:acceptance-20261110-financial，镜像96f58f17b2e3205f3c7edacf577f98007a4a26da2ddc66daaa8b2dfb3ec8126e；后端API/Worker/Beat继续acceptance-20261110-context。API、Worker、Beat、PG、Redis、S3容器ID/镜像ID/启动时间前后不变。无bootstrap或迁移。前端healthy，同源readyz200、匿名me401、客服me200。JS index-BWHdENXz.js SHA ec5062161a0cdf4e9e7e195dbd47b0c9edf86c0e7d2a8733afb65815967484a7，与5187已测产物及容器文件逐字节一致；CSS也核验。

真实BrowserSkill只读打开既有app退款查询工单，方案与回执页显示“未查到该订单的退款执行记录”；capture d10d0a279ca9c，0 POST，会话rrhn已停止。此项证明实际容器页面能正常读取原工单；UNKNOWN/SUBMITTING和连续提交仍以9项受控浏览器测试为证据，不外推真实故障验收。无模型费用或业务写入。证据reports/deployment/financial-ui-app.json、reports/browser-financial-ui-app.txt；私有capture .runtime/browser-financial-ui-app.json为600。之前“体验5186待同步”现由本条更新。


### 2026-11-10 真实浏览器对照与CSV下载

5187测试员读取既有single-routed a9359027-fb81-4ea4-927c-1795ddd6b6cf与multi-routed 6f0e5cf3-fea6-4eec-99eb-5298b613305a，9/9配对，实际为3个独立开发案例各3次重复。未知指标仍显示未知及分母0，人工意见两侧均0。C001第3次基线NEEDS_REVIEW/对照FAILED，筛选结果变化只保留此行。第1次基线详情和真实CSV下载成功，2176字节与独立HTTP一致，9行案例/重复/状态与实验逐条相符。第3次筛选后的两次工具click成功返回却未改变URL，原因尚不明确；一次request-help返回continued，接管时基线实验已打开，随后选第3次案例、重载该deep link均成功，右侧第3次详情也打开成功。明确为含人工协助的只读验收，不称完整自动点击通过。

报告reports/browser-comparison.json、browser-comparison-api-readback.json、browser-comparison-csv-readback.json；实际CSV browser-comparison-single-routed.csv。私有capture da78ad4580924保存.runtime/browser-comparison.json（600），34请求、0 POST、无网络规则或重放，会话bthd已停。无新增模型或评分，正式质量结论仍未完成。隔离fixture回归已加入，运行结果另记。

隔离fixture完整浏览器回归10/10通过，并通过lint/TS/build（reports/frontend-comparison-browser.log）。新增用例覆盖双层抽屉关闭、筛选、两侧第3次结果再次打开、右侧深链接刷新及0 POST；未复现上述工具点击无效，未包含同一受控流程CSV下载，不以此推定真实失败原因。产品源码未改，无需重新部署。


### 2026-11-10 交付文档与逐项复核缺口

根README与OPERATIONS已区分新安装离线基线和本机现有live-hybrid栈，当前backend context/frontend financial覆盖与宿主test混合运行方式已按现有容器/监听状态核对。四层Compose只读config验证成功；架构文档按真实源码重写，删除“向量与多Agent尚待实现”、审批分派限制等不成立描述，区分图恢复、投递租约和独立模拟执行账本。文档链接检查见reports/documentation-current-review.json。

完整需求再审发现实际产品缺口：后端支持tools/citations/facts逐项人工标签，但页面原先仅提供总评与备注；逐项输入与历史展示正在实现，尚未验收。契约见docs/REVIEW_ANNOTATION_UI_PLAN.md。用户对DEV-HR-01技术化提问反馈“看不懂你说啥”，此为可用性反馈而非质量结论；无人工标签写入。人工包已补照片审核与退款批准的白话例子，reports/human-review-usability-feedback.json记录边界。

下一批明确剩余交付包括逐项人审入口、按场景指标及证据下钻、每条政策至少两个边界例索引、独立金标与封存/验证冻结/正式留出比较、计时演示与断点复现。现有浏览器/工程通过不覆盖这些缺口。

### 2026-11-10 逐项人工复核表单验收

前述逐项输入缺口已完成：工具参数、引用支持和事实覆盖分别展示原始证据、发布说明与可选判断；默认暂不评，无自动金标。28项纯测试、13项受控浏览器测试通过，覆盖版本冲突、历史意见、显式替换和旧报告缺失证据。测试UI5187已部署并核对静态资源；体验UI5186尚未更新。真实BrowserSkill打开既有压缩多Agent实验C001第1次及复核表单，11项均默认暂不评，12个请求全部GET；独立HTTP读回人工意见仍0。滚动到表单入口后成功打开，未提交评价。提交、冲突及历史展示仍以受控测试为证据，不冒充真实人工验收。证据：[只读验收](../reports/browser-review-annotations.json)、[页面记录](../reports/browser-review-annotations-form.txt)、[部署](../reports/deployment/review-annotations-test-ui.json)。

### 2026-11-10 政策边界与场景聚合推进

[政策边界审计](POLICY_BOUNDARY_AUDIT.md)覆盖14条运行时政策、各至少两个具体边界；新增22项隔离测试，root独立复跑相关四文件52 passed，见reports/policy-boundary-root-review.log。未知质保资料被归为期限外的缺陷已派发修复，尚未通过新验收。POL08只读查询缺原请求定位、POL10通用严格审批不等于阈值专属分支，仍明确保留限制。

[场景指标计划](SCENARIO_METRICS_PLAN.md)已进入前后端实现。root只读保存现有真实single-routed实验9结果、3独立案例各3次及52项指标基线，reports/scenario-metrics-prechange-baseline.json。该旧实验仅有scorer_version、缺metrics_version，不回填或猜测历史定义；新功能须明确历史不可复现并在新实验冻结定义。当前接口和页面尚未部署验收。

### 2026-11-10 质保未知事实修复

POL11缺失、空值、布尔、负数、非整数或非数字质保天数改为HUMAN_REVIEW，原因WARRANTY_PERIOD_UNKNOWN；明确0天仍无质保，正整数保持包含到期时刻。无退款动作或可用退款额。root审查后独立复跑业务与政策四文件61 passed，覆盖精确到期、超1微秒、极大整数不溢出及无效数据；源码摘要与证据见reports/policy-warranty-root-review.json。仅隔离纯规则/SQLite服务测试，尚未部署共享API或验收实际浏览器该分支，未新增模型调用。

### 2026-11-10 场景统计公式兼容检查

root新增scripts/verify_scenario_metric_baseline.py，只读改动前保存的真实HTTP报告，用开发中当前聚合器重算。9运行/3独立案例的52项指标所有原有字段逐项一致，reports/scenario-metrics-baseline-parity.json。这是保存样本的本地公式回归，不能证明历史冻结版本可复现、当前真实接口可用或所有贡献项边界正确；最终代码冻结后仍需重跑并做新实验HTTP验收。旧实验缺metrics_version的限制保留。

### 2026-11-10 场景统计真实HTTP验收

root完整后端纯回归522 passed/1 skipped/11 deselected；最终代码重算旧保存报告52项原字段一致。真实测试库只读确认任务无PENDING/PUBLISHED/RUNNING后，更新test API64406/session37558、Worker64441/session37165、Beat64453/session19218；体验栈未变。测试前端最终文案资源index-BGOLR24m.js已核对HTTP字节，31纯测试、上一功能版18受控浏览器及最终文案1相关浏览器测试分别保留。

唯一新建零模型fixture实验3139c3ce-9f15-4f9e-895a-1c481bdc5f40，三场景各两次，6/6结束、0失败、0模型尝试、0人工意见，冻结ac-metrics-2。实际HTTP126项检查通过：52指标与原聚合一致、全部贡献入口分页无漏重、各场景2运行/1独立案及覆盖分母核对、空交集、匿名401/客服403、tester no-store、非法筛选422、指定不匹配口径摘要409及历史定义不可用。所有只读查询前后实验详情完全一致。409为显式错误摘要测试，不冒充并发复核发生；实际浏览器场景选择/下钻仍待完成。证据reports/scenario-metrics-http.json、reports/backend-scenario-root-tests.log、reports/scenario-metrics-baseline-parity-final.json、reports/deployment/scenario-test-services.json。

### 2026-11-10 场景指标实际浏览器验收（含协助）

真实BrowserSkill读取零模型实验3139c3ce-9f15-4f9e-895a-1c481bdc5f40。整体3独立案/6运行；场景下拉的隐藏输入无几何且点击失败后，一次request-help返回continued，用户选开箱发现破损；页面变为1独立案/2运行。随后工具打开任务成功率贡献，两条C001各分母1/分子0，打开第2次结果，深链接重载保留场景/指标/重复编号，关闭详情回到原贡献列表。全部29捕获请求为GET，场景及贡献接口200，独立API读回人工意见仍0。不是完全自动选择验收，也不证明真实模型质量或浏览器并发复核冲突。会话kxxf已停止，私有捕获0600；报告reports/browser-scenario-metrics.json及同前缀txt保留各步快照。

### 2026-11-10 实际页面断点补验

浏览器经一次人工选择订单/售后类型协助，工具填写描述并提交；真实POST进入BusinessService.create_case第367行，在终端读取customer/damaged/O0079/version1，continue后201并显示新工单。独立HTTP本人200、其他客户404。浏览器仅1次POST；调试入口未提供可选intake能力接口，该GET404及回退结构化建单已记录。页面/终端/临时SQLite链路已实测，非共享PG或队列/模型调试。会话与两进程已结束，专属临时数据已清理。证据reports/business-breakpoint-browser.json、business-breakpoint-terminal.txt、business-breakpoint-readback.json。此前本项NOT_RUN由此更新；完整3–5分钟演示计时仍未运行。

### 2026-11-10 已升级体验版：场景统计与逐项复核

体验UI5186及API/Worker/Beat已升级acceptance-20261110-scenario，含质保资料未知转人工、逐项人工复核和场景统计；旧context/financial镜像与覆盖文件保留供回退。升级前无活跃任务，停止应用写入进程后对36张非迁移public表全列计数/内容摘要、登录凭据摘要/权限/mtime及迁移版本进行前后核对，全部一致。存储PG/Redis/S3容器身份与启动时间不变。API/Worker/前端healthy，Beat running；同源readyz200、匿名401、客服身份200、历史工单可读。JS/CSS与容器及测试5187产物逐字节一致。无新模型调用，不是新质量批准。证据reports/deployment/scenario-app.json、scenario-app-preservation.json、scenario-app-build.json；实际浏览器场景链路证据仍在测试5187（含一次人工选择协助）。

### 2026-11-10 首条独立逐句清晰度反馈

用户针对DEV-HR-03旧应用意见“退款进度仅根据当前模拟账本反馈。”明确反馈“没说清楚，应直接说明没查到记录”。仅判该句清晰度，不等于整案事实/引用/处理评分或数据集金标，未提交结果review接口。记录reports/human-review-dev-hr03-clarity.json。root从已升级app只读回原修复运行b2036c90-746b-4e4c-841b-625e3e14cd9e，实际意见直接说退款执行账本未查到执行记录，并保留是否曾申请/失败/到账需另核对；新意见尚无用户评价。读回证据reports/human-review-clarity-fix-readback.json。

### 2026-11-10 固定数量真实HTTP只读负载

对测试API8022的既有六结果实验详情执行82次真实GET（前后各1，串行40、并发4共40）。两组均40/40成功，串行P50/P95为21.727/27.079ms，并发4为49.191/102.887ms；全部响应规范化摘要一致，未新建实验或调用模型。该测量包含本机HTTP与JSON处理，不含浏览器渲染/外网，固定先串行后并发，暖环境短样本，不能外推生产SLA。脚本scripts/verify_http_read_load.py、原始逐请求reports/http-read-load.json。

评测持久进度事件与重连补读已开始实现，业务SSE不算此项完成。封存方案正收敛为复用现有实验/预算的冻结记录与暴露追踪，不强制引入签名加密平台。两项均未部署验收；独立金标与正式发布仍需真实人工证据。
