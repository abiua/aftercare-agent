# 售后模拟数据 v0.1 契约

本轮仅交付模拟数据、可复现生成/校验工具和开发计划，不接真实商家、支付、模型服务。项目根 /Users/mbw/Documents/aftercare-agent；保留 digital-sentinel。标准库 Python 3.11+，金额均为整数分，时间使用带时区ISO8601，固定观察时间2026-11-10T12:00:00+08:00，随机种子42。所有客户、订单、政策均为虚构；政策不是法律意见或任何平台真实政策。

## 数据生成接口（A负责）

src/aftercare_data/generator.py 暴露 generate(seed=42) -> dict，键 products/customers/orders/shipments/policies，均为list。
30 products，100 customers，300 orders；所有订单关联存在的客户与商品；物流随状态生成。订单多行时折扣分摊精确守恒，已退款不超过实付。
每个order最低字段 id,customer_id,status,created_at,paid_at,shipped_at,delivered_at,items(list),subtotal_cents,discount_cents,payable_cents,paid_cents,refunded_cents,payment_status,cancelled_at。
items最低字段 line_id,product_id,quantity,unit_price_cents,discount_cents,payable_cents,paid_cents,refunded_cents。
产品id P001..P030，客户C001..C100，订单O0001..O0300；policy id POL01..POL12。政策包含version/effective_at/text及机器可校验参数。生成器不得依赖案例/答案模块。
A提供稳定的12条政策并写docs/POLICIES.md，优先覆盖：本人订单、支付状态、未发货取消、已发货拦截、退货时限、损坏补证、签收后退货、重复退款、部分退款、金额上限人工审批、质保、无结果不盲重试。规则间优先级明确。

## 案例接口（B负责）

src/aftercare_data/scenarios.py 暴露 generate_cases(data) -> (cases, answers)，各60条，id CASE001..CASE060。
case含id,split(dev/test),family,utterances,actor_customer_id,order_id,policy_ids,fixture,events。fixture为该案独立完整状态，不修改共享基表，含order及所需业务输入，可复制基表订单后覆盖。不得把期望动作、答案或判定理由放在case输入。复杂情形退款接口timeout/重复提交/审批等由events显式给出输入事件。
answer含case_id,expected_actions,forbidden_actions,expected_final_state,reason,policy_ids。训练/开发40，留出20，分割冻结且禁止同一fixture/措辞复制跨分割；GPT合成的留出不是独立人工金标。产品服务只加载cases与基表，answers放evaluation/private/，不进入工具输出。
B在依赖数据生成器完成前可先设计场景与实现，对齐A最终字段；不得自行改变公共契约。

## 文件归属
A 数据：src/aftercare_data/generator.py、docs/POLICIES.md、tests/test_generator.py。
B 案例：src/aftercare_data/scenarios.py、docs/SCENARIOS.md、tests/test_scenarios.py。
C 计划：docs/DEVELOPMENT_PLAN.md、docs/ARCHITECTURE.md（只文档，读取契约）。
主Agent：共享init/CLI、校验器、集成脚本、README、数据输出、验收报告。各自只改归属文件，接口调整先沟通。

## 输入隔离补充

generate_cases的family/policy_ids用于作者索引，构建器将其移到evaluation/private/case_index.json。实际Agent输入不含案例意图标签、适用政策索引或预期结果；完整业务政策库仍作为只读业务输入。未支付paid_cents=0，payable_cents保留折后应付，折扣分摊以应付守恒。
