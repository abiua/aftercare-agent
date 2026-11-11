# B · 售后业务与模拟执行

## 接入

- 将 `aftercare.business.api.router` 挂到 `/api`，注入 `app.state.business = BusinessService(engine, settings)`。ORM 使用共享 `aftercare.db.Base`。迁移注册需要导入 `aftercare.business.models`。
- `initialize(engine, data)` 可重复导入冻结的 S0 业务基表。再次导入保留实退、库存、审批和审计。每个模拟 SKU 的开账库存为 10。POL13 增补错发举证与换发，POL14 增补损坏处理。S0 文件保留原状。
- C 使用 `context(case_id, user)` 获取订单、可用政策、材料元数据、有效人工审核、规则结果、执行记录和版本。使用 `policy_catalogue()` 读取可索引的当前政策。使用 `validate_draft(case_id, user, expected_version=…, action=…, lines=…, policy_ids=…)` 校验草稿。此入口复用业务规则并保持只读。客服明确提交后才建立审批对象。
- 主流程从 S3 私有桶读写。以内容验证 MIME，接受单张 JPEG、PNG、WebP，上限 8 MiB、2000 万像素。上传后读回验证长度和 SHA-256。鉴权内容路由和 60 秒签名访问路由都按案件归属核验。
- 客户查本人订单、建立本人案件、上传。客服读取和审核、核对履约及退货、提交方案并发起模拟执行。审批人作出处置决定及方案审批。测试员只读业务资源。跨客户按不存在返回。
- 工单变更要求 `Idempotency-Key` 和 `expected_version`。创建时用订单版本，后续用案件版本。相同键同请求复用结果。相同键不同参数返回冲突。重复执行会返回同一账本的当前结果。

## 业务链

1. 客户建立案件，类型包括破损、错发、退款进度、取消、退货、质保。案件同订单同意图同选中订单行归为同一事项。
2. 破损要求商品照及外包裹照，错发要求实物照及标签照。客服核验私有对象后对完整材料版本提交审核意见。新材料使旧审核和方案失效。
3. 取消需要本订单版本的履约复核或已发货拦截确认。退货按订单行检查普通退货时间、商品可退资格、退件收到及验货。质保独立判定可服务的订单行。查进度按历史账本返回，金额余额无法反推某笔退款的结果。
4. 客服按当前事实和政策提交模拟退款或换发。部分退款先由审批人核准当前版本的部分处置，再核验整数分金额。所有执行方案均要求独立角色审批，批准有效期 15 分钟。订单、材料、事实或有效政策快照变化后，旧批准无法执行。
5. 模拟退款在短事务中按订单串行锁定，重验版本、批准、范围和额度并先落保留账本。模拟支付端在独立事务提交同一个业务键的回执。结果写回时更新行已退金额。未知或提交中的金额持续占用可用额度。核对只按原业务键查询，查无已确认回执时继续待核对。
6. 错发换发在订单与相关 SKU 库存锁下复核合计数量，扣模拟库存并与回执在同一事务提交。相同案件或同订单同意图的重叠订单行不能用其他请求键重复执行。
7. 每次已提交业务变更写入连续序号的事件。`GET /cases/{id}/events` 支持 `after`、`Last-Event-ID` 与 `follow=false` 补读。订阅和重连没有写入。

## 路由补充

| 方法及后缀 | 输入或结果 |
| --- | --- |
| GET `/orders`, `/cases` | `{items}`，`limit`、`offset` |
| POST `/cases` | `order_id`、`intent`、`description`、`expected_version`、可选 `line_ids` |
| GET `/cases/{id}` | `case`、`order`、`policies`、`materials`、`reviews`、`ledger`、`versions`、`eligibility`、`facts`、`proposals`、`approvals` |
| POST `/cases/{id}/materials` | 多段表单 `kind`、`file`、`expected_version` |
| POST `/cases/{id}/material-reviews` | `APPROVED`、`REJECTED` 或 `NEEDS_MORE` 与 `notes`、`expected_version` |
| POST `/cases/{id}/facts` | `facts`、`notes`、`expected_version`，可选 `line_ids` 用于指定退件核对的订单行 |
| POST `/cases/{id}/proposals` | `action`、`lines`、`notes`、`policy_ids`、`expected_version`，退款行用 `amount_cents`，换发行用 `quantity` |
| POST `/cases/{id}/approvals` | `proposal_id`、`decision`、`notes`、`expected_version` |
| POST `/cases/{id}/execute` | `proposal_id`、`expected_version` |
| POST `/cases/{id}/executions/{execution_id}/reconcile` | `expected_version`，返回原执行的最新账本 |

`facts` 键可选 `fulfillment_status`、`interception_status`、`return_receipt_status`、`inspection_status`、`resolution_status`，按案件类型及角色核验。材料审核及方案审批必须有书面理由。

## 检查证据

`cd backend && .venv/bin/pytest -q tests/business` 本地 15 项通过。测试使用文件型 SQLite、内存对象适配器、模拟支付故障和 FastAPI TestClient。覆盖身份与版本、真实图片解码、分块上传上限、对象篡改、审核失效、政策及订单变更、批准过期、部分退款、取消、按行退件、错发库存、支付落账后响应超时、支付端无回执、核对恢复和 SSE 游标。并发锁、私有 S3 行为、签名链接以及浏览器仍由 root 在隔离部署复验。

上传中断或版本冲突可能留下私有暂存对象。数据库先记录对象键，重复上传同内容可安全继续。暂存对象无材料记录时无 API 访问入口。当前保留该类对象作恢复和审计，定时清理未接入。支付提交前进程中止可能留下长期未知账本，当前可核对，后续确证失败或成功前保留占用。
