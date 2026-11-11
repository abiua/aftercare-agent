# 公开售后表达包

`public-expressions-0.1` 面向下一版评测数据。表达包保存 120 条独立的英文原文和对应 AI 中文改写。英文原文来自 Bitext 的公开混合合成语料，每条可按固定 revision、CSV 记录号和字段回溯。人工复核与金标均为 `PENDING`。公开原文和可见的中文表达已进入本目录，`holdout` 表示按来源族预留的候选分组。正式封存和独立人审待完成。

## 来源与许可

| 来源 | 固定版本 | 使用范围 | 本地许可档案 |
| --- | --- | --- | --- |
| [Bitext retail/eCommerce LLM chatbot training dataset](https://huggingface.co/datasets/bitext/Bitext-retail-ecommerce-llm-chatbot-training-dataset/tree/12dd624ddcd3057382b2faad661bcda1fa869491) | `12dd624ddcd3057382b2faad661bcda1fa869491` | 筛选 `instruction`，保留英文，另作中文改写。原始语料为混合合成数据。 | [CDLA-Sharing-1.0](licenses/CDLA-Sharing-1.0.html) |
| [τ²-Bench Retail](https://github.com/sierra-research/tau2-bench/tree/da0835f6c67baef58bdda18f2a3b037dfb3a1754) | `da0835f6c67baef58bdda18f2a3b037dfb3a1754` | 参考任务与分组结构。114 个任务中 train 74 个、test 40 个，任务 ID 无交集。 | [MIT，Sierra Research](licenses/tau2-MIT.txt) |

表达包包含 Bitext 的原文与改写，随包保留 Bitext 署名与 CDLA-Sharing-1.0 许可。固定来源、归档文件及 SHA-256 见 [manifest.json](manifest.json)。店铺政策、订单事实、标准动作和预期答复由下一版业务场景独立制定。公开语料的 `response` 和 τ² 的对话、答案及政策均未进入表达包。

## 选择与分组

保留能从原句识别目标请求的记录，优先覆盖对象、句式及描述线索。跳过意图不明的通用报备句、明显不合语境的表述和重复记录。中文改写统一英语拼写与口语差异，保留请求、涉及对象及明确给出的线索。每条记录的 `transformation` 标记改写来源与具体修改规则。

| 候选意图 | Bitext 原始标签与条数 | dev · 20 | validation · 10 | holdout · 10 |
| --- | --- | --- | --- | --- |
| 货损 `damaged` | `damaged_delivery` · 992 | 收到的商品受损 | 包裹或快件本身受损 | 明确在运输途中损坏 |
| 错发 `wrong_item` | `wrong_item` · 996 | 陈述收到的商品不符 | 直接提出错货报备 | 颜色、型号或尺寸不符 |
| 退款查询 `refund_status` | `refund_status` · 999 | 查看或检查状态 | 获取状态详情 | 追踪或跟进退款 |
| 合计 | 2,987 | 60 | 30 | 30 |

来源未提供模板父 ID。本包用原句描述的对象、动作和句式定义九个候选来源族。每族整组入一个分组，词形、拼写、语气和中文派生不会改变分组。同一来源记录只出现一次。英文及中文分别检查归一化精确重复与跨组字符 n-gram 相似度。[verification.json](verification.json) 保留计算口径、最高分记录及告警。对语义近义、模板变体和选句偏差仍需独立人审。退款的三类说法尤其接近，正式锁定前应复查候选族边界。

## 文件与复核

- [expressions.jsonl](expressions.jsonl) 用于构建下一版场景。每条含候选意图、分组、来源与中英文本。CSV 记录号从第一条数据行计 1，按 CSV 解析器计数，排除表头。
- [curation.tsv](curation.tsv) 保存可复现的选句和改写输入。组与分组映射由 [manifest.json](manifest.json) 固定。
- [human-review.csv](human-review.csv) 供人工复核，按记录检查意图、含义、自然中文及分组。复核员填写姓名或审阅编号、各项判断与依据。对含义漂移的改写记录修订，对跨族近义记录合并或整族移组，重跑重复检查后再决定分组。业务金标依据下一版订单事实、有效政策及脚本另行标注。

从项目根目录验证本地下载的归档和生成件，或去掉 `--check` 重新生成。脚本使用 Python 标准库。

```bash
python3 scripts/build_public_corpus.py --source-dir '/Users/mbw/Documents/简历/data' --check
```
