# τ²-bench 零售数据与本地会话

P1 使用 Sierra Research 的 τ²-bench 零售域，固定到 `da0835f6c67baef58bdda18f2a3b037dfb3a1754`。版本和哈希清单在 [data/tau/manifest.json](../data/tau/manifest.json)，归档的 [MIT 许可证](../data/corpus/licenses/tau2-MIT.txt) 与上游许可证一致。导入校验清单所列的 7 个源文件的字节数和 SHA-256。数据包含 50 个商品、500 个用户、1,000 个订单、114 个任务，沿用上游 74 个 train 和 40 个 test 的划分。该版本的 114 个任务均未声明独立的初始状态，初始化时共享同一只读数据基线，每个会话有各自的写入副本。

## 下载和离线导入

在仓库根目录运行以下命令。下载阶段需要连接公开的上游仓库，后续命令可以离线执行。已有固定版本的源文件时，直接用 `import --source <源目录>`。

```bash
python3 scripts/tau_retail.py fetch
python3 scripts/tau_retail.py import --source .runtime/tau-retail/source
```

导入将校验后的资产一起发布到 `.runtime/tau-retail/bundle`。同一份源文件可以重复导入，已有会话会保留当前状态。运行前会按清单复核基线、政策和三个派生文件，发现额外文件或哈希变化时拒绝执行。若需要另一份隔离目录，在子命令之前传入 `--workspace <目录>`。运行目录不纳入 Git，脚本只使用本地文件和 Python 自带的 SQLite。

## 初始化、修改与重置

用上游 train 任务 `0` 验证。任务对应的订单在上游数据中的初始状态为 `delivered`。

```bash
python3 scripts/tau_retail.py init p1-smoke 0
python3 scripts/tau_retail.py probe-exchange p1-smoke '#W2378156'
python3 scripts/tau_retail.py reset p1-smoke
```

`probe-exchange` 是运行器的状态写入探针，只把指定已送达订单标记为 `exchange requested`。它不执行换货、结算或正式评测。探针后的状态哈希会改变，重置返回与初始化完全相同的哈希。一个会话名绑定一个任务 ID。对同一会话和任务重复执行 `init` 会保留当前状态，需要恢复基线时使用 `reset`。

## 数据访问

| 使用方 | 资产或接口 |
| --- | --- |
| Agent | `RetailAgentTools` 提供政策文本、商品点查，以及当前顾客本人的资料和订单点查。可信调用方绑定会话和顾客身份，模型不应有权构造工具对象或更换身份。 |
| 运行器 | `runtime/baseline.json` 是原始商品、用户和订单基线。`runtime/task_index.json` 使用上游任务 ID 映射 train/test。 |
| 模拟客户 | `simulator/scenarios.json` 按任务 ID 保存上游 `user_scenario`，供后续模拟客户使用。 |
| 评测器 | `evaluation/references.json` 按任务 ID 保存上游 `evaluation_criteria`，包含动作参数和判定依据。 |

会话数据库只保存业务记录和运行器内部的任务 ID。任务描述和已知问题留在原始 `tasks.json` 中。模拟客户指令和评测参考位于各自的文件中，不进入会话数据库或 Agent 工具输出。P1 的隔离通过调用接口和文件用途实现，运行器仍需避免把本地文件访问能力交给模型。可用 `agent-view` 查看工具的只读输出，例如 `python3 scripts/tau_retail.py agent-view p1-smoke <可信顾客ID> --order '#W2378156'`。CLI 的顾客 ID 由本地可信调用者提供，当前接口没有实现上游身份核验流程。现有对话服务尚未注册这些工具。
