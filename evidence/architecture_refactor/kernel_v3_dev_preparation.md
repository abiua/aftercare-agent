内核 v3 整题开发检查：离线准备报告

2026-11-15，分支 review-v2。只做选题、脚本开发、合成测试与干运行，没有模型调用、付费评测、提交或推送。

新增与修改文件：

- `scripts/select_kernel_v3_dev_tasks.py`：保留上轮实现；只为两个 Git 查询加入 `--no-optional-locks`，避免只读查询更新索引或创建临时索引锁。选题前先核对固定 revision、干净工作树和两份数据的 SHA-256，再解析数据。
- `scripts/analyze_kernel_v3_dev.py`：新增运行目录只读汇总，参数 `--directory`，默认 `.runtime/kernel-v3-dev/run-v1`；成功分析后只写 `evidence/architecture_refactor/kernel_v3_dev_check.json`。
- `scripts/tests/test_kernel_v3_dev.py`：新增 19 个离线测试用例，包含确定性选题、数据哈希、咨询/读取/刷新、金额改正与软放行、认证、硬拦截、审查后备去重、费用未结算、缺失字段、六题 M2 干运行。
- `scripts/run_tau_official_campaign.py`：保留上轮仅有的三行增加，wiring 清单新增 `kernel_version`；没有改变执行逻辑、smoke/full 行为或任何已有排程。
- `evidence/architecture_refactor/kernel_v3_dev_tasks.json`：实际选题结果。
- `evidence/architecture_refactor/kernel_v3_dev_dry_run.json`：完整干运行 stdout，原样保存。
- `evidence/architecture_refactor/kernel_v3_dev_preparation.md`：本报告，包含完整 stdout/stderr。

选题结果：

| 组别 | 题号 | 匹配词 |
| --- | --- | --- |
| A | 29 | most expensive |
| A | 41 | easiest |
| A | 54 | maximum、cheaper、cheaper |
| A | 96 | cheapest |
| B | 16 | 无 |
| B | 22 | 无 |

train 原有 74 题，去除 44 后候选 73 题；A 候选 6 题，B 候选 15 题。开发集合为 16、22、29、41、44、54、96，均不得进入确认实验，剩余训练题 67 道。组内按数值排序，A 的动作计数允许同名操作重复，B 要求不同操作名至少两种。选题文件没有用户场景原文、参考动作参数或参考答案。

严格沿用现有 CONSTRAINT：其中包含 easiest、maximum 等非严格价格词，所以第 41 题因 easiest 入选。没有擅自收窄规则。集合排除声明写入选题文件；本任务未修改未来确认实验的入口来强制执行该声明。

运行入口及建议上限（以下为已验证的不带 --live 版本）：

```sh
cd /Users/mbw/Documents/aftercare-agent
GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/mbw/Documents/aftercare-agent/scripts:/Users/mbw/Documents/aftercare-agent/backend/src:/Users/mbw/Documents/aftercare-agent/.runtime/tau-official-source/src \
/Users/mbw/Documents/aftercare-agent/.runtime/tau-official-venv/bin/python3 -B \
  scripts/run_tau_official_campaign.py run \
  --scope wiring --architectures M2 \
  --task-ids 29,41,54,96,16,22 \
  --directory .runtime/kernel-v3-dev/run-v1 \
  --max-usd 2 --max-calls 4506 \
  --agent-calls 640 --user-calls 110 --max-output-tokens 1500 \
  --concurrency 6 --run-timeout 900 --request-timeout 120
```

上面的命令仍是干运行。用户后续若决定实际付费执行，需要自行另加 `--live`；本任务从未运行带该参数的命令。实际离线验证时额外在 PYTHONPATH 首位加入 `/private/tmp/kernel-v3-offline`，由临时 sitecustomize 禁止网络与 `.runtime` 写入，不属于正式运行配置。不要把这个禁止网络的临时路径带入后续实际付费命令。

调用上限分析：

- `tau_official_budget.role_bucket` 将 executor、reviewer、self_check、fact_expert、policy_expert、vision_expert 全部计入单题 `--agent-calls`；用户模拟单独计入 `--user-calls`。所有已预留的调用也计入整个 campaign 的 `--max-calls` 与共享 `--max-usd`。
- 清单 `max_review_calls_per_run=1` 对应官方 judge，不能当成程序内审查额度。它没有单独的 CLI 参数。本次六题的 reward_basis 均为 DB/NL_ASSERTION；16、29、54 各有一个自然语言断言，其他三题没有；ALL 模式不执行 reward_basis 之外的 communicate_info，因此保持官方 judge 上限 1。
- E2 为 agent=48、user=24、max_calls=14600（200 行批次），并发 6、单题超时 900 秒、请求超时 120 秒、单次输出 1500 tokens。本次沿用后三项运行设置和并发，单题调用额度提高。
- 专家最多 5 次咨询 × 每次 6 次模型调用 = 30 次 agent 调用。每个内核步骤最多执行者 2 次（一次原提议加一次护栏重试）、审查 2 次、修改 2 次，共 6 次。E2 的 48 次在专家用满时只剩 18 次供其他三个环节，可能过早截断。
- 当前官方 max_steps=100；另为初始化预留一步，采用保守包络 `6 × 101 + 30 = 636`，取 `--agent-calls 640`。用户模拟亦为最多 100 步加初始化预留，取 `--user-calls 110`。这两个值是宽松次数保护上限，不是预期调用量。contracts.Architecture 的 max_agent_calls=48 当前没有在官方执行路径中实施，真正上限来自 RowTransport 的 CLI limits。
- `--max-calls 4506 = 6 × (640 + 110 + 1)`，使全局次数上限不先于这六题各角色额度生效；费用仍硬封顶 $2。保守预留会按未缓存输入估计和最大输出占住预算，可能在已结算实际费用还不到 $2 时拒绝后续调用。并发六题共用预算，不能保证各题得到相同额度，也不能保证六题全部完成；超时和 100 步上限同样仍有效。没有提高总费用额度或通过新实验估算实际费用。

M2 客户端及凭据核查：

- contracts.architectures()['M2']：执行者 deepseek-flash（thinking disabled），独立审查与专家 gpt-6.1-sol。OfficialModelPort 使用 assignment.model，RowTransport 将 DeepSeek 发给其独立 Chat Completions 客户端，强模型走 OpenAI Responses。
- execute_campaign 的付费分支在排程含 M2 时装配 DeepSeek 客户端，base_url=https://api.deepseek.com；OpenAI 客户端 base_url=https://api.openai.com/v1。干运行在构造客户端与读取凭据之前返回。
- 与 E2 使用同一读取顺序：OpenAI 为 AFTERCARE_OPENAI_KEY → OPENAI_API_KEY → `.runtime/model-secret.json`；DeepSeek 为 AFTERCARE_DEEPSEEK_KEY → `.runtime/deepseek-secret.json`。只检查两个文件路径存在，没有读取或打印密钥内容，也没有验证凭据是否有效。
- 清单顶层 agent_model=gpt-6.1-sol 是现有通用字段，不是 M2 执行者的有效模型配置；review_model=gpt-5.4-mini-2026-03-17 指官方 judge，程序内审查使用 gpt-6.1-sol。未为改善这些旧字段而扩大 runner 修改范围。

汇总口径与边界：

- 七项 checks 使用这次要求的名称：all_finished、expert_reads_used、expert_consults_bounded、amount_rule、program_blocks、review_fallback、auth_reminder。金额判定沿用原定义：无触发或至少一次相邻重试 guard 放行；认证判定为所有出现提醒的题最终成功 find_user_id_*，或无提醒。程序拦截始终 needs_manual_review，不自动判断误拦。
- private_trace 没有官方 turn 编号。报告同时给出从事件顺序推导的内核 step，以及精确的零起始 event_index，不把它称为官方对话步骤。金额每次 guard 触发都计数；一次未改正的尝试可能对应原提议和重试两个触发事件。
- 只有紧邻的 executor_step → 可选 normalization → guard 才被当作同一步重试；post_review_guard 后紧邻 guard_soft_unresolved 按软放行处理，不把后来步骤的成功混算为改正。reply_amounts 来自内核金额解析函数，uncheckable_amounts 来自原 guard feedback。
- 后备审查按 proposal_review 的 fallback_reason 与关联 review_resolution=review_unavailable 取并集，同一次不重复计算。failures 是失败尝试列表长度，其中可能包含最终失败，不能说每条都产生了后续重试。
- 咨询事件未保存信号名，因此每次咨询 signal=unknown；不凭角色猜信号。缺少字段、费用文件缺失/损坏/尾行不完整等标 unknown，保留 unknown_fields；有完整事件列表但不存在相应事件才填 0。没有可用 schedule 时无法确定题目分母，会报错退出。
- 当前适配器在 GuardRejected 时保留 private_trace 并向外抛出；run_row 写 error_type=GuardRejected，没有自动转人工实现。报告区分 guard_rejected 和 transfer_tool_calls，后者只表示官方轨迹出现转人工工具调用，不推断因果。
- 本次没有真实 v3 运行目录，所以没有生成正式 `kernel_v3_dev_check.json`。合成结果仅在 pytest 临时目录写入和验证，不冒充整题运行证据。未来真实运行后可执行同样的 PYTHONPATH/Python 环境加 `scripts/analyze_kernel_v3_dev.py --directory .runtime/kernel-v3-dev/run-v1`。

验证结果：

- scripts：用户指定的 S 与 R、官方 venv Python 和 PYTHONPATH，`python3 -m pytest scripts/tests -q -p no:cacheprovider`：71 passed，3 subtests passed，6 warnings，9.66 秒。另设 PYTHONDONTWRITEBYTECODE=1、GIT_OPTIONAL_LOCKS=0。S 中临时加入离线审计 hook，测试过程禁止网络且禁止写真实 `.runtime`。警告来自现有 anyio、audioop、Pydantic 依赖。
- backend：`cd backend && PYTHONPATH=../scripts uv run --frozen pytest tests/agent_kernel -q`：171 passed，0.20 秒。另设 PYTHONDONTWRITEBYTECODE=1、UV_OFFLINE=1、GIT_OPTIONAL_LOCKS=0、UV_CACHE_DIR=/private/tmp/kernel-v3-offline/uv-cache；默认 uv cache 不在沙箱可写范围，第一次尝试被拒后改用临时目录，不涉及依赖安装或 lock 文件更改。
- 干运行退出码 0，完整 stdout 见下方；stderr 只有“未安装语音依赖，跳过语音用户注册”的 DEBUG 提示，不影响文字 Retail。
- `git diff --check` 通过；`git status --short .runtime` 无输出；backend/src、backend/uv.lock、scripts/tau_official_agents.py、docs 均无 diff。
- `.runtime` 前后 71682 个条目，无新增、无删除、无非目录条目变化，`.runtime/kernel-v3-dev` 不存在。快照发现 `tau-official-source/.git` 目录 mtime 变化；可能来自初次 Git status 的临时索引锁（未观察到锁本身）。后续选题查询加入 --no-optional-locks，其他命令设置 GIT_OPTIONAL_LOCKS=0。没有为了清理这项差异而回写目录时间戳，因此不能声称全部目录元数据逐字节未变。

完整干运行 stdout：

```json
{
  "schema_version": 1,
  "benchmark": "tau2-bench",
  "domain": "retail",
  "split": "train",
  "architectures": [
    "M2"
  ],
  "kernel_sha256": {
    "__init__.py": "8e178efe57c64b11019949e133317036628e4cae7be40a74858802a80163683e",
    "amount_arithmetic.py": "43376b183c9e059060d85b16d0a5cba9b34ce6e29d77147e0a5fc369c39cd297",
    "contracts.py": "fe26b8aa90f3079d7b710b0a31aff67c839f41f91167286354f833271e3c608b",
    "dependencies.py": "c629b29373277f78f0036bb2a447fe063894f4850a9bef947d8b798f41990f26",
    "engine.py": "ea2c193b42a28e78d93d487dbd313e61246ea072666202b5187b2a08bd5b5d60",
    "experts.py": "85f52c489b8dae85b687f1e45c6a3d5b071e1b6f24e470beaae0b14da2c30bd1",
    "retail_guards.py": "57fddae34a734d69864cc983f476c15fd4f45318092500f192c5c23c0255a617",
    "review.py": "3adb47dc8a29b029ab607ba5bcf9cf30c0d518776713e1d788aefcf96f26e8d5"
  },
  "scope": "wiring",
  "revision": "da0835f6c67baef58bdda18f2a3b037dfb3a1754",
  "task_ids": [
    "29",
    "41",
    "54",
    "96",
    "16",
    "22"
  ],
  "trials_per_task": 1,
  "agent_model": "gpt-6.1-sol",
  "user_model": "gpt-6.1-terra",
  "review_model": "gpt-5.4-mini-2026-03-17",
  "tool_names": [
    "calculate",
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "find_user_id_by_email",
    "find_user_id_by_name_zip",
    "get_item_details",
    "get_order_details",
    "get_product_details",
    "get_user_details",
    "list_all_product_types",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "transfer_to_human_agents"
  ],
  "budget": {
    "max_agent_calls_per_run": 640,
    "max_user_calls_per_run": 110,
    "max_review_calls_per_run": 1,
    "max_output_tokens_per_call": 1500,
    "max_steps": 100,
    "timeout_seconds_per_run": 900,
    "timeout_seconds_per_request": 120,
    "max_calls": 4506,
    "max_usd": 2.0
  },
  "dataset_sha256": {
    "db.json": "413a65160adbdb5fde0ffc0015c49b6d70250b10c18128de169b597af7766765",
    "tasks.json": "8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8",
    "split_tasks.json": "ed0580ec52575b63fbf76568af42490da6ee7783ecb4aa81af46961291358f20",
    "policy.md": "2c9652afbce57d6e087768d37cda64d31c53d50b3e3225cfdb791bac66466467"
  },
  "schedule": [
    {
      "task_id": "29",
      "architecture": "M2",
      "seed": 42,
      "task_summary": null
    },
    {
      "task_id": "41",
      "architecture": "M2",
      "seed": 43,
      "task_summary": null
    },
    {
      "task_id": "54",
      "architecture": "M2",
      "seed": 44,
      "task_summary": null
    },
    {
      "task_id": "96",
      "architecture": "M2",
      "seed": 45,
      "task_summary": null
    },
    {
      "task_id": "16",
      "architecture": "M2",
      "seed": 46,
      "task_summary": null
    },
    {
      "task_id": "22",
      "architecture": "M2",
      "seed": 47,
      "task_summary": null
    }
  ],
  "evaluation_type": "ALL",
  "reasoning_effort": "low",
  "agent_adapter_sha256": "c43ac0749f7f21a3341a528495a54f714e339b6712979bdb1958b74ecc87fff6",
  "pricing_usd_per_million": {
    "gpt-6.1-sol": [
      "2",
      "10",
      "0.10"
    ],
    "deepseek-flash": [
      "0.30",
      "1.20",
      "0.006"
    ],
    "gpt-6.1-terra": [
      "1",
      "5",
      "0.05"
    ],
    "gpt-5.4-mini-2026-03-17": [
      "0.75",
      "4.50",
      "0.075"
    ]
  },
  "pricing_sources": [
    "https://developers.openai.com/api/docs/pricing",
    "https://developers.openai.com/api/docs/models/gpt-5.4-mini"
  ],
  "price_order": [
    "input",
    "output",
    "cached_input"
  ],
  "cache_mode": "explicit with no breakpoints for gpt-6.1 models",
  "seed_scope": "official orchestrator; Responses does not expose a provider seed",
  "concurrency": 6,
  "kernel_version": "aftercare-kernel-v3"
}
```

完整干运行 stderr：

```text
2026-11-15 12:05:50.366 | DEBUG    | tau2.registry:<module>:297 - Voice dependencies not installed, skipping voice user registration
```
