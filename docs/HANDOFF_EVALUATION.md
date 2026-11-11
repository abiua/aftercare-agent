# E · 评测中台和隔离运行器

## 接入

- 挂载 `aftercare.evaluation.api.router` 于 `/api`。服务读取 `app.state.evaluation = EvaluationService(engine, settings, model=agent.model, dispatch=...)`。
- 在迁移中注册 `aftercare.evaluation.tables` 的四张表。创建实验时同一事务保存计划、用例及重复编号、评分参考、配置和调用配额。API 和 Worker 启动时记录已加载代码指纹。运行期间发现磁盘代码变化时，创建与执行要求先重启。队列使用已提交的记录唤醒 worker。
- Worker 构造 `EvaluationRunner(engine, settings, checkpointer, model=agent.model)` 并调用 `EvaluationService(...).process(experiment_id, runner)`。独立测试环境有权启动和取消。测试员权限与客服、财务审批权限分开。
- 列表、详情、取消、结果复核、对照、JSON 和 CSV 导出均在 `/api/evaluation` 下。CSV 对可能被表格当作公式的文本加前缀。启动和取消或复核使用请求幂等键，取消和复核还需提供所读版本。

## 执行与证据

1. 默认读取 v0.2 冻结数据。选定划分后可使用 `case_ids` 指定小样本。原样记录案例哈希、参考哈希、代码、评分器、模型注册、嵌入条件及计划配置。
2. 每次尝试在独立 PostgreSQL schema 中运行真实业务服务和 Agent 图。合成身份只在该 schema 中登记。存储对象键带尝试命名空间，图线程与业务案例独立。实验结束后保留该 schema 供审计。普通工单和执行库的查询范围仍为应用 schema。
3. 对象写入只接受测试 SeaweedFS 的宿主机或 Compose 服务地址。客户和客服脚本走业务角色及版本校验。预期行为与标签只在脚本运行后由评分器读取。业务使用每案冻结时钟，真实模型耗时使用运行时钟。
4. `fixture` 走规则模式应用与真实图，`load` 使用同样执行链但分开归档。并发由实验参数限定。`fault` 使用显式的时钟、支付、模型 transport 或并发注入。模型 transport 中断复用真实尝试账本和图恢复入口。它属于进程内故障证据。
5. `live` 只在已配置模型且明确启动时使用 provider。每个案例在创建时获得固定调用和估算费用配额，配额合计不超过实验上限。C 在同一 run 的补证恢复前后累计计入模型尝试。未知用量和费用保留为空。超额或无配额时停止，保留未运行与中途失败。
6. Cancel 停止后续调度，并在已启动脚本的事件边界与每次模型调用登记事务中检查取消和操作者权限。已经发出的请求及费用保留。worker 中断后的旧尝试记录为执行错误，待运行项可以继续。已落账的尝试和事件从对应评测 schema 补读，报告将仍在 STARTED 的调用记作 UNKNOWN，并保留原始状态。相同实验的队列重复投递由 PostgreSQL 排他锁保护。
7. 成对比较检查案例、重复编号、数据与政策快照、模型注册、代码、评分版本、预算、并发和证据来源。模型策略对照固定单 Agent。Agent 数量对照固定模型策略。尚未全部完成配对时，报告显示完成对数并保留逐案状态，差异指标等待完整配对。

## 统计与核验边界

- 成功率分母为已启动案例，失败、超时、执行错误和待复核都保留。覆盖率分母为计划数。重复编号为独立尝试，同一案例 ID 仍对应同一业务样本。
- 确定性检查分为 PASS、FAIL、UNKNOWN。字段缺失和显式空值分别评分。人工复核按版本追加，旧结论仍可查看。确定性失败和实际未执行的结果不能被复核成通过。
- 合成参考默认待人工核验。人工通过一次结果复核仍保留数据金标状态。发布门槛展示证据覆盖、确定性检查、人工金标、真实模型和发布阈值。阈值待独立冻结，当前没有自动放行。
- 检索 Recall@5 的分母为有实际检索记录的案例所列条款，参考来源单独标注。工具成功率依据实际调用，最终违规率等待独立审计。耗时 P50/P95 附样本数，包含脚本和隔离环境准备。
- schema 和私有对象目前保留审计，长期清理尚未接入。

## 检查

本地测试使用 SQLite、临时独立业务库、内存对象适配器、真实 LangGraph 与内存 checkpoint。生产部署证据由 root 在 PostgreSQL、SeaweedFS、Celery 和浏览器环境采集。已有隔离运行三条离线应用脚本（破损、错发、退款进度），保留三个待人工复核结果。见 `reports/evaluation-smoke.json`。

本地全量脚本重放分别在单 Agent 与三 Agent 各计划 144 案。每种配置均有 143 案待人工复核，671 条确定性检查通过。并发回归 AC02-R020 需要 PostgreSQL，在本地 SQLite 留下 1 项执行错误及 1 条未知检查。该结果使用合成输入、内存 checkpoint 和对象替身，真实模型调用为 0。

## 扩展评测入口

顺序执行器直接使用测试 PostgreSQL、S3 和 PostgreSQL checkpoint。启动前须完成隔离测试环境初始化。

```bash
PYTHONPATH=backend/src backend/.venv/bin/python -m aftercare.evaluation.cli \
  --split dev --case AC02-C001 --case AC02-C041 --case AC02-C081 \
  --mode multi --output .runtime/evaluation-sequential.json
```

默认使用离线规则。真实模型需要明确 source、案例 ID、调用次数和费用限额，以及进程环境中已配置的模型凭据。可选的真实意图探针仅输入客户文本，与图共用本次调用和费用上限。离线意图探针使用独立规则，输入无需预填意图。

- 报告导入验证原始 UTF-8 内容摘要、格式和执行计数，记录操作者及声明来源。导入记录与真实实验隔离，证据标记 imported_unverified。
- 补跑需明确原实验及结果版本，建立原实验与原结果链接，使用独立业务快照和运行。已有的失败、费用和人工意见保留。
- 负载来源要求至少两个案例执行通道。每案追加受权工单和事件读取，并记录数量、耗时和成功率。该压力针对应用服务路径。
- 各项新增指标记录分母与未知数。引用支持、关键事实覆盖和部分参数正确性需要逐项复核。离线或已完成案例仍可能缺人工标注或观察。
- 门槛从环境变量 AFTERCARE_EVALUATION_GATE_PATH 指定的 JSON 数组加载。每项包含 id、version、name、source、thresholds、required_checks，可选 additional_metrics。配置冻结到新实验。已取得的独立金标用 AFTERCARE_EVALUATION_GOLD_PATH 指定 JSON 数组，记录 dataset_id、case_id、case_sha256、reference_sha256、reviewer_id、reviewed_at 和 source。两个摘要由用例和参考 JSON 经业务 digest 函数计算。系统检查摘要后保存审核来源及日期。当前合成数据的人工审核状态由实际审核材料决定。
- 技术门槛支持 PASS、FAILED、INCOMPLETE。全部技术门槛达到时仍待独立发布批准。
