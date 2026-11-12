import json
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool
from aftercare.auth import token_hash
from aftercare.db import User
from aftercare.password_login import _attempts, password_hash, router


def test_password_login_checks_password_user_status_and_rotated_token(tmp_path):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    User.__table__.create(engine)
    token = 'a' * 64
    with engine.begin() as db:
        db.execute(User.__table__.insert().values(id='u1', role='customer', customer_id='c1',
            display_name='Customer', enabled=True, token_hash=token_hash(token)))
    path = tmp_path / 'accounts.json'
    path.write_text(json.dumps([{'username':'customer','user_id':'u1','token':token,
                               'password_hash':password_hash('demo-password')}]))
    app = FastAPI()
    app.state.engine = engine
    app.state.settings = SimpleNamespace(login_accounts_path=path)
    app.include_router(router)
    _attempts.clear()
    client = TestClient(app)
    def login(password='demo-password', username='customer'):
        return client.post('/auth/login', json={'username':username,'password':password})
    assert login().json() == {'token':token}
    assert client.post('/auth/demo-role', json={'role':'customer'}).status_code == 404
    app.state.settings.demo_role_login_enabled = True
    assert client.post('/auth/demo-role', json={'role':'customer'}).json() == {'token':token}
    assert client.post('/auth/demo-role', json={'role':'approver'}).status_code == 503
    assert client.post('/auth/demo-role', json={'role':'admin'}).status_code == 422
    assert login('wrong').status_code == 401
    assert login(username='missing').status_code == 401
    with engine.begin() as db:
        db.execute(update(User).values(enabled=False))
    assert login().status_code == 401
    assert client.post('/auth/demo-role', json={'role':'customer'}).status_code == 503
    with engine.begin() as db:
        db.execute(update(User).values(enabled=True, token_hash=token_hash('b' * 64)))
    assert login().status_code == 401
    assert client.post('/auth/demo-role', json={'role':'customer'}).status_code == 503
    assert 'password' not in login().text
    engine.dispose()
