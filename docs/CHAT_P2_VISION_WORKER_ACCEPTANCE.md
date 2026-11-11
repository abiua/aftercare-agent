# P2 隔离工作进程视觉验收协议

先运行只读预检。预检输出方案摘要、冻结原图和实际发送图摘要、模型登记摘要、镜像编号、端口和估算预留。执行使用同一摘要，要求独占的新报告和本轮专用准入记录。准入记录在试验后保留，以阻止重复启动。

```bash
PYTHONPATH=backend/src "$HOME/.cache/aftercare-agent/backend-venv/bin/python" \
  scripts/verify_chat_p2_vision_worker.py

PYTHONPATH=backend/src "$HOME/.cache/aftercare-agent/backend-venv/bin/python" \
  scripts/verify_chat_p2_vision_worker.py --resume-zero-provider --execute-paid \
  --expected-plan-sha256 <预检摘要> \
  --report reports/chat-p2-vision-worker-live-resume-20261111.json
```

- 使用冻结的 P2 后端和前端镜像，在 5192 和 58354 建独立栈。数据库、对象存储、Redis、队列及身份均属于本次运行。API 和调度器保持离线；唯一临时工作进程读取测试专用登记和密钥副本。BGE 只读挂载，依赖离线缓存。
- 只上传冻结的合成裂纹马克杯原图。用授权 HTTP 回读字节验证对象存储，再查实际模型尝试、观察和会话卡片。工作进程加载测试出站门，在默认 SDK 的真实 HTTP 传输前核对官方 Responses 地址、Terra、900 输出上限、禁用响应持久化、重试序号和唯一发送图摘要。一次出站前用 O_EXCL 持久记录，后续请求无法取得准入。门不改提供方响应或会话代码。
- 以登记完整输入上下文加 900 输出估算总预留，必须不超过 0.05 美元。这个估算不是提供方硬账单限额。仅在实际返回中记录用量、估价和原图/发送图摘要。持久观察应引用同一尝试和附件，公开卡片不暴露内部审计。重复投递同一任务后，观察和尝试数应保持一致。
- 执行结束移除带密钥的工作进程和该进程私有密钥副本，恢复离线工作进程。保留隔离栈、审计和原图。若主进程被强制终止，按专用状态文件中的运行 ID 和 Docker 所有权标签，仅移除对应的 `vision-worker`，再删除该状态目录下的 `vision-paid-runtime/model-secret.json`。避免用会删除卷的停止操作处理留证栈。保留准入文件，先检查报告及出站门记录再判断是否送达。

先前的合成图探针已验证提供方传输和受控观察，本协议增加真实会话、对象存储和队列路径。独立人审、业务审批、客户原图用途和普遍识别能力另行验证。

首轮启动在对象存储绑定 58352 时失败，尚未启动 API 或付费工作进程。原报告、状态、日志及准入均保留。恢复脚本检查原失败报告摘要、无发送记录、无付费目录、原 Docker 标签下无残留，并在生成新准入前检查两个端口。它只接受这次已确认零调用的启动故障。[首轮失败报告](../reports/chat-p2-vision-worker-live-20261111.json)
