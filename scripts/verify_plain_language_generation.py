"""One isolated style smoke call; no business state, rewrites, retries or old P5 budget."""
import json
import os
from pathlib import Path

from aftercare.intelligence.language import LANGUAGE_VERSION, PLAIN_LANGUAGE
from aftercare.intelligence.models import (
    Registry,
    _content,
    _estimated_price,
    _reserve_price,
    _transport,
)
from langchain_core.messages import HumanMessage, SystemMessage


def main():
    out = Path('reports/plain-language-generation-20261112.json')
    registry = Registry.read(Path('.runtime/models.json'))
    spec = next(row for row in registry.models if row.alias == 'analysis')
    os.environ[spec.api_key_env] = json.loads(Path('.runtime/model-secret.json').read_text())['api_key']
    cases = [
        {'id': 'refund_lookup', 'question': '我的退款有处理记录吗？', 'records': '已查询该订单退款执行记录，结果为空。'},
        {'id': 'refund_advice', 'question': '客服应该怎么处理？', 'records': '第一件白色杯实付4900分，已退1900分；人工检查发现1件损坏。第二件蓝杯尚未核对照片。暂无财务审批。规则允许退款，但两件须分开处置。'},
        {'id': 'uncertain_photo', 'question': '照片能看出来是什么问题吗？', 'records': '模型观察为疑似裂纹，未经过人工检查，损坏原因未知。'},
        {'id': 'unread', 'question': '图片收到了吗，还要我再发吗？', 'records': '已收到2张照片，尚未查看内容。'},
    ]
    system = PLAIN_LANGUAGE + '请逐项回答输入案例，输出JSON对象，键为案例id，值为直接回复或客服建议。不得添加输入中没有的事实。'
    prompt = json.dumps(cases, ensure_ascii=False)
    reserve = _reserve_price(spec, len((system + prompt).encode()) + 128, 1000)
    if reserve is None or reserve > 0.10:
        raise RuntimeError('Smoke cost bound exceeds USD 0.10')
    report = {'scope': 'shared writing instruction smoke, not a complete investigation',
              'language_version': LANGUAGE_VERSION, 'cases': cases, 'system': system,
              'status': 'DISPATCH_RESERVED', 'attempt_limit': 1, 'reserved_usd': str(reserve)}
    with out.open('x') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    try:
        result = _transport(spec, [SystemMessage(content=system), HumanMessage(content=prompt)], [], 60, 1000)
        report.update(status='RECEIVED', raw_output=_content(result), usage=result.usage_metadata,
                      estimated_usd=str(_estimated_price(spec, result.usage_metadata or {})))
    except Exception as exc:  # noqa: BLE001 - record failure type without leaking transport secrets
        report.update(status='UNKNOWN_OR_FAILED', error_type=type(exc).__name__)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: report[k] for k in ('status', 'reserved_usd')}))


if __name__ == '__main__':
    main()
