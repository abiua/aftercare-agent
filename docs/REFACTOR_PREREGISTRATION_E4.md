# E4 消融实验（预注册）

本文件于 2026-11-17 冻结。用户已明确授权按本文件执行一次，费用上限 $8。源码提交哈希记录在冻结清单 `evidence/architecture_refactor/preregistration_e4.json` 的 `git_head` 字段；清单在任何 E4 真实运行之前单独提交。冻结后不改代码、提示词和模型配置。

## 问题与 2×2 设计

在相同的低价执行者和规则检查下，独立审查和专家各自带来多少成功题数和费用变化？两者共同使用时，描述性差值是否不同？

| 组 | 独立审查 | 专家 | 数据来源 |
| --- | --- | --- | --- |
| S1c | 无 | 无 | 复用 E3 |
| M2r | 无 | 有 | E4 新运行 |
| M2x | 有 | 无 | E4 新运行 |
| M2 | 有 | 有 | 复用 E3 |

只新跑 `M2x,M2r`，固定组列表顺序，不重跑 S1c、M2。复用 E3 可以减少调用，并保留已经冻结的结果，避免重新运行后挑选有利结果。读取 `kernel_e3_results.json` 的字节后先核对 E4 预注册清单中的 SHA256，再解析和使用其数据；包括 E3 已有补跑的最终得分、所有尝试费用。E3 的 `complete=false`、未获评分记录和其他不完整信息不会因为复用而变成完整结果。

内核必须为 `aftercare-kernel-v3.1`。启动和补跑都核对当前内核目录全部 `.py` 的 SHA256、适配器 SHA256、数据 SHA256、官方 revision、模型名与价格，必须与只读的 `.runtime/kernel-e3/run-v1/manifest.json` 一致；记录到 E4 清单的对应字段也必须一致。任何差异直接退出。

四组执行者均为 `deepseek-flash / disabled`，启用时专家与独立审查者均为 `gpt-6.1-sol / low`；官方客户为 `gpt-6.1-terra`，官方裁判为 `gpt-5.4-mini-2026-03-17`。官方裁判 judge 与独立审查 reviewer 是不同角色；M2r 仍有官方裁判，不能把裁判调用算成禁用审查失效。模型角色配置由内核哈希固定，不修改 backend 源码。

## 题目、种子和日程

直接调用 `kernel_e3_protocol.population`，沿用 E3 的 train 题目总体、开发题排除项和数据校验，不另写题目筛选逻辑。每组分母始终为 67。两组每题各一次，共 134 次；不接受 `--task-ids`。

题目按 E3 顺序（题号数值升序），第 i 题种子为 `42+i`，i 从 0 起，种子 42–108。偶数题先 M2x 后 M2r，奇数题先 M2r 后 M2x。两组同题种子与 E3 相同。日程哈希覆盖按顺序排列的 `task_id/architecture/seed`，不含公开题目摘要。

并发完成次序不保证与提交日程相同。种子控制官方编排器，不能保证模型供应商输出可复现。

## 固定限额

| 项目 | 值 |
| --- | --- |
| 原始运行及补跑合计费用 | $8 |
| 总调用 | 134 × (160+60+1) = 29,614 |
| 每题每组 Agent 调用 | 160，执行者、专家与独立审查共享 |
| 每题每组模拟客户调用 | 60 |
| 每题每组官方裁判调用 | 1 |
| 每次最大输出 token | 1,500 |
| 并发 | 6 |
| 每次运行超时 | 900 秒 |
| 每次请求超时 | 120 秒 |
| 官方编排最大步数 | 100 |

除两组总费用与总调用数外，单次限额与 E3 相同。`ablate` 对任何不同的固定选项报错。费用采用原有预留再结算机制，未结算预留不当成零；并发预留可能使实际费用未达 $8 时提前停止。架构对象的 `max_agent_calls=48` 沿用 E3，不改变外层 RowTransport 实际采用的 160 次限制。

## 补跑规则

以下规则与 E3 协议逐字相同，仅总预算从 $20 改为 $8：

> 仅 error/unknown 且无官方得分可在同级 .repair 目录补跑一次；沿用原题、组和种子，不补跑已有 0 分、超时或预算耗尽项；合并所有尝试费用与事件，原始加补跑受原 $8 余额约束。

原始记录必须全部结束且齐全，不能对补跑批次再补跑，同级 `.repair` 已存在就退出。原始预算扣除已结算费用和未结算预留后为可用余额；沿用 E3 的单次补跑最高 $3 限制，总调用也不得超过原 29,614 次的剩余额度。账本缺失、不完整或费用上界失效时拒绝补跑。

分析器自动读取同级 `.repair`，复用 E3 使用的 `merge_repair` 核验原始清单、原始行哈希与日程。补跑结果成为该项结果，所有尝试的费用、调用与事件累加，分母仍为 67；不挑选较好的尝试。

## 报告内容

输出 `evidence/architecture_refactor/kernel_e4_results.json`。分析器只读运行目录与输入证据，结果文件仅允许新建，拒绝覆盖已有文件。

- 四格各自的成功数、成功率、Wilson 95% 区间、失败类型计数、总费用/67、逐题费用、每成功一题费用、按角色的费用与调用数。S1c、M2 的数字直接取 E3 汇总，并标记 `source: E3`。
- 四个配对比较：审查效应为 M2 对 M2r、M2x 对 S1c；专家效应为 M2 对 M2x、M2r 对 S1c。每个报告两边独对题数、题号列表、成功题数差、McNemar 精确双侧 p。沿用 E3 的 p 值保留四位小数约定。
- 交互描述值：`(M2 − M2r) − (M2x − S1c)`，单位为成功题数，不据此做假设判定。
- 新两组沿用 E3 `M2_descriptive` 的计数口径：专家咨询、成功执行的专家读取、自写入缓存更新、金额规则触发、审查 revise、审查 fallback、IncompleteResponse。最后一项按每次尝试的 call_id 去重，补跑独立累计。
- M2x 的专家事件或专家调用非零、M2r 的审查事件或 reviewer 调用非零时，列入 `anomalies`。额外检查 allow 审查事件，避免只看 revise 而漏报。缺失轨迹不代表已验证的零事件。
- `descriptive: true`、`complete`、未结算调用数、缺失行、缺失轨迹与未完成题。费用不完整时保留已知费用，完整总费用及其派生均值为 null。零成功时每成功费用为 null。`complete` 要求四格无 unfinished、费用完整，且已知行与轨迹无缺失；异常计数单列，不充当假设判定。

## 描述性定位与局限

**本实验为描述性实验，不做假设判定，不输出“成立/不成立”的结论。** 67 题、每题每组单次运行，功效不足，也无法估计同题重复运行波动。

“67 题下约 8 题以内的差距达不到显著”只能作为粗略功效提醒，不能写成固定统计门槛。精确 McNemar p 取决于两边独对题的分布：例如 6 对 0 的双侧 p 为 0.03125；E3 的 6 对 2 为 0.2891。报告原始配对计数和 p，不将其转成假设判定。

新旧两批运行日期不同。即使代码、数据、模型名与价格一致，也不能保证供应商服务状态和随机性一致；时间变化可能混入组间差异。四个比较均跨 E3/E4 批次。E3 未获成功分数的题保留为未成功，不填补为成功。67 题属于 train 的剩余题，不代表新的独立官方 test 成绩。

## 命令

以下为干运行，打印 134 条日程的清单，不创建目录；真实运行在同一命令后加 `--live`，并去掉 `PYTHONPATH` 中阻止网络和 `.runtime` 写入的 `$S`。沿用 E3 的环境变量写法；`S` 内的 sitecustomize 会阻止网络与 `.runtime` 写入。

离线测试还需要本机 `o200k_base` 词表缓存。准备阶段默认临时缓存缺失，已从现有虚拟环境 `litellm/litellm_core_utils/tokenizers/fb374d419588a4632f3f557e76b4b70aebbca790` 只读复制到 `tempfile.gettempdir()/data-gym-cache/` 的同名文件，复制前核对 SHA256 为 `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`。未下载词表，未改动 `.runtime`；临时缓存被清理后需按同样方式恢复。

```sh
S=/private/tmp/claude-501/-Users-mbw-Documents-aftercare-agent/859f4cca-73c1-453c-ab2c-cf866c21d3f4/scratchpad/pyt
R=/Users/mbw/Documents/aftercare-agent
cd "$R"
PYTHONPATH="$S:$R/scripts:$R/backend/src:$R/.runtime/tau-official-source/src" \
  "$R/.runtime/tau-official-venv/bin/python3" -B scripts/run_tau_official_campaign.py run \
  --scope ablate --architectures M2x,M2r \
  --directory "$R/.runtime/kernel-e4/run-v1" \
  --max-usd 8 --max-calls 29614 --agent-calls 160 --user-calls 60 \
  --max-output-tokens 1500 --concurrency 6 --run-timeout 900 --request-timeout 120

PYTHONPATH="$S:$R/scripts:$R/backend/src:$R/.runtime/tau-official-source/src" \
  "$R/.runtime/tau-official-venv/bin/python3" -B -m pytest scripts/tests -q -p no:cacheprovider

cd "$R/backend"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../scripts uv run --frozen pytest tests/agent_kernel -q
```

清单生成器 `scripts/freeze_kernel_e4.py` 记录设计、限额、规则、日程及其哈希、相关脚本与本草稿的哈希、E3 汇总与原始运行清单哈希、生成时 git HEAD 和状态。默认目标为 `preregistration_e4.json`，只允许新建。生成器写出的状态为草稿；冻结时在源码提交之后生成清单，并只把 `version`、`status`、`status_note`、`git_head_note` 四个字段改为冻结状态，其余内容不动。

运行（及补跑）结束后的离线分析命令：

```sh
cd "$R"
PYTHONPATH="$S:$R/scripts:$R/backend/src:$R/.runtime/tau-official-source/src" \
  "$R/.runtime/tau-official-venv/bin/python3" -B scripts/analyze_kernel_e4.py \
  --directory "$R/.runtime/kernel-e4/run-v1" \
  --e3-results "$R/evidence/architecture_refactor/kernel_e3_results.json" \
  --preregistration "$R/evidence/architecture_refactor/preregistration_e4.json"
```
