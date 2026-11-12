# 首轮失败逐条复核备注

机械结局“错误写入”仅表示官方失败且曾执行写工具，不等同于每次写操作违规。备注依据公开对话和工具轨迹；不作为 Agent 输入或 E1 自动标签。

| 任务 | 架构 | 结局 | 直接观察及局限 | 政策依据 | 对应层 |
|---|---|---|---|---|---|
| 5 | strong_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 5 | staged_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 5 | multi_agent | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 9 | staged_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 9 | multi_agent | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 9 | strong_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 18 | staged_single | handoff | 客户把退货改为同款替换后转人工；官方目标未满足，不能直接认定转人工不合理 | Exchange delivered order | 审查：核对变更后的诉求 |
| 18 | multi_agent | handoff | 客户把退货改为同款替换后转人工；官方目标未满足，不能直接认定转人工不合理 | Exchange delivered order | 审查：核对变更后的诉求 |
| 18 | strong_single | handoff | 客户把退货改为同款替换后转人工；官方目标未满足，不能直接认定转人工不合理 | Exchange delivered order | 审查：核对变更后的诉求 |
| 27 | strong_single | handoff | 同单退货与换货冲突后整体转人工，未让客户选择可行部分 | Return / Exchange delivered order | 放弃审查 |
| 27 | staged_single | handoff | 同单退货与换货冲突后整体转人工，未让客户选择可行部分 | Return / Exchange delivered order | 放弃审查 |
| 27 | multi_agent | handoff | 同单退货与换货冲突后整体转人工，未让客户选择可行部分 | Return / Exchange delivered order | 放弃审查 |
| 32 | staged_single | handoff | 遗失平板的退款或重购超出工具能力；是否完整告知物流信息需独立判断 | provide information; transfer if and only if outside scope | 放弃审查：逐项核对信息请求 |
| 32 | multi_agent | handoff | 遗失平板的退款或重购超出工具能力；是否完整告知物流信息需独立判断 | provide information; transfer if and only if outside scope | 放弃审查：逐项核对信息请求 |
| 32 | strong_single | handoff | 遗失平板的退款或重购超出工具能力；是否完整告知物流信息需独立判断 | provide information; transfer if and only if outside scope | 放弃审查：逐项核对信息请求 |
| 36 | multi_agent | handoff | 取消单件商品不支持后转人工，未继续协商整单取消等替代方案 | Cancel pending order | 放弃审查 |
| 38 | staged_single | wrong_write | 取消原因与官方目标不同，但客户实际确认 ordered by mistake；保留官方0分，不认定参数必错 | Cancel pending order: two reasons and confirmation | 评测解释；提交审查不能覆盖客户确认 |
| 38 | multi_agent | wrong_write | 取消原因与官方目标不同，但客户实际确认 ordered by mistake；保留官方0分，不认定参数必错 | Cancel pending order: two reasons and confirmation | 评测解释；提交审查不能覆盖客户确认 |
| 39 | multi_agent | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 39 | strong_single | handoff | 客户要求其他身份验证方式，未认证即转人工；不能擅自拆用户名 | authenticate via email or name + zip | 认证护栏 / 放弃审查 |
| 39 | staged_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 42 | multi_agent | wrong_write | 先改地址后以通用一次修改规则为由拒绝已确认商品修改；这是部分完成后官方0分，不等于已执行的地址写错 | Modify pending order items; generic once per order | 审查：具体章节优先 |
| 45 | multi_agent | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 45 | staged_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 56 | staged_single | handoff | 无法提供姓名或邮箱，未认证转人工 | authenticate via email or name + zip | 认证护栏 / 放弃审查 |
| 56 | multi_agent | handoff | 单件取消不支持后转人工 | Cancel pending order | 放弃审查 |
| 56 | strong_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 60 | multi_agent | wrong_write | 已执行蓝色耳机替换，但款式与官方目标不同；客户是否表达防水约束应按当前对话核验 | Modify pending order items; confirmation | 提交审查 / 评测解释 |
| 60 | staged_single | wrong_write | 已执行蓝色耳机替换，但款式与官方目标不同；客户是否表达防水约束应按当前对话核验 | Modify pending order items; confirmation | 提交审查 / 评测解释 |
| 62 | staged_single | handoff | 转人工处理诗歌问题，官方目标未达；对话与官方任务的偏离不能算架构可修复错误 | deny requests against policy; scope | 放弃审查，保留不确定性 |
| 64 | strong_single | inaction | 已认证并找到相机，追问是否该商品后客户退出，没有执行写操作 | provide information / Modify pending order items | 执行者 / 放弃审查候选 |
| 68 | staged_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 68 | multi_agent | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 68 | strong_single | authentication_failed | 只提供用户名等不完整认证资料，客户在补问后退出；无成功认证 | authenticate via email or name + zip | 认证护栏；不得绕过认证 |
| 74 | staged_single | wrong_write | 商品替换和另单取消均已执行但官方0分；需逐字段核对目标，不从0分推断具体参数错误 | Modify pending order items; Cancel pending order | 提交审查；待字段级根因复核 |
| 77 | multi_agent | handoff | 改为另购或要求核实同香型后转人工；新购超出工具范围，不能把此类一律标成不当放弃 | Exchange delivered order; scope | 放弃审查，确认诉求变更 |
| 77 | strong_single | handoff | 改为另购或要求核实同香型后转人工；新购超出工具范围，不能把此类一律标成不当放弃 | Exchange delivered order; scope | 放弃审查，确认诉求变更 |
| 77 | staged_single | handoff | 改为另购或要求核实同香型后转人工；新购超出工具范围，不能把此类一律标成不当放弃 | Exchange delivered order; scope | 放弃审查，确认诉求变更 |
| 101 | multi_agent | unfinished | 已执行两单修改，收尾仍运行常驻顾问，耗尽48次调用，无官方评分 | one tool at a time; bounded runner | 按需审查和专家 / 预算 |
| 111 | strong_single | handoff | 拆分地址请求不支持导致整体转人工，遗漏仍可协商的两单商品修改 | Modify pending order address/items | 放弃审查 |
| 111 | staged_single | handoff | 拆分地址请求不支持导致整体转人工，遗漏仍可协商的两单商品修改 | Modify pending order address/items | 放弃审查 |
