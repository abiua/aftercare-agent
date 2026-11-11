# U1、U3 与 U6 最终要求审计

2026-11-10。依据[重建计划](FULL_IMPLEMENTATION_PLAN.md)、[Notion 阶段说明](notion_snapshot/stages.md)和[评测规范](notion_snapshot/evaluation.md)，只读核对当前代码及已落盘报告。此次未调用共享服务和真实 API。报告里的在途状态是审计所读快照。

| 判断 | 口径 |
| --- | --- |
| 已证明 | 有与断言匹配的真实环境证据，或可确定的文件与结构证据 |
| 实现待验证 | 有实现和局部证据，缺规定的集成、故障或用户验收 |
| 功能缺口 | 代码或交付件尚未覆盖要求 |
| 人工待验 | 缺独立人工期望、事实、引用核验或发布批准 |

## U1 工单底座

| 必须项 | 代码与证据 | 审计判断 |
| --- | --- | --- |
| FastAPI、真实 PG、初始化及迁移 | [迁移](../backend/migrations/versions)、[PG 测试](../reports/runtime-pg-tests.log)、[Compose 应用链路](../reports/compose-app-flow.json)。 | 核心底座已证明，Compose 报告覆盖 Nginx、API、Worker、PG、Redis 下的离线工单。初始化保留既有记录的验证范围见[集成记录](HANDOFF_INTEGRATION.md)。 |
| 身份、角色和本人归属 | [鉴权](../backend/src/aftercare/auth.py)、[业务权限](../backend/src/aftercare/business/service.py)、[PG 测试](../backend/tests/integration/test_runtime_pg.py)。 | 真 PG 已检查四角色、匿名拒绝和客户只能读本人订单。跨客户工单、附件、事件已有局部测试。真业务创建脚本使用客服身份，客户浏览器创建和各角色写入拒绝矩阵待验证。 |
| 工单创建、查询、浏览器、重启保留 | [真业务闭环](../reports/business-full-flow.json)、[浏览器报告](../reports/browser-first-pass.json)、[进程恢复](../reports/process-restart.json)。 | 已证明客服浏览器登录、列表、材料审核历史、退款方案与回执、事件流。恢复保留原 case/run/thread，报告保留一次临时轮询失败。浏览器报告明确把创建、上传、批准、中台及移动布局列作未验证。 |
| 锁、CI、类型和前端构建 | [CI 定义](../.github/workflows/ci.yml)、[本地入口](../scripts/check.sh)、[冻结前端镜像验证](../reports/deployment/frozen-frontend-verify.log)、[冻结后端镜像验证](../reports/deployment/frozen-backend-verify.log)。 | 锁及离线 CI 配置已交付。宿主综合报告后端 352 项通过，ESLint 以 Zod 包配置错误结束，独立容器 lint、类型、SSE 测试及构建证据另计。未见远程 CI、Playwright 套件或 VS Code 调试配置通过的证据。 |

## U3 私有材料、审批与模拟执行

| 必须项 | 代码与证据 | 审计判断 |
| --- | --- | --- |
| 私有 S3、大小与格式、限时访问、归属和版本 | [业务 API](../backend/src/aftercare/business/api.py)、[存储](../backend/src/aftercare/business/storage.py)、[真 S3 记录](../reports/s3-first-flow.json)。 | 两张合成照片真实上传、字节一致回读、匿名拒绝已证明。业务用例检查假图、分块超限、跨客户和对象篡改。60 秒签名及到期、跨角色浏览器取件待真实证据。 |
| 人工审核，补交使旧意见失效 | [业务实现](../backend/src/aftercare/business/service.py)、[局部用例](../backend/tests/business/test_business.py)、[真闭环](../reports/business-full-flow.json)。 | 已证明材料人工审核后恢复同一线程。补证令旧材料意见和方案失效有局部测试，缺正式环境完整负例矩阵。 |
| 方案、审批、执行前重验 | [业务规则](../backend/src/aftercare/business/rules.py)、[方案和执行](../backend/src/aftercare/business/service.py)。 | 已证明客服提案、未批准拒绝、独立审批角色批准后执行。批准绑定订单、事实、材料、政策、方案版本和哈希，有有效期。部分退款须独立处置核准。过期和版本更改有局部证据，尚待真 PG 故障矩阵。 |
| 账本、额度、幂等、并发 | [执行模型约束](../backend/src/aftercare/business/models.py)、[执行事务](../backend/src/aftercare/business/service.py)、[真闭环](../reports/business-full-flow.json)。 | 真 HTTP 顺序同键重放获得同一执行 ID，账本一笔且已退金额一致。按订单串行锁复核各行扣除在途后的余额。不同键同案并发、不同案件争抢同一行额度及跨案库存竞争应分别取证。事项去重按订单、意图和订单行划界。 |
| 未知结果与恢复 | [模拟支付端](../backend/src/aftercare/business/payment.py)、[核对与额度](../backend/src/aftercare/business/service.py)、[业务故障用例](../backend/tests/business/test_business.py)。 | 局部故障证明支付落账后超时、无落账超时、额度持续占用、只按原键查询及无回执持续待核对。真实 SIGKILL 报告的对象是 Agent 运行，支付提交边界强杀仍待真实证据。 |
| 统一安全评分及恢复 | [评测执行器](../backend/src/aftercare/evaluation/runner.py)、[真实 v2 恢复](../reports/backup-v2-restore.json)、[首次恢复失败](../reports/backup-v2-restore-attempt1.json)。 | 真 v2 恢复核对 2,757 张表及 148 个对象，覆盖公共及评测附件。首轮锁池失败保留。拦截已有轨迹，最终违规率缺独立评分。 |

[24 案旧回归](../reports/offline-regression.json)均为未运行，原因为 FROZEN_INPUT_CHANGED。按当前执行器来源规则，完整矩阵应由 19 个 fixture 和 5 个 fault 组成。显式故障案为 AC02-R016、R020、R021、R022、R023。SQLite 局部重放在 single 和 multi 下各有 143 个待人工复核及 671 项确定性检查通过，R020 留作 PG_CONCURRENCY_REQUIRED 和一条未知。详见[评测交接](HANDOFF_EVALUATION.md)。

## U6 数据、对照与中台

| 必须项 | 代码与证据 | 审计判断 |
| --- | --- | --- |
| 120 核心和 24 冲突回归 | [v0.2 清单](../data/v0.2/manifest.json)、[数据测试](../backend/tests/evaluation/test_dataset.py)。 | 120 核心三意图各 40，开发／验证／保留为 60／30／30，24 个核心场景族分组。另有 24 个冲突案例及独立 S0。清单人工核验数为 0，人工金标与独立盲测待验。 |
| 真实应用隔离执行 | [运行器](../backend/src/aftercare/evaluation/runner.py)、[开发 60](../reports/dev-regression.json)、[验证 30](../reports/offline-validation.json)、[保留 30](../reports/offline-holdout.json)。 | 120 个核心案例实际离线应用运行，全部待人工复核。三批分别 290、180、160 项确定性检查通过，均启用同版 BGE。开发批代码指纹与验证／保留批不同，630 项仅是分批合计。评测按 schema、图线程、私有对象键隔离，参考答案进入评分器。 |
| 中台、版本复核与导出 | [页面](../frontend/src/Evaluation.tsx)、[API](../backend/src/aftercare/evaluation/api.py)、[服务测试](../backend/tests/evaluation/test_service.py)。 | 已实现用例筛选、实验、进度、结果下钻、人工追加复核、JSON／CSV、CSV 公式保护及 tester RBAC。真浏览器中台与导出和页面回读仍待验证。外部报告导入、独立顺序 CLI、关联旧失败的新尝试是交付缺口。 |
| 预算、取消、故障和负载 | [评测服务](../backend/src/aftercare/evaluation/service.py)、[执行器](../backend/src/aftercare/evaluation/runner.py)、[服务测试](../backend/tests/evaluation/test_service.py)。 | 有持久排队、固定每案配额、运行代码守卫、模型登记事务内取消准入、故障后补读调用。替身证据检查取消挡住后续模型请求。缺实际队列主动取消、在途费用核对及不同并发的负载数据。load 复用离线脚本且独立标源。 |
| 两组实验与成对比较 | [冻结计划](LIVE_COMPARISON_PLAN.md)、[比较服务](../backend/src/aftercare/evaluation/service.py)、[实验标识](../reports/live-comparison-ids.json)。 | 固定 single 对照 economy／analysis／routed，固定 routed 对照 single／multi。共同案例和配置校验；全部配对结束才输出对比指标。审计时 economy 已结束 4/9，另外三组快照未开始。36 次执行只覆盖三个独立开发案例。缺验证择参、冻结质量费用阈值及独立保留集真实质量对照。 |
| 真实调用、失败和复验 | [兼容性](../reports/model-compatibility.json)、[首验](../reports/live-smoke.json)、[复验](../reports/live-recheck.json)。 | 首验 1 案失败，2 项 FAIL、3 项 UNKNOWN。复验 1 案 6 次实际调用，5 条确定性检查通过，仍待人工复核。复验费用估算 0.02415025 美元，单案 P50／P95 无法支撑推广。首验与复验费用口径经历缓存计价修订。 |

## 指标与发布门槛

- 当前成功率是 PASSED / started。失败、超时、执行错误、在途和待复核纳入已启动分母。覆盖为 finished / planned。零分母、调用审计缺失或成本未知保持空值，已记录的部分调用只能作下限。详见[汇总代码](../backend/src/aftercare/evaluation/service.py)。
- 当前 Recall@5 的分母为有末次检索记录案例的待人审参考条款数。工具率基于已记录调用成功，草稿拦截统计 draft_rejected。P50／P95 是含隔离准备的脚本耗时。上述指标都有证据来源及分母。
- 功能缺口包括自由文本意图准确率、宏 F1 与混淆，标注决策点的工具选择和参数合法／业务正确分项，澄清必要性，引用支持及关键证据覆盖、版本命中，模型违规建议次数、最终违规率、恢复率、升级率、备用切换成功率、主动处理和等待时延分离，以及小样本不确定性。脚本预先提供意图，现有结果不支撑自由文本识别结论。最终违规率在代码中返回 null。
- 发布门槛当前只能显示 FAILED 或 INCOMPLETE，发布质量与费用阈值仍待独立冻结和配置。案例人工复核可追加，数据金标仍需独立审核。现有结果支持工程演示和带范围的规则证据。

## 审计后的执行和实现补记

四组冻结小样本已结束。每组 9 次执行均已启动并结束，仍是相同三个独立开发案例。保存的执行分布如下。

| 配置 | 确定性失败 | 待人工复核 | 模型调用 | 估算美元 | P50 / P95 毫秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| [单 Agent economy](../reports/comparison-single-economy.json) | 0 | 9 | 30 | 0.05789770 | 9,746 / 27,268 |
| [单 Agent analysis](../reports/comparison-single-analysis.json) | 2 | 7 | 36 | 0.15592550 | 19,507 / 52,372 |
| [单 Agent routed](../reports/comparison-single-routed.json) | 2 | 7 | 37 | 0.09755225 | 9,623 / 62,863 |
| [三 Agent routed](../reports/comparison-multi-routed.json) | 3 | 6 | 55 | 0.19614415 | 25,211 / 66,023 |

冻结报告未取得人工通过，四组任务成功均为 0/9。确定性失败、成本估算和观察时延可对照，样本和金标状态限制了质量归因。

冻结实验结束后，代码增加指标观测与版本化门槛、哈希核对的报告导入、关联补跑、独立顺序 CLI 和明确的负载读取。在断网 Docker 中验证了两案并发负载、六次读取、导入、补跑链接、磁盘代码变化拦截和三种门槛状态。新指标不回填冻结报告。人工金标、浏览器流程、真正 PostgreSQL 与 S3 的扩展路径仍按实际取证状态判断。详见[交接说明](HANDOFF_EVALUATION.md)。
