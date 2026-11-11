# P5 独立评审与对照评测审查

2026-11-11，开发审查中。未完成三臂真实比较、独立人工标签或正式留出，不能以本页工程验证代替决策质量。

已实现的第一条工程路径是：内部从真实持久化 P3 原始快照指定测试评审员 → 只读冻结消息、原图、业务事实与政策 → 封存独立可接受方案集合或无法判断 → 显式揭示候选并登记首次曝光 → 后置评价另存。仅 test profile 和指定启用 tester 可访问，无公开任务创建接口。原评测及历史标签不改。

root 源码审查确认事务锁、阶段版本、幂等键和唯一封存/曝光记录；读取及图片均重新鉴权，原图摘要与大小在分配和回读时核验，候选不在盲态响应中。指定入口内的顺序记录不能证明评审者在应用外从未看过样本。

root 要求修正并继续核对：

- 初判必须看到材料审核事实，但不能把它当财务审批，审核者及备注不作为提示。
- 揭示后完整保留理由、备选、未解决项和原始引用；错误引用不能被过滤掉，解析状态须可见。
- 盲态不得显示程序推导的条款适用结论；旧快照仅含已选条款，不能声称能独立判断整个政策目录。新完整目录接入中。
- 盲审能力接口报错时仍隔离普通实验读取，不自动回退到可能含候选的页面。
- 原始初判不可覆盖；新增信息和客服意见仍待后续同输入比较及指标接入，不天然视为金标。

已有证据：业务子代理第一轮回归583通过，实际独立PG迁移及并发2通过；后续揭示内容和材料事实修正58项定向通过。前端初版49项纯测试与39项受控浏览器通过，审查后的内容与错误回退修改仍待最终结果。实际HTTP/对象存储/原生浏览器及root最终整合尚未验收。报告和日志保留在reports与/tmp，不能将模拟返回当真实人审。

另：输出预算提交5bd8e26的托管浏览器出现旧评测引用单选按钮超时，36通过1失败。原日志 `.runtime/chat-p4-output-ci-failure.log` 保留，原因正在检查，不通过扩大超时或删除断言处理。


后续root整合：完整后端非PG回归947通过、2可选跳过、40 PG未选，日志 `.runtime/chat-p5-root-integration.log`。新盲态标记 COMPLETE_CATALOGUE/SCOPED_ONLY，旧记录不会变成完整目录证据；揭示时原有效政策聚合只对应当时实际选中的版本。真实模型与新盲审端到端仍未复验。

为真实test profile验收，offline_demo增加显式profile选项：新建专属ac_test数据库、test网络别名、test身份与私有凭据路径；默认app及旧状态兼容。私有配置检查确认app原字节不变、test目标及0600权限正确；首次检查发现独占写函数不允许覆盖新建模板，已改为私有暂存后替换并通过复验。尚未实际启动新test容器，不能宣称此启动路径已验收。


前端最终冻结检查：49项纯测试、41项受控浏览器、lint/类型/构建通过，日志 `/tmp/aftercare-p5-ui-frozen.log`、`/tmp/aftercare-p5-ui-pure.log`。root查看托管失败的逐帧图，点击期间嵌套窗口尚在移入；现已用两层窗口实际展开完成回调控制复核按钮。原用例本地自然时序重复15次均通过，不能声称每次都能复现托管故障；修复保留原断言和超时。新增盲态隔离用例首次误记录正常登录页遗留请求，改为直接盲审入口登录后通过，失败过程保留。尚未做新版原生浏览器验证。


## 实际隔离 test HTTP 路径

冻结84121cb构建独立5197/58359，真实ac_test与aftercare-v2-test身份检查通过。`reports/chat-p5-offline-20261111.json` 验收PASSED：两条聊天各两张原图，实际队列生成PROGRAM原始快照，完整政策目录盲态可读；未分配测试员和客服读取任务/原图均404。控制样本经真实HTTP封存UNDETERMINED初判、显式揭示、保存UNDETERMINED后置标签，摘要链、顺序、刷新、幂等重放和禁止覆盖均验证。数据库2任务、1初判、1曝光、1标签；模型尝试和执行均0。第二任务保留BLIND供随后原生浏览器验证。上述标签是自动化控制数据，不是人工金标。

托管检查42973755447（84121cb）已成功，原5bd8e26失败记录保留。新test启动器已实际通过，不再仅是配置检查。完整P5真实三臂、消融、独立人审与决策质量尚未完成。

## 原生浏览器与备份恢复完成

`reports/browser-chat-p5-20261111.json`：专属 test 5197 真实 Chrome 路径通过，盲态展示两张原图和14条完整政策；封存、显式揭示、后置标签各一次200，整页刷新重开后摘要不变。两次弹窗过渡期间点击在网络派发前被工具拒绝，核实未发送后重新定位，未重放写请求。独立数据库复核两任务均LABELED，四张盲审表各2行，三张模型审计表与财务执行表均0。均为自动化UNDETERMINED控制标签，不是人工判断。

root执行 `reports/chat-p5-backup-restore-20261111.json` 通过：真实test源导出至不同物理PG测试实例，57表指纹、4原图对象逐字节和两条初判/曝光/标签摘要一致；分别与HTTP和浏览器报告绑定。恢复后的评审员凭据哈希及服务层归属检查通过，未分配评审员读取任务和原图404；此处不宣称恢复后浏览器登录。恢复前后源指纹不变，root逐个独立确认临时容器、网络和卷均已移除，私有归档保留。

三臂清单与冻结执行仍在实现，尚无同输入三组真实效果比较、消融或独立人工正确率。

## 纯评分切片

5693f89新增独立评分/聚合模块，按预登记计划行核对缺行、额外行和重复；每行保存来源、同输入状态、排除原因与可知费用。主指标只称动作/对象/金额或数量结构匹配，补证自由文本逐字匹配另列诊断，不能解释为完整可接受率。无独立标签、控制标签、不可判断、无候选和未知都不计质量成功；完整可接受、客服同输入一致、错放误拦、事实政策引用、补查纠错及实际接管因资料不足保持NOT_MEASURABLE。不是正式评测完成。

## 三臂本地控制执行审查

新增manifest/runner以同一精确scope、业务时钟、完整政策和原图，逐案交错运行strong_single/single_staged/multi，实际调用AgentService worker核心及P3投影。受控普通退款候选三臂均经过程序规则；multi展示反例后只补查事实角色。程序硬门禁模型调用为零。不是Celery/PG/S3或真实模型三臂验收。

root与独立审查发现并修复：完整性错误不能被重读成功账本吞掉，缺路由或错误候选绑定不能计可评分，ready后缺运行绑定不能当零费用失败。当前明确SQLite-only，未验证PG写路径已移除；恢复只对账不重发，受损候选保留ID并标UNUSABLE，未知账本/费用仍未知。三臂跨库盲审登记、真实源拓扑与正式指标仍待接入。

root独立执行新runner控制验收通过，日志 `.runtime/chat-p5-runner-root.log`。提交9cc3fb7托管检查42976685218成功。其余后端回归为955通过、2可选跳过、40 PG未选；新增runner的定向控制覆盖单独记录。无新增真实三臂质量结果。

## 独立开发样本包

`data/chat-eval-v1/development/cases.json`固定四案、原视觉资产清单与政策源SHA，FrozenCase来源包含完整包摘要，未修改v0.1/v0.2或历史结果。普通1件实付6000分、明确该件损坏及退款诉求，拟议校验通过；3件受损数量缺失案以定向补证为开发预期；历史退款案只测试重复事项拟议门禁与原账本，旧执行详情当前工具不可读，该能力缺口明示；双行包裹归属冲突案实际路由MULTI，但局部补查/纠错仍NOT_RUN。DUPLICATE_MATTER发生在拟议规则校验，不能据此声称路由PROGRAM。

root查看原始合成杯子/包裹图，杯沿缺口与裂纹、箱体破损及TEST-PARCEL-A17可见；它们不能证明真实商品身份/运输原因，也不是本轮模型视觉结果。四案各独立临时SQLite，走现有聊天、原图绑定、材料审核入口，不执行新付款。root定向4通过，日志 `.runtime/chat-p5-development-root.log`。人工审核和历史账本均明确合成，开发预期不是独立金标。本包尚未接真实视觉/三臂付费/跨库盲审登记。


## 独立 PostgreSQL/S3 适配审查（尚未实际运行）

新增专属 test 环境适配器，核对物理数据库身份、S3 标记、活动租约，并为每臂保存独立业务表和 LangGraph 检查点。SQLite 控制路径保留。原图和业务材料均实读冻结，复制及恢复审计核对字节；不可用候选与未知费用不转为成功。备份新增隔离聊天、材料和检查点覆盖，旧 v1/v2 保持兼容；跨物理库恢复后必须重新核验运行授权。

独立审查修复了只复制聊天原图而漏掉业务材料对象、以及恢复未验证业务材料字节的问题。受控验收脚本另加入活动租约竞争和 started/ready 落盘后、Agent 启动前的中断探针；这不等于在途模型请求或 Celery 故障验收。

root 完整非 PG 回归 981 通过、2 可选跳过、40 PG 未选，日志 `.runtime/chat-p5-pg-root-regression-fixed.log`；备份定向 62 通过，变更文件 Ruff 通过。首次回归的两个旧备份契约失败保留在 `.runtime/chat-p5-pg-root-regression.log`，更新合法隔离聊天键与完整检查点组的契约后复验。当前仍未启动 5200 专属 PG/S3 栈、未进行本轮真实三臂模型调用，不能据此报告真实数据库或质量验收完成。


实际首次运行更新：提交 e77911d 的托管检查 42981119641 成功。使用该提交冻结构建的后端及未变更前端启动独立 test 5200/58362，运行 b256ad0c74d046fea0c1403a43d19d04。`reports/chat-p5-pg-controlled-20261111.json` 状态 FAILED，阶段 CONTROLLED_RUN，外层错误 P5_PG_CONTROLLED_RUN_FAILED；原始诊断只保留 ValueError，需补安全错误定位。只读检查两个源 schema READY、各一会话两附件两份业务材料，无 arm schema、无检查点、无 Agent/模型尝试/执行记录，清单只有 lock、尚无 plan。因此实际验收未通过，禁止直接重跑覆盖失败或再次播种原源。后续先只读定位，再审查具体恢复路径。


## 真实 PG/S3 控制执行恢复及计时缺陷

原失败定位为 SQLAlchemy 首次编译查询时给 schema_translate_map 加入同目标 `_none` 内部键，精确映射比较误拒；35e5c62 保留目标/身份限制并接受该等价映射，root 定向 11 项通过，托管检查 42981735512 成功。

`reports/chat-p5-pg-controlled-recovered-20261111.json` 的基础设施受控验收为 PASSED：复用原 5200/58362 栈、两个 READY 源和原 sentinel，仅运行修复镜像验证器；原失败报告 SHA 与旧 lock-only 清单目录保持不变。普通案三臂 COMPLETED，复杂案三臂 NEEDS_REVIEW，六份原生 P3 候选及隔离检查点均存在；复杂 MULTI 定向补查 fact/package。原图及业务材料逐字节回读，活动租约竞争被拒，独立 started/ready 后 Agent 启动前中断行重开两次 UNKNOWN/null费用/无新增调用。以上是受控 worker core，不是付费模型、独立人审或实际 Celery 调度比较。

root 审查发现所有六行 candidate_persisted_ms 为负：冻结业务时间与实际运行时间混用。该字段无效，原报告保留原值，本次不得报告候选耗时通过或用于性能比较。计时分离修复和更严格验收约束正在实施。其余结构结果还需独立只读复核，跨 schema 盲审登记仍在开发。


独立只读 PG 复核完成：6 臂各 1 运行及对应 AGENT P3、仅自身线程的持久检查点；尝试 2/5/5/2/5/7 共26次，检查点32条，臂和public执行均0。UNKNOWN中断臂无运行、尝试或候选。原报告及旧清单hash一致，验证器已移除。计时修复改用实际UTC保存候选，同时冻结政策时点不变；新增 runtime-utc-v1 区分旧收据，时钟逆序返回未知及明确原因。root 三项定向回归通过，新PG验收脚本拒绝负耗时；尚未再次真实PG验证新计时。


## 跨 schema 盲审登记实现（真实 PG 待验收）

新增私有 register_pg_blind 入口，沿已核验运行环境读取原计划、源输入、各臂原生运行及P3；不导入或重建P3。登记前复核scope/图片/候选校验和/检查点与实际运行绑定，同一输入的三个来源只生成一个盲审任务。缺失或未知臂留在私有映射，全部无候选只保存无任务原因。原候选、实验臂和运行信息不进入初判页面；原source/arm不写入。公开任务写入后私有映射失败可幂等补写。新实际UTC时钟与旧冻结时钟的checksum分支分别验证，非UTC业务时点纳入测试。

root与独立代理审查未发现阻断；root联合运行登记/备份66项通过（含原备份62项），静态检查通过。测试使用独立SQLite原生P3，不以此代替真实PG登记/浏览器。真实登记验收脚本正在准备，旧同schema入口保留。


## 新评测拓扑真实备份恢复

`reports/chat-p5-pg-backup-restore-20261111.json` 实际通过：5200源导出后恢复到不同物理PG测试实例，537表指纹、12个eval schema、24聊天原图+24业务材料对象逐字节一致，完整owner声明与原清单绑定，原生运行/P3/checkpoint及UNKNOWN空运行保留；源导出前/归档/结束后指纹一致。恢复结果明确REQUIRES_NEW_ATTESTATION，S3 sentinel/owner授权对象未转移。root逐一检查6个临时容器/网络/卷已不存在，私有归档保留 `.runtime/chat-backup-restore-1a7lwbd6`。

报告旧通用production_core列表含“authenticated conversation attachment service with owner/foreign readback”，该描述不适用于本次eval schema分支：本次验证的是记录/原图字节，不是恢复后评审登录授权。原报告保留，脚本已按模式修正后续报告描述。旧5197的人审权限/浏览器备份证据仍仅适用于其原范围。


真实跨schema登记 `reports/chat-p5-pg-registration-20261111.json` 已通过：复用5200原案例，每例3份原生P3投影到同一初判任务；普通任务ed151253-0fea-46ab-9f5c-6234fe403926完成控制UNDETERMINED初判/揭示/标签，复杂任务c2428b2e-00cc-441b-9064-a6c8dad6db8b登记后BLIND供浏览器验收。仅public盲审tasks+2、initial/exposure/labels各+1、eval_mutations+3，原source/arm全表及48对象字节不变，没有模型或财务写。控制初判不是独立人工金标。

root对冻结2988bbb源码完整非PG回归987通过、2可选跳过、40 PG未选，日志 `.runtime/chat-p5-registration-frozen-regression.log`。随后f637f22新增只读登记必须require_existing源的保护，12项定向通过。真实登记使用该版本固定镜像22ca76e1…e07a；旧API仍原镜像。


原生Chrome验收 `reports/browser-chat-p5-pg-20261111.json` PASS：复杂任务两张合成原图实际解码1254×1254和1536×1024，14条完整政策；BLIND/SEALED阶段无候选，显式揭示后三候选。封存/揭示/标签精确三次POST 200，版本1/2/3，29请求0丢失/0错误，普通任务未读取详情，原生刷新后初判/曝光/标签摘要不变。两次弹窗过渡拒绝发生在派发前，核对无POST后再点击，没有重放写。客户端响应核对无arm/run/snapshot标识，专属会话关闭。全部UNDETERMINED为自动化控制记录，非独立人工判断或金标。数据库独立复核待补。


独立只读数据库复核通过：两任务均v4/LABELED，各一条初判/曝光/标签，三候选均控制UNDETERMINED；eval_mutations精确每任务3条。复杂任务各阶段摘要和三幂等键摘要与浏览器捕获一致，登记报告与私有映射摘要一致。与备份归档比较，除预期4盲审表和eval_mutations外532表指纹不变，包括12个eval schema全部480表。主对照26+中断测试7次均INJECTED_SYNC；没有新视觉/供应方/财务写、审批或模拟付款。


## 首轮真实文本三臂：PARTIAL，非质量通过

2d6f77d固定镜像9acd27b8…5d67，预登记计划b182a6d5…51a2，执行 `reports/chat-p5-paid-ordinary-20261111.json`。同一普通开发案例/模型/冻结输入三臂，最多8次/$0.40每臂、24次/$1.20总保守预算。实际15个wire POST均HTTP200，估算$0.11795640、保守发送预留$0.735、未知响应和未知用量费用均0，实际账单未知。wire只按行绑定，不声称逐次逻辑调用匹配。

传输兼容性VERIFIED、候选全部可用，但工作流PARTIAL：strong_single 形成HUMAN_HANDOFF、single_staged 形成REQUEST_EVIDENCE，均完成其角色最终结构；multi因INVESTIGATION_PROMPT_LIMIT在最终核验前转人工，缺verify-0最终结果，不计工作流通过。不得用15次200或存在候选代替三臂成功/决策质量。三臂新candidate_persisted_ms为42923/56831/20931，latency_issue为空，实际UTC计时修复已在本轮验证。无视觉像素发送或独立人审质量结论。

root确认实际p5-paid-text专用容器不存在，临时secret目录无文件，报告记录temporary_local_key_copy_removed=true；旧API/worker仍按原离线配置运行，待独立复核。完整非PG回归994通过/2可选跳过/40 PG未选、定向9项与静态检查通过。原PARTIAL报告保留，正在分别只读诊断核验输入长度、两种单Agent结论与数据来源，不自动重跑。

## compact-v6 真实文本三臂：仍为 PARTIAL

固定 adcfd28 镜像 e4da6f2a…596e，独立 v2 计划 6919515a…35f5，结果保存在 `reports/chat-p5-paid-ordinary-v6-20261111.json`。保持每臂8次/$0.40、总24次/$1.20上限，同一原始输入和模型。实际15个HTTP200，估算$0.11116890，发送预留$0.735；未知响应/用量费用为0，实际账单未知。三臂候选及检查点存在，但业务质量仍为NOT_MEASURED。

strong_single 两次最终结构验证失败（INVALID_MODEL_OUTPUT），保存转人工兜底，不计工作流完成；single_staged 和 multi 均完成角色最终结构并提出 HUMAN_HANDOFF。候选耗时分别42760/51400/31088毫秒，无时钟异常。multi 此次直接完成 verify-0，没有定向补查或第二次核验，因此不能把本次完成归因于回执压缩。上一版本失败输入的精确离线回放与这次真实结果分别保留，不拼接为三臂全部通过。

旧验证提示将不同错误折叠为 `unknown_field.0.unknown_field / value_error`，没有保存非法原始输出，故无法事后确定本次究竟缺哪个字段。后续修复只增加静态安全路径和有限错误码，区分退款缺金额、补证缺具体要求等；不放宽业务校验、不增加重试或预算。此修复尚未经过真实供应方验收，也尚未更新实验版本。

root核对专用p5-paid-text容器已移除、临时凭据副本已清除；独立原生数据库复核另行记录。结构化人工材料观察仍为未集成开发项，尚未进入本次冻结镜像或模型输入，不得据此宣称模型已读取人工看图结论。

独立只读复核已完成：私有wire日志15次HTTP200按臂4/6/5，与公开receipt一致；实际PG身份匹配，每臂AGENT P3及自身线程检查点存在。strong最终两次INVALID、staged一次INVALID后修复、multi阶段完整均与报告一致。multi只有工具声明，没有实际targeted-check调用。public模型/视觉/执行计数均0，专属容器不存在，密钥副本目录为空。旧源范围及对象前后一致由原生验收回执支持，本次独立复核未逐行比较全部旧schema，不扩大不变性结论。

## 结构化人工图片观察：已集成，实际升级待验收

09029ba增加客服可选照片观察：可见破损/未观察到可见破损/无法判断，关联当前材料及可选订单行；受影响数量表示该商品行总数，多张照片不得相加或互相矛盾。服务端加HUMAN_MATERIAL_REVIEW来源，拒绝客户端伪造来源、越界材料/商品和非法数量。旧请求与旧幂等摘要保留，旧备注不提取、不回填。观察目前仅客服可见，未进入模型投影或改变退款审批。

新增public可空observations列；历史评测schema不迁移。旧模型读取延迟加载该列，复制历史源仅允许缺失这一已知可选列，目标置NULL，其他缺列仍失败。root定向18项及冻结完整非PG回归1013通过、2可选跳过、40PG未选；前端构建成功，代理的浏览器夹具验证不代替真实后端验收。日志 `.runtime/material-review-frozen-regression.log`。

固定新backend镜像56bb7422…2513、frontend镜像1b733891…a2c9已经构建，尚未替换运行栈。下一步使用独立5201/58363从旧版非空记录实际升级public、核对历史schema和照片保留，再验收真实客服填写及客户隔离；迁移验证脚本仍在开发。现有5200及共享演示未升级。

## 图片审核实际升级：首轮准备失败，待同栈恢复

验收脚本a3887a4在新5201/58363栈315cfced887148e3b4221b777887e929运行，计划8872af5b…8539；原报告 `reports/material-review-migration-20261111.json` 为FAILED/LEGACY_FIXTURE，原因TWO_CUSTOMERS_SUPPORT_AND_TESTER_REQUIRED。实际凭据仅一个customer及support/approver/tester各一，缺的是异客户账号。失败发生在登录身份读取阶段、创建旧工单之前，尚未执行迁移；不会重置原栈或覆盖失败报告。下一步恢复协议须核验实际旧镜像/物理数据库身份、零业务记录及原报告摘要，再登记独立第二客户测试账号后继续。账号凭据只留本机私有文件。

09029ba托管检查42989840392、诊断修复1ceb243检查42989619940及v6的adcfd28检查42987443601均成功，仍不替代实际升级/模型质量验收。

同栈恢复a533a41通过实际零业务记录/旧版本核验后登记一个隔离异客户测试账号，未改变原凭据。`reports/material-review-migration-recovered-20261111.json` 仍为FAILED，但已取得独立迁移证据：public升级到20261111_material_review_obs、旧审核新增列为SQL NULL、92表指纹与6个原对象保持，旧eval完整不变并克隆到新schema后观察仍为NULL。随后启动应用时因offline_demo.restore未恢复FreshStack.run所需prefix/label而AttributeError；新API尚未创建，仅本次PG/S3/Redis存活。不得把此报告称完整通过；后续只恢复应用与HTTP验收，不重复升级，原两次失败报告保留。

## 版本化人工观察投影准备

e907a46增加双显式启用的human-material-observations-v1：仅当前有效审核的结构化观察可进入冻结证据，校验当前事项商品行、材料版本、原图SHA及聊天关联；原备注和审核员身份不进入模型/盲态投影。新盲审v2直接展示人工观察，旧v1继续可读。现有compact-v6默认拒绝新投影，尚未启用到生产聊天或付费评测。root定向11项和冻结非PG回归1021通过、2可选跳过、40PG未选（`.runtime/human-observation-frozen-regression.log`）；前端受控盲审5项通过。独立审查还要求无观察时新投影也改变依赖摘要，防止复用旧候选，该边界正在补齐。实际新输入、模型语义与普通/复杂闭环仍需统一版本后验收。

8448873已补齐上述依赖边界：无观察时显式新投影仍改变dependency，旧合法候选不能覆盖新scope；None旧摘要逐字节不变，root9项定向通过。尚未启用新模型协议。

## 图片审核升级续验完成

1c10c0a修复恢复对象后，`reports/material-review-migration-app-resumed-20261111.json` PASSED，计划80624ed1…7ee0。复核已升级数据后仅首次启动新应用，没有重跑迁移。实际HTTP完成新版两条照片观察保存、相同幂等重放、改请求冲突、非法范围/数量/来源拒绝、客服回读和客户接口隐藏；最终原生PG复核旧数据与源schema不变，12个原对象逐字节验证，模型和财务计数为0。新增只涉及明示工单、材料、审核、上传、幂等及事件表。原两份FAILED及各自输入摘要保持；独立只读复核和真实浏览器操作另行记录，不将该技术验收称为模型决策正确。

独立5201现运行09029ba固定镜像56bb7422…2513/1b733891…a2c9，尚未包含e907a46的新证据投影与8448873依赖修复。原5200及其他演示未升级。

独立只读复核确认原eval的37表与私有baseline一致，clone旧审核逐字段一致、新列NULL，12对象SHA一致；public及两eval模型/财务记录为0，临时verifier全部清理。随后另建C002/O0241的浏览器专用工单d0de95c7-01a9-4d3a-857f-7983bd0b213d，不改迁移验收的两工单。`reports/browser-material-observations-20261111.json` PASSED：两张74字节合成纯色图实际加载，64请求无捕获错误/丢失，仅一次审核POST201；NEEDS_MORE两观察均L01总数1，客服刷新可读，owner响应和页面无内部观察，旧审核可读。root原生PG核对该case仅一审核、两观察、旧审核NULL及六类模型/财务计数0。该工单仅界面控件测试，不用于判断真实货损，专属浏览器会话已关闭。

## v7统一协议实现，真实多模态源仍待验收

e3c8baa将生产聊天和三臂受控评测接到同一协议选择器：compact-v7与human-material-payment-v1成对，新付款事实只取冻结订单状态、时间和版本；客服人工观察、付款记录均可引用，旧prepared marker仅能显式读取而不能运行。scope/持久binding/当前配置不一致在发送前拒绝，旧UNKNOWN只对账。默认仍compact-v6，未改变运行中服务。v7原始材料观察不等于真实性、运输责任、退款资格或批准；同商品行数量不能按图相加。

root整合回归1033通过、2可选跳过、40PG未选；最终补齐PROGRAM路由事件版本审计及工具语义后，冻结e3c8baa定向38项通过（包括v6/v7三臂、定向补查、未知不重发和付款事实实际ToolMessage读取），改动文件静态检查通过。一次扩大到全源码的Ruff检查发现7项未改旧文件问题，未将其宣称全仓静态通过。固定backend镜像1c490df0…5775、frontend镜像88f40f1c…1929已构建，尚未部署v7或发送v7供应方请求。

下一真实源采用独立5202/58364（尚未启动）：默认客户订单为背包/瑜伽垫，需预冻结新增模拟杯订单，不把杯图挂到错误商品。同会话杯与包装两图均走真实P2有界输入，再登记人工观察并冻结同一v7源；正常聊天闭环与三臂文本预算分别记录。v3付费门禁和源准备脚本正在开发，当前不能作为已通过的多模态闭环或模型质量证据。

## 5202双图真实视觉源通过，尚未冻结决策评测源

独立5202/58364（stack 1b35f06e604f41678d0ece578babe000）使用e3c8baa固定镜像；预冻结模拟订单O9902与白杯商品对应，不更改原种子。初始离线报告 `reports/chat-p5-v7-multimodal-offline-20261111.json` FAILED：两图POST已经201且原图GET200，随后验证器错误地让客户请求仅客服可见的材料接口而404。6fcc6b8修正验证器角色，原失败保留；只读对账 `reports/chat-p5-v7-multimodal-offline-reconciled-20261111.json` OFFLINE_ORIGINALS_READY，确认没有重发图片和任何模型/财务记录。

`reports/chat-p5-v7-multimodal-vision-20261111.json` PASSED，计划a38c4931…cf62，执行脚本788ae9c8…d6de。原待分析轮次按原生规则记为ANALYSIS_EXPIRED（调用0）；同会话正常补充消息生成新轮次，未改写旧期限。新轮次单次供应方HTTP200、一次SUCCEEDED/PUBLISHED及两条观察，原图与实际送模图逐张摘要对应。杯图ITEM_VISIBLE/POSSIBLE_CRACK；包装图PARCEL_VISIBLE/POSSIBLE_TEAR/POSSIBLE_DENT/LABEL_VISIBLE/PRINTED_MARKINGS。仍为模型观察，不证明运输原因、真实性或退款资格。

执行边界为专属test容器直接调用原生P2处理器（OWNED_TEST_NATIVE_P2_HANDLER），不是本次Celery队列端到端。禁止自动重试，预检估算最大预留$0.0445，实际预留$0.043375，估算费用$0.001519，实际供应方账单未知。Agent/P3/审批/执行/支付记录均0，专属付费容器及密钥副本已清除。异常交付无法对账时保留UNKNOWN，不能据FAILED重发。

v3文本比较入口已增量支持同一v7协议、双图同次视觉尝试绑定、人工结构化观察和付款事实；root定向18项离线检查及改动文件静态检查通过。尚未提交真实客服材料判断、冻结v7评测源或调用v7三臂。已向用户展示两张原图，仅请求可见裂纹/包装破损判断；没有把该问答当作退款批准或独立决策金标。

5202客户真实浏览器只读验收 `reports/browser-p5-multimodal-20261111.json` PASSED：两张PNG实际加载，各自1954558/2574993字节，两张“照片观察·仍需核对”卡片的附件引用对应，刷新一致；42请求均GET，无捕获错误或丢失，客户页面无内部角色讨论，专属会话已退出关闭。不计图片正确率或退款验收。

4418f1c已提交并推送v3付费准入和双图验收入口。含新runner准入的verifier镜像为 `sha256:f27ec78345c2db54a4256fbb62e23f79cc2702548d71bc2e18bf73afb057f1f1`（aftercare-backend:chat-v7-paid-20261111），构建成功但尚未执行三臂；5202运行API仍为e3c8baa原固定镜像，不会因构建替换。

4418f1c托管检查42998571187已成功（pure与browser-fixtures），仍不替代真实决策验收。新冻结入口 `scripts/verify_chat_p5_multimodal_freeze.py` 经过root及独立只读审查，只接受已提交的真实人工照片判断，不能用AI自动观察伪装HUMAN_MATERIAL_REVIEW；它不创建审核、不要求APPROVED、不恢复beat。静态检查及固定视觉回执校验通过，实际冻结尚未执行。后续顺序：等待照片事实判断→原API登记材料及审核→固定decision_at冻结同源→恢复专属beat→使用新verifier镜像预检并运行三臂；原会话保留供正常聊天与HITL闭环。

## 局部补查消融与客服一致统计增量

两臂受控消融已实现：multi与multi_no_recheck保持同一冻结输入/首轮取证/核验/预算，关闭组在合法局部补查请求后保留争议并转人工。新manifest协议隔离行ID及恢复；默认生产流程和付费v3入口不接受该消融。root39项定向检查通过，包括实际SQLite runner/AgentService双臂、零重复尝试和恢复前权限拒绝。PG消融、真实模型消融及其他两个消融仍未运行。完整预登记与证据边界见[CHAT_P5_ABLATION_PLAN.md](CHAT_P5_ABLATION_PLAN.md)。

新增纯函数support_agreement模块，预声明比较option ID并绑定原生快照/审核/结果摘要；分别统计客服动作和相同订单行的逐行退款金额一致，按审核动作及信息变化分层，保留缺失或不匹配行。ACCEPT选择已展示候选产生的机械一致单列，不代表正确率。补证语义缺结构字段，仍不可测；尚未接入实际PG导出或页面。已用真实DecisionService在测试SQLite生成候选及MODIFY审核核对SHA和父链，未伪造人工正确标签。

24d68c3托管检查42999136734出现browser-fixtures失败（其余43项浏览器用例通过）：案例关闭按钮在抽屉滑动动画中被点击，trace记录点击点处于视口边缘的窄裁切区域。原失败保留，正在修复关闭按钮就绪时机；不通过增加等待超时或仅重跑掩盖。

本轮整合回归：root完整非PG检查1047通过、2可选跳过、40PG未选（67.77秒）；不触碰共享数据库。改动后前端完整隔离浏览器夹具44通过、原失败用例连续15次通过、前端单元49通过及构建通过。修复仅等待已有抽屉动画就绪后启用关闭按钮，未修改路由/深链接或增加测试超时。当前运行中5202及其镜像未替换，本轮消融与一致统计的真实模型/PG验收仍待后续，不能用这些计数证明业务正确率。

## 原生只读一致率导出与PG受控消融通过

ac1b7a1加入只读原生审核导出：固定候选引用、比较option和截止时间，仅取首个审核；相同时间歧义、摘要或前驱无法复核时保留真实排除码，不改选后续记录。PG使用REPEATABLE READ READ ONLY事务；原生server-added evidence_gaps不误作Review请求字段，不输出内部备注。root7项SQLite服务链测试及改动静态检查通过。固定新verifier镜像 `sha256:a582056a873b76fe9f376a68aa8abcfa55478ee31397fb06c2666cb60abf25ad`，未替换运行服务。

`reports/chat-p3-support-agreement-20261111.json` PASSED（计划b87453d1…01b5）：只暂启原5194的PG，不启动旧应用。两条旧自动控制案例的原始候选及客服修改按原生SHA/父链匹配，均落在MODIFY/CHANGED_INFORMATION层，动作一致0/2；双方非均退款，退款金额分母0，补证语义也不可测。该结果验证差异统计，不说明真人或真实模型正确率。前后4快照、2审核、2审批、1执行的逐行摘要相同；旧state和旧报告不变，PG恢复停止、临时容器移除。

5200独立新campaign真实PG受控消融已PASSED：两案四臂共22次fixture调用、0供应方。普通5/5次输出一致，复杂7/5次分别执行一次局部补查/记录关闭原因转人工；原public指定9表及旧源保持，重放无新增尝试。独立原生只读复核确认初始取证及评估相同、关闭组无可执行方案、容器清理。详细报告及范围见[消融计划](CHAT_P5_ABLATION_PLAN.md)。真实模型消融、独立判断、客户同案业务闭环和补证结构化一致率仍未完成。

## 逐方案独立复核与原生程序门禁指标

新增显式chat-blind-labels-v2，与V1历史和幂等摘要兼容。初判/揭示后逐方案选择可接受、不可接受、无法判断；无默认通过，缺项/重复/越界拒绝。门禁只从原生不可变快照checks提取，私有登记绑定SHA和匿名option映射，不出现在评审页面。旧任务和旧映射不回填新证据。

新gate_accuracy纯模块按可执行option统计程序错放/误拦，严格区分程序门禁与模型自审/人工审批；原生时间顺序、摘要链、计划/输入/arm/匿名ID需一致。CONTROL、未知、缺V2、缺gate、非执行方案保留排除；按case_id+arm分别报告。没有外部核验的独立人工来源就不计分母，不把测试员身份或页面自报当独立金标。

root完整非PG回归1064通过、2跳过、40PG未选，末次分层增量后聚合+原生SQLite跨模块15通过；前端49单元、6相关Chromium及构建通过。真实运行栈未升级，原生PG V2/真实浏览器/独立质量结果仍未验收。范围与分母见[逐方案复核说明](CHAT_P5_GATE_REVIEW.md)。事实遗漏、政策适用/引用及补查前后质量标签继续待补，不用本项替代。

## 5201应用升级与原生V2盲审（2026-11-11）

root审查并执行 `upgrade_owned_material_demo.py`，将5201四个应用容器升级为b49f914固定镜像，PG/S3/Redis实体与数据卷保持。旧应用暂停后进行独立目标恢复验证，131纳管表摘要及14对象在源/归档/恢复目标字节一致；保持暂停直到网络隔离后移除旧应用，没有先恢复写入再替换的窗口。备份留在私有 `.runtime/chat-backup-restore-ytrmifwv/archive`。实际PG源7706834943065174055、恢复目标7706863907855241254不同，6个临时资源已独立确认不存在。首轮默认网络地址池耗尽FAILED保留，后续仅临时网使用经检查不重叠10.253.251.0/28；未清理历史网络或卷。新receipt为 `reports/owned-5201-blind-v2-upgrade-20261111.json`，计划f9db7f3c2f93a413f115d19b60d1e5e28580c620911f49b40c5933628de31b44。

`verify_chat_p5_option_labels.py` 在固定5201栈/PG身份与原sentinel下，新独立campaign完成六候选，普通V2任务bbe0a0d9-4942-4e12-b275-0a6a079d30ca保留3逐项标签，复杂V1任务6f2270e3-88a9-4f8e-b925-3f303c9b4227保留原整体标签。逐项匿名ID/status与不可变snapshot/checks核对，原生全摘要链及时间顺序核对，篡改曝光绑定被拒绝；新标签重放未新增记录，旧公共业务与原评审行保持。报告 `reports/chat-p5-option-labels-pg-20261111.json` PASSED，计划59782e90ca63e0b8c67f9179a73021d5a465b2905fa29ee56c9e708beb121689。26次受控调用、0供应方/独立人类标签；统计全部CONTROL排除，不可测。原生服务/PG写入不等于HTTP表单提交验收，第二tester隔离未测；已有非tester拒绝和旧数据保持证据。root定向15项离线测试及新增脚本Ruff通过。

原生浏览器后验 `reports/browser-blind-v2-after-20261111.json`：V2三逐项UNDETERMINED刷新前后保持只读，V1整体记录可读且不要求补填，旧客服材料审核与客户聊天入口保持。30请求无抓包错误/丢失；无业务写入，不视为HTTP表单提交验收。专属会话退出关闭。

## 材料审核自动重评与顺序传递消融（代码，2026-11-11）

生产修复使用既有conversation→order/case锁和同事务持久轮次登记；当前事项关联需匹配，三项聊天开关均开启才启用。有效PENDING/RUNNING P2保留，避免丢掉客户无法补图的文字或在途视觉调用；已发布轮次需要重新调查时提升revision，旧任务不能发布覆盖。无新照片不重复视觉分析，审核重放不重复登记；不会创建客户消息或直接审批/执行。公开进度对比当前调查依赖，不再被过期offer/review遮蔽；已有人工运行优先，阻断显式NEEDS_SUPPORT。SQLite原生AgentService+P3投影和HTTP回读覆盖三种照片状态、新审核冻结、历史快照保留、幂等、旧offer及人工优先。新增运行栈验证尚未执行，5201仍b49f914。

顺序传递消融为独立test capability/manifest，首轮policy在fact完成后收到主张、无继承引用权限；verify/recheck与默认工具预算保持。两案四臂实际SQLite runner共24 fixture attempts，普通5/5、复杂7/7，复杂两臂都一次补查与verify1。默认服务真实start拒绝，失能力恢复在checkpoint/model前拒绝，重放无新增。不能称纯并行实验，不代表真实模型或正确率。root runner/graph30项及最终完整非PG1075通过、2跳过、40PG未选；改动文件Ruff及diff检查通过。前一提交6b613ca托管CI43007178878成功。

## 材料重评真实队列续验（2026-11-11）

7da08d7后端7319ccca9d062c57d8b2f91db388473db2cedc594f3661605bc81b6d17c43ea8已部署5201，前端adc940…51c98保持。升级计划05b1583cc93a48589715a2cf64a8e774d86c9473c3b999f16d53804b285f0338，源PG7706834943065174055、独立恢复PG7706869421385400358；451纳管表/46对象源归档恢复一致，所有临时资源已独立确认移除。仅analysis从False改True，原配置私有归档。CI旧失败43008485649为ubuntu-latest的26.04字体安装不可用；固定24.04的5906b06对应43009102154成功，应用代码未因CI改动。

真实HTTP/Celery测试计划b8c8e1a1b13b07106ae413084774730b783f832f11bd57eabb3e8aef654a7650，C002/O0288/L01（C001两可用单行订单已有case，未复用或改历史数据），conversation0b1cce8e-2954-4477-9118-be01fb0b55a1，casefe8c0602-1a46-4c2d-a666-127088f5d084。客户显式选单行、文字、同会话双图，support关联原图并提交明确CONTROL模拟审核，无HUMAN结构化观察；不直接调用worker process/stage。审核后实际第三轮请求7fbb6d78-ad85-45ce-a015-874667c5f32c、run1f14ff3b-98a3-45f5-b854-840bd74240f2、新候选8d324f2b-3192-4481-9329-1bfed2f56d4f，记录有效review且规则ELIGIBLE，但模型关闭所以INVESTIGATION_MODEL_UNAVAILABLE/HUMAN_HANDOFF。客户消息仍2条，客户PENDING_SUPPORT。

原 `reports/chat-material-review-reassessment-20261111.json` 保留FAILED/NATIVE_CHAIN：验收错误要求自动AGENT snapshot.parent_id指向上一候选，该字段实际用于人工修改；自动前序通过持久request.previous与Agent binding.expected_previous绑定。只读补充 `...-reconciliation-20261111.json` PARTIAL验证双绑定、scope、三快照及原图历史回读；它当时尚未执行重复POST。root后续独立 `...-replay-20261111.json` PASSED，用MATERIALS_REVIEWED.sequence-1重建原expected_version并验证原Idempotency.request_hash，原actor/key/body精确HTTP重放得到原响应，前后轮次/请求/快照/审核/资金/模型读回摘要完全相同。原FAILED不改写，也不将补充核对伪装成原脚本完整成功。未来驱动断言改为previous双绑定，POST前保存body与baseline；root2项定向和Ruff通过。

root独立原生复核仍三轮/三请求/三快照、2客户消息，有效review4bf768bf-bca9-414c-942c-e92f55867a79；public proposals/approvals/executions/agent_attempts/chat_attempts/intake_audits全部0。此次是业务事件自动续跑的控制技术证据，不证明真实视觉、真实模型决策或真人审批质量。

浏览器 `reports/browser-material-reassessment-20261111.json` PARTIAL：客户2消息/2原图200且实际渲染，PENDING_SUPPORT、offer为空、无退款完成；客服第3候选HUMAN_HANDOFF和CONTROL审核标注可读，48GET无错误/丢失，退出关闭。发现自动AGENT parent为空时UI没有历史列表，不能从页面导航v1/v2；另有两条重复离线照片提示。历史API可读不替代页面可用性。两项后续修复中，总结报告保持PARTIAL，不改原失败或补充报告。

两项浏览器发现已在代码修复，尚未部署：support-only `GET /conversations/{id}/decisions/history` 当前issue倒序分页，返回issue_id与轻量摘要、exclusive before_version，原单快照历史及人工parent语义保留。前端列出自动和人工版本、历史只读，返回当前才能复核，跨事项迟到响应拒显，parent按钮改为修改前版本。审核重评且vision关闭时静默发布；普通客户新图仍给原不可查看提示，已有历史消息不删除。root后端相关22通过、1跳过及Ruff通过；UI代理5项Chromium、lint/类型/构建通过。当前5201仍7da08d7后端/b49f914前端，真实历史列表回查和新版提示需后续升级，PARTIAL报告保持。

## 候选历史保留数据升级与只读接口验收（2026-11-11）

root执行candidate-history阶段，计划92e4119874895e1cb6c86f8e0b384dc554381a44862c20f70b568b2adcfa69bb，部署72cb34d后端976230…88b3及前端09029e…e2f9；该提交CI43011303117成功。原配置SHA e9001758…5987保持，基础容器/卷不替换。独立恢复源PG7706834943065174055与目标7706874176240566311不同，451纳管表和50对象逐项一致，6临时资源已由root再次检查不存在。公开摘要reports/owned-5201-candidate-history-upgrade-20261111.json，私有原回执与备份保留。

新只读验收脚本绑定原conv/case/PG及两镜像，通过HTTP每页1条验证3→2→1及末页空，逐版摘要/当前head/issue与PG原生和原reconciliation报告一致；客户404，limit0/51均422，前后native相同，业务POST0。reports/chat-decision-history-http-20261111.json PASSED。root脚本纯测试1项及改动Ruff/diff通过；实际浏览器导航回查另行记录。没有新增模型调用、材料审核、候选或财务操作；审核后重复离线提示未在新部署上重触发，不能将本地测试写成运行栈效果验收。

真实浏览器 reports/browser-candidate-history-20261111.json PASSED：客服列表3→2→1，点开2/1的ID/version/SHA对上原生报告，旧版无审核或启动，返回第3版恢复当前控件。客户仍2条原消息、2图200、PENDING_SUPPORT、无内部历史入口或请求；51 GET无抓包错误/丢失，无业务写入，双方退出并关闭专属会话。旧PARTIAL报告保持，旧重复提示仍保留，本轮未触发防重复逻辑。此结果仅补齐历史导航实际可用性，不将原离线控制案例升级为真实模型或真人审核验收。
