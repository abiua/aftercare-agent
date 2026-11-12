# τ P5 独立复杂售后演示环境

[浏览器入口](http://127.0.0.1:5225/)使用 P5 API 18225。Compose 项目 `tau-chat-p5` 在已运行的 `colima-tau-rag` 中使用独立的 PostgreSQL 15440、Redis 16390、对象存储 18340 和具名卷。程序核验 PostgreSQL 的集群身份 `tau-chat-p5`。业务政策索引使用共享 Milvus 19538 中的新集合。P3 公共参考目录以原路径只读打开。

## 启动与恢复

以下命令从仓库根目录运行。Python 读取 P4 已安装的锁定依赖和检索扩展。运行环境和业务数据写入 `.runtime/tau-p5-demo`。

```sh
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-p5.yml up -d --wait
p5_python="$HOME/.cache/aftercare-agent/tau-p4-venv/bin/python"
export PYTHONPATH="$PWD/backend/src:$PWD:$HOME/.cache/aftercare-agent/tau-p4-rag-deps"
"$p5_python" scripts/tau_p5_runtime.py init
"$p5_python" scripts/tau_p5_runtime.py index
"$p5_python" scripts/tau_p5_runtime.py api
```

首次启动运行 `init` 和 `index`。保留卷与目录的重启只需启动容器和 API。`index` 会拒绝覆盖已有的政策快照。浏览器入口提供客户、客服、独立审批人和测试员四个角色按钮。

在另一个终端运行前端。

```sh
cd frontend
node --input-type=module -e '
  import { createServer } from "vite";
  const server = await createServer({server: {
    host: "127.0.0.1", port: 5225, strictPort: true,
    proxy: {"/api": {target: "http://127.0.0.1:18225"}}
  }});
  await server.listen();
  server.printUrls();
'
```

## 验收与模型调用

常驻 API 以离线模式启动。验收脚本通过 HTTP 建立两商品上下文、上传和回读合成原图、关联材料并记录客服审核，随后只处理该会话的无付费分析。现场验收的两条固定会话可在重启后回看。新会话自动分析和调查调度本环境未开启。进入客服角色后可打开对应工单，客户角色可查看原对话。

| 订单 | 客服工单 | 客户对话 |
| --- | --- | --- |
| O9951 | [工单](http://127.0.0.1:5225/?case=4a239e05-29a5-470c-a919-4c61917580ac) | [对话](http://127.0.0.1:5225/?conversation=669a3337-643c-4f2c-8676-f9cbfef6d9b1) |
| O9952 | [工单](http://127.0.0.1:5225/?case=7655fc46-707f-4d46-84ad-443f27337ff5) | [对话](http://127.0.0.1:5225/?conversation=34fb634b-7db3-403d-ae88-f45a91f087c9) |

`scripts/verify_tau_p5_live.py` 的 `prepare` 阶段使用新报告路径和 `--order O9951` 或 `--order O9952`，`audit` 阶段仅回读账本和事件。真实模型在已审查的受控 `live` 阶段运行一次，直接处理报告锁定的 turn；会在私有目录先提交一次性调度记录。单次最多 12 次模型调用、0.70 美元估算、120 秒运行期限。单请求超时为 25 秒，获审查的独立第二例使用配置允许的上限 30 秒。本轮跨报告累计上界为 24 次、1.40 美元估算，包含未对账尝试的预算预留。收到未知状态后保留其预算预留和原始记录供审查，禁止自动重放。修正已有事项时，`prepare-revision` 追加客户补充消息并生成新分析版本，原调查报告、材料与决策快照保留。费用来自应用账本，供应商账单未校验。

停止演示时先停止对应终端的 API 和前端，再执行以下命令保留所有卷。

```sh
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-p5.yml stop
```

## 当前回看状态

O9951 在政策角色超时后进入人工处理。O9952 最新第 3 版读取了两份已上传材料及审核记录，事实角色区分了两行的材料状态，反证角色调用超时后进入人工处理。可在 O9952 客服工单的候选历史查看前三版。四次有界运行的调用区间、错误、未对账预留和重启读回见 [运行审计](../reports/tau-p5-runtime-summary-20261112.json)。完整业务验收状态见 [P5 验收](../docs/TAU_P5_ACCEPTANCE.md)。

## 超时专项复验

专项复验使用原分析模型，SDK 单次请求最多 90 秒，每次调查 240 秒，单次调查仍最多 12 次 transport 和 0.70 美元估算。专项开始时全环境上限为新增 40 次 transport，因真实补查成功后最终核验仍有输出校验错误，调整为新增 60 次。累计账面上界仍为 3.00 美元，包含上一轮已知费用和未对账预留。新调度记入 `.runtime/tau-p5-demo/paid-dispatches-timeout-continuation-20261112.jsonl`。每个报告记录传给 SDK 的实际超时参数和测得的请求时长。最后一轮让核验器在结构化结果中直接提出补查，实际读取仍由有权限的角色执行并记录回执。

原 O9951 的新分析轮次被同事项未知费用规则拦截，未调用模型。随后使用 O9961 和 O9962 分别复验历史退款和材料归属。订单是相应原始合成订单的副本，保留原始字段和素材，换用独立订单 ID，并调整客户消息中的订单号。新旧订单经过 HTTP 逐字段核对，上传材料经 S3 回读核对哈希。历史工单及原尝试账本可继续查阅。材料审核记录已写入，退款未获批准或执行。

创建独立案例使用 `prepare --continuation --order O9951` 或 `O9952`。真实调度前用 `tau_p5_runtime.py configure-live-registry --timeout-seconds 90` 生成专用配置，随后以 `live --timeout-seconds 90` 处理新报告指定的轮次。

| 复验订单 | 客服工单 | 客户对话 |
| --- | --- | --- |
| O9961，历史退款 | [工单](http://127.0.0.1:5225/?case=e019964c-5402-4e1b-b68f-49188686af5b) | [对话](http://127.0.0.1:5225/?conversation=863d95bc-c90a-4d1e-a589-c607184e4f3d) |
| O9962，材料归属 | [工单](http://127.0.0.1:5225/?case=dab456a8-8fdd-44f6-bf2d-86923d1eff93) | [对话](http://127.0.0.1:5225/?conversation=a2ccac2b-f099-40af-aae3-dacd469841ec) |

O9961 和 O9962 的最终候选已发布到客服工单。两例均只对 L02 提出材料核对建议。L01 的退款候选分别为 30 元和 49 元，均被部分处置规则拦截，留待人工审核。已观察到核验请求超过原先 30 秒限制仍成功返回。见 [专项运行审计](../reports/tau-p5-runtime-timeout-continuation-summary-20261112.json)。

另以 `verify_tau_p5_controlled_live.py` 验证一处明确注入的上游金额错误。复用原 O9951 的冻结订单和取证回执，受控输入只将 L01 可退余额从 30 元改为 49 元。实际模型识别矛盾，读取范围从原两行缩小为 L01，撤销错误主张并确认正确金额。评测使用私有 SQLite 模型账本，业务 PostgreSQL 只读，不发布候选。专项合计 60 次新增模型调用，累计 81 次，保守估算 1.9135022 美元，含历史未对账预留。见 [受控验证](../reports/tau-p5-controlled-live-o9951-20261112.json)。
