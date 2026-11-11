"""Needs the isolated test worker + Redis; no model APIs are called."""
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aftercare.api import create_app
from aftercare.config import ROOT
from aftercare.queue import make_queue

pytestmark = pytest.mark.pg


def test_committed_run_recovers_lost_publish_and_ignores_duplicate_delivery():
    users = json.loads((ROOT / '.runtime/users-test.json').read_text())
    headers = {'Authorization': 'Bearer ' + next(u['token'] for u in users if u['role'] == 'support')}
    # Drop the initial publication deliberately. The DB row is the durable outbox.
    with TestClient(create_app('test', dispatch=lambda _: None)) as client:
        used = {r['order_id'] for r in client.get('/api/cases?limit=300', headers=headers).json()['items']}
        orders = client.get('/api/orders?limit=300', headers=headers).json()['items']
        order = next(r for r in orders if r['id'] not in used and r['status'] == 'DELIVERED')
        case = client.post('/api/cases', headers={**headers, 'Idempotency-Key': str(uuid4())},
            json={'expected_version': 1, 'order_id': order['id'], 'intent': 'damaged',
                  'description': '模拟队列故障验收：收到的商品破损，请处理。'})
        assert case.status_code == 201, case.text
        case_id = case.json()['id']
        base = f'/api/cases/{case_id}/runs'
        key = str(uuid4())
        body = {'expected_version': case.json()['version'], 'mode': 'multi', 'strategy': 'routed'}
        first = client.post(base, headers={**headers, 'Idempotency-Key': key}, json=body)
        assert first.status_code == 202, first.text
        run_id = first.json()['id']
        repeated = client.post(base, headers={**headers, 'Idempotency-Key': key}, json=body)
        assert repeated.json()['id'] == run_id
        queue = make_queue('test')
        queue.send_task('aftercare.recover')
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            run = client.get(f'{base}/{run_id}', headers=headers).json()
            if run['status'] == 'WAITING_INPUT':
                break
            time.sleep(.25)
        assert run['status'] == 'WAITING_INPUT', run
        before = client.get(f'{base}/{run_id}/events', headers=headers).json()
        delivery = queue.send_task('aftercare.process', args=[run_id], ignore_result=False)
        delivery.get(timeout=15)  # actual task completion, not a timer or control ping
        delivery.forget()
        after = client.get(f'{base}/{run_id}/events', headers=headers).json()
        assert before == after
        assert not any(e['kind'] == 'attempt' for e in after)
