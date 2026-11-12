# τ² Retail P0 现状审计

核对时间为 2026-11-12 15:07（北京时间）。本次检查读取本地源码、Git、历史验收记录和公开语料归档，并运行语料生成器的只读校验。未启动服务、查询数据库或调用模型。

## 当前工程和运行状态

| 项目 | 本地代码与既有证据 | 本次核对边界 |
| --- | --- | --- |
| 主会话、视觉和客服接管 | `backend/src/aftercare/conversations`、`conversation_analysis`、`decisions` 及前端 `CustomerChat.tsx`、`ChatDecisions.tsx`、`CaseMaterials.tsx` 已实现相应入口。退款闭环、双商品补证、账本查询三个演示已准备，订单和退款为模拟数据。[演示指南](DEMO_GUIDE.md) · [接线记录](HANDOFF_INTEGRATION.md) | 本次未打开页面、重放对话或回读旧工单。 |
| 业务规则和执行 | `backend/src/aftercare/business`、`intelligence`、`investigation` 提供业务、工具和调查能力，已有客服核对、客户确认、独立审批、模拟退款以及账本查询的演示记录。[业务交接](HANDOFF_BUSINESS.md) · [决策协议](CHAT_EVIDENCE_DECISION_CONTRACT.md) | 不宜把 τ² 上游 API 的动作或政策解释为现有审批/支付的授权。 |
| 已有评测 | `backend/src/aftercare/evaluation` 有既有评测、聊天评测和正式执行路径，前端有评测页和盲审页；部分历史版本完成过真实隔离 PG 或浏览器核对。[评测交接](HANDOFF_EVALUATION.md) · [正式验收记录](FINAL_ACCEPTANCE_REMAINING.md) | 既有测试覆盖部分受控路径。当前无法实查运行镜像版本。 [P5 审查](CHAT_P5_ROOT_REVIEW.md) · [P4 审查](CHAT_P4_ROOT_REVIEW.md) |
| Git | 初检工作区干净，`main` 位于 `516d73da65b0f6c424d6a5be5f1db4002fb39634`，与本地 `origin/main` 缓存各领先 0 个提交。 | 未刷新远端引用，未提交。 |
| 运行 | 默认 Colima 报告未运行，Docker socket 不可用。宿主机未见 5213、5215、5186、5187、5203、8020、8022 的 TCP 监听。 | 容器清单、数据库内容与镜像标记无法实查。5215 和 5213 为文档登记的演示入口。 [启停说明](QUICK_SERVICE_CONTROL.md) |
| 备份 | 项目有 PostgreSQL 一致性快照、对象存储归档及隔离恢复流程和历史验收。 [备份恢复说明](BACKUP_RESTORE.md) | P0 已保存并校验 `.runtime/tau-p0-baseline-20261112` 中的 544 个已跟踪文件归档和 558 个（其中 557 个为既有文件，1 个为并行实施时新建的 `data/tau/manifest.json`） data/reports 文件哈希。数据库与对象存储未备份，现有数据卷未实查。P1 不触碰旧存储，一致性业务备份延期到首次修改旧存储之前。 |

## 当前已知缺口

- **误判与纠错。** 5215 的真实模型提出 L01/49 元退款且程序校验通过，原候选保留未决事项，客服记录处理理由后保持原商品行和金额并完成模拟闭环。这个单例没有独立质量判定。5213 的双商品案例完成事实、政策和核验调用，客服公开两项补证缺口；标签冲突的显式识别和真实定向补查均未通过验收。受控 PG 消融已覆盖一次局部补查，供应方调用为零。现有证据足以指出冲突漏检风险，误判率和纠错收益仍未知。 [最新接线记录](HANDOFF_INTEGRATION.md) · [受控消融](CHAT_P5_ABLATION_PLAN.md)
- **结构化补证。** 自定义缺项的小修有本地 HTTP 证据，尚未部署到保留的旧演示栈。V7 工具参数、政策版本与证据投影已有修复回归；历史审查中的某次镜像未部署仅对应当时批次。 [最新接线记录](HANDOFF_INTEGRATION.md) · [修复验收](CHAT_TOOL_ARGUMENTS_ACCEPTANCE.md)
- **质量、路由和并发。** 尚无独立标注的正式保留集结果、同预算真实模型三臂比较或分场景路由准确率与收益。历史 40 次并发 4 的本地只读 HTTP 检查及部分 PG 并发验证已有记录，客户会话到 Worker 和模型供应方的 10/30/100 并发下成功率、排队时间和吞吐尚未测量。 [正式验收记录](FINAL_ACCEPTANCE_REMAINING.md) · [实施计划](TAU_IMPLEMENTATION_PLAN.md)

## 既有 τ² 和表达数据

- 本仓库 `data/corpus` 的 `public-expressions-0.1` 含 120 条 Bitext 英文客户表达及中文改写，分为 dev 60、validation 30、候选 holdout 30。人工复核和业务金标待确认。表达包未纳入 τ² 对话、答案、政策，也未纳入 Bitext 的 `response` 字段。 [表达包说明](../data/corpus/README.md)
- τ² Retail 用作任务与分组结构参考。固定 revision 为 `da0835f6c67baef58bdda18f2a3b037dfb3a1754`，114 个任务，官方 train 74、test 40，任务 ID 互斥。仓库有来源及摘要登记、MIT 许可和生成器。以既有本地来源目录执行 `scripts/build_public_corpus.py --check` 成功。该校验覆盖已登记输入和当前生成件的一致性。 [清单](../data/corpus/manifest.json) · [生成器](../scripts/build_public_corpus.py)
- 仓库外的既有 `raw/tau2_retail` 目录另含 `db.json`、`policy.md`、`data_model.py`。本次只确认这些文件存在。若 P1 使用清单外资产，需逐项登记固定来源和摘要，再以本地只读输入使用。应用源码目录未检出 `tau2` 或 `τ²` 文本引用，现有证据未显示 τ² Retail 已接入在线业务或评测运行链。

## P1 隔离验收

1. 以独立目录和入口管理 τ² 输入、转换输出、样例及报告，使用独立标识，保留上游 revision、许可、任务 ID 与 split。现有表达包、评测集、历史报告和项目演示数据保持原样。公开的 upstream test 仍标作 test，禁止混入开发样例或称为独立封存保留集。
2. 使用纯文件或临时隔离状态做确定性验收。默认入口零模型调用、零现有 API 写入，禁止接入旧 PG、Redis、S3、工作队列和支付执行器。上游动作校验限隔离的合成状态；缺失或未映射的工具、状态和动作要明确报出。
3. 复验固定输入与许可、114/74/40 和分组互斥、同一输入可复现、失败不留下半成品或覆盖历史证据。以最小样例核验分组、输出结构与动作前后差异。验收报告保留实际分母、失败和未运行范围，写明覆盖的是本地导入和受控验证。旧环境保持停止，后续真模型和真实栈另行验收。
