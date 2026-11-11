"""Local operator commands. Credentials are written privately, never logged."""
import argparse
import json
import os
import secrets
from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from aftercare.auth import token_hash
from aftercare.business.service import initialize
from aftercare.business.storage import S3ObjectStore
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine


def bootstrap(profile):
    settings = load_settings(profile)
    engine = guarded_engine(settings)
    try:
        data = json.loads((ROOT / 'data/v0.1/agent/business.json').read_text())
        initialize(engine, data)
        S3ObjectStore(settings).ensure_bucket()
        conninfo = make_url(settings.database_url).set(drivername='postgresql')
        with PostgresSaver.from_conn_string(conninfo.render_as_string(hide_password=False)) as saver:
            saver.setup()
        path = ROOT / '.runtime' / f'users-{profile}.json'
        if path.exists():
            credentials = json.loads(path.read_text())
        else:
            credentials = [
                {'user_id': role, 'role': role, 'customer_id': customer,
                 'display_name': name, 'token': secrets.token_urlsafe(32)}
                for role, customer, name in [
                    ('customer', 'C001', '模拟客户001'),
                    ('support', None, '售后客服'),
                    ('approver', None, '财务审批'),
                    ('tester', None, '评测人员'),
                ]
            ]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(credentials, stream, ensure_ascii=False, indent=2)
        with Session(engine) as session, session.begin():
            for row in credentials:
                if session.get(User, row['user_id']) is None:
                    session.add(User(id=row['user_id'], role=row['role'],
                        customer_id=row['customer_id'], display_name=row['display_name'],
                        token_hash=token_hash(row['token']), enabled=True))
        print(json.dumps({'profile': profile, 'seed': 'ready', 'private_objects': 'ready',
                          'checkpoints': 'ready', 'credentials_file': str(path)}))
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['bootstrap'])
    parser.add_argument('--profile', choices=['app', 'test'], default='app')
    args = parser.parse_args()
    bootstrap(args.profile)


if __name__ == '__main__':
    main()
