"""Verify production bootstrap in disposable Docker resources with fresh volumes."""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from setup_runtime import ROOT, private_write

IMAGES = {
    "backend": "aftercare-backend:acceptance-20261110-formal",
    "frontend": "aftercare-frontend:acceptance-20261110-refund-fixed",
    "pg": "pgvector/pgvector@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8",
    "redis": "redis@sha256:7b1753b4c5ad8458906c272a8132b8c15f5557b000379eb115fb239706a384b7",
    "objects": "chrislusf/seaweedfs@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59",
}
LABEL = "com.aftercare.fresh-bootstrap"
ROLE_CUSTOMERS = {"customer": "C001", "support": None, "approver": None, "tester": None}
BASELINE = {
    "business_customers": 100, "business_products": 30, "business_orders": 300,
    "business_order_lines": 501, "business_inventory": 30, "business_policies": 14,
    "users": 4, "checkpoint_migrations": 10, "alembic_version": 1,
}
OBJECT_LIST = """
import json
from aftercare.business.storage import S3ObjectStore
from aftercare.config import load_settings
store = S3ObjectStore(load_settings("app"))
print(json.dumps([row["Name"] for row in store.client.list_buckets()["Buckets"]]))
"""
SNAPSHOT = """
import json
from pathlib import Path
from stat import S_IMODE
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
import boto3
from sqlalchemy import text
from aftercare.auth import token_hash
from aftercare.business.storage import S3ObjectStore
from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine

settings = load_settings("app")
credentials_path = ROOT / ".runtime/users-app.json"
credentials = json.loads(credentials_path.read_text())
engine = guarded_engine(settings)
try:
    with engine.connect() as db:
        def query(statement):
            return db.execute(text(statement))
        names = list(query("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").scalars())
        counts, digests = {}, {}
        for table in sorted(names):
            quoted = db.dialect.identifier_preparer.quote_identifier(table)
            counts[table], digests[table] = query(
                "SELECT count(*), md5(coalesce(string_agg(to_jsonb(r)::text, chr(10) "
                f"ORDER BY to_jsonb(r)::text), '')) FROM public.{quoted} r"
            ).one()
        users = {row["id"]: row for row in query("SELECT * FROM users").mappings()}
        credential_checks = {}
        for item in credentials:
            row = users.get(item["user_id"], {})
            credential_checks[item["role"]] = (
                row.get("role") == item["role"] and row.get("customer_id") == item["customer_id"]
                and row.get("enabled") is True
                and row.get("token_hash") == token_hash(item["token"])
            )
        summary = {
            "counts": counts,
            "credential_mode": oct(S_IMODE(credentials_path.stat().st_mode)),
            "credential_db_matches": credential_checks,
            "inventory": list(query(
                "SELECT sum(available_quantity), min(available_quantity), max(available_quantity) "
                "FROM business_inventory"
            ).one()),
            "order_statuses": dict(query(
                "SELECT data->>'status', count(*) FROM business_orders GROUP BY 1"
            ).all()),
            "shipments": query(
                "SELECT count(*) FROM business_orders WHERE data->>'shipment' IS NOT NULL"
            ).scalar_one(),
            "line_amounts": list(query(
                "SELECT sum(paid_cents), sum(refunded_cents), "
                "count(*) FILTER (WHERE refunded_cents > 0) FROM business_order_lines"
            ).one()),
            "policies": list(query(
                "SELECT id || '@' || version FROM business_policies ORDER BY id, version"
            ).scalars()),
            "checkpoints": list(query("SELECT v FROM checkpoint_migrations ORDER BY v").scalars()),
            "alembic_heads": list(query("SELECT version_num FROM alembic_version").scalars()),
        }
finally:
    engine.dispose()

store = S3ObjectStore(settings)
head = store.client.head_bucket(Bucket=store.bucket)
objects = store.client.list_objects_v2(Bucket=store.bucket, MaxKeys=1)
unsigned = boto3.client(
    "s3", endpoint_url=settings.object_endpoint, region_name="us-east-1",
    config=Config(signature_version=UNSIGNED, connect_timeout=3, read_timeout=5,
                  retries={"max_attempts": 0}, s3={"addressing_style": "path"}),
)
try:
    unsigned.head_bucket(Bucket=store.bucket)
except ClientError as error:
    anonymous_status = error.response["ResponseMetadata"]["HTTPStatusCode"]
else:
    anonymous_status = 200
summary["s3"] = {
    "signed_head": head["ResponseMetadata"]["HTTPStatusCode"],
    "signed_list": objects["ResponseMetadata"]["HTTPStatusCode"],
    "object_count": objects["KeyCount"],
    "unsigned_head": anonymous_status,
}
print(json.dumps({"summary": summary, "digests": digests}, sort_keys=True))
"""
API_CHECK = """
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from aftercare.config import ROOT

base = "http://127.0.0.1:8000"
credentials = json.loads((ROOT / ".runtime/users-app.json").read_text())

def read(path, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with urlopen(Request(base + path, headers=headers), timeout=5) as response:
        return json.load(response)

ready = read("/api/readyz")
roles = {}
for item in credentials:
    result = read("/api/me", item["token"])
    roles[item["role"]] = all(
        result.get(key) == item[key] for key in ("role", "user_id", "customer_id", "display_name")
    )
runtime = read("/api/runtime", credentials[0]["token"])
try:
    read("/api/me")
except HTTPError as error:
    unsigned = error.code
else:
    unsigned = 200
print(json.dumps({"ready": ready.get("status"), "role_auth": roles,
                  "runtime_mode": runtime.get("mode"), "anonymous_http": unsigned}))
"""


class VerificationError(Exception):
    pass


QUEUE_CHECK = r'''
import json, time
from uuid import uuid4
import httpx
from sqlalchemy import text
from aftercare.config import ROOT, load_settings
from aftercare.db import guarded_engine

users = json.loads((ROOT / ".runtime/users-app.json").read_text())
token = next(row["token"] for row in users if row["role"] == "support")
with httpx.Client(base_url="http://frontend:8080/api", headers={"Authorization": "Bearer " + token}, timeout=10) as client:
    def read(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()
    order = next(row for row in read("/orders?limit=300")["items"]
                 if row["status"] == "DELIVERED" and row["payment_status"] == "CONFIRMED"
                 and not row["refunded_cents"])
    response = client.post("/cases", headers={"Idempotency-Key": str(uuid4())}, json={
        "order_id": order["id"], "expected_version": order["version"], "intent": "damaged",
        "description": "独立空卷离线验收：商品破损，尚未提交商品及外包装照片。",
    })
    assert response.status_code == 201
    case = response.json()
    path = "/cases/" + case["id"] + "/runs"
    response = client.post(path, headers={"Idempotency-Key": str(uuid4())}, json={
        "expected_version": case["version"], "mode": "multi", "strategy": "routed",
    })
    assert response.status_code == 202
    run = response.json()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = read(path + "/" + run["id"])
        if run["status"] not in {"QUEUED", "RUNNING"}:
            break
        time.sleep(.25)
    assert run["status"] == "WAITING_INPUT", run["status"]
    events = read(path + "/" + run["id"] + "/events")
    assert not any(event["kind"] == "attempt" for event in events)
engine = guarded_engine(load_settings("app"))
with engine.connect() as db:
    attempts = db.scalar(text("SELECT count(*) FROM agent_model_attempts"))
    jobs = db.execute(text("SELECT state FROM dispatch_jobs")).all()
    assert attempts == 0
    assert len(jobs) == 1 and jobs[0][0] == "DONE"
engine.dispose()
print(json.dumps({"case_id": case["id"], "run_id": run["id"], "status": run["status"],
                  "events": len(events), "model_attempts": attempts, "dispatch_state": jobs[0][0]}))
'''


def require(condition, code):
    if not condition:
        raise VerificationError(code)


class FreshStack:
    def __init__(self, directory):
        self.directory = directory
        self.runtime = directory / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.credentials_path = self.runtime / "users-app.json"
        self.run_id = uuid4().hex
        self.prefix = f"aftercare-fresh-{self.run_id}"
        self.label = f"{LABEL}={self.run_id}"
        self.resources = []
        self.secrets = [secrets.token_hex(24) for _ in range(3)]
        self.log_path = directory / "diagnostics.log"
        private_write(self.log_path, "")
        password, access, secret = self.secrets
        private_write(directory / "pg.env",
                      f"POSTGRES_USER=ac_app\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB=ac_app\n")
        private_write(directory / "s3.json", json.dumps({"identities": [{
            "name": "fresh-bootstrap", "credentials": [{"accessKey": access, "secretKey": secret}],
            "actions": ["Admin", "Read", "Write", "List", "Tagging"],
        }]}))
        private_write(self.runtime / "local.json", json.dumps({"app": {
            "database_url": f"postgresql+psycopg://ac_app:{password}@127.0.0.1:55450/ac_app",
            "object_endpoint": "http://127.0.0.1:58350",
            "object_access_key": access, "object_secret_key": secret,
            "redis_url": "redis://127.0.0.1:56379/0",
        }}))

    def log(self, label, output):
        try:
            tokens = [item["token"] for item in json.loads(self.credentials_path.read_text())]
        except (OSError, ValueError, KeyError, TypeError):
            tokens = []
        for secret in self.secrets + tokens:
            output = output.replace(secret, "[redacted]")
        with self.log_path.open("a") as stream:
            stream.write(f"\n[{label}]\n{output.rstrip()}\n")

    def docker(self, label, *args, timeout=30, required=True, log_output=True):
        try:
            result = subprocess.run(
                ["docker", *args], capture_output=True, text=True, errors="replace",
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            output = b"\n".join(item for item in (error.stdout, error.stderr) if item)
            self.log(label, "timeout\n" + output.decode(errors="replace"))
            if required:
                raise VerificationError(f"{label}_TIMEOUT") from None
            return None
        if log_output or result.returncode:
            self.log(label, result.stdout + result.stderr)
        if required:
            require(result.returncode == 0, f"{label}_FAILED")
        return result

    def json_exec(self, label, name, code):
        result = self.docker(label, "exec", name, "python", "-c", code, log_output=False)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def wait(self, label, *args, expected=None, seconds=45):
        deadline = monotonic() + seconds
        while monotonic() < deadline:
            result = self.docker(label, *args, timeout=8, required=False, log_output=False)
            if result and result.returncode == 0 and (
                expected is None or result.stdout.strip() == expected
            ):
                return
            sleep(1)
        raise VerificationError(f"{label}_NOT_READY")

    def create(self, kind, suffix, *args):
        name = f"{self.prefix}-{suffix}"
        self.resources.append((kind, name))
        self.docker(f"CREATE_{suffix.upper()}", kind, "create", "--label", self.label, *args, name)
        require(self.owned(kind, name) is True, "RESOURCE_OWNERSHIP_MISMATCH")
        return name

    def run(self, image, suffix, network, options=(), command=()):
        name = f"{self.prefix}-{suffix}"
        self.resources.append(("container", name))
        self.docker(
            f"START_{suffix.upper()}", "run", "--detach", "--pull=never", "--name", name,
            "--label", self.label, "--network", network, *options, image, *command,
        )
        require(self.owned("container", name) is True, "CONTAINER_OWNERSHIP_MISMATCH")
        return name

    def owned(self, kind, name):
        location = ".Config.Labels" if kind == "container" else ".Labels"
        template = '{{ index ' + location + ' "' + LABEL + '" }}'
        result = self.docker("INSPECT_OWNERSHIP", kind, "inspect", "--format", template, name,
                             timeout=12, required=False, log_output=False)
        if result and result.returncode == 0:
            return result.stdout.strip() == self.run_id
        if result and ("no such" in result.stderr.lower() or "not found" in result.stderr.lower()):
            return None
        raise VerificationError("RESOURCE_OWNERSHIP_CHECK_FAILED")

    def cleanup(self, failed):
        failures = []
        if failed:
            for kind, name in self.resources:
                try:
                    if kind == "container" and self.owned(kind, name):
                        self.docker("CONTAINER_DIAGNOSTICS", "logs", "--tail", "100", name,
                                    timeout=10, required=False)
                except (VerificationError, OSError):
                    pass
        for kind, name in reversed(self.resources):
            try:
                owned = self.owned(kind, name)
                if owned is None:
                    continue
                require(owned, "RESOURCE_OWNERSHIP_MISMATCH")
                flags = ("--force", "--volumes") if kind == "container" else ()
                self.docker("REMOVE_OWN_RESOURCE", kind, "rm", *flags, name, timeout=25)
            except (VerificationError, OSError):
                failures.append(f"{kind}:{name}")
        return failures


def validate_snapshot(snapshot):
    summary = snapshot["summary"]
    counts = summary["counts"]
    require(all(counts.get(table) == count for table, count in BASELINE.items()),
            "BASELINE_TABLE_COUNTS_MISMATCH")
    require(all(count == 0 for table, count in counts.items() if table not in BASELINE),
            "UNEXPECTED_NONEMPTY_TABLE")
    empty_required = {
        "business_cases", "business_executions", "agent_runs", "agent_run_events",
        "dispatch_jobs", "eval_experiments", "intake_model_audits", "provider_rate_scopes",
        "policy_vectors", "checkpoints", "checkpoint_blobs", "checkpoint_writes",
    }
    require(empty_required <= counts.keys(), "EXPECTED_MIGRATED_TABLES_MISSING")
    require(summary["order_statuses"] == {
        "DELIVERED": 150, "SHIPPED": 60, "PAID": 45, "UNPAID": 24, "CANCELLED": 21,
    }, "BASELINE_ORDER_STATUSES_MISMATCH")
    require(summary["inventory"] == [300, 10, 10] and summary["shipments"] == 210
            and summary["line_amounts"] == [21825791, 1824460, 66], "BASELINE_BUSINESS_SUMS_MISMATCH")
    require(summary["policies"] == [f"POL{index:02d}@1.0" for index in range(1, 15)],
            "BASELINE_POLICIES_MISMATCH")
    require(summary["checkpoints"] == list(range(10)), "CHECKPOINT_MIGRATIONS_MISMATCH")
    require(summary["credential_mode"] == "0o600"
            and summary["credential_db_matches"] == dict.fromkeys(ROLE_CUSTOMERS, True),
            "BOOTSTRAP_CREDENTIALS_MISMATCH")
    s3 = summary["s3"]
    require(s3["signed_head"] == s3["signed_list"] == 200 and s3["object_count"] == 0
            and s3["unsigned_head"] in (401, 403), "PRIVATE_S3_BUCKET_CHECK_FAILED")


def verify(stack, report):
    # Prevent accidental use of remote Docker contexts. No image is pulled.
    context = os.environ.get("DOCKER_CONTEXT")
    endpoint = os.environ.get("DOCKER_HOST") if not context else None
    if not endpoint:
        endpoint = stack.docker(
            "LOCAL_CONTEXT", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}",
            *((context,) if context else ()), log_output=False,
        ).stdout.strip()
    require(endpoint.startswith("unix://"), "LOCAL_DOCKER_SOCKET_REQUIRED")
    images = {
        service: stack.docker(
            f"IMAGE_{service.upper()}", "image", "inspect", "--format", "{{.Id}}", ref,
        ).stdout.strip()
        for service, ref in IMAGES.items()
    }
    require(all(image.startswith("sha256:") for image in images.values()), "IMAGE_ID_REQUIRED")
    report["image_ids"] = images

    network = stack.create("network", "net", "--internal")
    pg_volume = stack.create("volume", "pg-data")
    objects_volume = stack.create("volume", "objects-data")
    pg = stack.run(images["pg"], "pg", network, (
        "--network-alias", "pg-app", "--env-file", str(stack.directory / "pg.env"),
        "--mount", f"type=volume,source={pg_volume},target=/var/lib/postgresql/data",
    ), ("postgres", "-c", "cluster_name=aftercare-v2-app"))
    stack.run(images["objects"], "objects", network, (
        "--network-alias", "objects-app",
        "--mount", f"type=volume,source={objects_volume},target=/data",
        "--mount", f"type=bind,source={stack.directory / 's3.json'},target=/config/s3.json,readonly",
    ), ("server", "-dir=/data", "-ip=objects-app", "-ip.bind=0.0.0.0",
        "-s3", "-s3.port=8333", "-s3.config=/config/s3.json"))
    redis = stack.run(images["redis"], "redis", network, (
        "--network-alias", "redis-app", "--tmpfs", "/data",
    ), ("redis-server", "--save", "", "--appendonly", "no"))
    backend = (
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--env", "AFTERCARE_CONTAINER=1", "--env", "AFTERCARE_PROFILE=app",
        "--env", "AFTERCARE_CONTAINER_MODEL_MODE=offline",
    )
    init = stack.run(images["backend"], "init", network, (
        *backend, "--mount", f"type=bind,source={stack.runtime},target=/workspace/.runtime",
        "--entrypoint", "python",
    ), ("-c", "import time; time.sleep(900)"))

    stack.wait("PG_TCP", "exec", pg, "pg_isready", "-h", "127.0.0.1", "-U", "ac_app", "-d", "ac_app")
    stack.wait("REDIS_PING", "exec", redis, "redis-cli", "ping", expected="PONG")
    empty_pg = stack.docker(
        "PG_EMPTY_BASELINE", "exec", pg, "psql", "-AtX", "-U", "ac_app", "-d", "ac_app", "-c",
        "SELECT count(*) FROM pg_tables WHERE schemaname='public';",
    )
    require(empty_pg.stdout.strip() == "0", "POSTGRES_VOLUME_NOT_EMPTY")
    stack.wait("S3_EMPTY_BASELINE", "exec", init, "python", "-c", OBJECT_LIST, expected="[]")
    require(not stack.credentials_path.exists(), "CREDENTIAL_FILE_ALREADY_EXISTS")
    report["empty_before_bootstrap"] = {"pg_public_tables": 0, "s3_buckets": 0, "credentials": False}

    first_snapshot = None
    for cycle in (1, 2):
        stack.docker(f"MIGRATION_{cycle}", "exec", init, "alembic", "upgrade", "head", timeout=90)
        stack.docker(f"BOOTSTRAP_{cycle}", "exec", init, "python", "-m", "aftercare.manage",
                     "bootstrap", "--profile", "app", timeout=90)
        snapshot = stack.json_exec(f"VERIFY_BASELINE_{cycle}", init, SNAPSHOT)
        stack.log(f"BASELINE_SUMMARY_{cycle}", json.dumps(snapshot["summary"], sort_keys=True))
        validate_snapshot(snapshot)
        credentials = json.loads(stack.credentials_path.read_text())
        require(len(credentials) == 4
                and {item["role"]: item["customer_id"] for item in credentials} == ROLE_CUSTOMERS
                and all(item["role"] == item["user_id"] for item in credentials)
                and len({item["token"] for item in credentials}) == 4, "ROLE_CREDENTIAL_FILE_MISMATCH")
        identity_file = (stack.credentials_path.read_bytes(), stack.credentials_path.stat().st_mtime_ns)
        if cycle == 1:
            first_snapshot = snapshot, identity_file
            report["first_bootstrap"] = snapshot["summary"]
        else:
            require((snapshot, identity_file) == first_snapshot, "BOOTSTRAP_NOT_IDEMPOTENT")
            report["idempotence"] = {
                "all_table_contents_unchanged": True, "credential_bytes_mode_mtime_unchanged": True,
                "private_bucket_unchanged": True,
            }

    api = stack.run(images["backend"], "api", network, (
        *backend, "--network-alias", "api-app",
        "--mount", f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
    ))
    stack.wait(
        "API_READY", "exec", api, "python", "-c",
        "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/readyz',timeout=4).close()",
    )
    checks = stack.json_exec("API_AUTH", api, API_CHECK)
    stack.log("API_AUTH_SUMMARY", json.dumps(checks, sort_keys=True))
    require(checks == {"ready": "ready", "role_auth": dict.fromkeys(ROLE_CUSTOMERS, True),
                       "runtime_mode": "offline", "anonymous_http": 401}, "API_AUTH_READINESS_FAILED")
    worker_options = (*backend, "--mount",
                      f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly")
    worker = stack.run(images["backend"], "worker", network, worker_options, (
        "celery", "-A", "aftercare.worker", "worker", "--pool=solo", "--concurrency=1",
        "--loglevel=WARNING",
    ))
    stack.wait("WORKER_READY", "exec", worker, "celery", "-A", "aftercare.worker",
               "inspect", "--timeout", "2", "ping")
    beat = stack.run(images["backend"], "beat", network, worker_options, (
        "celery", "-A", "aftercare.worker", "beat", "--loglevel=WARNING",
        "--schedule=/tmp/fresh-beat",
    ))
    stack.run(images["frontend"], "frontend", network, (
        "--network-alias", "frontend", "--env", "AFTERCARE_UPSTREAM=api-app:8000",
    ))
    stack.wait("NGINX_READY", "exec", init, "python", "-c",
               "from urllib.request import urlopen; urlopen('http://frontend:8080/api/readyz',timeout=4).close()")
    report["nginx_auth"] = stack.json_exec("NGINX_AUTH", init,
                                          API_CHECK.replace("http://127.0.0.1:8000", "http://frontend:8080"))
    require(report["nginx_auth"] == checks, "NGINX_AUTH_MISMATCH")
    report["queue"] = stack.json_exec("QUEUE_BUSINESS", init, QUEUE_CHECK)
    require(stack.docker("BEAT_RUNNING", "inspect", "--format", "{{.State.Running}}", beat).stdout.strip() == "true",
            "BEAT_NOT_RUNNING")
    for kind, name in stack.resources:
        if kind == "container":
            result = stack.docker("HOST_PORT_CHECK", "port", name)
            require(not result.stdout.strip(), "UNEXPECTED_PUBLISHED_HOST_PORT")
    report["api"] = checks
    report["published_host_ports"] = 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-isolated-docker", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="New evidence JSON file")
    args = parser.parse_args()
    if not args.run_isolated_docker:
        parser.error("--run-isolated-docker is required")
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new evidence file.\n")
    report = {"status": "FAILED", "source": "real_docker_fresh_pg_s3_volumes",
              "scope": "fresh_offline_api_worker_beat_nginx_pg_s3_redis", "cleanup": "NOT_CREATED"}
    stack = None
    directory = None
    with os.fdopen(descriptor, "w") as output:
        try:
            runtime = ROOT / ".runtime"
            require(runtime.is_dir() and not runtime.is_symlink(), "PRIVATE_RUNTIME_DIRECTORY_REQUIRED")
            directory = Path(tempfile.mkdtemp(prefix="fresh-bootstrap-", dir=runtime))
            stack = FreshStack(directory)
            verify(stack, report)
            report["status"] = "PASSED"
        except VerificationError as error:
            report["failure"] = str(error)
        except (Exception, KeyboardInterrupt) as error:
            report["failure"] = type(error).__name__
        finally:
            if stack:
                leftovers = stack.cleanup(report["status"] != "PASSED")
                report["cleanup"] = "OWN_RESOURCES_REMOVED" if not leftovers else "FAILED"
                if leftovers:
                    report.update(status="FAILED", cleanup_leftovers=leftovers)
            if directory and report["status"] == "PASSED":
                shutil.rmtree(directory)
            elif directory:
                # Preserve diagnostics only. The generated settings and credentials are disposable.
                shutil.rmtree(directory / "runtime", ignore_errors=True)
                for name in ("pg.env", "s3.json"):
                    (directory / name).unlink(missing_ok=True)
                if stack:
                    report["private_diagnostics"] = str(stack.log_path.relative_to(ROOT))
            json.dump(report, output, indent=2, ensure_ascii=False)
            output.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
