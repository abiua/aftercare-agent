# 最终验收剩余项

2026-11-10。以 Notion 的 U1–U6 条件和本轮重建证据为准。[当前逐项审计](REQUIREMENTS_AUDIT_CURRENT.md)的历史记录保留。涉及同一功能时，以后续有报告的复验更新此前判断。以下是只读核对，未启动服务或实验。

[阶段要求](notion_snapshot/stages.md) · [架构约束](notion_snapshot/architecture.md) · [模型验收](notion_snapshot/models.md) · [数据与评测](notion_snapshot/evaluation.md) · [重建范围](FULL_IMPLEMENTATION_PLAN.md)

## 影响正式质量和发布的顺序

| 剩余项 | 分类 | 当前证据和最短下一步 |
| --- | --- | --- |
| 中文表达、人工参考与保留集封存 | **需独立人工决定** | 120 条公开客户表达有来源及改写记录。120 核心、24 冲突用例均已建，参考仍由项目生成。开发复核包四案整体仍为 PENDING。用户已判断 DEV-HR-03 的一句历史回复没有说清未查到记录；该句清晰度反馈已记录，不等于整案、独立金标或发布批准。独立复核者先用白话示例校准，逐条核对表达，再对允许动作、必要事实、条款、金额、禁止行为及预期账本签署带版本意见。正式保留集由独立人员封存并记录谁已见过什么。已用来调参的场景不得继续称未见，必要时另封未暴露批次。 [表达包](../data/corpus/README.md) · [复核表](../data/corpus/human-review.csv) · [数据边界](EVALUATION_DATASET.md) · [开发复核包](HUMAN_REVIEW_PACK.md) |
| 验证集选型与阈值冻结 | **需独立人工决定，随后缺验收证据** | 现有离线验证只有业务脚本证据。检索配对使用开发者期望，开发模型对照仅 3 个独立案。先用已独立复核的验证集比较模型、检索和路由，在开启正式保留集前确定选择依据、质量和成本限值。冻结代码、依赖、模型、提示词、路由、政策、索引、预算、评分及数据摘要。 [检索开发报告](../reports/policy-retrieval-dev-v1.json) · [离线验证](../reports/offline-validation.json) · [开发对照](LIVE_COMPARISON_RESULTS.md) |
| 冻结后的正式保留集两组对照 | **缺验收证据** | 30 案离线保留集记录 160 条程序检查通过、零模型调用、30 案待人工复核。真实开发对照 36 次是 3 个独立案在四条件重复，保留 7 次确定性失败和 29 次待人工复核。完成前两项后，提交逐配置调用与费用上限，获明确启动后在同一封存集合交错运行。A 固定单 Agent 比较经济、分析、规则路由，B 固定策略比较单／三 Agent。共用业务、证据、权限和总预算。可先每配置一次，分别报告独立案例、重复次数、全部失败、未知用量、主动时延、接管和最终违规。 [离线保留](../reports/offline-holdout.json) · [当前对照边界](LIVE_COMPARISON_PLAN.md) · [交错要求](notion_snapshot/evaluation.md#L144) |
| 单次结果语义复核及最终发布批准 | **需独立人工决定** | 逐项复核表已部署，真实浏览器确认可打开且默认不评。确定性规则能核验条款版本及部分条件，真实语义支持、工具业务正确和关键事实覆盖仍需人评。独立复核员应核对正式结果并提交可定位的意见。汇总后由获授权人员结合硬约束、未测与未知、阈值和费用作发布决定。现有 INCOMPLETE 不会因填完表单或重跑程序检查自动变为批准。 [逐项表单实测](../reports/browser-review-annotations.json) · [条件引用实测](../reports/response-clarity-citation-http.json) · [发布门槛](HANDOFF_EVALUATION.md) |

## 工程实现与验收剩余项

| 剩余项 | 分类 | 当前证据和最短下一步 |
| --- | --- | --- |
| 评测实验进度事件 | **已实现、验收并部署** | 已实现同事务持久事件、分页、SSE 游标恢复、逐帧鉴权与旧实验快照。根代理回归 530 项通过；隔离真实 PostgreSQL 验证 8 并发写入者的连续序号、事务回滚、旧结构升级不造历史，另 39 项备份兼容测试通过。页面已实现并通过根代理 35 项纯测试、子代理 24 项受控浏览器测试。根代理另复跑 3 项实际 HTTP/PG 测试：121 条事件分页及断开第 10 条后的补读逐项一致，活动流撤权后无后续事件、重连 401。此证据使用实际路由与服务、受控零模型执行器，不含共享 Worker 或正式工作台。共享测试库已升级，4,525 张旧表数据保留；真实 API/Celery 完成 3 条 fixture 案例，12 条进度事件及按游标补读通过、0 次模型调用。5187 页面资源与已审构建一致；实际 Chromium 显示进度已结束和新阅读卡，17 条请求全为 GET，无复核提交。浏览器 SSE 捕获为 HTTP 200 后 failed，终态后客户端主动取消与此一致，原始流正文未取得；逐条补读证据仍以独立 HTTP 验收为准。5186 体验版已升级，旧业务数据和凭据保留，历史工单及服务健康回读通过。 [体验部署](../reports/deployment/progress-app.json) [工作台实测](../reports/browser-progress-workbench.json)普通工单 SSE 不替代评测事件验收。 [根代理证据](../reports/evaluation-progress-root-review.json) · [Notion 评测接线](notion_snapshot/evaluation.md) |
| 指定旧退款请求的只读定位 | **已实现、验收并部署** | POL08 的同一已批准方案重复执行已复用原账本记录。只读退款进度当前按订单回读，不能凭同订单的其他记录认定是用户所指的原请求。已补按执行编号精确查询、含糊澄清、定向零模型回复和上下文重验；根代理复跑 178 项后端检查通过、1 跳过，前端 37 项通过，子代理受控浏览器 27 项通过。根代理已复跑实际路由、回环 TCP 和独立 PostgreSQL 验收，17 张表内容前后一致，支付提交/查询为零；记录为合成预置，不冒称支付真实执行。审查发现的空白分隔多编号歧义已修复，24 项解析检查通过。真实浏览器完成输入、清空、未知编号和显式选行，15 请求全 GET；5186 已升级 refund-fixed，37 张旧表及凭据保留。 [浏览器](../reports/browser-refund-lookup.json) · [部署](../reports/deployment/refund-app.json) [后端审查](../reports/refund-lookup-backend-root-review.json) · [页面审查](../reports/refund-lookup-frontend-root-review.json) [POL08 边界](POLICY_BOUNDARY_AUDIT.md) · [条件核验](../backend/src/aftercare/intelligence/citation_conditions.py) |
| Notion 列名的 VS Code 配置 | **配置及实际 DAP 断点已验证，GUI 操作未测** | 已有 `.vscode/launch.json` 和独立页面任务，脚本新增 `--editor` 使用普通业务服务避免与 pdb 冲突。子代理使用本机 VS Code 的 debugpy 经真实 DAP 和回环 HTTP 命中生产建单函数，暂停、继续后 201、本人 200/他人 404、临时文件清理均通过；根代理已审配置和分支源码，并独立复跑 DAP 全流程通过。 [根代理复验](../reports/vscode-debugpy-dap-root.json) GUI 点击启动及前端任务实操未测，不扩展为图形界面验收。 [DAP 报告](../reports/vscode-debugpy-dap.json) [Notion U1](notion_snapshot/stages.md#L50) · [断点报告](../reports/business-breakpoint-browser.json) · [独立调试说明](DEBUGGING.md) |
| 客户通过真实页面提交照片 | **真实页面上传及客服取件已验收** | 2026-11-11 用户协助客户登录及两次文件选择，原工单真实页面两次材料 POST 201，版本0→2、状态转待人工审核。两个 S3 对象字节和客服 HTTP 200 内容均与本机模拟图摘要一致；审核、方案、审批、执行、Agent运行均0。[本次报告](../reports/browser-customer-upload-resume-20261111.json)。下述失败保留为历史。 真实客户页面已登录、确认本人订单并建单。选文件两次受限，一次请求人工协助超时，材料 POST 为零。S3 实际上传字节回读和客服实际预览审核已分别通过。用一次可用的浏览器文件选择在已有测试案上传合成图，捕获 POST、材料版本及客服读取字节。保留前次未完成记录。 [客户页面](../reports/browser-intake.json) · [客服预览及审核](../reports/browser-material-review.json) · [私有材料真实证据](../reports/deployment/container-material-link.json) |
| 测试员通过真实页面导入与重复导入 | **真实页面导入及去重已验收** | 2026-11-11 用户协助选同一报告两次，浏览器两次 POST 201，数据库仅一条相同摘要记录，首次来源/创建时间/内容保持，页面明确显示复用且仍待核验。[本次报告](../reports/browser-import-resume-20261111.json)。下述失败保留为历史。 JSON/CSV 浏览器下载及独立回读已完成。真实导入曾两次无法操作文件输入，人工协助等待 60 秒超时，导入 POST 为零。真实 HTTP 导入与重用证据已单独留存。使用既有报告，经真实页面选择文件并提交两次，确认相同摘要及同一导入记录，核对来源和页面重用提示。 [浏览器受限记录](../reports/browser-evaluation-import-attempt.json) · [真实 HTTP 导入](../reports/import-retry-http.json) |
| 评测取消与逐项复核写入的真实页面动作 | **排队取消已验收；人工复核提交待验收** | 2026-11-11 用户协助登录后，真实页面一次取消 POST 200；恢复 Worker 后原实验保持 CANCELLED，案例 NOT_RUN，未开始执行。[排队取消实测](../reports/browser-queued-cancel-20261111.json)。下述在途证据和人工提交边界仍分别保留。 后端已证明实际 HTTP＋Celery 的受控在途取消，另有真实模型迟到用量和取消审计。复核表单真浏览器验收为只读，受控浏览器测试覆盖提交及冲突。排队取消已补齐；下一步在明确允许的测试样本由复核者提交真实判断并回读版本和历史。不得替复核者给出结论。 [在途取消](../reports/inflight-cancel-http.json) · [复核表单](../reports/browser-review-annotations.json) |
| 全链路演示计时 | **缺验收证据** | 台本目标 4 分 20 秒。实际浏览器已完成单笔模拟退款闭环，用户补照、同一运行恢复与整段计时仍无一份同场证据。在明确离线的独立测试环境按台本实跑一次，记录时长、工单与运行标识及回执。遇到选文件受限按实际记录；历史回放单列。 [台本](DEMO_WALKTHROUGH.md) · [浏览器退款回执](../reports/browser-financial-flow.json) · [同案恢复与闭环](../reports/business-full-flow.json) |
| GitHub 托管 CI 与最终版本干净启动 | **最新版空卷工程检查及托管 CI 均通过** | [托管检查](https://github.com/abiua/aftercare-agent/actions/runs/42945574844)在提交 1db1425 上通过两项任务：数据18、后端801、前端37、受控浏览器27项通过；后端1跳过、30项PG未选。首次缺少本机报告的失败保留，改为入库夹具后复验通过。早期真实空 PG/S3 卷已通过生产 API 的初始化和幂等回读，最新体验镜像有保留卷升级证据。私有仓库已创建并推送；托管检查只证明离线工程路径。当前 formal 后端及 refund-fixed 前端已在独立内部网络和全新 PG/S3 卷完成初始化、四角色 API/Nginx 鉴权、Worker/Beat 及零模型任务实际执行，并清理本次资源。[最新版空卷全栈报告](../reports/deployment/fresh-formal-stack.json)。 [CI 配置](../.github/workflows/ci.yml) · [空卷报告](../reports/deployment/fresh-bootstrap-acceptance.json) · [最新镜像升级](../reports/deployment/scenario-app.json) |
| 最终文档与 Notion 的最新证据同步 | **本轮增量已同步；最终收口仍随验收更新** | 首页和四子页曾同步并读回，最近独立读回报告列到在途取消、浏览器下载及导入限制。保存的读回尚未覆盖场景下钻及断点等最后增量。收口时追加最新可定位证据，保留前次记录及质量未通过标记，重新读取验证。 [四子页读回](../reports/notion-four-pages-readback.json) · [最近读回](../reports/notion-latest-acceptance-readback.json) |
| 逐阶段演示、预测和复盘材料 | **导览已补齐并审查；实际理解需本人参与** | [U1–U6 导览](LEARNING_REVIEW.md)提供各阶段体验动作、预期结果、预测题、真实函数链及失败证据。根代理已核对 51 处相对链接及行号范围，并核对关键调用链和配对失败行为。 [审查记录](../reports/learning-review-root.json)用户理解须依据本人操作或回答，按 Notion 约定不作为开发或质量发布门槛。 [阶段产物约定](notion_snapshot/architecture.md#L35) · [协作约定](notion_snapshot/home.md#L97) |

## 原生评测证据接线增量（2026-11-10）

调用路径来源已新增默认/注入、同步/流式及响应观察，旧缺证记录不回填。根代理真实调用两个模型各两种路径，共四次请求均通过，独立持久账本回读一致，估算费用 0.000396 美元，无未知费用。这仅验证合成提示的传输和记账，不证明售后质量或正式两组对照完成。[根代理审查](../reports/transport-provenance-root-review.json)

政策全文与选择代码、模型文件摘要、实际索引和每次查询证据已接入；调用与检索组合回归 35 项通过。全后端离线回归 624 项通过、1 跳过、17 未选中，但独立审查另发现核验角色再次检索未留痕，以及配对查询因政策不同而遗漏比较，两项修复中。真实 BGE 发现固定模块配置漏列无文件的 Normalize，已修复并通过根代理两项边界测试。真实 BGE/PostgreSQL 集成仍待修复后验收，正式来源状态仍为 NATIVE_LEDGER_ONLY。来源状态提升、私有目录与冻结预算执行接线仍需实现和独立验证，不能仅凭本轮四次调用放行正式冻结。[接线契约](FORMAL_VALIDATION_BRIDGE_CONTRACT.md)

更新：上述检索缺口已修复，根代理 68 项针对性测试通过；源码冻结后真实 PostgreSQL+BGE 复验通过，单 Agent 一次、三 Agent 两次查询均与原生回读、SQL 排序及独立 float32 余弦计算一致，14 条向量记录匹配，BM25 不加载模型，提前暂停不建索引。全后端 651 项通过、1 跳过、17 未选中；随后审计摘要补纳注册表摘要，35 项来源/原生测试和 1 项实际 PG 一致快照复验通过。本批尚未部署。来源判定已实现但仅离线合成证据验证，没有新增真实正式选型或打开留出；正式冻结与私有执行仍待后续接线。 [检索审查](../reports/retrieval-provenance-root-review.json) · [来源审查](../reports/formal-source-promotion-root-review.json)

部署更新：体验 API/Worker/Beat 已升级 acceptance-20261110-provenance，页面沿用 refund-fixed 的已验收资源。升级前备份及离线校验通过，37 张旧表、凭据、迁移版本保持一致；PG/Redis/S3 容器未重建。服务健康、匿名 401、客服鉴权、真实模型模式配置、历史工单与只读未命中查询通过，新增模型调用 0。此部署不等于正式来源实验、质量选型或发布批准。[部署报告](../reports/deployment/provenance-app.json)

正式执行准备增量：新版冻结草案及 CLI 80 项相关测试通过；普通入口隔离与私有包首版全后端 727 项通过、1 跳过、19 未选中。根代理实际 PG 迁移两例验证旧数据保留及并发降级拒绝；实际 HTTP/PG 隔离验证正式记录对照不存在的 14 对请求、普通访问和队列过滤。随后修复私有包读取失败的暴露记录、包内自报金标提升及重复历史工单范围，65 项针对性复验通过。均为合成输入与工程证据，新迁移未应用到共享 public；正式冻结、解锁、稳定执行和恢复仍未接通。[本批审查](../reports/formal-isolation-package-root-review.json)

正式执行核心更新：根代理独立复验 44 项接线/执行纯测、7 项真实隔离 PostgreSQL 检查及全后端非 PG 回归（780 通过、1 跳过、27 未选中）。固定批次、跨测试员复用、流式续传、撤权、持锁连接丢失、未知调用阻断和已结算中断后仅继续未开始行均有工程证据。原中断结果与冻结未知不改，恢复账本另列。生产模型调用为零，新迁移未部署；公开正式冻结/解锁/执行入口仍待最后接线，正式质量与人审链仍未完成。[执行审查](../reports/formal-execution-wiring-root-review.json)

公开登记入口更新：子代理因额度停止后，根代理接手补齐冻结预览、摘要/额度约束和身份门控测试。当前入口单独验收不等于整链完成；原 Notion 要求配置交错，而 V2 仍整条件分块，已列为必须修复项。新迁移未部署、真实留出未打开。[入口审查](../reports/formal-public-root-review.json)

交错调度更新：V3 已实现按重复、案例、冻结条件顺序交错，V2 旧记录语义不变。29 项公开登记/恢复检查和 9 项真实隔离 PG 检查通过，覆盖第二条件未知/中断后整批停止；全后端非 PG 回归 801 项通过、1 跳过、29 未选中。正式 CLI 至执行核心整链、部署和独立质量链仍未验收。[交错审查](../reports/formal-interleaving-root-review.json)

公开 CLI 整链更新：根代理用唯一隔离 PostgreSQL schema 与合成候选，跑通实际命令入口→冻结/解锁→正式执行核心→私有报告。12 行按 V3 交错执行；重复命令没有新增案例调用，保留旧报告并生成新版本。实际撤销临时测试员后，在重新读包前拒绝且无新报告。原生验证来源输入为受控合成记录、业务运行器零提供方，不能算真实模型效果或独立人工质量。新源码及迁移仍待部署。[整链审查](../reports/formal-cli-root-review.json)

正式工程版部署更新：5186/8020 后端已升级 `acceptance-20261110-formal`，迁移为 `20261110_eval_formal`，页面沿用 refund-fixed。升级前备份及校验成功，37 张旧表按旧列摘要/计数与凭据均保持；PG/Redis/S3 容器未重建。健康、匿名 401、客服身份、live 配置、历史工单、未命中退款 404 和页面资源实际读回通过。新增提供方调用 0、真实留出未打开。共享测试栈仍待同步，独立质量与发布链未完成。[部署审查](../reports/deployment/formal-app.json)

共享测试栈同步：8022/5187 已加载本批源码，测试库迁移至 `20261110_eval_formal`。升级前备份校验成功；4,591 张旧表按原列的行数/内容摘要与凭据保持。真实 HTTP 创建三意图 fixture 时客户端等待超时，未重发；按数据库唯一原编号回读实验 COMPLETED，3/3 结束，全部 NEEDS_REVIEW。原始三隔离账本模型尝试均 0，12 条进度序号连续；页面资源保持已验版本。保留超时而不报告为创建 HTTP 201 成功。[测试部署](../reports/deployment/formal-test-services.json)

最新版空卷全栈验收：使用已部署 formal 后端与 refund-fixed 页面镜像，独立内部网络、全新 PG/S3 数据卷及无凭据文件。生产迁移到 `20261110_eval_formal`，两次初始化后全表内容、凭据字节/权限/修改时间与私有桶一致；API 与 Nginx 代理均通过四角色及匿名 401 检查，Worker 响应、Beat 运行。通过 Nginx 创建新货损工单并启动三角色助手，实际队列状态 DONE、运行 WAITING_INPUT，原始模型尝试为 0。未发布宿主端口，所有本次资源已删除；此为离线工程路径，不是浏览器完整补照/退款演示或生产模型质量。[最新版空卷验收](../reports/deployment/fresh-formal-stack.json)

Notion 四子页同步：架构、U1—U6、模型、评测页均已追加本批部署/交错/来源/恢复和空卷证据，保留旧记录，明确独立人工质量、正式留出、浏览器与托管 CI 缺口。四页重新读取核对标题成功。[读回记录](../reports/notion-formal-four-pages-readback.json)

真实页面取消补验尝试：浏览器连接正常，新工作窗口停在测试员登录页，工具要求人工登录；一次登录协助等待超时。捕获 4 个静态请求、API 请求 0，无新实验、取消或模型调用。已导出私有捕获并关闭本次会话，保留未验收，不切换浏览器后端绕过登录协助；需用户新指示后续接。[本次记录](../reports/browser-formal-cancel-login.json)

## 保留的证据边界与不适用项

| 项目 | 分类与最短下一步 |
| --- | --- |
| 浏览器断网补读 | **缺验收证据，范围限真实浏览器。** 真实业务 TCP 重连和零重复运行已通过。活动连接撤权和跨页已在挂载真实生产路由的最小应用用隔离 PostgreSQL 验证。若要覆盖完整工作台，断开并恢复一个真实测试浏览器连接，核对事件补齐且无重复请求。 [真实业务 TCP](../reports/sse-http.json) · [真实 TCP 隔离边界](../reports/sse-active-synthetic.json) |
| 自然断流、提供方错误及失租组合 | **缺验收证据，范围限新增真实组合。** 两模型四次原生流的文本、工具和用量已经过真实传输检查；流式失败策略有受控测试。在途取消是真实模型响应终态到达后门控，租约交错使用真实 PG 加替身与受控时钟，强杀后的自然租约过期使用零模型运行。自然网络断流、真实提供方错误组合和真实请求同时失租没有同范围证据。仅在需要声称覆盖该真实组合时，单独设计有界测试并保留原始错误和费用。 [真实原生流](../reports/streaming-compatibility-wire-recheck.json) · [在途取消](../reports/inflight-cancel-http.json) · [租约交错](../reports/inflight-lease-context-recheck.json) · [Worker 强杀](../reports/worker-crash.json) |
| 压测延迟与生产 SLA | **有界 HTTP 读取已测；浏览器负载未测，生产 SLA 不适用。** 固定已有测试实验进行了 82 次 GET；40 次串行 P95 27.079 ms，随后 40 次并发 4 的 P95 102.887 ms，全部 200 且正文摘要未变。含本地 HTTP/JSON 时间，固定执行顺序，不外推生产 SLA。此前两案并发、内部服务调用测量分别保留。 [HTTP 读取报告](../reports/http-read-load.json) · [负载和取消报告](../reports/evaluation-load-cancel-http.json) |
| 历史旧库迁移、真实退款与生产收益 | **明确不适用。** 本轮按重建范围使用模拟数据，旧 SQLite 历史未纳入，不冒作当前图检查点。S0 冻结数据保留，现有新栈 PG、对象和检查点恢复已另验。无需导入旧库或联系真实支付系统。 [重建计划](FULL_IMPLEMENTATION_PLAN.md) · [备份恢复](BACKUP_RESTORE.md) |
| 独立高额审批分支、额外提供方和可选平台 | **明确不适用，除非范围改变。** POL10 要求超 50000 分必批，当前任何金额都须独立财务审批，已覆盖阈值两侧；政策未规定另一条专属流程。重建使用已实证的 Sol/Terra 履行单候选至两候选的角色，历史 DeepSeek 首选不作为新增阻断项。同一提供方范围内的模型切换不证明供应商容灾。Notion 将 LiteLLM、RouteLLM、LangSmith、模型评审和复杂大屏列作可选，暂不补装。 [高额边界](POLICY_BOUNDARY_AUDIT.md) · [模型兼容](../reports/model-compatibility.json) · [可选架构](notion_snapshot/architecture.md#L60) |

已经结案的功能包括场景统计、贡献分页和浏览器深链接，真实浏览器中的一次选项协助仍保留；逐项复核表单、质保未知转人工和场景统计已同步体验版。页面建单断点、模拟财务闭环、已知政策边界例、条件引用空账本路径以及恢复上下文绑定均有各自范围的证据。正式发布状态仍取决于前述独立质量链。 [场景 HTTP](../reports/scenario-metrics-http.json) · [场景浏览器](../reports/browser-scenario-metrics.json) · [体验升级](../reports/deployment/scenario-app.json) · [上下文绑定](../reports/context-binding-http.json)

真实页面排队取消补验通过（2026-11-11）：用户重新协助登录后，在测试页点击取消及确认，实际捕获一次取消 POST，expected_version=1，HTTP 200，页面显示已取消。为确定性覆盖排队状态，创建 fixture 前暂时停止测试 Worker，操作后恢复且 ping 成功。恢复后的数据库原实验 CANCELLED/version2，唯一案例 NOT_RUN、未开始、原因 EXPERIMENT_CANCELLED，派发 DONE 且未创建案例隔离库。浏览器会话已关闭。这仅完成真实页面排队取消，不替代在途取消、人工质量评价或完整演示；此前登录超时记录保留。[补验报告](../reports/browser-queued-cancel-20261111.json)
