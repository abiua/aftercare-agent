# 升级后的真实服务回归

2026-11-10，真实HTTP/PostgreSQL/S3/Celery环境。fixture与fault使用受控模型，不作为真实模型质量证据；三个live任务使用已授权模型和原预算。所有合成参考仍待独立人工核验。

| 实验 | 执行数 | 失败 | 调用数 | 估算USD | 门槛 |
| --- | ---: | ---: | ---: | ---: | --- |
| fixture-regression | 19/19 | 0 | 0 | 0.0 | INCOMPLETE |
| fault-regression | 5/5 | 0 | 1 | None | INCOMPLETE |
| c001-single-analysis | 1/1 | 0 | 2 | 0.013884 | INCOMPLETE |
| c001-single-routed | 1/1 | 0 | 4 | 0.0254105 | INCOMPLETE |
| c001-multi-routed | 1/1 | 1 | 5 | 0.02490075 | FAILED |

24个冲突回归已全部结束且无机器失败，包含5个fault脚本；这不能替代逐项审查故障轨迹。C001两种single策略复验无机器失败，multi-routed仍失败，原报告保留。

multi-routed前两轮材料澄清零模型调用；最终轮在第5次调用后因第6次请求费用预留超过cycle上限暂停。已实现精简重复说明的修复并通过受控测试，尚待新的真实复验。没有提高费用上限。

真实HTTP自然语言入口通过订单澄清、本人订单限制、显式确认与重复请求同案；正常任务仅领取一次并在缺材料时暂停，模型调用为0。详见 reports/intake-queue-http.json。

浏览器已实际完成自然语言描述、订单选择、核对与确认建单：错发商品工单ticket 4ba24e9f-6e4f-47e8-a231-e998e2b52e26，订单O0182。自动选择照片被扩展连续拒绝，正在等待用户协助；不能宣称浏览器上传已通过。
