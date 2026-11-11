"""Bounded real Responses compatibility probe; never prints credentials."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from openai import OpenAI, APIError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/Users/mbw/Documents/digital-sentinel/runtime/model.json')
MODELS = ['gpt-6.1-terra', 'gpt-6.1-sol']


def main():
    config = json.loads(SOURCE.read_text())
    if urlparse(config['base_url']).hostname != 'api.openai.com':
        raise RuntimeError('Only the configured official API endpoint is accepted')
    client = OpenAI(api_key=config['api_key'], base_url='https://api.openai.com/v1',
                    timeout=30, max_retries=0)
    report = {'at': datetime.now(timezone.utc).isoformat(), 'source': 'live',
              'maximum_requests': 4, 'output_token_cap_per_request': 256, 'models': []}
    for model in MODELS:
        row = {'model': model, 'status': 'FAILED', 'attempts': []}
        report['models'].append(row)
        try:
            started = time.monotonic()
            response = client.responses.create(model=model, max_output_tokens=256,
                input='For this simulated test, call lookup_order for order TEST-001.',
                tools=[{'type': 'function', 'name': 'lookup_order',
                        'description': 'Read a simulated order.', 'strict': True,
                        'parameters': {'type': 'object', 'properties': {'order_id': {'type': 'string'}},
                                       'required': ['order_id'], 'additionalProperties': False}}],
                tool_choice={'type': 'function', 'name': 'lookup_order'})
            row['attempts'].append({'response_id': response.id, 'latency_ms': round((time.monotonic()-started)*1000),
                                    'usage': response.usage.model_dump() if response.usage else None})
            calls = [item for item in response.output if item.type == 'function_call']
            assert len(calls) == 1 and calls[0].name == 'lookup_order', 'TOOL_CALL_MISSING'
            assert json.loads(calls[0].arguments) == {'order_id': 'TEST-001'}, 'TOOL_ARGUMENT_MISMATCH'
            started = time.monotonic()
            # Replay the actual function call and result; no hidden provider conversation dependency.
            response2 = client.responses.create(model=model, max_output_tokens=256,
                input=[{'role':'user','content':'Read the tool result and return its order_id and paid_cents exactly.'},
                       *[item.model_dump(exclude_none=True) for item in response.output],
                       {'type':'function_call_output','call_id':calls[0].call_id,
                        'output':json.dumps({'order_id':'TEST-001','paid_cents':1234})}],
                text={'format':{'type':'json_schema','name':'order_check','strict':True,
                    'schema':{'type':'object','properties':{'order_id':{'type':'string'},'paid_cents':{'type':'integer'}},
                              'required':['order_id','paid_cents'],'additionalProperties':False}}})
            row['attempts'].append({'response_id': response2.id, 'latency_ms':round((time.monotonic()-started)*1000),
                                   'usage':response2.usage.model_dump() if response2.usage else None})
            actual=json.loads(response2.output_text)
            assert actual == {'order_id':'TEST-001','paid_cents':1234}, 'STRUCTURED_RESULT_MISMATCH'
            row['status']='PASSED'
            row['checks']=['forced_function_call','exact_tool_arguments','tool_result_roundtrip','strict_json_schema']
        except APIError as error:
            row['error']={'type':type(error).__name__,'status_code':getattr(error,'status_code',None),
                          'code':getattr(error,'code',None)}
        except (AssertionError, ValueError) as error:
            row['error']={'type':type(error).__name__,'code':str(error)[:100]}
        (ROOT/'reports/model-compatibility.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'model':model,'status':row['status'],'completed_requests':len(row['attempts']),
                          'error':row.get('error')}),flush=True)

if __name__=='__main__':
    main()
