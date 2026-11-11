# P4 文字与只读工具的有界验收

本协议使用独立的 127.0.0.1:5195。前置是该栈的 P4 离线验收报告通过。同一工单已经从会话上传两张合成照片、建立原图关联并由客服单独确认材料。离线候选须是待人工处理，且工单没有执行记录、模型费用或进行中的运行。

## 预检

从仓库根目录运行。报告路径使用实际成功的离线验收报告。

    export PYTHONPATH=backend/src
    PYTHON="$HOME/.cache/aftercare-agent/backend-venv/bin/python"
    "$PYTHON" scripts/verify_chat_p4_live.py --offline-report reports/chat-p4-offline-20261111.json

预检只读取隔离栈及其 PostgreSQL。它校对两张原图关联摘要、旧候选运行输入、新运行允许使用的事项与案例、API 和 worker 镜像标识、项目已登记的 Terra 规格和估算费用。输出包含本次计划摘要。预检不读取项目模型密钥、不创建运行，也不发模型请求。

## 单次执行

验收操作者确认预检输出的范围及费用后传入精确计划摘要和新报告路径。

    "$PYTHON" scripts/verify_chat_p4_live.py --offline-report reports/chat-p4-offline-20261111.json --execute --expected-plan-sha256 '<预检输出的摘要>' --report reports/chat-p4-text-live-20261111.json

脚本写入一次性的本地执行记录，再临时替换该隔离栈的 worker。API 保持离线，视觉保持关闭。私有 worker 使用官方 Responses、唯一 analysis 别名下的 gpt-6.1-terra、默认同步 HTTP 传输和现有模型审计。模型请求只收文字与结构化资料。附件以受控引用提供。它只对已冻结的合成事项提交一次客服调查，请求键先写报告，超时保留结果未知。

出站门在实际 HTTP 传输前核对主机、路径、模型、输入摘要及可见证据范围、900 输出上限、禁存储、禁重试、允许的只读工具和无图片内容。最多放行 8 次 POST。每次先落独立序号和请求摘要，回包原样交给 SDK。8 次登记整上下文估算加 900 输出必须不超过 0.40 美元，应用内原事项费用限制继续生效。估算依赖登记的模型规格与价格。

## 读报告与清理

报告并列保存实际请求次数及 HTTP 响应、持久化模型用量、工具使用、客服候选，以及 worker 和密钥清理状态。成功要求真实 HTTP 200、成功模型审计和可读费用、至少一次受控工具读取及同一事项候选。候选可以仍需人工处理。失败或未知保留原报告和一次性执行记录，脚本没有自动重跑入口。结束时移除临时 worker 和密钥副本，恢复原离线 worker。原图、工单和审计保留。

此验收覆盖合成事项在人工材料确认后的真实文字工具链。视觉质量、多架构质量差异和生产支付均有独立的验收范围。

如果主验收进程被强制终止，先用 P4 离线状态文件中的 run ID 和资源标签确认归属，再停止并移除本次登记的 p4-paid-worker、移除其私有运行目录中的 model-secret.json，并恢复本栈原 worker。保留 admission 和 p4-text-wire，供核对已放行次数及未知响应。其他栈的 worker、密钥和数据库不在清理范围内。

## compact-v2 复验

原 5195 报告和执行记录保留。复验通过固定的 compact-v2 协议使用 5196 和对象端口 58358，运行已冻结的 930a162 镜像。离线路径与真实模型路径共用该协议选择。

    export PYTHONPATH=backend/src
    PYTHON="$HOME/.cache/aftercare-agent/backend-venv/bin/python"
    "$PYTHON" scripts/verify_chat_p4_path.py --protocol compact-v2 --backend-image-id sha256:14a93ee24066e3d3d0ffac7aad42fd102580c222aab11833a5c3d870c0dfd4cf --frontend-image-id sha256:931caa17b56da6222d5fa020f41bc8401e32e0ec21e5416326a12dfef2ad3e20

验收操作者审阅预检后，给同一命令追加 --execute、--expected-plan-sha256 及 --report reports/chat-p4-compact-offline-20261111.json。该离线报告成功后再运行：

    "$PYTHON" scripts/verify_chat_p4_live.py --protocol compact-v2 --offline-report reports/chat-p4-compact-offline-20261111.json

真实复验的执行入口使用上述命令和 --execute、--expected-plan-sha256 及 --report reports/chat-p4-compact-text-live-20261111.json。它沿用最多 8 次 POST、每次最多 900 输出、整上下文估算总额不超过 0.40 美元和 0.20 美元的事项估算上限。仅验证同一合成事项的文字和工具链。出站门要求 compact-v2 格式及精确事项范围。

复验报告分别记录兼容性和业务候选。业务通过需要真实后续轮次、可追溯的工具证据、处于已核实范围的金额或数量，以及客服候选对应的程序校验通过。转人工、提示超限、带未解决项的候选和待执行的支付均不计入业务通过。

## catalogue-v3 完整目录复验

独立5198/58360使用84121cb冻结镜像。`--protocol catalogue-v3` 先执行离线路径，实际报告为 `reports/chat-p4-catalogue-offline-20261111.json`，再由 `verify_chat_p4_live.py` 预检同报告并以精确计划摘要单次执行。旧5195/5196报告与一次性记录不变。

此协议将已登记Terra规格派生为本轮私有analysis规格：仅文字会话，输出1800，源规格摘要与派生摘要分别绑定，不改共享900注册表。出站同时校验compact-v3输入、精确事项和完整原始政策目录摘要。最多8次POST，登记整上下文加输出的最大估算0.392美元，应用原事项0.20美元上限继续生效；没有自动重试新一轮。报告保留兼容与业务候选两个结论。业务通过还需真实多步工具调用、带版本条款引用、完整对象金额或数量、程序校验通过及无未解决项。真实结果以新报告为准，离线通过不代表模型方案通过。

## references-v4 引用修复复验与离线恢复

独立5199/58361冻结5693f89，`--protocol references-v4`要求compact-v4提示、完整目录和与catalogue-v3一致的费用/输出上限，使用专属state/admission/report。首次离线报告 `reports/chat-p4-references-offline-20261111.json` 在自动调查GET等待时记录HTTP_TIMEOUT_OUTCOME_UNKNOWN。事后实际数据库与接口确认原消息、两原图和PROGRAM候选已经落库，模型/材料关联/审核/执行均为零，不能从现有证据确定超时原因。

已审查的 `--resume-source reports/chat-p4-references-offline-20261111.json` 只允许该协议/失败阶段。它绑定原报告摘要与同镜像、同会话、消息请求摘要、原图字节和零后续操作；先只读预检，再以精确计划摘要和专属恢复路径执行。它不重建栈、不重发聊天或照片，从尚未发生的材料关联继续。`reports/chat-p4-references-offline-recovered-20261111.json` 实际通过，原FAILED不改。真实模型入口接受此专属恢复报告并重新核对来源摘要链。
