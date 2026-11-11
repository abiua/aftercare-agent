# 售后项目 U1–U6 体验与复盘导览

建议每次只选一个阶段。先读操作，预测页面会发生什么，再看对应结果和代码。可以只看报告，随时跳过题目。答题与项目开发和发布相互独立。这里记录可练习的内容，不评定用户掌握程度。重建实现及工程验证由 GPT 辅助完成，用户体验和反馈按实际记录保留。[阶段约定](notion_snapshot/stages.md#每阶段的交付约定) · [架构职责](notion_snapshot/architecture.md#架构与职责)

需要写入时由集成负责人确认测试工作台 `5187` 的 API 和任务进程均使用离线策略，再选模拟订单。体验入口 `5186` 可能调用真实模型。只读回放可直接使用各节的报告。完整演示另有 4 分 20 秒台本，尚无一份覆盖全程的计时验收。[运行说明](OPERATIONS.md) · [完整演示台本](DEMO_WALKTHROUGH.md)

## U1 · 登录、确认订单、建立工单

- **体验与观察**　以测试客户登录，点「描述问题发起售后」，输入一条订单和意图都不明确的描述。页面要求确认需要处理的订单或售后类型。核对本人订单后提交，记下工单编号，刷新仍可查看。
- **预测题**　如果只输入描述，然后关掉窗口，没有点「确认并建单」，工单列表会多一条记录吗？
- **实际调用链**　`CustomerIntake.previewNow` 发预判请求，`IntakeService.preview` 根据身份和输入返回澄清项。确认时 `CustomerIntake.createCase` 发订单、意图和订单版本，`IntakeService.confirm → BusinessService.create_case` 核对归属及版本后写工单。登录身份来自 `current_user`。[前端入口](../frontend/src/CustomerIntake.tsx#L47) · [确认服务](../backend/src/aftercare/intake.py#L243) · [业务写入](../backend/src/aftercare/business/service.py#L356) · [认证入口](../backend/src/aftercare/auth.py#L26)
- **失败与证据**　订单无法确认时先澄清，未授权订单不能用于建单。真实客户页面已完成澄清确认与建单。页面到业务函数的断点用 pdb 验过，选择时有人工协助。GitHub 托管 CI 和编辑器专用配置的证据另列。[客户浏览器](../reports/browser-intake.json) · [断点实测](../reports/business-breakpoint-browser.json) · [未完成项](FINAL_ACCEPTANCE_REMAINING.md)

## U2 · 缺证暂停与同一次运行恢复

- **体验与观察**　用客服打开一张缺照片的破损工单，在「助手协作」启动单助手，查看缺材料与暂停提示。补齐材料并按页面提示完成审核后，点「补充后继续」。对照继续前后的工单、运行和线程标识，也可只读回放已完成的同案记录。
- **预测题**　补照后继续处理，原运行编号应保持，还是应新增一个运行编号？
- **实际调用链**　`RunEditor` 请求 `intelligence.api.start/resume`，进入 `AgentService.start/transition`。任务进程调用 `AgentService._process`，按线程读取图检查点并核对周期上下文。图的 `_gate → _pause` 在需要材料或人工审核时停住。[页面请求](../frontend/src/RunPanel.tsx#L217) · [运行接口](../backend/src/aftercare/intelligence/api.py#L49) · [恢复处理](../backend/src/aftercare/intelligence/service.py#L776) · [暂停判断](../backend/src/aftercare/intelligence/graph.py#L534)
- **失败与证据**　版本或恢复依据不一致时，页面要求重新核对。现有应用证据覆盖同一运行和线程继续，以及真实 TCP 断线补读。完整浏览器断网补读及客户真实页面上传仍待独立验收。[同案恢复](../reports/business-full-flow.json) · [上下文变化](../reports/context-binding-http.json) · [真实 TCP 事件](../reports/sse-http.json) · [浏览器边界](FINAL_ACCEPTANCE_REMAINING.md)

## U3 · 看照片、审核方案、读模拟回执

- **体验与观察**　优先用已上传合成照片的测试案。客服在「材料与审核」预览并登记意见，在「方案与回执」提交金额和商品。独立审批人批准，客服再确认「模拟执行」。读回审批人、金额、模拟状态和回执编号。
- **预测题**　照片审核通过，但审批人还没批准方案，这时能生成退款回执吗？
- **实际调用链**　`CaseMaterials → BusinessService.review_materials` 更新材料审核。随后 `CaseProcessing` 走 `propose → approve → execute` 各自接口。执行事务重验审批与额度并写账本，结果未知时走 `reconcile` 核对原执行。[材料页面](../frontend/src/CaseMaterials.tsx#L12) · [方案页面](../frontend/src/CaseProcessing.tsx#L22) · [材料审核](../backend/src/aftercare/business/service.py#L786) · [审批](../backend/src/aftercare/business/service.py#L1203) · [执行](../backend/src/aftercare/business/service.py#L1320) · [核对](../backend/src/aftercare/business/service.py#L1502)
- **失败与证据**　材料或订单变动可能让旧批准失效，执行结果未知先查原记录。真实浏览器已完成客服预览审核和一笔模拟退款流程。该退款验收使用准备好的测试案，照片通过真实浏览器从客户端上传尚无通过记录。[材料审核](../reports/browser-material-review.json) · [财务浏览器](../reports/browser-financial-flow.json) · [异常回执边界](FINANCIAL_RETRY_UI_REVIEW.md)

## U4 · 看三种售后场景和条款原文

- **体验与观察**　以测试员在「用例库」对照破损、错发和退款进度，再打开已有实验。测试页有「先读这次售后」时，从那里看客户原话、业务阶段和应用处理意见。也可直接读开发复核包。查看所引条款的原文及前提，区分候选条款、能回到原文的引文和本案有记录支持的前提。
- **预测题**　退款进度的订单账本为空时，能否用一条要求先有既有退款记录的条款，证明这次退款已经处理？
- **实际调用链**　图的 `_find_evidence` 先取适用政策快照，交 `PolicySearch.search_selected` 在可用范围检索，再由 `assess_citations` 标记前提证据。评测页 `ScenarioMetrics` 请求 `EvaluationService.scenario_metrics`，按冻结场景统计后回到原案例。[图取证](../backend/src/aftercare/intelligence/graph.py#L180) · [检索](../backend/src/aftercare/intelligence/policies.py#L203) · [引用前提](../backend/src/aftercare/intelligence/citation_conditions.py#L63) · [场景指标接口](../backend/src/aftercare/evaluation/service.py#L786)
- **失败与证据**　无退款记录时不能据此判断是否曾申请或是否到账。真实 HTTP 已核对空账本的条件引用，三种检索方法有开发集对照，未建立独立人工金标。已有案例和场景下钻可只读复盘，真实浏览器选择报告文件并导入仍待验收。[条件引用](../reports/response-clarity-citation-http.json) · [开发复核包](HUMAN_REVIEW_PACK.md) · [检索对照](POLICY_RETRIEVAL_BENCHMARK.md) · [场景下钻](../reports/browser-scenario-metrics.json) · [导入受限记录](../reports/browser-evaluation-import-attempt.json)

## U5 · 路由、预算、任务进度和恢复

- **体验与观察**　以测试员打开一个已保存的预算失败实验，查看冻结配置、调用记录、未运行案例和失败原因。再对照独立任务进程被强制退出的报告，查看原运行编号、任务领取次数和最终状态。只有经确认的独立离线环境才启动新的 fixture 实验。
- **预测题**　调用已到预算上限时，即使还有兼容的备用模型，系统会发下一次模型请求吗？
- **实际调用链**　`ModelRunner.call` 按 `eligible_models` 和尝试审计判断是否可发请求。Celery 任务经 `process_target` 核对数据库任务代次和领取权，再走 Agent 或 `EvaluationService._process`。模型候选、图的业务步骤和队列领取分别决策。[模型入口](../backend/src/aftercare/intelligence/models.py#L347) · [任务领取与分流](../backend/src/aftercare/queue_delivery.py#L45) · [评测执行](../backend/src/aftercare/evaluation/service.py#L1460)
- **失败与证据**　预算失败和未知用量分别保留。真实任务强制退出的恢复报告使用零模型运行，真实模型兼容证据另存。评测取消有实际 HTTP 与受控响应交付证据，浏览器取消写入尚未单独验收。评测实时进度的隔离测试与普通工单事件分开核对。[强制退出恢复](../reports/worker-crash.json) · [模型兼容](../reports/model-compatibility.json) · [在途取消](../reports/inflight-cancel-http.json) · [评测进度隔离 HTTP 测试](../reports/evaluation-progress-http-pg-root.log) · [部署验收边界](FINAL_ACCEPTANCE_REMAINING.md)

## U6 · 比较两个版本，解释发布门槛

- **体验与观察**　在测试中台打开「版本对照」，选两份已保存的配对实验。先核对共同案例和重复编号，再打开两边同一次结果，看失败、待复核、耗时和费用。另打开实验的发布门槛，辨认缺少哪些证据。
- **预测题**　如果对照组少了一次重复运行，页面能直接把其余运行的平均成功率当作本轮成对结论吗？
- **实际调用链**　`Compare → EvaluationService.compare` 核对冻结配置和 `case_id + repetition`，未完成配对时不返回对照指标。实验详情分别交 `gates.evaluate` 检查证据、阈值和覆盖，技术门槛之外仍需独立发布判断。[版本对照页面](../frontend/src/Evaluation.tsx#L374) · [配对检查](../backend/src/aftercare/evaluation/service.py#L1272) · [技术门槛](../backend/src/aftercare/evaluation/gates.py#L122)
- **失败与证据**　真实浏览器已打开配对实验并下钻，过程有人工协助。开发对照共 36 次运行，来自 3 个独立开发案例，保留失败、待复核和费用。正式保留集的两组对照、独立金标与发布批准仍缺相应证据。[浏览器对照](../reports/browser-comparison.json) · [开发对照结果](LIVE_COMPARISON_RESULTS.md) · [保留集计划](HOLDOUT_FREEZE_PLAN.md)

复盘时可只记录一行，写明做了哪一步、预期是什么、实际看到什么、下一步想核对哪里。用户的操作或回答由本人给出，报告中的历史验收不代表用户已经体验。
