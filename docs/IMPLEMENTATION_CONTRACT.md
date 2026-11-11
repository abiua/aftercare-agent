# 重建实现契约 AC-v1

2026-11-10最新用户要求：按Notion完整售后内容实现多Agent项目，所需API验证可直接执行。范围取代S0停止及旧Notion仅U2/不派发限制。历史源码已删除；docs/notion_snapshot是需求与历史证据，不能当当前实现。保留data/v0.1及digital-sentinel。完整需求见FULL_IMPLEMENTATION_PLAN.md。

## 共享基础（root）
backend Python3.11，包aftercare，数据库SQLAlchemy2/PG，Base在aftercare.db；Settings在aftercare.config。
Settings提供database_url(str),profile(app/test),object_endpoint,object_access_key,object_secret_key,object_bucket,model_config_path(Path),model_enabled(bool),redis_url；密钥不输出。root提供guarded_engine(settings)，只接受本项目独立55450/55451 PG集群。模型真实调用仅root集中执行，所有子Agent只替身/纯模块，不调用付费API、共享PG或真实浏览器。
aftercare.auth.UserContext Pydantic: user_id,role(customer/support/approver/tester),customer_id optional；current_user(Request)依赖从app.state.engine查询User。User ORM在aftercare.db，id/token_hash/role/customer_id/display_name/enabled。API统一/api，错误{detail:{code,message}}，变更要求Idempotency-Key与expected_version。schema_version='ac-v1'。
root维护api.py根装配、配置、认证、共享DB、依赖锁、迁移、infra、启动/验收/评测脚本。

## B 业务与执行
归属 backend/src/aftercare/business/、backend/tests/business/、docs/HANDOFF_BUSINESS.md。
实现独立models.py Base派生、service.py BusinessService(engine,settings)、api.py router、规则、私有S3材料与人工审核、版本化方案/审批/模拟支付账本执行、SSE事件。提供 initialize(engine,data) 导入模拟数据幂等保留历史，角色从公共auth使用。订单/案件/材料/提案/审批/执行/事件ORM归B但注册迁移由root。
核心路由 /orders,/cases,/cases/{id}, /cases/{id}/materials, /cases/{id}/material-reviews, /cases/{id}/proposals, /cases/{id}/approvals, /cases/{id}/execute, /cases/{id}/events。B先给C/D明确schema与服务方法契约。三核心意图 damaged/wrong_item/refund_status，补充已有60案金额/退货/超时场景。不存在对应事实必须未知/补证。
C通过BusinessService只读 context(case_id,user)获取order/policies/materials/reviews/ledger/versions，不给模型写财务工具。C结果 propose 经B规则重验；图状态不等同业务授权。

## C Agent/模型/政策检索
归属 backend/src/aftercare/intelligence/、backend/tests/intelligence/、docs/HANDOFF_INTELLIGENCE.md。
真实LangGraph和PGcheckpointer，三角色协调/政策证据/方案核验，single基线，同预算与工具；可有界返工，硬规则不能模型覆写。run持久化、状态事件、明确启动/恢复/取消接口。与B先协调，根挂router并注入服务。
模型LangChain统一适配，Responses或兼容Chat接口按实际配置，economy/analysis两个不同真实模型；唯一重试所有预算计入。输出验证、调用轨迹、无候选/未知usage明确，别名不可冒充真实模型。政策BM25+BGE中文small512维+pgvector+RRF，适用性/版本/引用校验。依赖需求发root统一锁；只纯测试，实际embedding下载/PG/API由root。

## D 工作台
归属 frontend/、docs/HANDOFF_FRONTEND.md。React/TypeScript/Vite/AntD，登录、客户/客服/审批/测试角色，工单/私有材料上传与审核、三Agent轨迹/恢复、方案与审批/模拟回执、SSE补读、测试中台用例/运行/指标/失败复核/对照。先和B/C确认实际API，不自行另起后端。可以受控mock UI测试但与真实集成分开。端口5186 app/5187 test，API8020/8021。不得启动真实浏览器/PG。锁文件frontend归D，其余根由root。

## 公共规则
金额整数分，未知结果占用额度不盲重试，审批绑定事实/材料/政策版本与payload哈希，外部I/O不持长事务，幂等与所有权每条路由校验，模拟支付绝不真实转账。未实现项明确，禁止placeholder当成功。新数据版本可扩展错发与120核心+24冲突集，但不可覆盖S0冻结数据。改共享接口先发消息，专属文件内自主实现；root审查后串行真实验收。
