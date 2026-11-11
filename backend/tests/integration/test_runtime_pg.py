"""Real isolated services; run serially under the integration owner."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from aftercare.api import create_app
from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine

pytestmark = pytest.mark.pg


def test_auth_ownership_and_checkpoint_schema():
    users = json.loads((ROOT / '.runtime/users-test.json').read_text())
    credentials = {row['role']: {'Authorization': 'Bearer ' + row['token']} for row in users}
    data = json.loads((ROOT / 'data/v0.1/agent/business.json').read_text())
    other = next(row['id'] for row in data['orders'] if row['customer_id'] != 'C001')
    with TestClient(create_app('test')) as client:
        assert client.get('/api/me').status_code == 401
        for role, headers in credentials.items():
            result = client.get('/api/me', headers=headers)
            assert result.status_code == 200
            assert result.json()['role'] == role
        headers = credentials['customer']
        assert client.get(f'/api/orders/{other}', headers=headers).status_code in {403, 404}
        result = client.get('/api/orders', headers=headers)
        assert result.status_code == 200
        assert result.json()['items']
        assert all(row['customer_id'] == 'C001' for row in result.json()['items'])
        runtime = client.get('/api/runtime', headers=headers).json()
        assert runtime['mode'] == ('live' if load_settings('test').model_enabled else 'offline')
        tables = inspect(client.app.state.engine).get_table_names()
        assert {'checkpoints', 'checkpoint_writes', 'checkpoint_blobs', 'checkpoint_migrations'} <= set(tables)


def test_target_guard_rejects_other_project():
    settings = load_settings('test')
    with pytest.raises(ValueError, match='DATABASE_TARGET_REJECTED'):
        guarded_engine(settings.model_copy(update={
            'database_url': 'postgresql+psycopg://x:x@127.0.0.1:55441/digital_sentinel'
        }), test_only=True)
