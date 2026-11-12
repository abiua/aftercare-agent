# 阶段2：共享执行入口与历史回放

2026-11-13；全部为离线验证，没有新增付费调用。

## 实现

- `backend/src/aftercare/agent_kernel/engine.py` 接收模型端口、工具定义和政策，只返回下一步提议。内核不导入官方库或业务服务；硬护栏失败最多重新生成一次，第二次仍失败不返回可执行动作。审查/专家还未接通，不把配置存在当作能力完成。
- `scripts/tau_official_agents.py` 转换官方消息/工具与内核契约。新的S0回放走共享内核；旧strong_single/staged_single/multi_agent仍分派到逐字保存的 `tau_official_agents_legacy.py`，不改变历史协议。
- `scripts/tau_official_budget.py` 接收显式aftercare_role，先归入Agent/模拟客户/官方裁判预算桶，再记录具体actor。历史调用按调用点名称兼容，不从模型名推断角色。执行者、审查、自检、专家共享每题Agent上限。
- DeepSeek独立Chat传输与OpenAI Responses并存，离线测试验证同一模型可扮演不同角色、DeepSeek执行者不记到模拟客户、未声明角色在预约之前拒绝。真实批次的DeepSeek客户端装配与五组排程仍留待后续接线，不悄悄改变旧批次配置。

## 回放证据

`replay_kernel_baseline.py` 将首轮强单40题的公开模型输出作为替身，经新官方适配器与内核回放454个执行步骤。每步核对完整可见对话、回复文本和所有工具调用的ID/名称/参数，全部一致；任务79使用独立技术补跑的有效轨迹。结果：`evidence/architecture_refactor/s0_replay.json`。

这验证转换没有改变已有序列，不证明新模型成功率，也没有让评分标签进入模型输入。原始文件哈希再次验证未变。

## JSON包装的来源

固定官方版本 `src/tau2/agent/llm_agent.py` 的AGENT_INSTRUCTION明确要求“Always make sure you generate valid JSON only.”；旧RetailAgent继承该提示。首轮原始响应raw_data的content已经是`{"message": ...}`，不是传输层后加的。`llm_utils._generate_once`把response content直接写入AssistantMessage，模拟客户接收此content，并无统一拆包。

新内核的执行者提示要求普通文本，不再继承JSON-only要求；单步单工具，不允许把批量工具调用作为通用建议。S0回放和历史模块保留原提示。没有把历史回复原地替换成“更好看的”文字。

## 验证与边界

- 原官方适配器、预算、固定分母、评分接线和补跑测试：10项通过。
- 新角色/跨供应商传输测试：3项通过（无网络替身）。
- 内核及契约：6项通过。
- 还不能宣称新S1～M2已端到端可运行：阶段3护栏、阶段4审查、阶段5专家以及产品提交端口尚待接线。缺少对应能力时显式报错，不能悄悄以无护栏单Agent代替。
