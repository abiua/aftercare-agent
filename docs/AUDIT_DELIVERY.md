# U1 与 U6 交付审计

2026-11-10。核对范围为本轮重建源码、Notion 需求快照及已保存的验收证据。历史项目的浏览器和测试成绩独立保留。本次审计读取现有源码、日志与依赖元数据，并在宿主临时目录复验冻结前端。工作区仅写入本报告。

需求来源：[完整实施计划](FULL_IMPLEMENTATION_PLAN.md)、[Notion 分阶段验收](notion_snapshot/stages.md)、[Notion 评测中台](notion_snapshot/evaluation.md)、[部署与演示](OPERATIONS.md)。

## U1 部署、CI 与前端

| 验收项 | 本轮可确认的证据 | 未覆盖的部分 |
| --- | --- | --- |
| 部署与迁移 | 体验 Compose 栈经 Nginx 创建工单，容器 API 与任务进程经 PostgreSQL／Redis 推进离线运行至等待补证。根交接记录最新镜像更新。隔离测试库有迁移及检查点表证据，最终镜像断网验收了指纹和启动拒绝。 | 两套 Compose 配置可解析。隔离测试 Compose 全栈冷启动、空卷到可用后的二次初始化及历史行保留对照，缺单独记录。 |
| 认证与业务 API | 真 PostgreSQL 角色和归属检查 2 项通过。PG／Redis 真实队列检查 1 项通过。真实 PG／私有 S3 业务流有补证、审核、审批、模拟退款和幂等回执，真实进程重启有同案恢复记录。 | API 和故障脚本是服务验收证据。 |
| 浏览器 | Chromium 生产构建有客服登录、订单／工单读取、材料审核历史、退款方案／回执读取及实时事件读取的记录。 | 首份浏览器记录未覆盖实际新建、私有上传、审批、角色切换或移动端。测试员后来验证的中台读取路径另列于 U6。首次 Vite 开发入口出现请求 400。站点图标后补，最终容器文件已核对。 |
| CI 与静态检查 | Python 与 npm 依赖锁、GitHub Actions 离线配置、纯检查和独立 PG 入口已提交。冻结 Linux 构建环境的前端 lint、类型、3 项事件协议与构建通过。宿主最终日志中 18 项数据测试、6 个冻结文件字节核验、352 项后端离线测试通过。 | 宿主最终组合检查在前端 lint 退出 2，后续前端测试与构建因命令串联未执行。未见 GitHub 托管 CI 的运行记录。CI 未接真实 PG、浏览器主路径或后端格式检查，项目未见 VS Code 调试配置和断点验收。 |

证据：[体验容器栈](../reports/compose-app-flow.json)、[根集成交接](HANDOFF_INTEGRATION.md)、[PG 权限](../reports/runtime-pg-tests.log)、[PG 队列](../reports/queue-pg-tests.log)、[业务 API](../reports/business-full-flow.json)、[进程重启](../reports/process-restart.json)、[浏览器记录](../reports/browser-first-pass.json)、[最终宿主组合日志](../reports/final-pure-check.log)、[冻结 Linux 前端](../reports/deployment/frozen-frontend-verify.log)、[Linux 构建日志](../reports/deployment/frozen-images-build.log)、[CI 配置](../.github/workflows/ci.yml)。

### 宿主 lint 失败归因

最终日志中的 ESLint 10.14.0 在加载 npm 依赖阶段报 `Invalid package config .../node_modules/zod/package.json`。较早的宿主单独 lint 曾在加载 `debug` 时得到 `ETIMEDOUT: connection timed out, read`。两次堆栈均停留在 Node 模块加载层。

本次先对文件做元数据检查。报错的 `zod/package.json` 逻辑大小 4142 字节、分配块 0、标记为 `compressed,dataless`，`debug/src/common.js` 与 `node.js` 同样为 0 块的 `dataless` 文件。随后以独立 Node 进程直接读取并解析同一份 `zod/package.json`，2.21 秒读到有效 JSON 和锁定版本 `4.6.7`。读取后该文件大小仍为 4142 字节、分配块 16、`dataless` 已消失。直接读取两份 `debug` 依赖也在 1.91 秒后完成，两份文件均实化为 16 个分配块。这组前后证据支持宿主文件提供程序按需读取依赖时发生加载失败。冻结 Linux 构建环境以同一锁文件和源码通过 ESLint 10.14.0，对应源码没有产生 lint 诊断。

原目录的宿主最终日志保留退出 2。复验把同一前端源码、测试、配置和锁文件暂存到宿主临时目录，用相同的 Node v24.14.0 和 npm 离线缓存执行 `npm ci --offline`，233 个包安装完成，随后 `npm run lint` 退出 0。干净 Darwin 环境、冻结 Linux 容器和原目录失败是三条独立证据。运营文档中的组合通过表述须按来源阅读。证据：[宿主日志](../reports/final-pure-check.log)、[Linux 容器日志](../reports/deployment/frozen-frontend-verify.log)、[npm 锁文件](../frontend/package-lock.json)。

## U6 测试中台与真实证据

| 模块 | 当前交付 | 交付缺口或验收边界 |
| --- | --- | --- |
| 用例库与实验 | 测试员可按类型、场景、难度、划分筛选，查看输入、参考与快照。实验表单支持来源、Agent／路由、重复、并发、调用与费用上限及案例子集。详情有轮询进度和显式取消。API 和真实离线队列有运行证据。根集成已目视核对真实浏览器的用例库、九次实验计数、单案状态和证据。 | 容器默认离线，创建评测限独立测试环境。浏览器启动、取消在途实验、权限拒绝和回读仍待验收。实验列表仅读取 API 默认最近 100 条，界面没有更早实验翻页。失败后可新建独立实验，未见显式的补跑关联字段或入口。 |
| 案例复核 | 实际与预期并排，保留失败原因、评分依据、证据轨迹和复核人、时间、意见。复核带版本号并追加历史。后端有复核幂等、取消、导出和测试角色的纯模块／接口测试。 | 输入与快照在用例库的独立抽屉。单次运行详情未连回原始用例快照，成对差异表也未直达两侧案例结果。浏览器提交复核、版本冲突和二次读取待核验。 |
| 能力指标 | 当前表显示任务成功率及分母、覆盖率、确定性检查、调用审计、已知费用和未知比例、P50／P95、Recall@5、工具调用成功率、草案拦截。场景筛选作用于结果表。 | 未见指标级场景重算或点击下钻。意图准确率／宏 F1／混淆、工具参数合法与业务正确分离、追问与接管准确率、引用支持／版本错误、主动时长与等待分离、升级／备用率、独立最终违规和恢复比率均缺完整指标。最终违规率明确为未知。小样本提供原始计数和分母，未见不确定性区间。 |
| 对照、回归与发布 | 后端核对冻结条件、相同案例和重复编号。前端展示兼容原因、配对完成数和结果变化。未完成配对时保留未配对行，汇总指标留空。门槛保留失败和未知。JSON 包含完整报告，CSV 含案例状态、复核、执行计数、任务成功率与门槛。 | API 返回 `IMPROVED`／`CHANGED`，当前前端未给这两种结果中文映射或新增失败／修复筛选。CSV 为案例摘要，未逐项导出全量指标；未见浏览器下载与页面数值对账。质量回退／费用发布阈值尚未冻结，后端门槛仅产生失败或未完成。 |

浏览器证据：[测试员案例页面](../reports/browser-evaluation-case.png)。

源码：[中台页面](../frontend/src/Evaluation.tsx)、[结果状态标签](../frontend/src/ui.tsx)、[评测 API](../backend/src/aftercare/evaluation/api.py)、[评分和版本比较](../backend/src/aftercare/evaluation/service.py)、[评测模块和接口测试](../backend/tests/evaluation/test_service.py)。

### 页面可读性补齐计划

测试员实际打开单案时，页面默认展示两块较大的 JSON，并直接显示 `HUMAN_REFERENCE_PENDING`。先展示用自然中文组织的摘要，包括预期行为、实际动作、关键失败、模型调用与估算费用、引用条款。费用或引用证据缺失时标注未知。将 `HUMAN_REFERENCE_PENDING` 显示为“案例参考待人工核验”，保留原状态码在展开证据中。将预期、实际、评分和工具轨迹的原始 JSON 放入可展开的证据区，供复核定位。证据：[测试员案例页面](../reports/browser-evaluation-case.png)。

### 已保存的运行状态

- 真实离线队列中台冒烟完成 3／3，均待人工复核。三意图离线核心集为开发 60／验证 30／保留 30，共 120 案完成，均待人工复核。此前 24 项冲突回归因冻结输入变化被拒绝，24 项未启动。该报告不能作为已知硬约束全部通过的证据。证据：[评测冒烟](../reports/evaluation-smoke.json)、[开发集](../reports/dev-regression.json)、[验证集](../reports/offline-validation.json)、[保留集](../reports/offline-holdout.json)、[未启动回归](../reports/offline-regression.json)。
- 真实模型首验失败及修复后单案复验分别保留。复验确认了真实模型来源，结果仍待核验。16:40 的对照报告快照中，四条件总计计划 36 次，覆盖 3 个独立开发案例。单 Agent 经济模型已启动 5 次、结束 4 次，其余三个条件均未启动。该快照没有两组完成的配对结果、独立人工金标或验证集选型后冻结并复测保留集的真实模型证据。证据：[冻结方案](LIVE_COMPARISON_PLAN.md)、[首验失败](../reports/live-smoke.json)、[真实复验](../reports/live-recheck.json)、[经济](../reports/comparison-single-economy.json)、[分析](../reports/comparison-single-analysis.json)、[单 Agent 路由](../reports/comparison-single-routed.json)、[三 Agent 路由](../reports/comparison-multi-routed.json)。

U1 有真实体验部署、业务 API 和代表性浏览器读取证据。U6 已接通测试中台和队列评测。宿主组合检查、浏览器关键写入、完整指标与发布阈值，以及已完成的真实成对结果，分别保留上述验收状态。
