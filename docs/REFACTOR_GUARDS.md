# 阶段3：政策护栏与首轮回放

2026-11-13。实现：`backend/src/aftercare/agent_kernel/retail_guards.py`。依据固定官方版本的 `data/tau2/domains/retail/policy.md`，不使用官方任务答案。新S1/S1c接入该护栏；S0/历史三组保持原样。

## 对照表

| 规则ID | 公开政策条文/章节 | 实现行为 | 正反测试 |
|---|---|---|---|
| authentication | authenticate via email, or via name + zip；即使已有用户ID也必须认证 | 只采纳认证工具的成功结果；保存已尝试路线；未认证只允许认证和回复 | test_authentication_and_reply_routes、test_error_auth_is_never_identity |
| authentication_reply | 同认证条文；同时提供两条路线是计划新增的交互约束 | 回复缺邮箱或姓名邮编之一则反馈补齐 | test_authentication_and_reply_routes |
| single_user | only help one user per conversation | 查询或写入用户不符则拒绝；订单须先由用户资料或已验证订单结果建立归属 | test_single_user_and_known_order |
| single_tool | at most make one tool call at a time；不同时回复 | 保留第一个调用，清空同一步文本，记录原提议与规范化结果 | test_normalization_drops_extra_calls_and_speech |
| order_status | Cancel/Modify pending；Return/Exchange delivered | 用最新工具结果核对；成功写入返回的新状态覆盖旧pending | test_status_latest_write_result_invalidates_pending |
| item_membership/item_variant | Modify items / Exchange：same product, different option, available | 订单内商品数量、两列表长度、同产品有货新款式 | test_variant_membership_quantity_and_availability |
| payment_owner/refund_method/payment_change/gift_balance | Modify payment/items、Return、Exchange | 已有支付方式、退款路线、不能重复原方式、差价/订单额与礼品卡余额比较 | test_payment_owner_refund_change_balance |
| cancel_reason | Cancel：no longer needed / ordered by mistake | 两个固定原因之一 | test_reason_and_structural_confirmation |
| confirmation | list action details and obtain explicit confirmation | 仅核对最近客户可见回复后出现客户消息；是否语义同意交独立审查 | test_reason_and_structural_confirmation |

认证“两条路线每次都必须出现”、订单读取前先取得订单清单，是保守实现选择，不宣称它们是官方政策额外逐字要求。护栏确认的订单状态等事实会传给后续审查；当前阶段尚未接入审查。

## 回放结果及误拦解释

`replay_retail_guards.py` 检查首轮三组1345个公开执行步骤，报告 `evidence/architecture_refactor/guard_replay.json`。总212条反馈：197条认证回复、3条认证前转人工、6条未建立订单归属、6条未读取用户支付方式。

其中官方成功轨迹有130条反馈：122条只询问一种认证方式的回复，4条直接查询客户提供的订单号，4条直接使用订单原支付方式但未读用户资料。每条都登记题号、架构、原文件、消息位置、提议、反馈和解释。不能把这些计作130次真实错误，也不能声称误拦接近零。按当前计划严格交互要求保留，后续应核对额外步骤的成本和客户退出风险。

其余成功步骤未发现状态、商品款式、取消原因或确认结构的拦截。任务60、61的归属/支付拦截可以用一次用户资料查询同时补齐，不需要更改客户选择或官方答案。

首轮认证阶段退出的5/9/39/45/56/68等轨迹触发两路线规则。由于客户模拟有随机性，这只证明反馈触发，不证明能挽回客户退出。

## 验证及剩余

内核/契约/护栏14项通过；官方适配、评分、角色预算与新S1补问路径14项通过；S0的40题454步再次一致；原始冻结文件哈希未变。均为离线，费用0。

仍需随阶段4落实：审查接收已确认事实、确认语义与引用核验，以及审查后新提议再次检查硬规则。护栏重试失败当前抛出GuardRejected，由后续产品接入保存原会话并处理，绝不返回该写操作执行。
