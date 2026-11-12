from decimal import Decimal
from aftercare.intelligence.models import ModelSpec, _client, _estimated_price


def spec():
    return ModelSpec(alias='economy', provider='deepseek', model='deepseek-flash',
        endpoint='https://api.deepseek.com', api_key_env='DEEPSEEK_TEST_KEY',
        input_usd_per_million='0.30', output_usd_per_million='1.20',
        price_date='2026-11-13', source='https://api-docs.deepseek.com/quick_start/pricing/')


def test_deepseek_tools_do_not_require_reasoning_transcript(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_TEST_KEY', 'fixture')
    client = _client(spec(), [], 10, 200)
    assert client.extra_body == {'thinking': {'type': 'disabled'}}
    assert client.use_responses_api is False


def test_cache_usage_has_conservative_cost_without_fabricated_ttl():
    assert _estimated_price(spec(), {'input_tokens': 1000, 'output_tokens': 100,
        'input_token_details': {'cache_read': 900}}) == Decimal('0.00042')
    assert spec().cache_write_ttl is None
