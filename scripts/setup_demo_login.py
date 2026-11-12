"""Configure password login without changing existing business users or records."""
import argparse
import getpass
import json
import os
from pathlib import Path

from aftercare.password_login import password_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--users', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    password = getpass.getpass('演示账号共用密码（至少10位）: ')
    if not 10 <= len(password) <= 128:
        raise SystemExit('密码长度须为10至128位。')
    aliases = {'customer': 'customer', 'support': 'support', 'approver': 'finance', 'tester': 'tester'}
    rows, seen = [], set()
    for user in json.loads(args.users.read_text()):
        if user['role'] in seen:
            continue
        seen.add(user['role'])
        rows.append({'username': aliases[user['role']], 'user_id': user['user_id'],
                     'token': user['token'], 'password_hash': password_hash(password)})
    if seen != set(aliases):
        raise SystemExit('缺少演示角色，未写入。')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        json.dump(rows, out, indent=2)
    print('已配置账号: ' + ', '.join(row['username'] for row in rows))


if __name__ == '__main__':
    main()
