# P1 会话接口契约（实施基线）

2026-11-11。配合 CHAT_REDESIGN_PLAN.md；当前为实现契约，未验收。不要把P1程序回复描述为真实视觉/Agent能力。后续改契约先通知另一端。

所有路径以 `/api` 为前缀，沿用Bearer身份和业务错误格式、`Idempotency-Key`。客户只访问自己会话；support可处理会话；approver/tester不自动获得客户消息写权限。服务端决定角色，不能由客户端提交role。

## 返回结构

- Conversation：`id, customer_id, status(OPEN/HUMAN_HANDOFF/CLOSED), revision, created_at, updated_at, active_case_id(nullable), active_order_id(nullable)`。
- Message：`id, conversation_id, sequence, role(customer/support/assistant), text, created_at, attachments[], cards[]`。
- Attachment：`id, filename, content_type, size, sha256, content_path`。`content_path`是需鉴权的站内路径，不提供对象密钥或无鉴权永久地址。
- Card：`type(order_candidates/case_link/status), data`；订单候选的`data`包含`orders, new_issue`，每个订单含`id, items, products, status, version`，客服选择的是订单/商品，不是内部售后类别。case_link含`case_id, order_id`。
- GET list采用现有`{items:[]}`风格；conversation detail返回`{conversation, messages, has_more, next_after}`，按sequence升序有界分页。

## 端点

1. `POST /conversations` JSON `{}`，幂等创建（customer）；返回Conversation。
2. `GET /conversations?limit=&offset=` 当前角色授权范围，返回`{items:Conversation[]}`。
3. `GET /conversations/{id}?after=0&limit=100` 返回detail。
4. `POST /conversations/{id}/messages` multipart：`client_message_id`（UUID）、`text`（可空）、重复`files`（图片0–6张，总量与单张均不超过8 MiB）。text与files至少一项；消息ID+请求摘要幂等，重发同ID不同内容冲突；完整提交后消息可见。返回`{conversation, messages:[本次新增或复用消息]}`。客户和support可发消息，服务器决定role。support文本直接作为公开回复并进入`HUMAN_HANDOFF`，不能把内部trace自动投影。
5. `GET /conversations/{id}/attachments/{attachment_id}/content` 鉴权图片流，校验摘要；附件只能属于该会话。
6. `POST /conversations/{id}/context` JSON `{order_id, line_ids:[], expected_revision, new_issue:false}`，幂等；仅客户明确选择本人候选对象，无intent/model/mode。返回`{conversation,messages}`。换订单/新事项先展示确认，不把旧图默认转挂；普通补充关联既有active_case。未能从文本明确内部意图时自然语言澄清或转人工，不能要求客户选内部枚举。
7. `POST /conversations/{id}/handoff` JSON `{expected_revision}`，幂等；返回`{conversation,messages}`。客户和客服均可请求接管。保持原消息、附件和已关联工单。

## 首期执行边界

- 客户输入、客服回复、对象确认和首次接管推进会话revision，程序回执只占用消息sequence。已确认对象的回执含`ORDER_CONFIRMED`，仍需选择商品时保留候选卡。
- 后端复用现有intake规则和订单授权；明确查询退款记录，读取实际账本返回确定性结果，无模型调用。明确本人订单可直接查询退款；多商品订单的退款进度按订单查询。对象不明确只返回本人订单候选。
- 多商品范围不清楚时确认商品；不能默认为整单。绑定有可恢复的事务/状态，重试不重复建单；无法安全匹配的既有工单需确认。
- 图先于订单确认可上传并留在原会话；P1只保存附件，不能伪造材料kind或声称图片已审核。P2才做视觉观察和材料桥接。
- P1收到图片回复可以只确认收到；不立刻逐张催补证。连续输入聚合与旧在途分析发布栅栏在P2交付，P1不启动未实现的模型调查。
- 客户默认聊天，历史工单链接仍可查看；原表单保留在后台/兼容页面。客户不可见内部分类/模式选择。
- 新数据库表通过新增迁移；不得重置现有库、修改历史评测或现有检查点。数据库先登记私有暂存，收到全部对象并逐一核对后才公开消息。超过24小时且尚未公开的暂存可用`python -m aftercare.conversations.cleanup --profile test`预检，附`--execute`清理，每批最多100项。旧消息编号保留过期标记，已公开附件不参与清理。

## P1最小端到端

客户发退款查询→必要订单/商品确认→读到账本回复；同会话补文字+两张图→刷新可回读；客服收到该会话并回复→客户看到回复；全部经过真实HTTP/PG/对象存储。另验证另单确认、跨客户越权和重复提交。P1完成仍不代表P2–P5完成。
