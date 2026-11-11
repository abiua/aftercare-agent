"""Create a disposable, offline browser demo without touching the shared stack.

Run with Python 3.11+: start, status, or stop. Stop removes this demo's data.
"""
import argparse
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.request import urlopen

from setup_runtime import ROOT
from verify_fresh_bootstrap import API_CHECK, FreshStack, IMAGES, require

STATE = ROOT / '.runtime/offline-browser-demo.json'


def save(state):
    target = STATE.with_suffix('.tmp')
    target.write_text(json.dumps(state, indent=2) + '\n')
    target.chmod(0o600)
    target.replace(STATE)


def restore(state):
    run_id = state['run_id']
    require(re.fullmatch(r'[a-f0-9]{32}', run_id) is not None, 'INVALID_RUN_ID')
    directory = Path(state['directory']).resolve()
    require(directory.parent == STATE.parent.resolve() and directory.name.startswith('browser-demo-'),
            'INVALID_DEMO_DIRECTORY')
    resources = state['resources']
    for kind, name in resources:
        require(kind in ('container', 'network', 'volume') and
                name.startswith(f'aftercare-fresh-{run_id}-'), 'INVALID_RESOURCE_NAME')
    stack = FreshStack.__new__(FreshStack)
    stack.run_id = run_id
    stack.directory = directory
    stack.runtime = directory / 'runtime'
    stack.credentials_path = stack.runtime / 'users-app.json'
    stack.log_path = directory / 'diagnostics.log'
    stack.secrets = []
    stack.resources = [tuple(x) for x in resources]
    return stack


def status(state):
    stack = restore(state)
    alive, missing, stopped = [], [], []
    for kind, name in stack.resources:
        owned = stack.owned(kind, name)
        require(owned is not False, 'RESOURCE_OWNERSHIP_MISMATCH')
        if owned:
            alive.append(name)
            if kind == 'container':
                running = stack.docker('CONTAINER_STATE', 'container', 'inspect', '--format',
                                       '{{.State.Running}}', name, log_output=False)
                if running.stdout.strip() != 'true':
                    stopped.append(name)
        else:
            missing.append(name)
    ready = False
    if state['status'] == 'READY' and not missing and not stopped:
        try:
            with urlopen(state['url'] + '/api/readyz', timeout=4) as response:
                ready = response.status == 200
        except OSError:
            pass
    return {'status': state['status'], 'healthy_now': ready, 'url': state['url'],
            'owned_resources_present': len(alive), 'missing_resources': missing, 'stopped_containers': stopped,
            'credentials_file': str(stack.credentials_path), 'model_mode': 'offline'}


def start(args):
    if STATE.exists():
        state = json.loads(STATE.read_text())
        current = status(state)
        # Never reset a stopped process or overwrite its data based on a stale marker.
        if state['status'] == 'READY' and current['healthy_now']:
            return {**current, 'reused': True}
        require(state['status'] == 'STOPPED' and current['owned_resources_present'] == 0,
                'EXISTING_DEMO_REQUIRES_STATUS_OR_STOP')
    require(args.port != args.object_port, 'PORTS_MUST_DIFFER')
    directory = Path(tempfile.mkdtemp(prefix='browser-demo-', dir=STATE.parent))
    stack = FreshStack(directory)
    state = {'status': 'STARTING', 'run_id': stack.run_id, 'directory': str(directory),
             'resources': stack.resources, 'url': f'http://127.0.0.1:{args.port}'}
    save(state)
    # Journal before Docker writes, including resources whose command outcome is unknown.
    original_docker = stack.docker
    def journaled(label, *values, **kwargs):
        save(state)
        return original_docker(label, *values, **kwargs)
    stack.docker = journaled
    try:
        endpoint = stack.docker('LOCAL_CONTEXT', 'context', 'inspect', '--format',
                                '{{.Endpoints.docker.Host}}').stdout.strip()
        require(endpoint.startswith('unix://'), 'LOCAL_DOCKER_REQUIRED')
        for image in (args.backend_image, args.frontend_image, IMAGES['pg'], IMAGES['objects'], IMAGES['redis']):
            stack.docker('IMAGE_REQUIRED', 'image', 'inspect', '--format', '{{.Id}}', image)
        config_path = stack.runtime / 'local.json'
        config = json.loads(config_path.read_text())
        config['app']['object_presign_endpoint'] = f'http://127.0.0.1:{args.object_port}'
        config_path.write_text(json.dumps(config))
        # Only the UI proxy and object store have host ingress. Model workers stay internal.
        ingress = stack.create('network', 'ingress')
        network = stack.create('network', 'net', '--internal')
        pg_volume = stack.create('volume', 'pg-data')
        object_volume = stack.create('volume', 'objects-data')
        pg = stack.run(IMAGES['pg'], 'pg', network, (
            '--network-alias', 'pg-app', '--env-file', str(directory / 'pg.env'),
            '--mount', f'type=volume,source={pg_volume},target=/var/lib/postgresql/data'),
            ('postgres', '-c', 'cluster_name=aftercare-v2-app'))
        objects = stack.run(IMAGES['objects'], 'objects', ingress, (
            '--network-alias', 'objects-app', '-p', f'127.0.0.1:{args.object_port}:8333',
            '--mount', f'type=volume,source={object_volume},target=/data',
            '--mount', f'type=bind,source={directory / "s3.json"},target=/config/s3.json,readonly'),
            ('server', '-dir=/data', '-ip=objects-app', '-ip.bind=0.0.0.0', '-s3', '-s3.port=8333',
             '-s3.config=/config/s3.json'))
        stack.docker('OBJECT_INTERNAL_NETWORK', 'network', 'connect', '--alias', 'objects-app', network, objects)
        redis = stack.run(IMAGES['redis'], 'redis', network, ('--network-alias', 'redis-app', '--tmpfs', '/data'),
                          ('redis-server', '--save', '', '--appendonly', 'no'))
        common = ('--user', f'{os.getuid()}:{os.getgid()}', '--env', 'AFTERCARE_CONTAINER=1',
                  '--env', 'AFTERCARE_PROFILE=app', '--env', 'AFTERCARE_CONTAINER_MODEL_MODE=offline')
        init = stack.run(args.backend_image, 'init', network, (*common, '--mount',
            f'type=bind,source={stack.runtime},target=/workspace/.runtime', '--entrypoint', 'python'),
            ('-c', 'import time; time.sleep(900)'))
        stack.wait('PG_READY', 'exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'ac_app', '-d', 'ac_app')
        stack.wait('REDIS_READY', 'exec', redis, 'redis-cli', 'ping', expected='PONG')
        stack.docker('MIGRATE', 'exec', init, 'alembic', 'upgrade', 'head', timeout=90)
        stack.docker('BOOTSTRAP', 'exec', init, 'python', '-m', 'aftercare.manage', 'bootstrap', '--profile', 'app', timeout=90)
        readonly = (*common, '--mount', f'type=bind,source={stack.runtime},target=/workspace/.runtime,readonly')
        api = stack.run(args.backend_image, 'api', network, (*readonly, '--network-alias', 'api-app'))
        stack.wait('API_READY', 'exec', api, 'python', '-c',
                   "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/readyz',timeout=4).close()")
        worker = stack.run(args.backend_image, 'worker', network, readonly,
            ('celery', '-A', 'aftercare.worker', 'worker', '--pool=solo', '--concurrency=1', '--loglevel=WARNING'))
        stack.wait('WORKER_READY', 'exec', worker, 'celery', '-A', 'aftercare.worker', 'inspect', '--timeout', '2', 'ping')
        stack.run(args.backend_image, 'beat', network, readonly,
            ('celery', '-A', 'aftercare.worker', 'beat', '--loglevel=WARNING', '--schedule=/tmp/demo-beat'))
        front = stack.run(args.frontend_image, 'frontend', ingress,
            ('-p', f'127.0.0.1:{args.port}:8080', '--env', 'AFTERCARE_UPSTREAM=api-app:8000'))
        stack.docker('FRONTEND_INTERNAL_NETWORK', 'network', 'connect', '--alias', 'frontend', network, front)
        stack.wait('NGINX_READY', 'exec', api, 'python', '-c',
                   "from urllib.request import urlopen; urlopen('http://frontend:8080/api/readyz',timeout=4).close()")
        auth = stack.json_exec('AUTH', api, API_CHECK.replace('http://127.0.0.1:8000', 'http://frontend:8080'))
        require(auth['runtime_mode'] == 'offline' and all(auth['role_auth'].values()), 'OFFLINE_AUTH_REQUIRED')
        require(stack.owned('container', init), 'OWN_INIT_REQUIRED')
        stack.docker('REMOVE_INIT', 'rm', '-f', init)
        stack.resources.remove(('container', init))
        state.update(status='READY', auth=auth, backend_image=args.backend_image, frontend_image=args.frontend_image)
        save(state)
        result = status(state)
        require(result['healthy_now'], 'HOST_INGRESS_NOT_READY')
        return result
    except BaseException:
        state['status'] = 'FAILED'
        state['cleanup_leftovers'] = stack.cleanup(True)
        save(state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'status', 'stop'])
    parser.add_argument('--port', type=int, default=5190)
    parser.add_argument('--object-port', type=int, default=58352)
    parser.add_argument('--backend-image', default='aftercare-backend:local')
    parser.add_argument('--frontend-image', default='aftercare-frontend:local')
    args = parser.parse_args()
    require(1024 <= args.port <= 65535 and 1024 <= args.object_port <= 65535, 'INVALID_PORT')
    STATE.parent.mkdir(exist_ok=True)
    # Serialize lifecycle changes; never launch two stacks on the same state path.
    fd = os.open(STATE.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'BUSY', 'message': '演示环境正在启动或停止，请稍后查询；未执行新的操作。'}, ensure_ascii=False))
            return 1
        if args.command == 'start':
            result = start(args)
        elif not STATE.exists():
            result = {'status': 'NOT_STARTED'}
        else:
            state = json.loads(STATE.read_text())
            if args.command == 'stop':
                state['cleanup_leftovers'] = restore(state).cleanup(False)
                state['status'] = 'STOPPED' if not state['cleanup_leftovers'] else 'CLEANUP_INCOMPLETE'
                save(state)
            result = status(state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('status') not in ('FAILED', 'CLEANUP_INCOMPLETE') else 1


if __name__ == '__main__':
    raise SystemExit(main())
