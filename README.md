# 电商售后多 Agent 工作台

当前评测已切换为固定版本官方 τ²-bench Retail：40 个测试任务，强单 Agent、单 Agent 三阶段、多 Agent 各运行一次，使用官方评分器。旧自定义评测用例库及对应 P8/P9 对比已移除；官方主批次与一次技术补跑已结束，强单29/40、三阶段25/40、多 Agent25/40（含1条预算未完成）；最终结果见 [官方评测](docs/TAU_OFFICIAL_EVALUATION.md)。[测试中台](http://127.0.0.1:5225/?eval=campaigns) 可选择测试员直接进入；[环境安装](docs/TAU_OFFICIAL_SETUP.md)、[命令入口](docs/TAU_P6_P9_OPERATIONS.md)。P6 失败回退与会话找回、P7 问题目录、P9 路由能力继续保留。以下早期部署段落只作历史记录。

历史阶段记录：2026-11-12：τ-bench P0—P4 已完成阶段验收，P5 已修复本轮超时与参数问题，两条真实复杂案例形成待人工建议并通过浏览器回读，受控定向补查纠错通过；自然定向补查及完整政策覆盖仍未全部证实，退款候选未执行。P6 未开始。见 [P5续验收与保留项](docs/TAU_P5_CONTINUATION_ACCEPTANCE.md)。P4 已接入客户聊天政策检索，真实模型49元模拟退款闭环和更正后会话记忆通过实际接口、浏览器与重启验证。见 [P4验收](docs/TAU_P4_ACCEPTANCE.md)、[独立演示运行](infra/TAU_P4_RUNTIME.md)。P3 已实测 Milvus/pgvector、混合召回、语义重排、持久化与引用生成；18条开发查询上结构重排 Recall@5 为80.56%，仍保留缺证据坏例。见 [P3验收](docs/TAU_P3_ACCEPTANCE.md)、[完整计划](docs/TAU_IMPLEMENTATION_PLAN.md)、[OCR技术选型](docs/TECH_STACK_OCR.md)。以下历史演示与记录保留。

当前P4演示入口 http://127.0.0.1:5224/ 点击客户、客服、审批人或测试员即可进入，无需输入账号密码。角色权限和原工单保持；验证见 `reports/demo-role-login.json`。下文旧体验栈登录说明为历史部署记录。

快速启动/关闭/重启：见 [一键启停说明](docs/QUICK_SERVICE_CONTROL.md)，也可以双击项目根目录的四个 `.command` 文件。

2026-11-12 当前以可用演示版收尾：[三个演示场景与操作步骤](docs/DEMO_GUIDE.md)。普通退款已完成真实模型提案、客服核对、客户确认、独立审批和 49 元模拟回执；双商品案例可在原聊天补证。正式质量对照和扩展机制暂缓。

2026-11-11 起按[聊天售后增量改造计划](docs/CHAT_REDESIGN_PLAN.md)推进：客户聊天、Agent 调查并提出决策、客服审核。该计划明确区分现有能力与待建能力；下文为此前可用工作台，不能视为新聊天架构已完成。

当前聊天增量的工程证据见 [P4 调查审查](docs/CHAT_P4_ROOT_REVIEW.md) 与 [P5 盲审审查](docs/CHAT_P5_ROOT_REVIEW.md)。P5 独立测试环境已通过实际接口、浏览器和备份恢复验证；真实模型调查仍有结构校验失败，三组效果比较与独立人工评测尚未完成。

面向模拟电商售后的可运行项目：客户提交工单，助手查询业务事实和政策、识别缺失材料并提出草案；客服审核材料和方案，独立审批角色批准后，由执行器办理模拟退款或换发。所有金额为整数分，模型意见不能绕过业务规则或人工审批。

本项目按完整 Notion U1—U6 计划重新实现，没有恢复已删除旧项目的代码、客户资料或照片。`digital-sentinel` 独立保留。当前工程仍在最终验收：真实模型首验失败及后续修复均留有记录，合成参考未冒充人工金标，发布门槛尚未通过。

## 体验与运行

需要零模型费用的独立浏览器演示时，可按[演示启动说明](docs/DEMO_WALKTHROUGH.md#独立离线演示环境)运行 `python3 scripts/offline_demo.py start`。此命令使用独立资源，不修改下面的已有体验栈。

本机现有体验栈在 <http://127.0.0.1:5186>，已启用真实模型和本地混合检索。退款和库存操作均为模拟。登录使用本机 `.runtime/users-app.json` 中对应客户、客服、审批人或测试员的 `token`。测试工作台使用 <http://127.0.0.1:5187> 及其对应的 `.runtime/users-test.json`。凭据文件仅保存在本机。

维护现有体验栈时，使用操作手册中的 live overlay 和镜像覆盖命令。2026-11-10 场景指标验收时，体验栈前端为 `financial`，后端为 `context`。测试工作台 `5187` 已提供场景指标和贡献案例下钻，静态入口为 `index-BGOLR24m.js`。已有对应端口监听时，请复用现有服务。[现有栈维护](docs/OPERATIONS.md#维护本机现有体验栈)

场景指标通过真实测试 API 的 126 项核对。浏览器只读验收覆盖场景切换、指标贡献、精确重复编号和深链接，场景选择由用户协助一次。该轮使用零模型调用的合成案例，浏览器未写入复核。[真实接口](reports/scenario-metrics-http.json) · [真实浏览器](reports/browser-scenario-metrics.json) · [场景指标说明](docs/SCENARIO_METRICS_PLAN.md#实施后的口径补充)

以下命令供新安装或空闲环境启动默认离线基线。完整步骤见 [操作手册](docs/OPERATIONS.md#全新安装离线基线)。

```sh
python3 scripts/setup_runtime.py
./scripts/build_images.sh
AFTERCARE_UID=$(id -u) AFTERCARE_GID=$(id -g) docker-compose --env-file /dev/null \
  -f infra/compose.yml -f infra/compose.application.yml --profile app up --no-build -d --wait
```

Docker Compose 插件可用时，将 `docker-compose` 换为 `docker compose`。应用和测试环境使用独立数据库、对象桶、Redis、网络和端口。测试中台实际实验只在 `test` 环境执行；本机测试 API 为 8022，应用 API 为 8020。首次启动生成的随机凭据只写入权限为 0600 的私有文件。

离线基线默认关闭真实模型。启用后，受控进程读取私有注册表及密钥，并限制调用数、时限和估算费用。缓存读写计价、未知用量和失败尝试分别记录。

## 功能与结构

| 位置 | 作用 |
| --- | --- |
| `frontend/` | React/Ant Design 工作台：材料、审核、Agent 轨迹、审批、回执、评测比较与场景下钻 |
| `backend/src/aftercare/business/` | 订单归属、私有 S3、规则校验、版本审批、库存与幂等执行账本 |
| `backend/src/aftercare/intelligence/` | LangGraph 三角色与单 Agent、PostgreSQL 检查点、模型路由和 BM25/BGE/pgvector 融合检索 |
| `backend/src/aftercare/evaluation/` | 隔离快照、脚本重放、批量实验、调用预算、复核、可比性检查和导出 |
| `backend/src/aftercare/queue.py` | Celery/Redis 投递；数据库待运行记录支持漏发与重启恢复 |
| `backend/src/aftercare/backup.py` | 数据库一致快照、材料哈希校验及恢复到独立新库/新桶 |
| `infra/` | 锁定依赖、CPU 镜像、Nginx 与 Compose 配置 |
| `reports/` | 实际验收结果，包含失败与未完成项 |

三角色分别协调事实、核验政策、审查方案。发生分歧时最多一次整轮返工，仍不一致则交人工处理。只读政策及业务工具有权限边界；模型不能自行审批、直接退款或操作任意对象。

材料和方案绑定版本。材料、订单事实、政策或审批有效期变化后，旧授权不能继续使用。支付响应未知时保留额度，按原业务键核对，不盲目重复付款。数据库检查点保存工作流状态，业务账本负责执行幂等。

## 数据和验证边界

- `data/v0.1/`：只保留演示业务底库（30 商品、100 客户、300 订单），自定义评测案例及参考答案已删除。
- `data/v0.2/`：旧案例和评分材料已删除，清单仅保留版本识别信息，禁止恢复为活跃评测库。
- `data/tau/manifest.json`：官方数据与源码的固定版本和校验值；实际评测运行官方原始任务、环境和评分器。
- 图像是明确标记的合成测试材料，只验证上传、存储和人工审核流程，不宣称自动识别真实破损。
- 数据及实现作者已接触案例，不能称为独立盲测；人工金标和单次结果复核分别记录。
- fixture、真实模型、故障注入与负载实验分开报告。失败、超时、未运行和未知费用不抹掉；未完成的对照不显示误导性比较指标。

检查入口：

```sh
./scripts/check.sh pure
# 仅限独立测试服务、关闭真实模型后，由集成负责人串行运行：
AFTERCARE_RUN_PG=1 ./scripts/check.sh pg
```

参考 [完整要求矩阵](docs/FULL_IMPLEMENTATION_PLAN.md)、[官方评测说明](docs/TAU_OFFICIAL_EVALUATION.md)、[智能工作流](docs/HANDOFF_INTELLIGENCE.md)、[业务执行](docs/HANDOFF_BUSINESS.md)、[评测后端](docs/HANDOFF_EVALUATION.md)、[备份恢复](docs/BACKUP_RESTORE.md) 与 [集成证据](docs/HANDOFF_INTEGRATION.md)。S0 阶段文档保留其当时边界，不代表当前实现仍停留在数据准备。

最近一次部署验收记录为后端 `acceptance-20261110-formal`、前端 `acceptance-20261110-refund-fixed`，包含正式评测执行接线、逐项复核、场景统计与指定退款查询。升级时数据与凭据核对一致；完整质量验收仍未完成。[部署记录](reports/deployment/formal-app.json)

## 仓库与本机验收记录

源码仓库为私有仓库 `abiua/aftercare-agent`。仓库包含源码、锁文件、合成数据、测试和开发文档；`.runtime/`、凭据、模型缓存及 `reports/` 本机运行证据不上传。文档中的 `reports/` 链接需要在原工作目录或运行相应验收脚本后查看，仓库本身不代表这些历史验收已经复现。GitHub Actions 执行离线程序及受控浏览器测试，不注入真实模型凭据，不代表 PostgreSQL/S3、真实模型质量或人工发布审批通过。

[首次完整通过的 GitHub 检查](https://github.com/abiua/aftercare-agent/actions/runs/42945574844)对应提交 `1db1425`：数据测试18项、后端801通过（1跳过、30项数据库测试未选）、前端37项、受控浏览器27项，lint及构建通过。此前缺少本机报告的失败已保留，阅读卡回归现使用入库夹具，不再依赖 `reports/` 目录。
