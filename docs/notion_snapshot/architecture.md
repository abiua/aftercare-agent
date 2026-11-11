Here is the result of "fetch" for the Page with URL https://app.notion.com/p/40fca1c66b50812e9a6fcfdf02546694 as of 2026-11-09T08:48:59.768Z:
<page url="https://app.notion.com/p/40fca1c66b50812e9a6fcfdf02546694">
<ancestor-path>
<parent-page url="https://app.notion.com/p/40bca1c66b508120994df96bf220c8b3" title="电商售后助手｜项目主线与实施计划"/>
</ancestor-path>
<properties>
{"title":"01 技术架构与技术栈"}
</properties>
<iconMetadata>null</iconMetadata>
<content>
## A-U3.1-close-final · 最新工程状态（2026-11-09）

实际审核链：React/TypeScript submit→FastAPI/Pydantic与Depends身份校验→SQLAlchemy服务两次短事务（权限/版本/幂等/互斥/完整材料清单）→事务外S3核验→PG保存意见及案件版本→页面GET回读。图片字节在私有对象，数据库存元数据。人工意见不进入LangGraph模型循环；明确resume才读取检查点继续。

本轮311项纯模块/保护通过、后端5源码类型及Ruff/格式、前端TypeScript/lint通过；正式页面4项真实API/PG/私有对象通过（20.6秒），真实HTTP并发/强杀重启6组通过。E十二案全部执行：33 PASS/36 UNKNOWN、12 NEEDS\_REVIEW，0失败/错误/超时/未运行；发布门槛false，不自批人工金标或将财务/违规未知置零，不宣称整个U2/U3完成。

最终报告 <code>evaluation/reports/20261109T084008274957Z-b38e200e/report.json</code>；页面 <code>backend/reports/a_u24_audit/browser-20261109T083854Z/html/index.html</code>；HTTP <code>reviews-20261109T083928-b07b1651.json</code>。首轮页面2过/1并发等待超时/1未运行保留；修正真实双POST的发送调度后4过，所有业务断言保留，产品页面不改。初次PYTHONPATH、适配版本null、诊断baseURL及格式问题均另记，云盘读取等待不算通过。无新增依赖/迁移，原数据保留：audit原8表行摘要一致，体验启动36表摘要一致。

详细技术栈、函数链、设计取舍和失败回溯：<code>docs/parallel/U31_CLOSE.md</code>；当前步骤：<code>U31_EXPERIENCE.md</code>。A页已记录各小阶段并读回；B—E子页未改/未派发消息。用户本轮尚未操作或新增技术回答，不新增掌握或独立实现记录；实现/核验由GPT完成，用户负责需求、体验和反馈。

## U2.4-prepare：当前接续安排

日期：<date start="2026-11-05" />。本批准备已完成，新增事件接口/SSE仍未实现，无新增应用验收成绩。已核对现有WorkflowEvent/run内seq可复用、页面仍GET轮询、E008/009/010仍未运行；U2.3历史371后端/8浏览器等成绩原样保留。<br /><code>CURRENT_STAGE.md</code>更新至v1.2，<code>CONTRACT.md</code>维持v1.1/HTTP u2-v1，启动指令在 <code>docs/parallel/U24_START.md</code>。B接事件查询/SSE，D接游标补齐/去重与连接状态，E先接010实际进程重启再补008/009；C暂无新增独立功能包。A负责公共装配/受控故障环境/串行验收。临时接管的B/D/E文件按OWNERSHIP归还原负责人，必须保留A集成修正。<br />只验证文档引用与当前源码状态，没有运行会清库的PG/浏览器检查，没有停止用户演示、修改业务数据或调用付费模型。用户自行启动原B/D/E会话，A不自动派发。订阅/重连只读，不能重复业务处理或模型调用；不同时启动完整U3—U6。

## U2.3-A-integrated：最新滚动验收

更新：<date start="2026-11-05" />。GPT完整实现，用户体验/预测/反馈，不设编程作业或复盘门槛。<br />A已接B/C/D/E：后端371 passed，浏览器8 passed（6真实API＋2视图替身），真实HTTP后端SIGKILL→新PID→私有照片上传→同case/run/thread恢复通过；累计调用2→4，重复恢复/上传不增版本，超限413及跨客户404实际拒绝。后端Ruff/66文件格式/mypy35通过，前端lint/type/build通过，大包警告保留。<br />E真实CLI10计划/7执行/7待复核/3未运行，27确定性检查PASS/17UNKNOWN，发布门槛false。C135和E99由A独立复跑，数字与完整后端重叠，不相加。真实模型调用0，unknown usage/费用为null。<br />契约文档v1.1记录实际语义，HTTP仍u2-v1；根装配、共享配置/锁、迁移注册、SeaweedFS测试服务和串行脚本已接入。应用aftercare库未迁移，runtime/原始数据不动。<code>scripts/check_isolated.py demo</code>已启动实测，使用隔离测试库；访问 [http://127.0.0.1:5174](http://127.0.0.1:5174) ，新建A1001→运行→等待补证→上传→恢复→HUMAN\_REVIEW，Ctrl-C结束。

真实链路：网页→鉴权/版本API→业务服务与图→C模型适配/程序工具规则→业务PG与独立检查点→私有对象材料→页面快照。图位置不覆盖当前业务事实，照片UPLOADED不等于核验/审批。模型及存储I/O在短事务外，无性能收益数字。

业务场景、痛点、角色规则、执行链、关键取舍、真实失败与证据在 <code>docs/parallel/U23_INTEGRATION.md</code>；正常/异常体验、配置预测和逐题复盘在 <code>LEARNING_REVIEW.md</code>。用户本轮确定需求并转交各角色交付；实际操作/预测/解释尚未观察，不填掌握或薄弱、不虚构独立实现。首轮失败保留，详细过程见 <mention url="https://app.notion.com/p/40fca1c66b508180b8e4d617f1a4f4d0">A 总控与滚动集成</mention>。

更新：<date start="2026-11-05" />。目标是可演示、可恢复、可测试的单商家电商售后系统；所有订单、政策、审批和退款执行均为模拟业务。

## 产品目标与协作方式

固定 React 工作台支持破损、错发、退款进度三类工单。第一条主线先完成：提出售后 → 查单和查规则 → 补材料 → 人工材料审核 → 形成方案 → 授权审批 → 程序重验 → 模拟回执。

GPT 完整实现基础设施、页面、API、业务规则、存储、模型、测试与迁移；用户体验和反馈。可重构已有代码，保留证据和业务边界，不设教学 TODO 或用户编程门槛。每阶段还交付业务讲解、正常／异常演示、预测实验与逐题面试复盘，依据真实操作记录参与和理解。

当前：U1工单链路、U2-1破损检查及最新结果保存已由用户验收。U2-2新增LangGraph单次model/tools循环、单候选路由入口、运行记录与网页面板；候选过滤函数已实现；本轮纯函数 25 passed、Agent API 12 passed（包含替身与一项真实过滤接口），见 <code>parallel/BASELINE.md</code>。默认离线固定脚本，DeepSeek专用入口由用户主动启用。代理长调用不持有数据库事务，业务检查和工具事件一同提交；同工单运行互斥、请求编号去重。详情见<code>U2_2_AGENT.md</code>。

agent\_runs保存运行、工具事实和草稿，当前已有真实LangGraph PostgreSQL checkpointer与同run/thread补证恢复；SSE、RAG及财务闭环尚未实现。下面技术栈仍包含后续目标，不能视作全部已经安装或实现。

## 目标技术栈

| 层次 | 选型 | 用途／引入阶段 |
| --- | --- | --- |
| 工作台 | TypeScript、React、Vite、Ant Design | U1；工单列表、详情、消息，审核页随U3扩展 |
| 接口 | FastAPI、Uvicorn；REST＋SSE | U1 REST；U2运行事件和流式展示 |
| 后端与配置 | Python 3.11、Pydantic、pydantic-settings | 普通业务函数、接口契约；环境变量优先 |
| 模型接口 | LangChain；先延续DeepSeek | 模型、工具、消息与结构化输出 |
| 业务编排 | LangGraph | U2单Agent业务图；U6三角色对照 |
| 模型路由 | Python规则＋Pydantic注册表；统一调用适配层 | U2建立接口及单模型基线；U5多模型、升级与故障切换；U6评测 |
| 业务数据库 | PostgreSQL、SQLAlchemy、psycopg、Alembic | U1；案件、订单行、材料、审批、账本、审计 |
| 图状态 | LangGraph PostgreSQL checkpointer | U2；与业务表分开管理，保存执行位置而非授权真相 |
| 检索 | 中文分词＋BM25；Sentence Transformers＋BGE中文small v1.5；pgvector；RRF | U4；条款检索、融合与引用核验 |
| 文件 | S3标准操作＋boto3；本地SeaweedFS | U3；私有附件、限时访问、对象生命周期 |
| 后台任务 | Celery＋Redis | U5；导入与评测任务，运行迁入Worker需单独验证 |
| 观测 | 结构化日志、业务审计、持久运行事件 | U1开始；LangSmith可选，U5按需加Prometheus／Grafana |
| 测试 | pytest、HTTPX、独立PG测试库、Playwright | 随功能加入，包含权限、事务、恢复与端到端测试 |
| 测试中台 | 复用React／Ant Design、FastAPI、PostgreSQL、evaluation；U5接Celery | 用例库、实验管理、能力指标、失败复核与版本对比；LangSmith可选 |
| 质量与依赖 | uv／pnpm锁文件、Ruff、mypy、ESLint、TypeScript | U1；兼容性验证后锁定 |
| 交付 | GitHub Actions、Docker Compose、Nginx | U1基础CI／Compose，后续完整代理与干净环境验收 |

LiteLLM在需要多提供商／多实例治理时再评估；RouteLLM、长期记忆PostgresStore、案例向量化、重排序均为可选扩展。不把安装全部组件设为第一步。

## 架构与职责

React → 认证API → 工单服务／LangGraph → ModelRouter → 模型调用适配层。<br />模型提出工具请求 → 程序检查权限和参数 → 业务工具查询事实 → 普通Python规则判断 → 返回图节点。<br />独立审核／审批API → 业务服务 → PostgreSQL事务／账本 → 模拟回执。

业务图决定下一步做什么；模型路由决定本次推理用哪个模型；Celery决定任务何时由哪个Worker执行。三者共用业务服务，不能各自维护状态真相或重试预算。

业务表保存可信事实与授权；图检查点保存执行状态；模型上下文只含本次必要信息；对象存储保存附件；向量只用于检索候选。跨会话记忆不能覆盖现行政策和当前订单。

测试中台是同一工作台的独立模块，汇总规则回归、真实模型评测和故障实验，但分别标记来源。模型方案质量、程序拦截效果和最终业务结果分别统计；指标能下钻到案例与证据。数据与评分契约详见<code>docs/DATA_AND_EVALUATION.md</code>。U2先运行器／结果契约，U4最小界面，U5批量调度，U6对照与发布门槛；当前U2 CLI与评分已实现并接真实应用；页面和后续能力仍未实现。

## 数据契约与硬约束

- 分开定义案件ID、工单ID、运行ID、图线程ID。案件有递增版本，重新开启案件保留历史，不静默清零预算。
- 身份来自后端认证上下文；客户／客服／审批人权限分开。所有订单、工单、图线程、事件、附件访问均检查归属。
- 金额用整数分，按订单行实付减已退及在途占用计算；未知值保留未知，禁止默认零或自动符合条件。
- 用户描述、政策文档、模型输出都是数据，不能授予工具权限、修改规则或批准退款。
- 材料审核、财务方案审批、执行结果分别记录。材料齐全不等于真实性已确认，材料确认不等于退款获批。
- 方案绑定材料、订单和政策快照；审批绑定方案版本、内容哈希、审批人、有效期。依据变化后必须重审。
- 模拟执行在事务内重验审批、额度和版本；稳定业务键由案件与获批动作确定，绑定payload哈希。同键同参数返回原回执，同键不同参数拒绝。
- 不同请求键也不能绕过同一事项和额度约束。并发校验同一订单行剩余额度，不能各自读到旧余额后都成功。
- 超时后先查账本；没有成功回执不能称退款完成。interrupt等待节点与执行写入节点分开，重放仍经过幂等执行器。
- 同案运行串行化或用版本冲突拒绝；不同案可并行。模型调用、人工等待、文件上传期间不持有长事务。

## 工程落地标准

认证、基础CI、迁移、锁文件和最小Compose从U1开始。模型API Key留在根目录.env／部署环境，不进前端、日志、截图或备份。日志只记录必要摘要与关联ID；审计包含操作者、目标、版本、时间和结果。

SSE只是订阅：事件编号、游标补发、终态和权限必须明确；断开不取消业务任务，过期游标先读状态快照。文本流和已提交业务状态分开显示。

附件校验大小、真实格式和归属，使用私有桶、服务端生成对象键和限时链接；上传不自动变成审核通过。验证对象缺失、重复上传和重启恢复。

U5任务创建与outbox同事务提交，消费者按稳定键去重；记录租约、重试和终态。人工等待释放Worker；重试与恢复先核对业务账本。每个并发请求／任务独立数据库Session，阻塞I/O和嵌入计算不得阻塞异步事件循环。

迁移先备份，导入独立PG库并核对数量、归属、金额和状态，再验证回退。旧Python会话不直接假装成LangGraph执行位置；业务历史可以迁移，新图从安全节点开始。业务库、检查点、对象元数据需做恢复演练。

## 项目目录

当前：docs为新主线，prototype为可复用基线，runtime保存已有会话；.venv-agent只服务原型。backend使用独立.venv／uv；frontend使用pnpm；infra运行最小PG，scripts提供启动与独立测试库的浏览器检查。

后端U1采用可读的小模块api、auth、schemas、models、ticket\_service、ticket\_rules；后续按实际需要增加workflows、tools、repositories、jobs、evaluation，不预先创建大量空层。未创建的API和组件均不视为实现。

## 选型依据

[Ant Design](https://ant.design/docs/react/introduce/)、[LangGraph PostgreSQL记忆接口](https://docs.langchain.com/oss/python/langgraph/add-memory)、[pgvector](https://github.com/pgvector/pgvector)、[BGE模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[SeaweedFS](https://github.com/seaweedfs/seaweedfs)、[Celery Redis](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)。

BGE中文small为512维，政策／查询使用同一模型空间，查询指令配置单独固定。少量条款先精确向量检索，再根据实测决定近似索引。MinIO社区仓库已归档，因此本地采用SeaweedFS候选；以S3操作验收后锁版本，不宣称完全兼容所有S3特性。

## 当前并行落地

范围与接口以 <code>parallel/CURRENT_STAGE.md</code>、<code>CONTRACT.md</code> v1.0、<code>OWNERSHIP.md</code> 为准。当前只补齐 U2；为正式页面补证闭环前置 U3 最小 SeaweedFS 私有上传，B 实现业务，D 接网页，A 装配服务和依赖。上传成功只代表材料登记，不代表核验。其余 U3 财务流程、U4 RAG、U5 队列／多模型仍待后续。

已将 Agent HTTP 路由移至 <code>agent_api.py</code>；<code>api.py</code> 保持根装配。B／C／E 可拥有独立路由、schema 和 ORM，沿用 models.Base；共享表和配置由 A 维护，迁移按 OWNERSHIP 预分配。禁止另起认证、数据库基类或重复业务状态源。

## U2.4-A-rolling · 首批公共接入（2026-11-05）

既有WorkflowEvent/run内seq→鉴权GET/SSE→按游标补读；不另建事件真相。状态和水位同SQL快照，无id结束/错误通知不推进持久seq。文档CONTRACT v1.2，HTTP仍u2-v1；无新增迁移/依赖。

A独立环境：PG55433/aftercare\_test、私有SeaweedFS58433、API8002、页面5175，独立卷/锁；原用户演示不停止、不清库。11项环境保护检查、真实HTTP强杀重启上传恢复、事件GET和SSE核心断线→补图恢复→强杀→游标1补读至19通过；无id等待/终态通知、header优先、401/404/409与只读不变调用/版本通过。<br />证据：docs/parallel/U24\_[INTEGRATION.md](http://INTEGRATION.md)；backend/reports/a\_u24\_integration/events-20261105T122041-f5a83ad3.json，源码前后指纹一致。两份前序失败保留：早期重启RuntimeError根因未确定；端口探测OSError已调整TIME\_WAIT兼容探测。实际运行结果不等于全部故障矩阵、页面和模型质量均已验证；没有远端CI运行结果。

### U2.4-A-rolling 接入增量

A已执行D首批正式页面：10项中9过1失败（54.5s），新增U2.4两项真实用例通过；失败为frontend/e2e/agent.spec.ts:48的旧视图替身按钮定位重复，精确交接已写HANDOFF\_A，仍由D修复，整体不标通过。E新CLI尚未执行。公共保护检查最终13 passed（含原11项），A静态检查通过；原演示只读健康检查HTTP200/offline。汇总backend/reports/a\_u24\_integration/rolling-validation.json；新失败与未验证边界保留，无付费调用，U2.4未关闭。

## U2.4-A-BE-accepted · 本轮真实接入结果（2026-11-05）

B真实PG/API/reader检查18 passed/26.94s；D修复后页面10 passed/45.4s（8真实API/2视图替身），独立UI fixture3 passed/10.3s，历史9过1失败保留。E检查点连接参数编码缺陷由A修正，保留5/15/5秒限制、真实PG读取与原断言，修正文件已归还E；162项纯检查包含旧161，公共保护17包含旧13，均不与历史求和。<br />E本轮完整v2真实10案报告evaluation/reports/20261105T123641125716Z-24187a3e/report.json：10计划/10启动/10结束/0未运行，8待复核/1工具错误/1业务超时，51检查PASS/25UNKNOWN，无FAIL，门槛false/退出1。009实际延迟121.006270秒，原120秒期限下迟到业务写入被拒绝；010本轮主动SIGKILL换PID、同案/运行/线程/检查点保留后真实私有上传恢复。未知审计、usage/cost和人工期望不改成已验证，付费模型调用0。<br />新体验http://localhost:5175已启动并检查登录/查询，持独立55433测试锁、不清库、沿用browser检查点；原5174保留。用户尚未体验/预测，记录LEARNING\_REVIEW，不虚构掌握或独立实现。下一步体验复盘与活跃长连接撤权/持续写入完整矩阵、人工期望核验并行；真实模型仍待明确启动，整个U2不关闭。<br />汇总：docs/parallel/U24\_[INTEGRATION.md](http://INTEGRATION.md)、backend/reports/a\_u24\_integration/be-validation.json。Notion A进度05—08逐步读回；无远端CI运行结果。

本轮体验保护补充：初次跨启动环境测试发现TMPDIR不同可使旧锁分裂，一次幂等prepare实际执行（未清库）。A已修固定项目.local/test-locks并兼容旧/macOS原生临时锁，只重启自有新版体验；最终u24/shared均实际拒绝不同环境的prepare，证据lock-cross-environment.json。失败与A页时序更正保留，新旧服务健康200/offline。be-validation.json已实际生成，用户新版体验尚未发生。

## U2.4收尾更新 · A09—10

本次已验证：活跃运行中SSE断线补读、连接内持续接收新事件、注销/会话到期关闭及重连401。真实HTTP/PG应用，原离线响应只增加明确的测试等待屏障；没有改业务规则/时钟，未调用付费模型。公共锁重构后复验3种场景PASSED，报告backend/reports/a\_u24\_audit/20261105T134921-0c85eb18/report.json；注销/过期约0.994/1.004秒关闭。独立u24audit PG55434/对象58434/API8003，保留用户5175及旧5174演示；31项入口/隔离纯检查通过，两个演示锁均实际拒绝并发启动。

E十案整理为docs/parallel/U24\_CASE\_[REVIEW.md](http://REVIEW.md)和case-review.json，原始报告不变：8 NEEDS\_REVIEW/1 ERROR/1 TIMEOUT，51 PASS/25 UNKNOWN，门槛false。25未知为10个人工期望、10项最终违规、5项财务动作审计；人工确认期望也不能替代缺失审计。故障保护正确不等于客户请求成功，不宣称真实模型抗误导能力。复核单含执行链、设计取舍、检查点连接编码故障及真实贡献说明。

用户体验已实际完成：只读重查仍9条→上传事件10/版本2→同案恢复至19/HUMAN\_REVIEW→切换客户不可见原工单；用户能独立解释后端身份与工单归属校验。依据A页L01—L06和LEARNING\_REVIEW，原“尚未体验”为历史时点；笔误不记为薄弱点。用户需求/操作/预测/反馈与GPT实现/自动验收分开。

当前阶段v1.4、契约v1.2/HTTP u2-v1不变。真实模型入口默认预览/默认u24audit，业务/检查点固定测试库，最多3次调用、每次输出1024，建议本次0.03美元预算（不是供应商账户硬限额），待用户明确启动，当前付费0。未验证真实模型质量、人工金标与未知审计；代理级网络故障及动态角色/归属变更不在本次覆盖范围。整个U2未关闭，不全面提前开U3。

## A-U2.4-12 · 首次真实模型单案已运行

用户在本机终端隐藏输入密钥，执行已授权的一个缺照片案例。run 81cf9b0b80ad402f90965b0908741fc1，2026-11-05北京时间22:02:27—22:02:31；deepseek-flash实际2次调用，均成功，原报告WAITING\_INPUT/NEEDS\_REVIEW。A本轮没有新增远程调用，也未读取根.env。剩余次数不自动用于其他案例。

真实链路：程序重验→模型要求get\_order和check\_damage\_claim→应用读取模拟订单与规则→模型生成草稿→程序再重验→缺照片暂停。草稿与A1001/陶瓷杯/签收3天/期限7天/缺照片一致，明确请补清晰破损照片，并说明照片登记不代表真实性确认或退款批准、回复未发送。A辅助审阅符合本单案期望；用户尚未确认草稿，不能替代人工核验，原NEEDS\_REVIEW保留。

A持55434固定锁，以READ ONLY/REPEATABLE READ核对PG：运行状态、模式、调用数、草稿和trace与报告一致；业务WAITING\_CUSTOMER，案件版本1、材料版本0，连续10条事件，checkpoint\_ready=true、该thread下7行检查点。仅证实本次持久化，不能冒充已运行真实模型强杀恢复。

usage合计输入1727/输出194/总1921 token；按既有高峰未缓存价格快照估算0.0007509美元，低于本次0.03美元授权。供应商账单未知，逐次cost仍null，估算不是实际扣款。计划模型版本不是响应返回版本的独立证明。

原报告backend/reports/model\_acceptance/81cf9b0b80ad402f90965b0908741fc1.json，另存同目录81cf9b0b80ad402f90965b0908741fc1-review.json，含原SHA256/PG只读检查/用量核算/边界；不覆盖历史证据。实际用户参与记录LEARNING\_REVIEW L07：授权、终端执行及反馈，GPT实现与自动核验；没有新增面试回答或虚构独立贡献。

本单例连通、工具调用、缺材料暂停已有真实模型证据；补证恢复、复杂误导及总体质量未验证。E的8待复核/1错误/1超时、金标和未知审计不改。当前“等待密钥/尚无远程请求”已是历史状态；下一步展示草稿请用户核对本例，不自动重跑。

## A-U2.4-13 · 用户要求调整客户口吻

首个真实模型案例的人工反馈为CHANGES\_REQUESTED：客户回信应礼貌自然，不带内部免责声明或“未发送草稿”字样。已调整提示词与三状态固定文案；客户正文说明事实和下一步，工作台保留内部状态与notice；授权、材料、退款等规则未降低，旧报告和回复不改。

124项相关模块检查、Ruff及mypy两源码通过；独立u24audit三种真实HTTP/PG应用场景通过（缺照片/超期进一步核查/签收未知），使用原离线模型，报告backend/reports/a\_u24\_audit/customer-reply-20261105T141424.json。本轮付费0，修改后真实模型措辞未复验，不宣布质量全面通过。用户贡献为产品评审/需求修改，AI负责实现和检查；LEARNING\_REVIEW L08留证。当前演示进程及历史数据保留，旧回复不自动替换；新进程才载入新提示词。接口结构v1.2不变。

## A-U3.1-00 · 人工材料审核启动准备

用户认可礼貌回复示例并要求继续，记录为文案方向认可（LEARNING\_REVIEW L09），不是新版远程模型复验或E全部金标通过。已核对现有support/customer/approver角色、私有材料访问、版本与迁移head0005；完成docs/parallel/U31\_[START.md](http://START.md)：客户上传→support查看私有图片并记录可继续核查/需要补充→客户查看→补交新材料使旧意见失效。

CURRENT\_STAGE v1.5/CONTRACT文档v1.3。既有u2-v1不变，新增material-reviews的u3-review-v1接口冻结（尚未实现），明确角色、case/material版本、同键同内容重放、旧意见失效及不可变历史；审核不调用模型、不驱动run、不批准退款。本批不提前开财务账本、RAG或多模型。

归属：B四个material\_reviews模块/专属测试/0006→0005意见表；A根注册/共享配置/锁/脚本/真实验收；D审核面板；E专属u3-material-review-v1案例/适配；C无新增开发包。原C0006/E0007未实施预留取消，后续编号重新协调。本轮未创建迁移或改应用数据，未调用模型、未重启演示；仅文档一致性检查通过，证据backend/reports/a\_u24\_audit/u31-preparation.json，不新增应用成绩。

7类验收覆盖真实私有图片、补证失效、跨客户/角色拒绝、版本竞争、幂等/同键改内容、响应丢失/进程重启、原流程回归。A使用独立u24audit串行，B/D/E只跑纯检查；用户自行启动原会话，A未派发消息。U2旧模型/工程证据、人工期望与未知审计继续保留，不把旧发布门槛改成通过；独立材料审核开发不受讲解测验阻塞。下一步模块一交付即滚动接入，不等全部完成。

## U3.1滚动接入 · A01—03

A已接B路由/ORM并在独立u24audit迁移0006。0005结构快照与计数保存，不含业务数据，不冒充完整备份恢复。最初根注册前NOT\_READY；注册后A漏迁移导致首次实际GET失败，保留报告；补迁移后真实HTTP/PG/私有图片、权限、意见增版/幂等、补图失效、完整清单、双客服并发和HTTP SIGKILL新PID保留意见通过。最终接口证据backend/reports/a\_u24\_audit/reviews-20261105T145118-fe172544.json，新增审核不调模型/不改业务或run状态。

D正式浏览器首轮1 passed/1 failed/2未运行，20.5秒；u31-material-review.spec.ts:117的“材料已登记”匹配成功提示与事件两处，精确修复已登记HANDOFF\_A，另Spin的p/div嵌套警告交D。A未抢写D文件、不删断言；D独立UI替身另4 passed/11.8秒。browser-u31不清库，真实失败不被替身成绩覆盖。

E真实报告evaluation/reports/20261105T145815235621Z-74cf9460/report.json：12计划/10启动并结束/2未运行，10 NEEDS\_REVIEW、0失败/错误/超时；31技术PASS/38UNKNOWN，发布门槛false/exit1，源码未变。009独立真实强杀PID60646→60657后持久意见/原响应重放一致；008专用support2仅新增隔离测试身份。011对象缺失、012有效租约缺A安全故障设施，保持NOT\_RUN，不删除对象或伪造用户租约。金标、财务及违规未知不改。

A公共保护30 passed/3.41秒，静态/格式/脚本语法通过；CI新增审核HTTP/页面步骤但远端未运行。只读核验原真实模型run状态/调用/草稿/trace保留、表计数未下降、head0006。当前用户8002/5175健康200；旧8001/5174未监听，原因未确认，A未主动结束，不能称两套均健康。核验u31-integration-summary.json。新增付费0、未部署应用aftercare库。

下一步D修定位后A复验页面，A提供安全故障设施供E补011/012；B专属会清库的5项PG套件尚未另跑，不冒充已通过。阶段尚未全验收，用户尚未体验审核页面；学习不设门槛。完整边界和精确交接见docs/parallel/U31\_[INTEGRATION.md/HANDOFF\_A。](http://INTEGRATION.md/HANDOFF_A%E3%80%82)

### A-U3.1-04 · D修复后复验（最新）

D上传提示定位/Spin容器修正已落盘。A真实浏览器复验1失败/3未运行：受权照片307→对象200且成功解码，随后事件快照回读使材料区刷新，已展开照片被卸载并重置收起。只读诊断确认再次点击可显示；不代表4项页面验收通过。D须修复同版本复核成功后的展开状态保留，同时保留失权/失败/材料变化时撤下内容的保护；A未抢写D代码或派发消息。精确链路、修复要求及报告见docs/parallel/HANDOFF\_[A.md](http://A.md) A-U3.1-04、backend/reports/a\_u24\_audit/u31-photo-diagnostic.json及browser-20261105T151454Z/html/index.html。初次诊断的来源配置/客户端配置失败分别保留，不算业务成绩。

用户5175/8002仍健康，工单照片未清理，U3.1尚未升级到用户体验服务。新增付费调用0；B既有真实审核/重启证据保持，E011/012及金标/未知审计仍待补。契约v1.3/u3-review-v1、阶段v1.5不变，不宣称U2或U3全部完成。本次用户无新增体验/回答记录；下一步D修竞态，A串行复验后带用户正常/异常体验。

### A-U3.1-05/06 · 照片修复通过，5175可体验（2026-11-06，最新）

A串行复验：6项独立界面替身通过/16.7秒；4项真实API、PG、私有对象页面流程通过/35.8秒，正式断言未放宽。覆盖看图审核/客户读取/刷新只GET、补图失效与历史、双客服旧页面冲突、角色与跨客户权限/幂等。历史失败保留，D照片展开竞态不再是当前待修项。报告browser-20261105T161111Z/html/index.html位于backend/reports/a\_u24\_audit，fixture见docs/frontend/reports/u31/ui-fixture-2026-11-05T16-10-39-707Z/html/index.html。

A升级u24体验0005→0006，35张原有表（含检查点）数据摘要一致；重启后36张表再次一致。只导出结构基线，未宣称完整备份恢复。现有http://localhost:5175已接审核功能；真实浏览器打开用户原工单af1ed28971a54960bd0194a36c539593，照片解码成功、case2/material1/审核0，原run79c36da75af440b1b03ca15c6c84149e保持19事件/4调用，未替用户提交业务操作。证据backend/reports/a\_u24\_integration/u31-experience-upgrade-20261105T161504-7850beea.json、u31-experience-start-verification.json、u31-experience-browser.json。新增升级保护3纯检查通过，Ruff/格式/语法通过；A专属升级脚本和检查已登记OWNERSHIP。

体验步骤/请求链/取舍/故障与贡献边界见docs/parallel/U31\_[EXPERIENCE.md](http://EXPERIENCE.md)。客服先看图、记录需补充意见，客户查询；后续逐步体验旧版本冲突与预测验证。用户尚未实际完成本阶段体验，不记已掌握；实现/验收为AI辅助。审核不进入模型循环、不自动恢复Agent、不批准退款。契约v1.3/u3-review-v1、阶段v1.5不变，E011/012、金标/未知审计、新提示词远程复验仍待补，不宣称U2/U3全部完成。用户工单照片和应用aftercare未清理，本轮新增付费0。

## A-OPENAI-01 · Sol/Terra适配完成，真实验收待启动（2026-11-06）

用户明确指定gpt-6.1-sol与gpt-6.1-terra。已核对官方模型ID及工具调用需Responses；新增显式openai模式/OPENAI\_MODEL选择，默认仍offline，不做自动升级/备用/失败重试。沿用两程序工具与原业务规则；SDK30秒/max\_retries0/输出1024/保守上下文16000，store=false与加密续接仅内部检查点保存，明文推理/加密状态不进入公开回复、trace或评测报告。恢复同时核对provider与model\_name，拒绝同run悄悄换模型。前端正确显示OpenAI与模型名。HTTP u2-v1/u3-review-v1和尝试u2-model-v1保持，新增route\_version u2-openai-single-v1；CONTRACT文档v1.4/CURRENT\_STAGE v1.6。

实际213项离线检查通过/1.86秒（含此前180，不相加），真实SDK/service/业务图配HTTP替身、SQLite/内存检查点；不是PG恢复或真实模型证据。49文件类型检查、Ruff/格式，前端类型/lint/build通过，原大包提示保留。最早18过2失败为A测试夹具方法名笔误，已修正并保留失败日志；SDK类型/JSON标注问题亦已修正，无跳测/放宽断言。openai3.46.0原已存在，仅补直接依赖及离线锁，无版本升级。证据backend/reports/openai-validation-20261106.json、openai-final.log、openai-types-final.log、openai-frontend.log。

新scripts/openai\_[acceptance.py](http://acceptance.py)默认仅预览，不读取密钥/PG/网络；固定u24audit/55434/aftercare\_test与锁。每模型单案最多3请求，Sol预算上限\$0.30、Terra\$0.15；两款最多6请求/\$0.45。按高缓存写入价保守估计，不是供应商硬账单限额。真实执行须明确execute/预算/价格日期，用户本机额外read-local-env才读根.env。A本次只预览和追加注释（未读原.env），未启动Colima/项目服务，付费请求0；账号权限、模型质量及本批PG恢复/浏览器正式验收均尚未验证。配置/入口/官方来源见docs/parallel/OPENAI\_[ACCEPTANCE.md](http://ACCEPTANCE.md)。

A临时C/B/D改动已登记并归还各模块，A保留配置/锁/公共脚本/保护测试/主线；未自动创建或派发会话。U3.1用户体验、E011/012、人工金标/未知审计和旧回复真实复验继续待补，不把本次说成U5或全阶段完成。用户本轮贡献为指定模型与配置意图，尚未实际调用或展示对Responses的解释，不记独立实现/已掌握。

## A-OPENAI-02 · 两模型真实单案完成

<date start="2026-11-06" />。用户填写配置后明确授权，A按每款最多3次、合计6次/\$0.45计划串行执行。仅本机验收进程读取密钥，不输出或复制。固定u24audit/55434/aftercare\_test与测试锁、0006，无清库/迁移；用户u24体验库、应用aftercare与照片未操作。

Sol：运行652ce1507e1b40c9b3fe5c4dbe52230c，1次请求，输入803/输出63 token，6连续事件/5行检查点。Terra：运行70552cb37a304cdca21d79e637f341f0，2次请求，输入1807/输出78 token，9连续事件/7行检查点。均WAITING\_INPUT/缺photos，回复礼貌且未承诺退款，没有原调试式免责声明。实际应用API→业务图/程序规则→Responses→PG已执行；独立只读核验草稿/trace一致、事件连续、检查点存在。Sol使用预注入规则结果，未主动调工具；Terra真实调用check\_damage\_claim并回传结果。

合计3次请求，按计划高输入价保守估算\$0.0095525（Sol \$0.004645、Terra \$0.0049075），不是账单，原trace费用仍null。原报告NEEDS\_REVIEW保持，A辅助审阅符合单例期望；用户确认待完成。不代表Sol工具往返、上传后OpenAI恢复、进程重启、浏览器、照片理解或总体质量通过。历史213离线检查不改，CONTRACT v1.4/CURRENT\_STAGE v1.6保持，未宣布U2/U3/U5完成。

证据：<code>backend/reports/openai_acceptance/652ce1507e1b40c9b3fe5c4dbe52230c.json</code>、<code>70552cb37a304cdca21d79e637f341f0.json</code>、<code>authorized-20261106-review.json</code>。命令及预算见<code>docs/parallel/OPENAI_ACCEPTANCE.md</code>。A首次只读核验误假设暂停清空lease\_token而失败，代码491/512证明保留token用于检查点匹配，实际清除lease\_expires\_at；修正诊断并保留失败记录，未改业务或正式测试。临时审计库/Colima均已关闭，数据卷保留，未追加调用。

业务取舍：程序确认期限/缺项，模型礼貌表达；模型无退款权限。用户贡献是指定模型、本机配置、授权，GPT负责实现/执行/核验。尚无用户解释OpenAI机制的证据，不新增已掌握或独立实现记录。下一步用户审阅实际回复；补图恢复和异常场景作为后续受限验收，余量不自动使用。
</content>
</page>
