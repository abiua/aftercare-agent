"""Verify an owned 5201 old-to-new material-review migration without model calls."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes, token_urlsafe
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/offline-browser-demo-material-observations.json"
PORT, OBJECT_PORT = 5201, 58363
URL = f"http://127.0.0.1:{PORT}"
OLD = "sha256:22ca76e1dfcf2fba79734280dc298fc065b16493e408f4e2e34d540ee1d7e07a"
OLD_FRONTEND = "sha256:9415757c2cc4ee663167160e0a416126cfd4f07653b79fb24f25d098075d65d8"
NEW_BACKEND = "sha256:56bb74223bcd81a34749c48e5a8c4241960470d579e650d6c0ec6b2784b52513"
NEW_FRONTEND = "sha256:1b7338916d19a0e39e253bc5eb373a2d691d1524db0f22f5fd0dd0df3c42a2c9"
OLD_REV, NEW_REV = "20261111_chat_blind_review", "20261111_material_review_obs"
CAMPAIGN = "material-review-migration-legacy-source"
CASE = "DAMAGE-ORDINARY"
FOREIGN_CUSTOMER = "C002"
LEGACY_INPUT = "material-review-migration-input"
RECOVERY_INPUT = "material-review-migration-recovery-input"
APP_RECOVERY_INPUT = "material-review-migration-app-recovery-input"


def _check(condition, code):
    if not condition:
        raise ValueError(code)


def _hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _docker(*args):
    completed = subprocess.run(
        ["docker", *args], check=False, capture_output=True, text=True, timeout=15
    )
    _check(completed.returncode == 0, "MATERIAL_MIGRATION_DOCKER_PREFLIGHT_FAILED")
    return completed.stdout.strip()


def _plan(images, failed_report=None):
    from verify_chat_p5_pg import FILES

    if failed_report is None:
        _check(not STATE.exists(), "MATERIAL_MIGRATION_STACK_ALREADY_EXISTS")
        for port in (PORT, OBJECT_PORT):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    raise ValueError(f"MATERIAL_MIGRATION_PORT_{port}_BUSY") from None
    endpoint = os.environ.get("DOCKER_HOST")
    _check(
        (not endpoint or endpoint.startswith("unix://"))
        and _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}").startswith(
            "unix://"
        ),
        "MATERIAL_MIGRATION_LOCAL_DOCKER_REQUIRED",
    )
    _check(
        images["old_backend"] == OLD
        and images["old_frontend"] == OLD_FRONTEND
        and images["new_backend"] == NEW_BACKEND
        and images["new_frontend"] == NEW_FRONTEND,
        "MATERIAL_MIGRATION_IMAGE_PAIR_REQUIRED",
    )
    for image in set(images.values()):
        _check(
            re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            and _docker("image", "inspect", "--format", "{{.Id}}", image) == image,
            "MATERIAL_MIGRATION_IMAGE_PIN_MISMATCH",
        )
    plan = {
        "scope": "ISOLATED_TEST_PUBLIC_MIGRATION_AND_HISTORICAL_EVAL_CLONE",
        "profile": "test",
        "url": URL,
        "object_port": OBJECT_PORT,
        "images": images,
        "old_revision": OLD_REV,
        "new_revision": NEW_REV,
        "files": {name: _hash(ROOT / name) for name in FILES[1:]},
        "script_sha256": _hash(Path(__file__)),
        "maximum_model_or_financial_writes": 0,
        "foreign_customer_fixture": {
            "customer_id": FOREIGN_CUSTOMER,
            "maximum_new_users": 1,
            "profile": "test",
        },
    }
    if failed_report is not None:
        from verify_chat_backup_restore import private_json

        failed = private_json(failed_report)
        plan["recovery"] = (
            _app_recovery_plan(images, plan["files"], failed_report, failed)
            if failed.get("stage") == "MIGRATING"
            else _recovery_plan(images, plan["files"], failed_report)
        )
    return plan


def _topology(stack, backend, frontend):
    import verify_chat_p5_pg as pg

    with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
        return pg._topology(stack, {pg.BACKEND: backend, pg.FRONTEND: frontend})


def _pristine(stack, system_id):
    from verify_chat_p2_offline import owned_container

    observed = stack.json_exec(
        "MATERIAL_MIGRATION_PRISTINE_READBACK",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import inspect, text
from aftercare.business.errors import BusinessError
from aftercare.business.storage import S3ObjectStore
from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.evaluation.chat.pg_runtime import sentinel_key
settings = load_settings('test')
assert settings.container and not any((settings.model_enabled,settings.chat_vision_enabled,settings.embedding_enabled,settings.chat_analysis_enabled))
engine = guarded_engine(settings,test_only=True)
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    identity = db.execute(text("SELECT current_database(),current_user,session_user,current_setting('cluster_name'),system_identifier::text FROM pg_control_system()")).one()
    assert tuple(identity) == ('ac_test','ac_test','ac_test','aftercare-v2-test',{system_id!r})
    tables = ('business_cases','business_materials','business_material_reviews','business_idempotency','business_executions','business_approvals','business_simulated_payments','chat_conversations','agent_model_attempts','chat_model_attempts','intake_model_audits')
    counts = {{table:db.scalar(text(f'SELECT count(*) FROM public.{{table}}')) for table in tables}}
    revision = db.scalar(text('SELECT version_num FROM public.alembic_version'))
    old_columns = [col['name'] for col in inspect(db).get_columns('business_material_reviews',schema='public')]
    eval_schemas = db.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname ~ '^eval_[a-f0-9]{{32}}$'"))
    foreign_exists = bool(db.scalar(text('SELECT 1 FROM public.business_customers WHERE id=:id'),{{'id':{FOREIGN_CUSTOMER!r}}}))
    foreign_actors = db.scalar(text('SELECT count(*) FROM public.users WHERE customer_id=:id'),{{'id':{FOREIGN_CUSTOMER!r}}})
    old_users = db.scalar(text('SELECT count(*) FROM public.users'))
try:
    S3ObjectStore(settings).get(sentinel_key({stack.run_id!r}),64)
except BusinessError as error:
    assert error.code == 'MATERIAL_OBJECT_MISSING'
else:
    raise ValueError('MATERIAL_MIGRATION_SOURCE_MARKER_ALREADY_EXISTS')
print(json.dumps(dict(revision=revision,counts=counts,eval_schemas=eval_schemas,
    legacy_columns='observations' not in old_columns,foreign_customer_exists=foreign_exists,
    foreign_actors=foreign_actors,original_users=old_users,source_marker_present=False)))
engine.dispose()
""",
    )
    _check(
        observed["revision"] == OLD_REV
        and observed["legacy_columns"]
        and observed["eval_schemas"] == observed["foreign_actors"] == 0
        and not any(observed["counts"].values())
        and observed["foreign_customer_exists"]
        and not observed["source_marker_present"],
        "MATERIAL_MIGRATION_LEGACY_STACK_NOT_PRISTINE",
    )
    return observed


def _recovery_plan(images, files, failed_path):
    import offline_demo
    from aftercare.business.rules import digest
    from verify_chat_backup_restore import private_json

    failed = private_json(failed_path)
    original = failed["plan"]
    state = private_json(STATE)
    _check(
        failed["status"] == "FAILED"
        and failed["stage"] == "LEGACY_FIXTURE"
        and failed["failure_code"] == "TWO_CUSTOMERS_SUPPORT_AND_TESTER_REQUIRED"
        and failed["plan_sha256"] == digest(original)
        and original["scope"] == "ISOLATED_TEST_PUBLIC_MIGRATION_AND_HISTORICAL_EVAL_CLONE"
        and original["images"] == images
        and original["files"] == files
        and original["old_revision"] == OLD_REV
        and failed["stack_id"] == state["run_id"]
        and failed["owned_url"] == state["url"] == URL
        and state["profile"] == "test"
        and state["status"] == "READY"
        and state["backend_image"] == OLD
        and state["frontend_image"] == OLD_FRONTEND
        and "legacy_case_id" not in failed,
        "MATERIAL_MIGRATION_FAILED_RUN_NOT_RECOVERABLE",
    )
    stack = offline_demo.restore(state)
    _, identity = _topology(stack, OLD, OLD_FRONTEND)
    _check(
        identity == failed["system_identifier"]
        and offline_demo.status(state)["healthy_now"]
        and not (stack.directory / RECOVERY_INPUT).exists()
        and not _foreign_sidecar(stack).exists(),
        "MATERIAL_MIGRATION_RECOVERY_STACK_CHANGED",
    )
    staged = stack.directory / LEGACY_INPUT
    original_files = {**files, "verifier.py": original["script_sha256"]}
    _check(
        all(
            (staged / name).is_file()
            and not (staged / name).is_symlink()
            and _hash(staged / name) == checksum
            for name, checksum in original_files.items()
        ),
        "MATERIAL_MIGRATION_ORIGINAL_STAGE_CHANGED",
    )
    return {
        "failed_report": str(failed_path.resolve()),
        "failed_report_sha256": _hash(failed_path),
        "stack_id": stack.run_id,
        "system_identifier": identity,
        "original_staged_directory": str(staged),
        "original_staged_files": original_files,
        "pristine_readback": _pristine(stack, identity),
    }


def _app_recovery_topology(stack, system_id):
    from verify_chat_backup_restore import published_port
    from verify_fresh_bootstrap import IMAGES

    prefix = f"aftercare-fresh-{stack.run_id}"

    def info(kind, suffix, field):
        name = f"{prefix}-{suffix}"
        _check(
            (kind, name) in stack.resources and stack.owned(kind, name) is True,
            "MATERIAL_MIGRATION_APP_RECOVERY_UNOWNED",
        )
        return json.loads(
            stack.docker(
                "MATERIAL_MIGRATION_APP_RECOVERY_TOPOLOGY",
                kind,
                "inspect",
                "--format",
                "{{json ." + field + "}}",
                name,
                log_output=False,
            ).stdout
        )

    network = f"{prefix}-net"
    ingress = f"{prefix}-ingress"
    _check(
        info("network", "net", "Internal") is True
        and info("network", "ingress", "Internal") is False,
        "MATERIAL_MIGRATION_APP_RECOVERY_NETWORK",
    )
    for role, expected_networks, volume in (
        ("pg", {network}, "pg-data"),
        ("redis", {network}, None),
        ("objects", {network, ingress}, "objects-data"),
    ):
        expected_image = _docker("image", "inspect", "--format", "{{.Id}}", IMAGES[role])
        _check(
            info("container", role, "Image") == expected_image
            and info("container", role, "State.Running") is True
            and info("container", role, "NetworkSettings.Networks").keys() == expected_networks,
            "MATERIAL_MIGRATION_APP_RECOVERY_INFRA_CHANGED",
        )
        if volume:
            _check(
                any(
                    mount.get("Name") == info("volume", volume, "Name")
                    for mount in info("container", role, "Mounts")
                ),
                "MATERIAL_MIGRATION_APP_RECOVERY_VOLUME_CHANGED",
            )
    for suffix, roles in (("net", ("pg", "redis", "objects")), ("ingress", ("objects",))):
        _check(
            {item["Name"] for item in info("network", suffix, "Containers").values()}
            == {f"{prefix}-{role}" for role in roles},
            "MATERIAL_MIGRATION_APP_RECOVERY_NETWORK",
        )
    _check(
        published_port(stack, f"{prefix}-objects", 8333) == OBJECT_PORT,
        "MATERIAL_MIGRATION_APP_RECOVERY_OBJECT_PORT",
    )
    for role in ("api", "worker", "beat", "frontend"):
        ident = f"{prefix}-{role}"
        _check(
            ("container", ident) not in stack.resources and stack.owned("container", ident) is None,
            "MATERIAL_MIGRATION_APP_RECOVERY_APP_PRESENT",
        )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", PORT))
    observed = stack.docker(
        "MATERIAL_MIGRATION_APP_RECOVERY_SYSTEM_ID",
        "exec",
        f"{prefix}-pg",
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" PGOPTIONS="-c default_transaction_read_only=on" '
        "psql -h 127.0.0.1 -U ac_test -d ac_test -XAt -v ON_ERROR_STOP=1 "
        '-c "SELECT system_identifier::text FROM pg_control_system()"',
        log_output=False,
    ).stdout.strip()
    _check(observed == system_id, "MATERIAL_MIGRATION_DATABASE_IDENTITY_MISMATCH")
    return network


def _app_recovery_plan(images, files, failed_path, failed):
    import offline_demo
    from aftercare.business.rules import digest
    from verify_chat_backup_restore import private_json

    original = failed["plan"]
    previous = original["recovery"]
    state = private_json(STATE)
    migration = failed["migration"]
    _check(
        failed["status"] == "FAILED"
        and failed["failure_code"] == "AttributeError"
        and failed["plan_sha256"] == digest(original)
        and original["scope"] == "ISOLATED_TEST_PUBLIC_MIGRATION_AND_HISTORICAL_EVAL_CLONE"
        and original["files"] == files
        and original["images"] == images
        and state["status"] == "MIGRATING"
        and state["profile"] == "test"
        and state["backend_image"] == OLD
        and state["frontend_image"] == OLD_FRONTEND
        and failed["owned_url"] == state["url"] == URL
        and failed["stack_id"] == previous["stack_id"] == state["run_id"]
        and failed["system_identifier"] == previous["system_identifier"]
        and migration["revision"] == NEW_REV
        and migration["old_review_observations_sql_null"]
        and migration["old_public_projection_unchanged"]
        and migration["old_eval_full_fingerprint_unchanged"]
        and migration["legacy_eval_clone_observations_sql_null"]
        and not any(key in failed for key in ("typed_review", "final_readback")),
        "MATERIAL_MIGRATION_APP_RECOVERY_REPORT_REJECTED",
    )
    stack = offline_demo.restore(state)
    baseline_path = stack.directory / "material-review-legacy-baseline.json"
    baseline = private_json(baseline_path)
    prior_dir = stack.directory / RECOVERY_INPUT
    orig_dir = stack.directory / LEGACY_INPUT
    prior_files = {**files, "verifier.py": original["script_sha256"]}
    _check(
        _hash(baseline_path) == failed["private_legacy_baseline_sha256"]
        and baseline["source_namespace"] == failed["legacy_eval_schema"]
        and baseline["source_scope_sha256"] == migration["old_source_scope_sha256"]
        and not (stack.directory / APP_RECOVERY_INPUT).exists()
        and _hash(Path(previous["failed_report"])) == previous["failed_report_sha256"]
        and str(orig_dir) == previous["original_staged_directory"]
        and all(
            _hash(orig_dir / name) == checksum
            for name, checksum in previous["original_staged_files"].items()
        )
        and all(_hash(prior_dir / name) == checksum for name, checksum in prior_files.items()),
        "MATERIAL_MIGRATION_APP_RECOVERY_ARTIFACT_CHANGED",
    )
    return {
        "mode": "APPLICATION_ONLY",
        "failed_report": str(failed_path.resolve()),
        "failed_report_sha256": _hash(failed_path),
        "stack_id": stack.run_id,
        "system_identifier": failed["system_identifier"],
        "network": _app_recovery_topology(stack, failed["system_identifier"]),
        "original_staged_directory": str(prior_dir),
        "original_staged_files": prior_files,
        "baseline_sha256": _hash(baseline_path),
        "legacy_case_id": failed["legacy_case_id"],
        "legacy_review_id": failed["legacy_review_id"],
        "migration": migration,
        "earliest_failure": previous,
    }


def _foreign_sidecar(stack):
    return stack.runtime / "material-review-other-customer.json"


def _clients(stack, managers):
    import httpx
    from verify_chat_backup_restore import private_json
    from verify_chat_p1_http import read

    original = private_json(stack.credentials_path)
    foreign = private_json(_foreign_sidecar(stack))
    selected = {
        name: [row for row in original if row["role"] == name] for name in ("customer", "support")
    }
    _check(
        all(len(rows) == 1 for rows in selected.values())
        and foreign["role"] == "customer"
        and foreign["customer_id"] == FOREIGN_CUSTOMER
        and selected["customer"][0]["customer_id"] != foreign["customer_id"],
        "MATERIAL_MIGRATION_REQUIRED_ACTORS_MISSING",
    )
    result = {}
    for name, row in {
        **{name: rows[0] for name, rows in selected.items()},
        "other_customer": foreign,
    }.items():
        client = managers.enter_context(
            httpx.Client(
                base_url=URL + "/api/",
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                headers={"Authorization": f"Bearer {row['token']}"},
            )
        )
        actor = read(client, "me")
        _check(
            all(actor.get(key) == row.get(key) for key in ("role", "user_id", "customer_id")),
            "MATERIAL_MIGRATION_ACTOR_BINDING_MISMATCH",
        )
        result[name] = client
    return result


def _case(clients, order, marker):
    from verify_chat_p1_http import call, png, read, with_key

    owner, support = clients["customer"], clients["support"]
    line = order["items"][0]
    created = call(
        owner,
        "POST",
        "cases",
        201,
        headers=with_key(marker + "-case"),
        json={
            "order_id": order["id"],
            "intent": "damaged",
            "description": "隔离迁移验收合成商品损坏。",
            "line_ids": [line["line_id"]],
            "expected_version": order["version"],
        },
    ).json()
    ident = created["id"]
    material_ids, objects = [], []
    for index, (kind, color) in enumerate((("ITEM", (10, 40, 90)), ("PARCEL", (60, 90, 15)))):
        original = png(color)
        body = call(
            owner,
            "POST",
            f"cases/{ident}/materials",
            201,
            headers=with_key(f"{marker}-upload-{index}"),
            data={
                "kind": kind,
                "expected_version": read(support, f"cases/{ident}")["case"]["version"],
            },
            files={"file": ("synthetic.png", original, "image/png")},
        ).json()
        material_ids.append(body["id"])
        content_path = f"cases/{ident}/materials/{body['id']}/content"
        _check(
            call(support, "GET", content_path, 200).content == original
            and call(owner, "GET", content_path, 200).content == original,
            "MATERIAL_MIGRATION_ORIGINAL_BYTES_MISMATCH",
        )
        call(clients["other_customer"], "GET", content_path, 404)
        objects.append(
            {"id": body["id"], "sha256": sha256(original).hexdigest(), "size": len(original)}
        )
    context = read(support, f"cases/{ident}")
    return {
        "id": ident,
        "line": line["line_id"],
        "quantity": line["quantity"],
        "version": context["case"]["version"],
        "objects": objects,
        "materials": material_ids,
    }


def _post_review(clients, case, key, *, observations=None, expected=201):
    from verify_chat_p1_http import call, with_key

    body = {
        "expected_version": case["version"],
        "decision": "APPROVED",
        "notes": "隔离迁移验收。备注不生成观察，也未批准付款。",
    }
    if observations is not None:
        body["observations"] = observations
    return call(
        clients["support"],
        "POST",
        f"cases/{case['id']}/material-reviews",
        expected,
        headers=with_key(key),
        json=body,
    ).json()


def _verify_old_public(stack, order):
    with ExitStack() as managers:
        clients = _clients(stack, managers)
        case = _case(clients, order, "material-migration-old")
        review = _post_review(clients, case, "material-migration-old-review")
        _check(
            review == _post_review(clients, case, "material-migration-old-review")
            and "observations" not in review
            and bool(review["notes"]),
            "MATERIAL_MIGRATION_LEGACY_REVIEW_REQUIRED",
        )
        return {"case": case, "review": review}


def _verify_new_public(stack, old, order):
    from verify_chat_p1_http import read

    with ExitStack() as managers:
        clients = _clients(stack, managers)
        old_case = old["case"]
        old_data = read(clients["support"], f"cases/{old_case['id']}")
        _check(
            old_data["reviews"] == [{**old["review"], "effective": True}]
            and _post_review(clients, old_case, "material-migration-old-review") == old["review"],
            "MATERIAL_MIGRATION_LEGACY_API_CHANGED",
        )
        case = _case(clients, order, "material-migration-new")
        common = {
            "finding": "VISIBLE_DAMAGE",
            "line_id": case["line"],
            "affected_quantity": case["quantity"],
        }
        observed = [{"material_id": ident, **common} for ident in case["materials"]]
        for index, invalid in enumerate(
            (
                [{**observed[0], "line_id": "outside-scope"}],
                [{**observed[0], "affected_quantity": case["quantity"] + 1}],
                [{**observed[0], "source": "HUMAN_MATERIAL_REVIEW"}],
            )
        ):
            _post_review(
                clients,
                case,
                f"material-migration-invalid-{index}",
                observations=invalid,
                expected=422,
            )
        key = "material-migration-new-review"
        review = _post_review(clients, case, key, observations=observed)
        expected = sorted(
            [{**item, "source": "HUMAN_MATERIAL_REVIEW"} for item in observed],
            key=lambda item: item["material_id"],
        )
        _check(
            review["observations"] == expected
            and _post_review(clients, case, key, observations=list(reversed(observed))) == review,
            "MATERIAL_MIGRATION_TYPED_REPLAY_MISMATCH",
        )
        changed = [{**observed[0], "affected_quantity": None}]
        conflict = _post_review(clients, case, key, observations=changed, expected=409)
        _check(
            conflict["detail"]["code"] == "IDEMPOTENCY_CONFLICT",
            "MATERIAL_MIGRATION_REPLAY_CONFLICT_MISSING",
        )
        path = f"cases/{case['id']}"
        support = read(clients["support"], path)
        customer = read(clients["customer"], path)
        _check(
            support["reviews"][0]["observations"] == expected
            and read(clients["support"], path + "/material-reviews")["items"][0]["observations"]
            == expected
            and "observations" not in customer["reviews"][0]
            and "observations"
            not in read(clients["customer"], path + "/material-reviews")["items"][0]
            and support["ledger"] == [],
            "MATERIAL_MIGRATION_VIEW_SCOPE_MISMATCH",
        )
        return {"case": case, "review_id": review["id"], "observation_count": len(expected)}


def _worker(action, payload):
    root = Path("/acceptance")
    sys.path.insert(0, str(root / "backend/tests/business"))
    from uuid import UUID

    import chat_eval_development_fixtures as fixture
    from aftercare import backup
    from aftercare.business.models import Customer, MaterialReview
    from aftercare.business.rules import digest
    from aftercare.business.storage import S3ObjectStore, verify_object
    from aftercare.config import load_settings
    from aftercare.db import User, guarded_engine
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.runner import _populate
    from sqlalchemy import inspect, select, text
    from sqlalchemy.orm import Session

    _check(
        action in {"foreign", "baseline", "migrate", "app-recovery-readback", "final"}
        and _hash(Path(__file__)) == payload["script_sha256"],
        "MATERIAL_MIGRATION_WORKER_SCRIPT_CHANGED",
    )
    settings = load_settings("test")
    _check(
        settings.container
        and settings.chat_investigation_enabled
        and not any(
            (
                settings.model_enabled,
                settings.embedding_enabled,
                settings.chat_analysis_enabled,
                settings.chat_vision_enabled,
            )
        ),
        "MATERIAL_MIGRATION_OFFLINE_WORKER_REQUIRED",
    )
    engine = guarded_engine(settings, test_only=True)
    store = S3ObjectStore(settings)
    try:
        with engine.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            identity = db.execute(
                text(
                    "SELECT current_database(),current_user,session_user,current_setting('cluster_name'),system_identifier::text FROM pg_control_system()"
                )
            ).one()
        _check(
            tuple(identity)
            == ("ac_test", "ac_test", "ac_test", "aftercare-v2-test", payload["system_id"]),
            "MATERIAL_MIGRATION_DATABASE_IDENTITY_MISMATCH",
        )
        if action == "foreign":
            user_id = payload["foreign_user_id"]
            hashed = payload["foreign_token_hash"]
            _check(
                str(UUID(user_id)) == user_id and re.fullmatch(r"[0-9a-f]{64}", hashed),
                "MATERIAL_MIGRATION_FOREIGN_FIXTURE_INVALID",
            )
            with Session(engine) as db, db.begin():
                owner = db.get(User, payload["owner_user_id"])
                _check(
                    owner is not None
                    and owner.enabled
                    and owner.role == "customer"
                    and owner.customer_id != FOREIGN_CUSTOMER
                    and db.get(Customer, FOREIGN_CUSTOMER) is not None
                    and db.scalar(
                        select(User.id).where(User.customer_id == FOREIGN_CUSTOMER).limit(1)
                    )
                    is None
                    and db.scalar(text("SELECT version_num FROM public.alembic_version")) == OLD_REV
                    and not db.scalar(text("SELECT EXISTS(SELECT 1 FROM public.business_cases)")),
                    "MATERIAL_MIGRATION_FOREIGN_FIXTURE_PRECONDITION",
                )
                db.add(
                    User(
                        id=user_id,
                        token_hash=hashed,
                        role="customer",
                        customer_id=FOREIGN_CUSTOMER,
                        display_name="Material migration test customer",
                        enabled=True,
                    )
                )
            return {"user_id": user_id, "customer_id": FOREIGN_CUSTOMER, "role": "customer"}
        key = sentinel_key(payload["stack_id"])
        if action == "baseline":
            from aftercare.business.errors import BusinessError

            try:
                store.get(key, 64)
            except BusinessError as error:
                _check(
                    error.code == "MATERIAL_OBJECT_MISSING", "MATERIAL_MIGRATION_MARKER_UNVERIFIED"
                )
            else:
                raise ValueError("MATERIAL_MIGRATION_MARKER_ALREADY_EXISTS")
            sentinel = token_bytes(64)
            store.put(key, sentinel, "application/octet-stream")
            verify_object(store.get(key, 64), 64, sha256(sentinel).hexdigest())
        else:
            sentinel = store.get(key, 64)
            verify_object(sentinel, 64, payload["baseline"]["sentinel_sha256"])
        attestation = PgAttestation(
            payload["stack_id"], identity[4], store.bucket, sha256(sentinel).hexdigest(), 64
        )
        pack = fixture.load_development_cases(root)
        source_hash = digest([pack, CASE])
        with open_pg_runtime(settings, attestation, CAMPAIGN) as runtime:
            with runtime.source(CASE, 1, source_hash, require_existing=action != "baseline") as (
                source_engine,
                storage,
                fresh,
            ):
                _check(fresh == (action == "baseline"), "MATERIAL_MIGRATION_SOURCE_STATE_CHANGED")
                case = fixture.replay_development_case(
                    source_engine,
                    storage,
                    settings,
                    root,
                    CASE,
                    fresh=fresh,
                )
            frozen = runtime.freeze(case)

        def snapshot():
            with engine.connect() as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                revision = db.scalar(text("SELECT version_num FROM public.alembic_version"))
                columns = {
                    col["name"]
                    for col in inspect(db).get_columns("business_material_reviews", schema="public")
                }
                normalized_review = (
                    db.execute(
                        text(
                            "SELECT to_jsonb(t)-'observations' FROM public.business_material_reviews t "
                            "WHERE id IS DISTINCT FROM :typed ORDER BY id"
                        ),
                        {"typed": payload.get("typed_review_id")},
                    )
                    .scalars()
                    .all()
                )
                db_hashes = {
                    key: val.model_dump()
                    for key, val in backup._fingerprints(db).items()
                    if key not in {"public.alembic_version", "public.business_material_reviews"}
                }
                objects = backup._materials(db)
                eval_columns = {
                    col["name"]
                    for col in inspect(db).get_columns(
                        "business_material_reviews",
                        schema=frozen.record["source_namespace"],
                    )
                }
                counts = {
                    table: db.scalar(text(f'SELECT count(*) FROM "public"."{table}"'))
                    for table in (
                        "agent_model_attempts",
                        "chat_model_attempts",
                        "intake_model_audits",
                        "business_executions",
                        "business_approvals",
                        "business_simulated_payments",
                    )
                }
                null_legacy = "observations" in columns and db.scalar(
                    text(
                        "SELECT observations IS NULL FROM public.business_material_reviews WHERE id=:id"
                    ),
                    {"id": payload["legacy_review_id"]},
                )
                case_tables = {
                    "business_cases": "id",
                    "business_materials": "case_id",
                    "business_object_uploads": "case_id",
                    "business_events": "case_id",
                }
                historical = {}
                for table, field in case_tables.items():
                    rows = db.scalars(
                        text(
                            f"SELECT to_jsonb(t) FROM {backup._qualified('public', table)} t "
                            f"WHERE {field} IS DISTINCT FROM :typed_case"
                        ),
                        {"typed_case": payload.get("typed_case_id")},
                    )
                    historical[table] = digest(sorted(digest(row) for row in rows))
                keys = [
                    f"material-migration-new-{suffix}"
                    for suffix in ("case", "upload-0", "upload-1", "review")
                ]
                idem = db.scalars(
                    text(
                        "SELECT to_jsonb(t) FROM public.business_idempotency t WHERE key NOT IN (:a,:b,:c,:d)"
                    ),
                    dict(zip(("a", "b", "c", "d"), keys, strict=True)),
                )
                historical["business_idempotency"] = digest(sorted(digest(row) for row in idem))
            for item in objects:
                verify_object(store.get(item.key, item.size), item.size, item.sha256)
            return {
                "revision": revision,
                "public_columns": sorted(columns),
                "eval_columns": sorted(eval_columns),
                "database": db_hashes,
                "legacy_review_digest": digest(normalized_review),
                "objects": [item.model_dump() for item in objects],
                "finance_model_counts": counts,
                "historical_public_case_rows": historical,
                "old_review_observations_sql_null": bool(null_legacy),
            }

        observed = snapshot()
        _check(
            "observations" not in observed["eval_columns"]
            and not any(observed["finance_model_counts"].values()),
            "MATERIAL_MIGRATION_OLD_EVAL_CHANGED",
        )
        if action == "baseline":
            _check(
                observed["revision"] == OLD_REV
                and "observations" not in observed["public_columns"],
                "MATERIAL_MIGRATION_WRONG_BASELINE",
            )
            return {
                **observed,
                "source_namespace": frozen.record["source_namespace"],
                "source_scope_sha256": frozen.record["scope_sha256"],
                "sentinel_sha256": attestation.sentinel_sha256,
            }

        baseline = payload["baseline"]
        if action == "app-recovery-readback":
            clone = payload["clone_schema"]
            _check(
                re.fullmatch(r"eval_[0-9a-f]{32}", clone) and clone != baseline["source_namespace"],
                "MATERIAL_MIGRATION_APP_RECOVERY_CLONE_INVALID",
            )
            qualified = backup._qualified(clone, "business_material_reviews")
            with engine.connect() as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                schemas = set(
                    db.scalars(
                        text(
                            "SELECT nspname FROM pg_namespace WHERE nspname ~ '^eval_[0-9a-f]{32}$'"
                        )
                    )
                )
                clone_review = db.execute(
                    text(f"SELECT notes, observations IS NULL FROM {qualified}")
                ).one()
                legacy = db.execute(
                    text(
                        "SELECT c.order_id, c.version, i.response, i.request_hash "
                        "FROM public.business_cases c JOIN public.business_idempotency i "
                        "ON i.scope = 'material-review:' || c.id "
                        "WHERE c.id=:id AND i.key='material-migration-old-review'"
                    ),
                    {"id": payload["legacy_case_id"]},
                ).one()
            order, version, review, request_hash = legacy
            eligible = [
                value
                for value in (version, version - 1)
                if digest([review["decision"], review["notes"], value]) == request_hash
            ]
            cloned = {
                key: val
                for key, val in observed["database"].items()
                if not key.startswith(clone + ".")
            }
            objects = {item["key"]: item for item in observed["objects"]}
            _check(
                schemas == {baseline["source_namespace"], clone}
                and tuple(clone_review)
                == (frozen.scope.frozen_context["reviews"][0]["notes"], True)
                and observed["revision"] == NEW_REV
                and observed["old_review_observations_sql_null"]
                and observed["public_columns"]
                == sorted([*baseline["public_columns"], "observations"])
                and cloned == baseline["database"]
                and all(objects.get(item["key"]) == item for item in baseline["objects"])
                and all(
                    observed[k] == baseline[k]
                    for k in (
                        "eval_columns",
                        "legacy_review_digest",
                        "finance_model_counts",
                        "historical_public_case_rows",
                    )
                )
                and frozen.record["scope_sha256"] == baseline["source_scope_sha256"]
                and review["id"] == payload["legacy_review_id"]
                and len(eligible) == 1,
                "MATERIAL_MIGRATION_APP_RECOVERY_DB_CHANGED",
            )
            return {"legacy_order": order, "legacy_version": eligible[0], "legacy_review": review}
        if action == "final":
            with engine.connect() as db, db.begin():
                db.execute(text("SET TRANSACTION READ ONLY"))
                raw = db.execute(
                    text(
                        "SELECT observations FROM public.business_material_reviews WHERE id=:id AND case_id=:case_id"
                    ),
                    {"id": payload["typed_review_id"], "case_id": payload["typed_case_id"]},
                ).scalar_one()
                schema_names = list(
                    db.scalars(
                        text(
                            "SELECT nspname FROM pg_namespace WHERE nspname ~ '^eval_[0-9a-f]{32}$'"
                        )
                    )
                )
                finance_and_model = 0
                for schema in schema_names:
                    for table in (
                        "agent_model_attempts",
                        "chat_model_attempts",
                        "business_executions",
                        "business_approvals",
                        "business_simulated_payments",
                    ):
                        finance_and_model += db.scalar(
                            text(f"SELECT count(*) FROM {backup._qualified(schema, table)}")
                        )
            original_scope = baseline["source_namespace"] + "."
            touched = {"public." + table for table in baseline["historical_public_case_rows"]}

            def unchanged_public(data):
                return {
                    key: value
                    for key, value in data["database"].items()
                    if key.startswith("public.") and key not in touched
                }

            allowed_delta = {
                key
                for key in touched
                if observed["database"].get(key) != baseline["database"].get(key)
            }
            seen = {item["key"]: item for item in observed["objects"]}
            _check(
                observed["revision"] == NEW_REV
                and set(schema_names) == {baseline["source_namespace"], payload["clone_schema"]}
                and len(raw) == 2
                and {item["source"] for item in raw} == {"HUMAN_MATERIAL_REVIEW"}
                and finance_and_model == 0
                and observed["old_review_observations_sql_null"]
                and observed["legacy_review_digest"] == baseline["legacy_review_digest"]
                and observed["historical_public_case_rows"]
                == baseline["historical_public_case_rows"]
                and unchanged_public(observed) == unchanged_public(baseline)
                and {
                    "public.business_cases",
                    "public.business_materials",
                    "public.business_idempotency",
                    "public.business_events",
                }
                <= allowed_delta
                and all(
                    observed["database"].get(key) == value
                    for key, value in baseline["database"].items()
                    if key.startswith(original_scope)
                )
                and all(seen.get(item["key"]) == item for item in baseline["objects"]),
                "MATERIAL_MIGRATION_FINAL_LEDGER_MISMATCH",
            )
            return {
                "typed_review_native": True,
                "legacy_eval_still_unchanged": True,
                "verified_object_count": len(observed["objects"]),
                "finance_model_rows": finance_and_model,
                "public_case_delta_tables": sorted(
                    allowed_delta | {"public.business_material_reviews"}
                ),
            }
        _check(
            frozen.record["source_namespace"] == baseline["source_namespace"]
            and frozen.record["scope_sha256"] == baseline["source_scope_sha256"]
            and all(observed[k] == baseline[k] for k in observed),
            "MATERIAL_MIGRATION_BASELINE_DRIFT",
        )
        migration = subprocess.run(
            ["alembic", "upgrade", NEW_REV],
            cwd="/workspace/backend",
            capture_output=True,
            check=False,
            timeout=90,
        )
        _check(migration.returncode == 0, "MATERIAL_MIGRATION_ALEMBIC_FAILED")
        current = snapshot()
        stable = {
            "eval_columns",
            "database",
            "legacy_review_digest",
            "objects",
            "finance_model_counts",
            "historical_public_case_rows",
        }
        _check(
            current["revision"] == NEW_REV
            and current["old_review_observations_sql_null"]
            and set(current["public_columns"]) == set(baseline["public_columns"]) | {"observations"}
            and all(current[k] == baseline[k] for k in stable),
            "MATERIAL_MIGRATION_RESTORED_STATE_MISMATCH",
        )
        with open_pg_runtime(settings, attestation, CAMPAIGN) as runtime:
            with runtime.source(CASE, 1, source_hash, require_existing=True) as (
                source_engine,
                source_storage,
                fresh,
            ):
                _check(not fresh, "MATERIAL_MIGRATION_CLONE_SOURCE_CHANGED")
                reopened_case = fixture.replay_development_case(
                    source_engine,
                    source_storage,
                    settings,
                    root,
                    CASE,
                    fresh=False,
                )
            reopened = runtime.freeze(reopened_case)
            _check(reopened.scope == frozen.scope, "MATERIAL_MIGRATION_CLONE_SOURCE_CHANGED")
            with runtime.source(
                "LEGACY-CLONE", 1, digest([source_hash, "migration-compatibility"])
            ) as (target_engine, target_storage, fresh):
                _check(fresh, "MATERIAL_MIGRATION_CLONE_ALREADY_EXISTS")
                _populate(reopened, target_engine, target_storage)
                with target_engine.connect() as db, db.begin():
                    db.execute(text("SET TRANSACTION READ ONLY"))
                    review = MaterialReview.__table__
                    records = list(db.execute(select(review)).mappings())
                    null = db.scalar(select(review.c.observations.is_(None)))
                _check(
                    len(records) == 1
                    and null is True
                    and records[0]["notes"] == frozen.scope.frozen_context["reviews"][0]["notes"],
                    "MATERIAL_MIGRATION_CLONE_REVIEW_MISMATCH",
                )
                for item, key, content in (*frozen.images, *frozen.materials):
                    _check(
                        target_storage.get(key, item["size"]) == content,
                        "MATERIAL_MIGRATION_CLONE_OBJECT_MISMATCH",
                    )
                clone_schema = target_engine.get_execution_options()["schema_translate_map"][None]
        return {
            "revision": current["revision"],
            "old_review_observations_sql_null": True,
            "old_public_projection_unchanged": True,
            "old_eval_full_fingerprint_unchanged": True,
            "old_source_scope_sha256": frozen.record["scope_sha256"],
            "unchanged_database_table_count": len(current["database"]),
            "verified_object_count": len(current["objects"]),
            "legacy_eval_clone_observations_sql_null": True,
            "legacy_eval_clone_schema": clone_schema,
        }
    finally:
        engine.dispose()


def _job(stack, state, network, image, stage, staged, payload):
    import offline_demo
    from verify_fresh_bootstrap import LABEL

    ident = f"aftercare-fresh-{stack.run_id}-material-{stage}-verifier"
    _check(stack.owned("container", ident) is None, "MATERIAL_MIGRATION_WORKER_ALREADY_EXISTS")
    stack.resources.append(("container", ident))
    state["resources"] = stack.resources
    offline_demo.save(state)
    try:
        result = stack.docker(
            "MATERIAL_MIGRATION_" + stage.upper(),
            "run",
            "--rm",
            "--pull=never",
            "--name",
            ident,
            "--label",
            f"{LABEL}={stack.run_id}",
            "--network",
            network,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--mount",
            f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
            "--mount",
            f"type=bind,source={staged},target=/acceptance,readonly",
            "--entrypoint",
            "python",
            image,
            "/acceptance/verifier.py",
            "_worker",
            stage,
            json.dumps(payload),
            timeout=210,
            required=False,
            log_output=False,
        )
        if result is None or result.returncode != 0:
            codes = result.stderr.strip().splitlines() if result is not None else []
            code = codes[-1] if codes else ""
            raise ValueError(
                code
                if re.fullmatch(r"MATERIAL_MIGRATION_[A-Z0-9_]+", code)
                else "MATERIAL_MIGRATION_WORKER_FAILED"
            )
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        owned = stack.owned("container", ident)
        _check(owned is not False, "MATERIAL_MIGRATION_WORKER_OWNER_MISMATCH")
        if owned:
            stack.docker("MATERIAL_MIGRATION_REMOVE_VERIFIER", "rm", "-f", ident)
        _check(
            stack.owned("container", ident) is None,
            "MATERIAL_MIGRATION_VERIFIER_NOT_REMOVED",
        )
        stack.resources.remove(("container", ident))
        offline_demo.save(state)


def _replace_apps(stack, state, network, images, *, start):
    import offline_demo
    from verify_chat_p2_offline import owned_container

    if not start:
        for role in ("frontend", "beat", "worker", "api"):
            ident = owned_container(stack, role)
            stack.docker("MATERIAL_MIGRATION_REMOVE_OLD_APP", "rm", "-f", ident, log_output=False)
            stack.resources.remove(("container", ident))
            offline_demo.save(state)
        return
    common = (
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "AFTERCARE_CONTAINER=1",
        "--env",
        "AFTERCARE_PROFILE=test",
        "--env",
        "AFTERCARE_CONTAINER_MODEL_MODE=offline",
        "--mount",
        f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
    )

    def run(image, suffix, net, options=(), command=()):
        ident = f"aftercare-fresh-{stack.run_id}-{suffix}"
        _check(
            stack.owned("container", ident) is None, "MATERIAL_MIGRATION_APP_NAME_ALREADY_PRESENT"
        )
        try:
            return stack.run(image, suffix, net, options, command)
        finally:
            offline_demo.save(state)

    api = run(images["new_backend"], "api", network, (*common, "--network-alias", "api-test"))
    stack.wait(
        "MATERIAL_MIGRATION_API_READY",
        "exec",
        api,
        "python",
        "-c",
        "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/readyz',timeout=4).close()",
    )
    worker = run(
        images["new_backend"],
        "worker",
        network,
        common,
        (
            "celery",
            "-A",
            "aftercare.worker",
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=WARNING",
        ),
    )
    stack.wait(
        "MATERIAL_MIGRATION_WORKER_READY",
        "exec",
        worker,
        "celery",
        "-A",
        "aftercare.worker",
        "inspect",
        "--timeout",
        "2",
        "ping",
    )
    run(
        images["new_backend"],
        "beat",
        network,
        common,
        (
            "celery",
            "-A",
            "aftercare.worker",
            "beat",
            "--loglevel=WARNING",
            "--schedule=/tmp/demo-beat",
        ),
    )
    ingress = f"aftercare-fresh-{stack.run_id}-ingress"
    _check(stack.owned("network", ingress) is True, "MATERIAL_MIGRATION_INGRESS_UNOWNED")
    front = run(
        images["new_frontend"],
        "frontend",
        ingress,
        (
            "-p",
            f"127.0.0.1:{PORT}:8080",
            "--env",
            "AFTERCARE_UPSTREAM=api-test:8000",
        ),
    )
    stack.docker(
        "MATERIAL_MIGRATION_FRONTEND_INTERNAL",
        "network",
        "connect",
        "--alias",
        "frontend",
        network,
        front,
    )
    stack.wait(
        "MATERIAL_MIGRATION_FRONTEND_READY",
        "exec",
        api,
        "python",
        "-c",
        "from urllib.request import urlopen; urlopen('http://frontend:8080/api/readyz',timeout=4).close()",
    )
    state.update(
        backend_image=images["new_backend"], frontend_image=images["new_frontend"], status="READY"
    )
    offline_demo.save(state)


def _stage_inputs(stack, plan, directory):
    staged = stack.directory / directory
    staged.mkdir(mode=0o700)
    for name, expected in plan["files"].items():
        source = ROOT / name
        _check(_hash(source) == expected, "MATERIAL_MIGRATION_SOURCE_FILE_CHANGED")
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o400)
    verifier = staged / "verifier.py"
    verifier.write_bytes(Path(__file__).read_bytes())
    verifier.chmod(0o400)
    _check(_hash(verifier) == plan["script_sha256"], "MATERIAL_MIGRATION_VERIFIER_CHANGED")
    return staged


def _recovery_order(stack, old):
    from verify_chat_p1_http import read

    with ExitStack() as managers:
        clients = _clients(stack, managers)
        orders = [
            row
            for row in read(clients["customer"], "orders")["items"]
            if row["status"] == "DELIVERED"
            and row["payment_status"] == "CONFIRMED"
            and row["items"]
            and row["paid_cents"] > 0
        ]
    _check(
        len(orders) >= 2 and orders[0]["id"] == old["order_id"],
        "MATERIAL_MIGRATION_RECOVERY_ORDER_CHANGED",
    )
    return orders[1]


def _resume_apps(stack, state, plan, report, report_path):
    from verify_chat_backup_restore import private_json
    from verify_streaming_compatibility import _save

    recovery = plan["recovery"]
    identity = recovery["system_identifier"]
    _check(
        stack.run_id == recovery["stack_id"]
        and _app_recovery_topology(stack, identity) == recovery["network"],
        "MATERIAL_MIGRATION_APP_RECOVERY_STACK_CHANGED",
    )
    baseline_path = stack.directory / "material-review-legacy-baseline.json"
    _check(
        _hash(baseline_path) == recovery["baseline_sha256"],
        "MATERIAL_MIGRATION_APP_RECOVERY_ARTIFACT_CHANGED",
    )
    baseline = private_json(baseline_path)
    staged = _stage_inputs(stack, plan, APP_RECOVERY_INPUT)
    payload = {
        "stack_id": stack.run_id,
        "system_id": identity,
        "script_sha256": plan["script_sha256"],
        "legacy_case_id": recovery["legacy_case_id"],
        "legacy_review_id": recovery["legacy_review_id"],
        "clone_schema": recovery["migration"]["legacy_eval_clone_schema"],
    }
    report.update(
        stage="APP_RECOVERY_READBACK",
        stack_id=stack.run_id,
        owned_url=URL,
        system_identifier=identity,
        migration=recovery["migration"],
        migration_reexecuted=False,
    )
    _save(report_path, report)
    readback = _job(
        stack,
        state,
        recovery["network"],
        plan["images"]["new_backend"],
        "app-recovery-readback",
        staged,
        {**payload, "baseline": baseline},
    )
    old = {
        "case": {"id": recovery["legacy_case_id"], "version": readback["legacy_version"]},
        "order_id": readback["legacy_order"],
        "review": readback["legacy_review"],
    }
    report["stage"] = "APP_RECOVERY_START"
    _save(report_path, report)
    return _finish(
        stack,
        state,
        recovery["network"],
        plan["images"],
        staged,
        payload,
        baseline,
        old,
        None,
        report,
        report_path,
    )


def _finish(
    stack, state, network, images, staged, payload, baseline, old, next_order, report, report_path
):
    import offline_demo
    from verify_streaming_compatibility import _save

    _replace_apps(stack, state, network, images, start=True)
    _check(
        _topology(stack, images["new_backend"], images["new_frontend"])
        == (network, payload["system_id"])
        and offline_demo.status(state)["healthy_now"],
        "MATERIAL_MIGRATION_UPGRADED_STACK_UNHEALTHY",
    )
    report["stage"] = "TYPED_HTTP"
    _save(report_path, report)
    order = next_order if next_order is not None else _recovery_order(stack, old)
    typed = _verify_new_public(stack, old, order)
    report.update(stage="FINAL_READBACK", typed_review=typed)
    _save(report_path, report)
    final = _job(
        stack,
        state,
        network,
        images["new_backend"],
        "final",
        staged,
        {
            **payload,
            "baseline": baseline,
            "typed_review_id": typed["review_id"],
            "typed_case_id": typed["case"]["id"],
            "clone_schema": report["migration"]["legacy_eval_clone_schema"],
        },
    )
    return {"stack_stays_ready": True, "final_readback": final}


def _execute(plan, report, report_path):
    import offline_demo
    from verify_chat_p1_http import read
    from verify_chat_p3_offline import P3Stack
    from verify_fresh_bootstrap import private_write
    from verify_streaming_compatibility import _private_json, _save

    class MigrationStack(P3Stack):
        def __init__(self, directory):
            super().__init__(directory)
            config_path = self.runtime / "local.json"
            values = _private_json(config_path)
            values["app"].update(chat_analysis_enabled=False, chat_investigation_enabled=True)
            staged_config = config_path.with_suffix(".migration")
            private_write(staged_config, json.dumps(values))
            staged_config.replace(config_path)

    images = plan["images"]
    recovery = plan.get("recovery")
    _check(STATE.exists() == bool(recovery), "MATERIAL_MIGRATION_STACK_STATE_CHANGED")
    with patch.multiple(offline_demo, STATE=STATE, FreshStack=MigrationStack):
        if not recovery:
            offline_demo.start(
                SimpleNamespace(
                    port=PORT,
                    object_port=OBJECT_PORT,
                    profile="test",
                    backend_image=images["old_backend"],
                    frontend_image=images["old_frontend"],
                )
            )
        state = _private_json(STATE)
        stack = offline_demo.restore(state)
        state["resources"] = stack.resources
        if recovery and recovery.get("mode") == "APPLICATION_ONLY":
            return _resume_apps(stack, state, plan, report, report_path)
        network, system_id = _topology(stack, images["old_backend"], images["old_frontend"])
        report.update(
            stage="LEGACY_FIXTURE",
            stack_id=stack.run_id,
            system_identifier=system_id,
            owned_url=URL,
        )
        _save(report_path, report)
        pristine = _pristine(stack, system_id)
        if recovery:
            _check(
                stack.run_id == recovery["stack_id"]
                and system_id == recovery["system_identifier"]
                and pristine == recovery["pristine_readback"],
                "MATERIAL_MIGRATION_RECOVERY_STACK_CHANGED",
            )
        staged = _stage_inputs(stack, plan, RECOVERY_INPUT if recovery else LEGACY_INPUT)
        from aftercare.auth import token_hash

        actors = _private_json(stack.credentials_path)
        owners = [row for row in actors if row["role"] == "customer"]
        _check(
            len(owners) == 1 and owners[0]["customer_id"] != FOREIGN_CUSTOMER,
            "MATERIAL_MIGRATION_OWNER_FIXTURE_INVALID",
        )
        credentials_sha = _hash(stack.credentials_path)
        user_id = str(uuid4())
        token = token_urlsafe(32)
        sidecar = _foreign_sidecar(stack)
        private_write(
            sidecar,
            json.dumps(
                {
                    "role": "customer",
                    "user_id": user_id,
                    "customer_id": FOREIGN_CUSTOMER,
                    "token": token,
                }
            ),
        )
        foreign = _job(
            stack,
            state,
            network,
            images["old_backend"],
            "foreign",
            staged,
            {
                "stack_id": stack.run_id,
                "system_id": system_id,
                "script_sha256": plan["script_sha256"],
                "owner_user_id": owners[0]["user_id"],
                "foreign_user_id": user_id,
                "foreign_token_hash": token_hash(token),
            },
        )
        _check(
            foreign == {"role": "customer", "user_id": user_id, "customer_id": FOREIGN_CUSTOMER}
            and _hash(stack.credentials_path) == credentials_sha,
            "MATERIAL_MIGRATION_FOREIGN_FIXTURE_CHANGED",
        )
        report["foreign_customer_fixture"] = {**foreign, "private_credentials_path": str(sidecar)}
        _save(report_path, report)
        with ExitStack() as managers:
            clients = _clients(stack, managers)
            orders = [
                row
                for row in read(clients["customer"], "orders")["items"]
                if row["status"] == "DELIVERED"
                and row["payment_status"] == "CONFIRMED"
                and row["items"]
                and row["paid_cents"] > 0
            ]
        _check(len(orders) >= 2, "MATERIAL_MIGRATION_TWO_OWNED_ORDERS_REQUIRED")
        old = _verify_old_public(stack, orders[0])
        payload = {
            "stack_id": stack.run_id,
            "system_id": system_id,
            "legacy_review_id": old["review"]["id"],
            "script_sha256": plan["script_sha256"],
        }
        baseline = _job(stack, state, network, images["old_backend"], "baseline", staged, payload)
        baseline_file = stack.directory / "material-review-legacy-baseline.json"
        private_write(baseline_file, json.dumps(baseline))
        report.update(
            stage="MIGRATING",
            old_revision=baseline["revision"],
            legacy_case_id=old["case"]["id"],
            legacy_eval_schema=baseline["source_namespace"],
            legacy_review_id=old["review"]["id"],
            private_legacy_baseline_sha256=_hash(baseline_file),
        )
        _save(report_path, report)
        state["status"] = "MIGRATING"
        offline_demo.save(state)
        _replace_apps(stack, state, network, images, start=False)
        report["migration"] = _job(
            stack,
            state,
            network,
            images["new_backend"],
            "migrate",
            staged,
            {**payload, "baseline": baseline},
        )
        return _finish(
            stack,
            state,
            network,
            images,
            staged,
            payload,
            baseline,
            old,
            orders[1],
            report,
            report_path,
        )


def main():
    import offline_demo
    from aftercare.business.rules import digest
    from verify_streaming_compatibility import _save

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-backend", "old-frontend", "new-backend", "new-frontend"):
        parser.add_argument(f"--{name}-image-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--failed-report", type=Path)
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact plan digest and a new --report")
    images = {
        role: getattr(args, role + "_image_id")
        for role in ("old_backend", "old_frontend", "new_backend", "new_frontend")
    }
    with offline_demo.lifecycle_lock(STATE, shared=not args.execute):
        plan = _plan(images, args.failed_report)
        checksum = digest(plan)
        if not args.execute:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
                )
            )
            return 0
        _check(checksum == args.expected_plan_sha256, "MATERIAL_MIGRATION_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "stage": "STARTING",
            "plan_sha256": checksum,
            "plan": plan,
        }
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            report.update(_execute(plan, report, args.report))
            report.update(status="PASSED", stage="VERIFIED")
        except Exception as error:  # noqa: BLE001 - never print unrestricted errors or credentials
            code = str(error)
            report.update(
                status="FAILED",
                failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
            )
        finally:
            recovery = plan.get("recovery")
            if recovery:
                try:
                    prior_artifacts = (
                        (recovery, recovery["earliest_failure"])
                        if recovery.get("mode") == "APPLICATION_ONLY"
                        else (recovery,)
                    )
                    preserved = all(
                        _hash(Path(prior["failed_report"])) == prior["failed_report_sha256"]
                        and all(
                            _hash(Path(prior["original_staged_directory"]) / name) == checksum
                            for name, checksum in prior["original_staged_files"].items()
                        )
                        for prior in prior_artifacts
                    )
                except OSError:
                    preserved = False
                report["original_failed_artifacts_preserved"] = preserved
                if not preserved:
                    report.update(
                        status="FAILED", failure_code="MATERIAL_MIGRATION_ORIGINAL_STAGE_CHANGED"
                    )
            _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_worker":
        try:
            print(json.dumps(_worker(sys.argv[2], json.loads(sys.argv[3]))))
        except Exception as error:  # noqa: BLE001 - bound worker error channel
            code = str(error)
            print(
                code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__, file=sys.stderr
            )
            raise SystemExit(1) from None
    else:
        try:
            raise SystemExit(main())
        except (ValueError, RuntimeError, OSError):
            print("MATERIAL_MIGRATION_PREFLIGHT_BLOCKED", file=sys.stderr)
            raise SystemExit(1) from None
