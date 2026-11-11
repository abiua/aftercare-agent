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
from secrets import token_bytes
from types import SimpleNamespace
from unittest.mock import patch

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


def _plan(images):
    from verify_chat_p5_pg import FILES

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
    return {
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
    }


def _topology(stack, backend, frontend):
    import verify_chat_p5_pg as pg

    with patch.multiple(pg, PORT=PORT, OBJECT_PORT=OBJECT_PORT):
        return pg._topology(stack, {pg.BACKEND: backend, pg.FRONTEND: frontend})


def _clients(stack, managers):
    from verify_chat_backup_restore import private_json
    from verify_chat_p1_http import actor_clients

    private_json(stack.credentials_path)
    return actor_clients(URL + "/api/", stack.credentials_path, managers)


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
    import chat_eval_development_fixtures as fixture
    from aftercare import backup
    from aftercare.business.models import MaterialReview
    from aftercare.business.rules import digest
    from aftercare.business.storage import S3ObjectStore, verify_object
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.runner import _populate
    from sqlalchemy import inspect, select, text

    _check(
        action in {"baseline", "migrate", "final"}
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
    _check(not STATE.exists(), "MATERIAL_MIGRATION_STACK_ALREADY_EXISTS")
    with patch.multiple(offline_demo, STATE=STATE, FreshStack=MigrationStack):
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
        network, system_id = _topology(stack, images["old_backend"], images["old_frontend"])
        report.update(
            stage="LEGACY_FIXTURE",
            stack_id=stack.run_id,
            system_identifier=system_id,
            owned_url=URL,
        )
        _save(report_path, report)
        staged = stack.directory / "material-review-migration-input"
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
        _replace_apps(stack, state, network, images, start=True)
        _check(
            _topology(stack, images["new_backend"], images["new_frontend"]) == (network, system_id)
            and offline_demo.status(state)["healthy_now"],
            "MATERIAL_MIGRATION_UPGRADED_STACK_UNHEALTHY",
        )
        report["stage"] = "TYPED_HTTP"
        _save(report_path, report)
        typed = _verify_new_public(stack, old, orders[1])
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
    args = parser.parse_args()
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires exact plan digest and a new --report")
    images = {
        role: getattr(args, role + "_image_id")
        for role in ("old_backend", "old_frontend", "new_backend", "new_frontend")
    }
    with offline_demo.lifecycle_lock(STATE, shared=not args.execute):
        plan = _plan(images)
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
