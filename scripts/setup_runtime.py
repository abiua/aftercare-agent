"""Create this project's private configuration once, without exposing any credentials."""
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def private_write(path, text):
    with path.open('x') as handle:
        path.chmod(0o600)
        handle.write(text)


def setup():
    root = ROOT / '.runtime'
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    if (root / 'local.json').exists():
        print('Existing project configuration preserved.')
        return
    config = {}
    for profile, port, s3_port, redis_port in [('app',55450,58350,56379), ('test',55451,58351,56380)]:
        password, access, secret = (secrets.token_hex(24) for _ in range(3))
        database = 'ac_' + profile
        config[profile] = {
            'database_url': f'postgresql+psycopg://{database}:{password}@127.0.0.1:{port}/{database}',
            'object_endpoint':f'http://127.0.0.1:{s3_port}',
            'object_access_key':access, 'object_secret_key':secret,
            'redis_url':f'redis://127.0.0.1:{redis_port}/0',
        }
        private_write(root/f'pg-{profile}.env',f'POSTGRES_USER={database}\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB={database}\n')
        private_write(root/f's3-{profile}.json',json.dumps({'identities':[{'name':'aftercare-'+profile,'credentials':[{'accessKey':access,'secretKey':secret}],'actions':['Admin','Read','Write','List','Tagging']}]},indent=2))
    private_write(root/'local.json',json.dumps(config,indent=2)+'\n')
    print('Project-private database and object-storage configuration created.')


if __name__ == '__main__':
    setup()
