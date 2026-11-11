# 模拟政策边界审计

Notion 要求每条政策至少两个边界例。本表覆盖当前 14 条基线政策，版本均为 1.0。POL01–POL12 来自冻结基线，POL13 与 POL14 由运行时种子补入。替代政策版本由独立的版本选择测试验证。业务准入、执行权限和条件引用分别取证。 [边界例要求](notion_snapshot/evaluation.md#L110) · [冻结政策](../data/v0.1/agent/business.json#L10913) · [扩展条款](../backend/src/aftercare/business/seed.py#L12)

| 条款 | 至少两个具体边界 | 证据与剩余缺口 |
| --- | --- | --- |
| POL01 本人订单 | ① 所属客户可建单并读取本案。② 其他客户看不到订单，也无法按 ID 读取订单、案件或事件。 | 已有隔离服务测试。程序归属验证和买家外部身份判断分别取证。 [归属测试](../backend/tests/business/test_business.py#L209) |
| POL02 实付核验 | ① 状态已确认、有带时区的付款时间、实付为正时可继续。② 状态未知、时间缺失或无时区、实付为零任一情况都转人工，无退款动作。 | 新增真实条款的纯资格测试。 [付款边界](../backend/tests/business/test_policy_boundaries.py#L65) |
| POL03 未发货取消 | ① 已付款但缺当前版本未交运复核，方案拒绝。② 当前版本确认未交运后，可按全单剩余额度提交、经审批后模拟取消；部分余额仍拒绝。 | 已有隔离服务测试覆盖资格、全额约束与执行。 [取消测试](../backend/tests/business/test_business.py#L553) |
| POL04 已发货拦截 | ① 拦截待确认或失败，或旧订单版本的确认，均无退款动作。② 当前订单版本确认拦截成功后允许提交方案。 | 新增已交运纯资格测试。拦截结论来自模拟事实，未接真实物流。 [拦截边界](../backend/tests/business/test_policy_boundaries.py#L83) |
| POL05 签收退货时限 | ① 可退商品签收后恰好 168 小时，且已有退件验货记录，可以继续。② 超过边界 1 秒或商品不支持普通退货时拒绝。 | 新增固定时钟、逐商品资格测试。 [退货时限](../backend/tests/business/test_policy_boundaries.py#L108) |
| POL06 损坏补证 | ① 无照片时缺商品照和外包裹照。② 两类有效照片登记、当前版本审核后可提交方案；被篡改的附件不能通过审核。 | 已有隔离服务测试覆盖图片登记与完整性。 [照片完整性](../backend/tests/business/test_business.py#L252) · [材料审核后流程](../backend/tests/business/test_business.py#L297) |
| POL07 签收后退货退款 | ① 无退件签收不得登记验货通过。② 两行中仅第一行收到且验货通过时，只能提出第一行的退款，第二行仍待验货。 | 已有隔离服务测试，草案验证不创建正式方案。 [逐行退件](../backend/tests/business/test_business.py#L587) |
| POL08 重复退款 | ① 同一批准方案已有执行记录时，同键或新键重试都返回原记录，无再次提交。② 同订单另有记录、没有可信的同一请求绑定时，条款前提为未知。 | 已有执行幂等和条件引用测试。只读退款查询仍缺可信原请求定位，尚未证明它能选出用户所指的旧请求。 [执行记录复用](../backend/tests/business/test_business.py#L452) · [同请求条件](../backend/tests/intelligence/test_citation_conditions.py#L85) |
| POL09 部分退款金额 | ① 缺独立处置决定的部分退款拒绝，有当前绑定后才可继续。② 恰好等于选中订单行可退上限可通过草案校验，超 1 分拒绝。 | ① 已有隔离服务测试；② 新增精确分值测试。样本使用订单行商品实付，未单独证明费用拆分。 [金额测试](../backend/tests/business/test_business.py#L348) |
| POL10 高额退款审批 | ① 50,000 分可提交方案，执行仍须独立财务审批。② 50,001 分未批拒绝、批准后才可模拟执行。 | 新增两侧金额测试。所有退款共用更严格的执行审批门禁。当前没有以阈值区分的高额提示或专属审批分支，通用门禁不证明阈值参数已参与选择。 [金额两侧用例](../backend/tests/business/test_business.py#L420) · [执行审批](../backend/src/aftercare/business/service.py#L1320) |
| POL11 商品质保 | ① 恰好达到质保终点，即使商品不支持普通退货，仍可进入质保服务人工审核，无退款动作。② 超过终点 1 微秒或明确 0 天时不符合条件。③ 天数缺失、空值、布尔值、负数或其他非整数时转人工，无退款动作，原因标为质保期限未知。 | 已补固定时钟和无效数据纯回归。正整数按原有含终点规则，较大的正整数也可判断。 [质保边界](../backend/tests/business/test_policy_boundaries.py#L135) · [无效数据回归](../backend/tests/business/test_policy_boundaries.py#L162) · [资格实现](../backend/src/aftercare/business/rules.py#L188) |
| POL12 退款执行结果未知 | ① 模拟支付已提交但回执丢失时，UNKNOWN 保留额度，原键核对成功后记账。② 提交超时且原键查不到结果时仍为 UNKNOWN，新执行键不触发再次提交。 | 已有隔离服务测试。条件引用另验证可信 UNKNOWN 记录与 SUBMITTING 但没有超时证据的区别。 [回执丢失](../backend/tests/business/test_business.py#L452) · [原键结果未明](../backend/tests/business/test_business.py#L634) · [引用条件](../backend/tests/intelligence/test_citation_conditions.py#L29) |
| POL13 错发举证与换发 | ① 货损照片不能替代错发实物照和包裹标签照；只有实物照时仍缺标签。② 两类照片齐全也须材料审核；可换数量以内可提交，超过行量或可用库存拒绝。 | 新增照片类别与审核边界，复用隔离执行及库存测试。提出换发仍须正式批准。 [举证边界](../backend/tests/business/test_policy_boundaries.py#L183) · [换发行量](../backend/tests/business/test_business.py#L494) · [库存争用报告](../reports/business-contention-pg-tests.log) |
| POL14 损坏处置 | ① 当前材料版本通过审核，只允许提出方案，未财务批准不得模拟执行。② 更换材料后原审核和批准失效，旧方案不可执行。 | 已有隔离服务测试验证两条路径并读取空账本。 [双重门禁](../backend/tests/business/test_business.py#L297) |

版本解析另验证发布时间、撤回、过期、商品范围、冲突与替代。真实 PG 与 BGE 报告证明索引和检索路径，与上述准入测试分开。 [版本解析](../backend/tests/intelligence/test_policy_versions.py) · [开发检索报告](../reports/policy-retrieval-dev-v1.json)

## 执行与补测

- 运行 [纯资格测试](../backend/tests/business/test_policy_boundaries.py)，覆盖 POL02、POL04、POL05、POL11 与 POL13。用冻结政策、固定时钟和资格函数，每项改变需要区分的条件。
- 运行 [隔离业务服务测试](../backend/tests/business/test_business.py) 和 [条件引用测试](../backend/tests/intelligence/test_citation_conditions.py)。新增 POL09、POL10 金额边界使用本地隔离 SQLite、内存附件和模拟支付。
- 若只读入口增加按已登记原请求定位，补同一请求匹配、同订单其他请求不匹配两例，并断言没有新增执行或外部支付查询。缺可信绑定仍保持未知。
- 若要求高额独立提示，先确定产品口径，再补阈值两侧区别的断言。现有通用审批测试保留。
- 新政策及影响执行的参数版本更新时，附两个边界案例，明确版本、商品、时点和预期，验证旧版本仍可按原时点解释。

初次边界审计执行上述三个测试文件和版本解析测试，共 52 项通过。POL11 修正后复跑资格与隔离业务服务测试，47 项通过。Ruff 格式与静态检查通过。未修改冻结政策或人工评分，未访问共享服务或调用付费模型。

root独立复跑同四个文件：52 passed（4.43秒），日志 [policy-boundary-root-review.log](../reports/policy-boundary-root-review.log)。这是当前边界覆盖的隔离测试，不是新增真实模型或真实物流验证；质保资料未知转人工的修复另行实现、验收，不由本次通过数代替。
