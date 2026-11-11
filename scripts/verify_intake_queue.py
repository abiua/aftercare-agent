"""Real test-profile HTTP intake, confirmation and durable queue acceptance.

The missing-material gate must finish before any model call. Credentials stay
in memory, and all business inputs belong to the synthetic test dataset.
"""
import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('test', 'app'), default='test')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    profile = args.profile
    base = 'http://127.0.0.1:' + ('8022' if profile == 'test' else '5186')
    output_path = args.output or ROOT / 'reports' / ('intake-queue-http.json' if profile == 'test' else 'app-intake-queue-http.json')
    if args.output and output_path.exists():
        raise SystemExit('Choose a new output path to preserve previous evidence.')
    users = json.loads((ROOT / f'.runtime/users-{profile}.json').read_text())
    token = next(user['token'] for user in users if user['role'] == 'customer')

    def request(path, body=None, key=None, expected=200):
        headers = {'Authorization': 'Bearer ' + token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if key:
            headers['Idempotency-Key'] = key
        req = urllib.request.Request(base + '/api' + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                status, content = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, content = error.code, error.read()
        assert status == expected, (path, status, content[:300])
        return json.loads(content)

    message = '模拟验收：收到的商品有破损，请帮我处理。'
    capabilities = request('/intake/capabilities')
    assert capabilities['rules'] is True
    preview = request('/intake/preview', {'message': message})
    assert preview['status'] == 'CLARIFICATION_REQUIRED' and 'order' in preview['required']
    assert preview['intent'] == 'damaged' and preview['classifier_source'] == 'rules'
    owned = {row['id'] for row in request('/orders?limit=300')['items']}
    assert {row['id'] for row in preview['order_candidates']} <= owned
    cases = request('/cases?limit=300')['items']
    used = {row['order_id'] for row in cases if row['intent'] == 'damaged'}
    order = next(row for row in preview['order_candidates'] if row['id'] not in used)
    invalid = request('/intake/preview', {'message': message, 'order_id': 'O99999999'})
    assert invalid['order'] is None and 'order' in invalid['required']
    ready = request('/intake/preview', {'message': message, 'order_id': order['id']})
    assert ready['status'] == 'READY'
    confirmation = {'message': message, 'order_id': order['id'], 'intent': 'damaged',
                    'expected_version': ready['order']['version'], 'confirmed': True}
    request('/intake/confirm', {**confirmation, 'confirmed': False}, str(uuid4()), expected=422)
    key = str(uuid4())
    case = request('/intake/confirm', confirmation, key, expected=201)
    assert request('/intake/confirm', confirmation, key, expected=201)['id'] == case['id']
    run_key = str(uuid4())
    run_path = f"/cases/{case['id']}/runs"
    body = {'expected_version': case['version'], 'mode': 'multi', 'strategy': 'routed'}
    run = request(run_path, body, run_key, expected=202)
    assert request(run_path, body, run_key, expected=202)['id'] == run['id']
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        run = request(run_path + '/' + run['id'])
        if run['status'] not in {'QUEUED', 'RUNNING'}:
            break
        time.sleep(.25)
    assert run['status'] == 'WAITING_INPUT', run
    events = request(run_path + '/' + run['id'] + '/events')
    assert not any(event['kind'] == 'attempt' for event in events)
    run_id = str(UUID(run['id']))
    query = ("SELECT row_to_json(j) FROM (SELECT state,claims,failures,publications "
             f"FROM public.dispatch_jobs WHERE kind='agent' AND target_id='{run_id}' AND generation=0) j")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        output = subprocess.check_output(['/opt/homebrew/bin/docker', 'exec',
            f'aftercare-v2-pg-{profile}-1', 'psql', '-U', f'ac_{profile}', '-d', f'ac_{profile}', '-Atc', query], text=True)
        job = json.loads(output)
        if job['state'] == 'DONE':
            break
        time.sleep(.25)
    assert job['state'] == 'DONE' and job['claims'] == 1, job
    report = {'status': 'PASSED', 'source': 'real_http_postgresql_celery',
              'case_id': case['id'], 'run_id': run['id'], 'job': job,
              'checks': ['order_clarification', 'owned_orders_only', 'invalid_order_clarified',
                         'explicit_confirmation', 'duplicate_confirmation_same_case',
                         'duplicate_start_same_run', 'missing_material_gate_zero_model_calls'],
              'model_calls': 0, 'run_status': run['status'], 'profile': profile, 'base_url': base}
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
