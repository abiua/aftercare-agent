"""Verify NOT_RUN child retry with real test HTTP; preserve the original report."""
import json
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    users = json.loads((ROOT / '.runtime/users-test.json').read_text())
    token = next(user['token'] for user in users if user['role'] == 'tester')

    def request(path, body=None, key=None):
        headers = {'Authorization': 'Bearer ' + token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if key:
            headers['Idempotency-Key'] = key
        req = urllib.request.Request('http://127.0.0.1:8022/api/evaluation' + path,
            headers=headers, data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    parent_id = 'bb799b76-78e7-4d8b-9cc1-90fc72fb58b8'
    path = '/experiments/' + parent_id
    parent = request(path)
    chosen = next(row for row in parent['results'] if row['case_id'] == 'AC02-R020')
    assert chosen['status'] == 'NOT_RUN'
    body = {'expected_version': parent['version'], 'result_ids': [chosen['id']],
            'name': 'NOT_RUN 真实补跑验收'}
    destination = ROOT / 'reports/retry-http-fault-recheck.json'
    if destination.exists():
        evidence = json.loads(destination.read_text())
        assert evidence['parent_id'] == parent_id
        child = request('/experiments/' + evidence['child_id'])
    else:
        key = str(uuid4())
        child = request(path + '/retry', body, key)
        duplicate = request(path + '/retry', body, key)
        assert duplicate['id'] == child['id']
        evidence = {'status': 'RUNNING', 'parent_id': parent_id, 'child_id': child['id'],
                    'original_result_id': chosen['id'], 'same_key_same_child': True,
                    'parent_snapshot': parent}
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    deadline = time.monotonic() + 180
    while child['status'] in {'QUEUED', 'RUNNING'} and time.monotonic() < deadline:
        time.sleep(.5)
        child = request('/experiments/' + child['id'])
    after = request(path)
    assert after == evidence.get('parent_snapshot', parent), 'Retry changed the parent report'
    assert child['status'] == 'COMPLETED', child['status']
    assert child['counts']['finished'] == 1 and child['counts']['failed'] == 0
    assert child['parent_experiment_id'] == parent_id
    result = child['results'][0]
    assert result['retry_of_result_id'] == chosen['id']
    evidence.update(status='PASSED', parent_report_unchanged=True,
                    child_status=child['status'], child_counts=child['counts'],
                    parent_link_verified=True, result_link_verified=True,
                    source='real_http_postgresql_celery_fixture')
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in evidence.items() if k != 'parent_snapshot'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
