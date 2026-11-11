# 正式私有目录执行的最小接线计划

目前仅实施第一片的原生来源判定。正式冻结、私有目录执行和留出解锁未开放。方案复用已有具名声明、测试员权限和解锁交接。来源核验、质量门槛、独立人工判断和发布决定各自留证。[原生证据契约](FORMAL_VALIDATION_BRIDGE_CONTRACT.md) · [冻结与留出计划](HOLDOUT_FREEZE_PLAN.md)

## 第一片 · 原生验证来源

当前 `validation-native` 复用顺序 CLI 的测试员身份。后端确认 PostgreSQL 目标，并在同一只读 `REPEATABLE READ` 事务读取原始实验、案例结果、复核与各结果隔离账本。普通导入和合成候选不能经过这个入口获得正式来源。[登记入口](../backend/src/aftercare/evaluation/formal/__main__.py#L27) · [只读事务](../backend/src/aftercare/evaluation/service.py#L1264) · [原生登记守卫](../backend/src/aftercare/evaluation/formal/register.py#L458)

- 原实现的 `NativeValidationSnapshot.source_verification` 只有 `NATIVE_LEDGER_ONLY`。增加 `NATIVE_VALIDATION_VERIFIED` 和逐条件阻断理由 `evidence[experiment_id].source_blockers`。旧快照省略新字段，保持原摘要。验证计划按每个案例和每个 `mode/strategy` 预登记 `model_call_expectations`，说明该条件应观察真实模型响应，或应由哪一种确定性门禁零调用完成。可用键是计划中的四个唯一条件。B 组复用 A 组同策略的单 Agent。未登记的旧计划仍可核对原生账本，不能提升。
- 四个条件均需匹配原始、已终态的验证实验，且覆盖完整 `case_id + repetition`。冻结输入、参考、配置、隔离尝试标识、默认路径与响应标记必须匹配。原生账本及冻结尝试均须带同一完整基础注册表摘要，并与实验配置匹配。旧记录缺摘要时保留原证据，不能按注册表版本回填。每条件至少一项观察到的默认传输响应。存在未执行、在途、用量或费用未知、账本差异、预算超限、模型绑定不明或适用的政策和索引证据缺失时，保留账本级别。只有明确的正常发现可通过，新增未知发现默认阻断。历史尝试无 `run_id` 仍不可从唯一 AgentRun 猜配；这里比早期契约更严格。
- 预登记零调用的案例需要冻结输出和隔离库持久的同一运行、同一周期上下文绑定与对应结束或暂停事件相符。要求专用标记的门禁，还要在该周期绑定之后、状态发布之前看到匹配事件。空账本、模型开关、遗漏检索均不足以证明门禁。有效零调用允许没有模型尝试和检索，仍需要适用的原生政策记录。
- 单个业务结果可以失败或待人工复核。证据可追溯且完整时保留真实失败。未知费用或未收尾尝试阻断来源提升。技术来源提升不改变 `UNAUTHENTICATED_DECLARATION`，也不生成质量结论。[契约模型](../backend/src/aftercare/evaluation/formal/contracts.py) · [来源核验](../backend/src/aftercare/evaluation/formal/native.py) · [受控测试](../backend/tests/evaluation/test_formal_source_verification.py)

目前 `CampaignRegister._validate_draft` 仍对 `purpose=formal` 无条件返回 `FORMAL_VALIDATION_SOURCE_UNVERIFIED`，并限定现有草案预算为零。来源判定本轮不修改这一防线。历史报告不会因升级代码自动改写为已核验。[冻结守卫](../backend/src/aftercare/evaluation/formal/register.py#L225)

## 第二片 · 冻结授权与私有只读目录

1. 新版 `FreezeDraft` 绑定已保存的原生来源快照、已确认的阈值、正式候选清单、唯一条件和分块顺序、案例重复、每案配额与各条件及全批费用上限。费用用整数微美元校验。现有草案只有单组预算与选中实验，无法描述完整正式 A／B 矩阵。验证期的完整配置摘要含验证集输入。正式期应复用已冻结的非数据控制项，独立承诺留出案例及政策集合摘要。两批案例标识不同，不能拿验证集的逐案政策摘要当成留出承诺。仅在新版接线全可用后，更新 `_validate_draft`、`_frozen` 和 `freeze` 按已锁存原生快照验证，无需引入另一轮审批。
2. 现有 `unlock` 会先写 OPENING、记录案例已读摘要，失败时保留暴露不确定，并在成功后写 `UNLOCKED`。成功事件未持久保存目录路径，`_inspect_package` 也只返回 manifest。为成功解锁增加私有路径绑定，连同冻结及清单摘要一次写入成功记录，公开摘要仍无路径。用现有无符号链接、大小和格式限制，在同一个打开的目录视图读取、校验后返回不可变的 cases 和 references。增加所有者与私有权限检查。恢复从绑定路径重验，禁止用调用参数另选目录。预检仍只读取登记元数据。若需实际照片文件，先明确路径白名单、逐件摘要与读取契约。本阶段最小接线仍用既有合成材料函数。
3. `formal.__main__` 增独立 `run --campaign … --execute`，先校验测试目标、当前有效 tester、冻结、解锁和来源状态，再打开私有目录。复用 `cli._runtime/_actor`，向内部专用目录适配器注入已验证内存视图。区分原始参考摘要与经过独立签署校验后实际用于评分的参考摘要。普通 `ExperimentInput` 和 `v0.2` 目录保持原样。零模型的合成流程只在受控测试里用内部构造，不通过正式 CLI 取得真实执行能力。[目录校验](../backend/src/aftercare/evaluation/formal/register.py#L286) · [解锁流程](../backend/src/aftercare/evaluation/formal/register.py#L593) · [文件边界](../backend/src/aftercare/evaluation/formal/files.py) · [测试运行环境](../backend/src/aftercare/evaluation/cli.py)

## 第三片 · 稳定实验、执行和恢复

- 在 `EvaluationService.create` 提取共享计划准备和数据库写入。新增仅内部可调用的 `create_formal/process_formal`。只接收已核验的冻结绑定与从清单派生的条件配置，不接受任意路径或普通 `ExperimentInput`。使用固定命名空间的 UUIDv5 由冻结摘要和条件键推导实验 ID。创建事务锁也按该稳定绑定加锁，以现有主键唯一性兜底。存在时核对绑定和配置，完全一致就回读原实验。保留原 `created_by`。当前执行者从这次已有 tester 凭据取得，在租约内给 `_cancelled/_admit_attempt` 显式传递。现有实现始终检查创建者，单靠 actor-scoped 幂等键或同 ID 不足以安全处理跨操作员继续。
- 现有 `process/_process` 支持整实验顺序运行，并在租约重入时把已领取的 RUNNING 结果结为 `ERROR/WORKER_INTERRUPTED`，用 `EvaluationRunner.recover_audit` 回读原隔离账本。复用固定分块调度和这一恢复语义。继续只领取原实验中未开始的 QUEUED，已完成、失败或状态未知的原结果不重新进 runner。不要给同一个结果重建 `eval_<UUIDhex>` 或启动新的条件实验。若必须采用跨条件逐案交错，需先新增一次只领取预登记指定案例的调度入口，整实验 `process` 本身不能提供该顺序。
- `_allocate` 已按微美元拆分单实验费用，`EvaluationRunner._model` 也会收紧逐案模型额度。正式条件使用预登记额度，不从失败、未花完或未知的条件转移。保留已准入但费用不明的保守预留。创建、每次恢复和首个付费准入前重验代码／锁、评分器、指标、实际模型注册及默认传输、清单／原始及有效参考、政策选择器、索引配方和配额。当前 `_process` 主要在开始检查代码、模型和向量设置，`_claim` 查单案输入，`_admit_attempt` 只查状态和创建者权限。把额外核验作为同一内部绑定传入 `_run_one`、`_admit_attempt`，Agent 与意图探针共用。失败后保留原账本与结果，阻止后续付费。向量构建和实际使用结果仍由 runner 的持久证据证明。[实验创建](../backend/src/aftercare/evaluation/service.py#L837) · [领取与恢复](../backend/src/aftercare/evaluation/service.py#L1522) · [逐案执行](../backend/src/aftercare/evaluation/runner.py#L464)

## 第四片 · 普通入口隔离与验收

- 加不可由普通请求传入的正式绑定标记。用同一个可见性规则守卫 `_find/experiments`，并覆盖绕开 `_find` 的 `progress/read_progress`、`scenario_metrics`、按结果读取的 `review/supplemental_usage`。确保普通实验列表、详情、导出、比较、下钻、流、取消与补跑都不会泄露或操作正式记录。原生验证读取也仅接受原始验证实验。正式报告和以后的人审只走明确绑定的私有操作，公开输出仅给汇总和摘要。
- Celery 的 `pending`、`target_generation`、`reconcile_failed` 已过滤普通 inline 任务。额外按正式标记拒绝被污染的队列记录。普通 inline 的 `process` 也要拒绝正式实验，单检查 `execution_driver` 不够。私有输入、结果和隔离库含明文，测试库备份与归档权限一并按私有材料约束处理。[普通服务入口](../backend/src/aftercare/evaluation/service.py#L773) · [按结果直读](../backend/src/aftercare/evaluation/service.py#L1234) · [Celery 领取](../backend/src/aftercare/queue_delivery.py#L12)
- 先用纯测试验证旧摘要兼容、明确失败仍可追溯、未知或新发现无法升格。后续用隔离测试 PostgreSQL 和专用合成包验证跨测试员并发创建只有一个条件实验、崩溃后仅领取未开始项、全部普通读写入口与队列拒绝、漂移和未知费用不会突破预算。真实验证来源、私有新候选、正式留出结果和人审另行取证。本轮没有读取私有候选、解锁或调用提供方。

## 第二至四片的实现契约

下面是待实施接口。第二片可以先实现草案校验和受控目录测试。正式冻结和解锁守卫等稳定执行、普通入口隔离及对应集成检查全部就绪后才一起开放。沿用已有确认与交接记录，无新增人工审批。

### 2A · 从验证原始快照提取运行控制

在 `native.build_snapshot` 从已核验的四份原始配置、在原有只读事务内生成版本化 `controls_by_condition`，随原生快照写一次并纳入摘要。同一单 Agent 基线在 A／B 共用，只保存一条控制记录。每条控制记录绑定来源实验 ID、原始 `config_sha256`，以及单独的 `controls_sha256`。正式草案引用这些摘要，不能提交一份自行拼装的“验证配置”。旧快照若没有该字段保持原序列化和摘要，不允许仅根据 `code_digest` 或 `plan.policies_digest` 补齐。需有原生证据才可生成新版投影，旧锁存文件保持原样。

| 投影分类 | 字段及核验规则 |
| --- | --- |
| 必须继承的运行行为 | `mode`、`strategy`、`source=live`、`probe_intent`、代码和依赖指纹、评分器／指标版本、完整模型快照摘要及基础注册表摘要、模型和流式模式、政策选择器版本及摘要、完整检索配方摘要。检索配方覆盖 BM25 参数、分词、融合、向量模型和快照、维度、前缀、距离。代码指纹覆盖内置提示词和路由。运行时要求锁文件存在并参与指纹，不能接受缺锁回退。 |
| 按留出候选独立承诺 | 数据集标识、划分、案例及顺序、输入／参考摘要、逐案政策语料摘要、汇总 `policy_snapshot.cases_sha256`。从新候选清单与通过核验的私有目录视图生成，不从验证集逐案摘要复制。新政策内容允许与验证集不同，同一留出案例在所有条件与重复中使用同一份冻结政策目录。索引内容在运行后按实际语料和冻结配方取证。 |
| 冻结时独立声明 | 正式运行固定 `inline`、`concurrency=1`、分块顺序、重复及预算。验证期的调度方式、重复数、预算、状态和分数保留在源报告，分别记载，不能误称与正式预算相同。技术门槛使用冻结正文及摘要。若所选验证配置已有门槛，正式草案沿用同一正文；若源配置无门槛，在现有草案确认中一并绑定正式门槛及其依据。 |
| 默认拒绝 | 对原始配置采用版本化字段分类。遇未归类的新字段先拒绝，尤其不能用“删除数据字段后对余项取摘要”跨过字段语义审查。缺运行必要字段、模型登记、锁或索引配方时，来源摘要依然可查，正式草案不可确认。 |

原生验证原有 `input_digest`、`reference_digest`、`policies_digest` 配对检查继续保留。正式端对投影只比较非数据控制项。例如比较 `policy_snapshot.selector_sha256`，然后独立核对新候选的 `cases_sha256`。禁止把整份验证 `config_sha256` 或整份 `policy_snapshot` 当作正式配置等式。[原生快照构建](../backend/src/aftercare/evaluation/formal/native.py#L468) · [政策与检索快照](../backend/src/aftercare/evaluation/retrieval_evidence.py#L44) · [运行指纹](../backend/src/aftercare/evaluation/service.py#L127)

### 2B · 草案版本与预算

`FreezeDraft` 保留旧版 fixture 解析。新版草案使用 `schema_version=ac-formal-freeze-draft-v2`，外层冻结文件仍使用 `ac-formal-freeze-v2`。新版字段拒绝额外键，整数字段拒绝布尔、浮点和字符串，缺版本不可被识别为新版。一次写入的 `freeze.json` 继续绑定既有 `FreezeConfirmation.draft_sha256`。

| 字段 | 约束 |
| --- | --- |
| `candidate_sha256`、`manifest_sha256`、`validation_sha256`、`validation_plan_sha256` | 必须与当前登记、已锁存的 `NATIVE_VALIDATION_VERIFIED` 原生快照及其计划完全一致。仅有导入或旧快照阻断。 |
| `selected_validation_experiment_id`、`gate_policy` | 入选 ID 等于锁存选择，门槛正文使用现有模型和确认。选型结果保留原报告，技术门槛不改写人工声明。 |
| `group_b_strategy`、`block_order`、`conditions` | 固定键为 `single/economy`、`single/analysis`、`single/routed` 和 `multi/<group_b_strategy>`。组 B 策略等于验证计划的唯一 multi 策略。A 引用三条 single，B 引用同策略 single 及 multi；共运行四块。顺序数组是四键的一个显式排列，绝不排序替代登记。每条件有 `validation_experiment_id`、`controls_sha256` 和该条件总配额，按来源核验。 |
| `row_order`、`repetitions`、`case_count` | 首版只支持 `REPETITION_THEN_CASE_ID_ASC`，重复为 1–3，案例数等于候选分布总数。选入清单全部案例，解锁前无需展开任何案例 ID。解锁后由稳定排序派生 `(case_id, repetition)`。结果用原生结果唯一约束。 |
| `row_quota`、条件 `quota`、`campaign_quota` | 各有 `calls`、`budget_microusd` 两个正整数。首版四块使用同一逐案逐重复配额，避免策略对照同时改变预算。每条件额度严格等于该配额乘以案例数与重复数，调用数不超过现有每实验 144 上限，微美元不超过现有每实验 10,000,000 上限。四条件总额分别不超过本次已有授权的全批调用与费用封顶；不能由草案放宽外部有效上限。部署端为正式执行入口配置明确的正整数调用和微美元封顶，缺任一值时保持关闭。冻结与运行均检查该封顶。可在封顶内留余量，禁止把余量或失败、未知、未使用份额转给其它条件。 |

只在底层计价或兼容展示时转换 `Decimal(microusd) / 1_000_000`。内部不经浮点，也不让普通 `_allocate` 自行按剩余额度发放或产生零配额尾行。可提取普通分配共用的转换和范围校验，正式计划直接提供每行的冻结配额。受控测试使用现有 fixture 分支或仅内部的模拟入口；正式 CLI 不接受注入 transport、不支持无模型降级。

### 2C · 私有包和成功解锁绑定

新增 `ac-formal-package-v2`，继续对清单和每个固定名称文件按原始字节验 SHA-256。除 `cases_sha256`、`references_sha256` 外，新增 `policy_catalogues_sha256`，其值是从案例内实际政策和覆盖按 `case_id → catalogue_sha256` 得到的摘要。可选包内金标声明按固定文件名和原始摘要登记，并与逐案案例及原始参考绑定。读取层单独保留这份 `UNAUTHENTICATED_DECLARATION`，有效评分参考保持 `PENDING`，不计入独立人工覆盖。若后续有独立人审来源，须对照冻结绑定确认具体案例与参考，再形成可追溯的有效参考版本。同一包内的摘要只证明文件绑定，不能认证审核人或证明独立性。首版沿用由案例生成的合成材料，不接收任意文件路径。加载前用正常数据及脚本校验器检查允许的事件、合成业务数据和引用完整性，参考与 Agent 和业务写入层分开。

目录绝对路径不能在工作区内，沿路径逐级 `O_NOFOLLOW`，最终目录及各文件必须属于运行 OS 用户，目录仅所有者可访问（如 `0700`），文件仅所有者可读写（如 `0600`），拒绝可替代的硬链接及类型异常。中间目录禁止符号链接，但系统目录不要求同一 UID。使用同一目录 fd 读固定名称、有大小上限的普通文件，按内容摘要判定，校验后转换成只读视图；目录适配器每次返回独立副本。工作区排除以实际打开的无符号链接路径核对。不同测试员账号可在同一受控 OS 运行身份下继续，认证身份和文件所有权各自校验。

沿用现有暴露日志和锁。先追加并持久化 `OPENING`，读案例明文即写 `CASES_READ`。仅成功时在同一条私有 `UNLOCKED` 事件保存 `freeze_sha256`、原始 manifest 摘要、规范化路径及打开目录身份（设备与 inode），再回读核对。读失败、写失败或只见 `OPENING` 时阻断执行，保留暴露不确定。后续 `run` 没有目录参数，只从该事件打开，重新比对身份、权限、内容摘要、有效参考和政策视图。替换目录或改内容即拒绝。预检只读登记记录及公开运行指纹，任何返回、错误和公开日志都不含路径、候选、参考或密钥。[安全文件读取](../backend/src/aftercare/evaluation/formal/files.py) · [解锁与暴露日志](../backend/src/aftercare/evaluation/formal/register.py#L593)

### 3 · 数据库绑定、分块与未知恢复

- `python -m aftercare.evaluation.formal run --campaign <id>` 默认只读登记预检，不打开候选、凭据或模型。正式执行能力开放后，必须显式使用 `--execute`，并由项目运行目录中的私有 `.runtime/formal-execution.json` 绑定对应的 `campaign_id`、`freeze_sha256` 和 `execution_cap`。该文件使用 `ac-formal-execution-deployment-v1`，额度为严格整数 `calls`、`budget_microusd`。沿用私有 `users-test.json` 的测试员凭据，多个测试员时由本地环境变量 `AFTERCARE_FORMAL_TESTER_ID` 选择已存在的测试员。命令不收私有目录或额度参数。每次成功取得本批私有报告后，在登记目录原子保留一个带记录时间、权限为 `0600` 的版本，终端只显示状态、条件、实验编号和计数。
- 在实验表保存可索引、不可由普通创建传入的正式绑定。建议可空的 `formal_freeze_sha256`、`formal_condition_key` 成对存在，并设唯一约束。规范 ID 为 `UUIDv5(固定项目命名空间, freeze_sha256 + "/" + condition_key)`。内部创建在同一事务拿绑定锁、按确定 ID 查找并核对案例／政策／参考／配置／配额摘要，冲突即拒绝。创建绑定、实验、全部结果和首条进度应同提交。进度、结果和审计留在现有表，原创建者不变。冻结摘要、配置摘要及四实验对应关系可以从数据库重建，不能把缓存本地汇总当作唯一状态。暴露检查范围为本地登记账本，兼容 `--root` 但不覆盖其它账本或外部披露。同一冻结摘要在同一测试库始终使用原实验和额度。
- 顺序 CLI 只允许 guarded test PostgreSQL 与有效 tester。按冻结摘要拿全批专用锁，复用每个实验租约。严格按 `block_order` 一次运行一个条件。重复命令、并发操作员及崩溃恢复都读相同实验。会话凭据撤销后只停止当前操作，新的有效 tester 可以接手既定冻结。领取与尝试准入检查当前操作者仍有效，记实际操作者及变更时间。普通实验保留原创建者检查。锁范围不得锁住测试员身份行跨外部调用，保持可撤权。
- 只领取尚未开始的 `QUEUED` 结果。原运行中断时按已有租约逻辑结为 `ERROR/WORKER_INTERRUPTED` 并回读原隔离账本；状态不明的尝试按原预留计入，严禁重建该结果 schema 或重发同一请求。业务失败、明确的模型失败或质量未通过原样记录，已结算且来源仍可信时可继续。费用或尝试状态未知、账本不可读、租约丢失或配置／目录漂移后停止新的付费准入，正式批次保留 `INCOMPLETE` 和原因。恢复命令仅在原账本证明可继续后领取尚未开始行。后到用量可解释原尝试，但不回填冻结结果或补跑。全批预算固定，没有退款式重新分配。
- 在创建、继续、每块开始、首个付费准入前复核冻结数据和运行投影。后续每次准入通过同一绑定检查会话权限、数据库绑定、未进入停止状态、逐案限额和保守费用。用尝试持久记录阻断已出现的 `UNKNOWN`、遗留 `STARTED` 或费用缺证，覆盖同一模型逻辑调用内的自动技术重试和意图探针，不等到整案返回才停止。代码或注册表漂移不得绕过现有守卫。文件在本次读取时形成不可变视图，运行只使用已校验字节；跨阶段核验同一绑定，不允许重新从未校验目录加载参考。进度以全批、各块和原结果 ID 关联，汇总保留未执行、失败、未知及实际与预留费用。技术门槛仍不代表发布批准。[领取及准入](../backend/src/aftercare/evaluation/service.py#L1493) · [模型配额](../backend/src/aftercare/evaluation/runner.py#L488)

### 4 · 普通入口隔离与实施分工

使用独立于 JSON 配置的表列定义正式可见性。普通 API 直接按主键请求一律返回与不存在相同的 404。列表及总数排除正式行。按结果 ID 查询先关联父实验再授权；覆盖复核和补充用量。覆盖独立进度查询及 SSE、场景指标／贡献者、比较、JSON／CSV 导出、取消、补跑、原生验证来源读取，以及任意直查实验的聚合路径。普通目录、普通 CLI 和导入接口不能通过路径、文件或配置构造正式记录。普通 `process`、`pending`、派发、队列目标解析、失败调节和任务执行最终入口均要拒绝正式行。即使人为插入队列记录或篡改 `execution_driver` 也不能领取。备份私有实验和隔离库时沿用私有文件及恢复权限；普通报告和诊断导出不携带其明文或位置。[普通列表和详情](../backend/src/aftercare/evaluation/service.py#L773) · [独立进度读取](../backend/src/aftercare/evaluation/progress.py#L46) · [队列目标](../backend/src/aftercare/queue_delivery.py#L12)

| 可并行片 | 独立文件与验收 |
| --- | --- |
| 来源投影与冻结草案 | `formal/contracts.py`、`formal/native.py`、`formal/register.py` 的草案／冻结校验。纯测试验证旧版摘要不变、不同留出政策内容可配对、控制字段逐一变化被拒、四条件及共用基线、顺序与预算边界、历史快照和导入阻断。 |
| 私有目录 | 新 `formal/package.py`、`formal/files.py`、独立目录测试。用临时合成包覆盖权限、所有权、符号／硬链接、替换目录、破损文件、读取顺序、金标绑定、预检零读取和日志脱敏。与登记负责人协调后合入 `register.unlock`，避免并行编辑同一文件。 |
| 表绑定与普通入口隔离 | `evaluation/tables.py`、新 Alembic 迁移、`evaluation/progress.py`、`queue_delivery.py` 及实际任务入口。先提交表列和内部可见性接口，`service.py` 由同一负责人集成过滤。测试所有直查和关联查询、伪造队列记录、普通旧数据兼容。 |
| 执行及恢复 | 新 `formal/execution.py`、`formal/__main__.py`、`evaluation/service.py`、`evaluation/runner.py`。先用控制替身做无付费测试，然后由 root 串行用隔离测试 PostgreSQL、合成包验证两位 tester 并发仅一份实验、启动后进程中断、已有未知尝试不重放、漂移和撤权使后续调用为零。仅在此项与隔离项完成后移除正式冻结硬拒。 |

文档方案不授权读取实际私有候选、开启留出、发起正式评测或追加人工评分。受控样本验证工程接线，真实来源与最终质量以各自保存的证据为准。


## 执行接线接口审定（实施中）

根代理已批准三方开始接线，以下是实现约束，不代表已验收或正式功能已开放。

- 注册表 `open_for_execution` 以 campaign、预期冻结摘要、部署额度和操作者记录取得 `ExecutionHandoff`，包含冻结摘要、V2 草案、条件控制与不可变私有包。没有任意路径参数。每次读取有唯一 read_id，旧 UNLOCKED 不得掩盖未闭合的 REOPENING；未完成读取保持暴露不确定。相同已知案例集合的重开可记录比较摘要，但首次完整指纹及任何新集合指纹保留。
- 内部 `FormalEvaluationService.run` 接受当前 tester、凭据摘要、冻结摘要、草案、控制、私有包及明确 execution_cap。由服务再次检查额度和身份，不只信任 CLI。条件 UUID 由冻结摘要和条件键确定。原创建者保留，当前执行者另行检查。普通 API 无法构造内部执行上下文。
- CLI 使用 `formal-cli:<freeze>` 外层锁覆盖开包至报告，执行服务用 `formal:<freeze>` 内层锁保护内部调用。只能外层到内层，不得反向取锁。两个 key 不同，不能在不同连接上争用同一 key 造成自锁。连接丢失意味着锁失去，后续准入停止，不能靠连接池重连继续。
- 运行只继续原 QUEUED 结果；中断的 RUNNING 用原审计记录结为中断，不能重建其隔离 schema。当前案例及前序案例/条件存在 STARTED、UNKNOWN 或费用缺证时阻止后续付费准入，保守预留不转移。代码、依赖、模型控制和有效身份按边界复核。
- `run` 的小型摘要不能取代完整报告。内部私有报告入口按同一批次绑定和当前身份读取四组原冻结结果、逐案轨迹、复核、门槛、费用及未执行项，带 formal_freeze_sha256 和条件标记。CLI 仅在 campaign 私有目录以 0600 原子写入版本化报告，不覆盖旧快照；stdout 只显示摘要，普通导入继续拒绝正式记录。
- 正式 freeze/unlock 总开关保持关闭，直到上述接线与普通入口隔离、中断恢复的集成验收全部完成。纯测试使用临时合成包，不授权读取实际候选、开启正式留出或追加人工评分。

## 原需求对照修正：配置交错执行（待实现）

Notion 原始评测要求明确写明“配置交错运行，降低供应商时段波动的影响”（notion_snapshot/evaluation.md 冻结与运行）。上文及 V2 草案采用整条件分块，不能满足此要求；此前通过的固定分块工程测试仍保留，但不得据此宣告完整正式评测实现完成。

下一步使用新版本冻结草案显式绑定交错顺序，旧 V2 摘要和历史结果保持原义。新调度按重复、案例及冻结条件序列逐项执行四种条件；只领取指定原 QUEUED 行。恢复前先核对整个批次所有既有行的审计，避免后面条件有未知调用时先启动前面条件。各条件原 UUID、逐案额度、原创建者与不可重放规则保持。全批顺序由冻结版本决定，不依赖数据库排序或上次内存游标。须验证真实领取顺序、跨条件中断恢复、任一条件 UNKNOWN 阻止全批后续、额度不转移和旧 V2 读取兼容。正式新批次启用须等交错与公开入口整链验收完成。

### V3 交错实现与针对性验收

已新增 `ac-formal-freeze-draft-v3`，必须填写 `schedule: REPETITION_CASE_CONDITION`。它继承 V2 的四条件、逐案及全批额度约束；`block_order` 在 V3 中表示每个案例内部的四条件顺序。重复编号升序、案例编号 Python 升序，依次运行四条件后才进入下一案例。条件序列写入各实验配置并参与恢复绑定，登记与执行交接保留 V3 字段。V2 仍保留整条件分块语义及原摘要，不能称作交错；新交错批次使用 V3。

首次领取前对四条件全部执行恢复和账本核验。任一条件存在未知费用或未终态调用即停止，不先补跑其他条件；已启动案例原 ID/结果保留，只领取指定原 QUEUED。24 项调度及既有执行纯测、29 项登记/交接/恢复测试、9 项实际隔离 PG 检查通过。实际 PG 使用受控零提供方运行器，覆盖完整交错顺序和第二条件中断后全批停止，不是正式模型效果验收。全链公开 CLI 集成及部署仍待完成。[交错审查](../reports/formal-interleaving-root-review.json)
