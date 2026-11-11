"""Verify input-first chat review against the isolated model-disabled test stack."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import offline_demo
import verify_chat_p3_offline as p3
from aftercare.business.rules import digest
from verify_chat_p1_http import call, message, png, read, request_ids
from verify_chat_p2_offline import _begin, _wait, owned_container
from verify_fresh_bootstrap import ROOT, VerificationError, require
from verify_streaming_compatibility import _private_json, _save

STATE = ROOT / ".runtime" / "offline-browser-demo-p5.json"
PORT, OBJECT_PORT = 5197, 58359
BACKEND = "aftercare-backend:chat-p5-20261111"
FRONTEND = "aftercare-frontend:chat-p5-20261111"
BASE = "evaluation/chat-blind-reviews"
MESSAGE = "两张合成验收照片，分别对应商品和包裹，请核对。"
INITIAL = {"expected_version": 1, "judgment": "UNDETERMINED", "notes": "控制样本，未提交人工判断。"}

RUNTIME = """
import json
import os
from sqlalchemy import text
from sqlalchemy.orm import Session
from aftercare.business.service import BusinessService
from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.evaluation.chat.service import BlindReviewService
from aftercare.evaluation.chat.tables import BlindExposure, BlindInitial, BlindLabels, BlindTask

assert os.environ['AFTERCARE_PROFILE'] == 'test'
settings = load_settings('test')
assert settings.profile == 'test'
assert settings.chat_analysis_enabled and settings.chat_decisions_enabled and settings.chat_investigation_enabled
assert not any((settings.model_enabled, settings.chat_vision_enabled, settings.embedding_enabled))
engine = guarded_engine(settings, test_only=True)
with engine.connect() as conn:
    assert conn.scalar(text('SELECT current_database()')) == 'ac_test'
    assert conn.scalar(text("SELECT current_setting('cluster_name')")) == 'aftercare-v2-test'
"""

ASSIGN = """
from secrets import token_urlsafe
from uuid import uuid4
from aftercare.auth import token_hash
from aftercare.db import User

with Session(engine) as db, db.begin():
    tester = db.get_one(User, reviewer)
    assert tester.enabled and tester.role == 'tester'
    other_token = token_urlsafe(48)
    db.add(User(id=str(uuid4()), role='tester', enabled=True,
                display_name='Unassigned P5 fixture tester', token_hash=token_hash(other_token)))
service = BlindReviewService(BusinessService(engine, settings))
tasks = []
for case_label, snapshot_id in sources:
    args = dict(reviewer_id=reviewer, case_label=case_label, input_snapshot_id=snapshot_id,
                candidate_snapshot_ids=[snapshot_id])
    task = service.assign(**args)
    assert service.assign(**args) == task
    tasks.append(task)
print(json.dumps({'tasks': tasks, 'other_token': other_token}))
"""

AUDIT = """
from aftercare.business.rules import digest
from aftercare.decisions.tables import DecisionSnapshot

with Session(engine) as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    closed, browser = (db.get_one(BlindTask, ident) for ident in task_ids)
    assert closed.reviewer_id == browser.reviewer_id == reviewer
    for task, source_id in zip((closed, browser), source_ids, strict=True):
        source = db.get_one(DecisionSnapshot, source_id)
        assert task.source_sha256 == digest([source.input['input_sha256'], source.id])
        assert task.input_sha256 == digest(task.input)
        assert task.input['policy_coverage'] == 'COMPLETE_CATALOGUE'
        assert len(task.image_objects) == 2 and len(task.candidates) == 1
    assert closed.version == 4 and browser.version == 1
    initial = db.get_one(BlindInitial, closed.id)
    exposure = db.get_one(BlindExposure, closed.id)
    labels = db.get_one(BlindLabels, closed.id)
    assert initial.payload['judgment'] == 'UNDETERMINED'
    assert initial.sha256 == digest([closed.input_sha256, initial.payload])
    assert exposure.initial_sha256 == initial.sha256
    assert exposure.candidates_sha256 == digest(closed.candidates)
    assert exposure.sha256 == digest([closed.id, initial.sha256, exposure.candidates_sha256])
    assert labels.exposure_sha256 == exposure.sha256
    assert labels.sha256 == digest([exposure.sha256, labels.payload])
    assert [row['decision'] for row in labels.payload['items']] == ['UNDETERMINED']
    assert closed.created_at <= initial.created_at <= exposure.created_at <= labels.created_at
    assert all(db.get(table, browser.id) is None for table in (BlindInitial, BlindExposure, BlindLabels))
    counts = {
        'tasks': db.scalar(text('SELECT count(*) FROM eval_chat_blind_tasks')),
        'initials': db.scalar(text('SELECT count(*) FROM eval_chat_blind_initials')),
        'exposures': db.scalar(text('SELECT count(*) FROM eval_chat_blind_exposures')),
        'labels': db.scalar(text('SELECT count(*) FROM eval_chat_blind_labels')),
        'agent_model_attempts': db.scalar(text('SELECT count(*) FROM agent_model_attempts')),
        'chat_model_attempts': db.scalar(text('SELECT count(*) FROM chat_model_attempts')),
        'intake_model_attempts': db.scalar(text('SELECT count(*) FROM intake_model_audits')),
        'executions': db.scalar(text('SELECT count(*) FROM business_executions')),
    }
    print(json.dumps({'counts': counts, 'hashes': [initial.sha256, exposure.sha256, labels.sha256]}))
"""


class P5Stack(p3.P3Stack):
    def __init__(self, directory):
        super().__init__(directory)
        target = self.runtime / "local.json"
        config = _private_json(target)
        config["app"]["chat_investigation_enabled"] = True
        target.write_text(json.dumps(config))


def _plan(images):
    with patch.multiple(
        p3,
        STATE=STATE,
        PORT=PORT,
        OBJECT_PORT=OBJECT_PORT,
        IMAGES={tag: sha.removeprefix("sha256:") for tag, sha in images.items()},
    ):
        verified = p3._preflight()
    return {
        **verified,
        "scope": "P5_REAL_HTTP_TEST_PROFILE_PG_S3_OFFLINE_CONTROLLED_FIXTURES",
        "profile": "test",
        "state_file": STATE.name,
        "chat_investigation_enabled": True,
        "scenario_count": 2,
        "maximum_simulated_executions": 0,
        "model_calls": 0,
        "human_judgments": 0,
        "browser_task_final_stage": "BLIND",
    }


def _open_stack(report):
    images = report["plan"]["images"]
    with patch.multiple(offline_demo, STATE=STATE, FreshStack=P5Stack):
        require(not STATE.exists(), "P5_STATE_ALREADY_EXISTS")
        offline_demo.start(
            SimpleNamespace(
                port=PORT,
                object_port=OBJECT_PORT,
                profile="test",
                backend_image=images[BACKEND],
                frontend_image=images[FRONTEND],
            )
        )
        state = _private_json(STATE)
        stack = offline_demo.restore(state)
    require(
        state["profile"] == "test" and state["url"] == f"http://127.0.0.1:{PORT}",
        "P5_TEST_LOOPBACK_REQUIRED",
    )
    report["isolated_url"] = state["url"]
    return stack


def _client(resources, token):
    return resources.enter_context(
        httpx.Client(
            base_url=f"http://127.0.0.1:{PORT}/api/",
            trust_env=False,
            timeout=10,
            headers={"Authorization": f"Bearer {token}"},
        )
    )


def _source(customer, support, order, report, name):
    ident, case, _ = _begin(
        customer,
        report,
        name,
        order,
        f"订单 {order['id']} 的商品有破损，请帮忙核对。",
    )
    photos = [
        ("synthetic-item.png", png((182, 75, 70))),
        ("synthetic-parcel.png", png((63, 98, 144))),
    ]
    posted = message(
        customer,
        ident,
        MESSAGE,
        request_ids(report, name + "_photos"),
        photos,
    )
    attachments = [
        item
        for row in posted["messages"]
        if row["role"] == "customer"
        for item in row["attachments"]
    ]
    originals = {row["id"]: content for row, (_, content) in zip(attachments, photos, strict=True)}
    base = f"conversations/{ident}"
    _wait(
        customer,
        base,
        lambda data: (data.get("investigation_progress") or {}).get("state") == "PENDING_SUPPORT",
        seconds=90,
    )
    snapshot = read(support, base + "/decisions")["snapshot"]
    require(
        snapshot
        and snapshot["origin"] == "AGENT"
        and snapshot["input"]["frozen_context"]["policy_catalogue"]["documents"]
        and {item["id"] for item in snapshot["input"]["frozen_context"]["attachments"]}
        == set(originals)
        and all(
            item["action"] not in {"REFUND", "REPLACE"} for item in snapshot["candidate"]["options"]
        ),
        "P5_OFFLINE_SOURCE_MISSING",
    )
    require(
        read(customer, base + "/decisions/offer")["offer"] is None, "P5_PUBLIC_OFFER_UNEXPECTED"
    )
    route = next(
        (
            row["payload"]
            for row in read(
                support, f"cases/{case}/runs/{snapshot['original_run_id']}/events?after=0"
            )
            if row["kind"] == "investigation_route"
        ),
        {},
    )
    require(route.get("selected_path") == "PROGRAM", "P5_AUTO_PROGRAM_ROUTE_REQUIRED")
    report["sources"][name] = {
        "conversation_id": ident,
        "case_id": case,
        "snapshot_id": snapshot["id"],
        "original_run_id": snapshot["original_run_id"],
        "selected_path": "PROGRAM",
        "original_images": [
            {"id": image_id, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for image_id, content in originals.items()
        ],
    }
    return snapshot["id"], originals


def _blind(tester, foreign, support, task, originals):
    endpoint = f"{BASE}/{task['id']}"
    response = call(tester, "GET", endpoint, 200)
    initial = response.json()
    frozen = initial["input"]
    require(
        response.headers.get("cache-control") == "no-store"
        and initial["status"] == "BLIND"
        and initial["version"] == 1
        and initial["initial"] is initial["exposure"] is initial["labels"] is None
        and set(initial) == set(task) | {"input", "initial", "exposure", "labels"}
        and set(frozen)
        == {
            "schema_version",
            "intent",
            "messages",
            "images",
            "facts",
            "policies",
            "policy_coverage",
            "policy_context",
        }
        and digest(frozen) == task["input_sha256"]
        and frozen["policy_coverage"] == "COMPLETE_CATALOGUE"
        and frozen["policy_context"]["products"]
        and any(row["text"] == MESSAGE for row in frozen["messages"])
        and set(originals) == {row["id"] for row in frozen["images"]}
        and "selected_for" not in json.dumps(frozen),
        "P5_BLIND_PROJECTION_MISMATCH",
    )
    for actor in (foreign, support):
        call(actor, "GET", endpoint, 404)
        call(actor, "GET", endpoint + "/images/" + next(iter(originals)), 404)
    for image in frozen["images"]:
        response = call(tester, "GET", endpoint + "/images/" + image["id"], 200)
        require(
            response.headers.get("cache-control") == "no-store"
            and response.headers.get("content-type") == "image/png"
            and response.content == originals[image["id"]]
            and len(response.content) == image["size"]
            and hashlib.sha256(response.content).hexdigest() == image["sha256"],
            "P5_ORIGINAL_BYTES_MISMATCH",
        )
    return initial


def _complete(tester, task, report):
    endpoint = f"{BASE}/{task['id']}"
    initial_key = p3._key(report, "p5_initial")
    reveal_key = p3._key(report, "p5_reveal")
    p3._code(
        p3._json(tester, endpoint + "/reveal", 409, reveal_key, {"expected_version": 2}),
        "BLIND_REVIEW_STAGE_CHANGED",
    )
    sealed = p3._json(tester, endpoint + "/initial", 200, initial_key, INITIAL)
    require(
        sealed["status"] == "SEALED"
        and sealed["version"] == 2
        and sealed["exposure"] is sealed["labels"] is None
        and read(tester, endpoint) == sealed,
        "P5_SEALED_READBACK_MISMATCH",
    )
    revealed = p3._json(tester, endpoint + "/reveal", 200, reveal_key, {"expected_version": 2})
    candidates = revealed["exposure"]["candidates"]
    require(
        revealed["status"] == "REVEALED"
        and revealed["version"] == 3
        and revealed["initial"] == sealed["initial"]
        and len(candidates) == 1
        and candidates[0]["options"][0]["rationale"]
        and read(tester, endpoint) == revealed,
        "P5_REVEAL_MISMATCH",
    )
    label = {
        "expected_version": 3,
        "items": [
            {
                "candidate_id": candidates[0]["id"],
                "decision": "UNDETERMINED",
                "notes": "控制样本。",
            },
        ],
    }
    label_key = p3._key(report, "p5_labels")
    labeled = p3._json(tester, endpoint + "/labels", 200, label_key, label)
    require(
        labeled["status"] == "LABELED"
        and labeled["version"] == 4
        and labeled["initial"] == sealed["initial"]
        and labeled["exposure"] == revealed["exposure"]
        and read(tester, endpoint) == labeled
        and p3._json(tester, endpoint + "/initial", 200, initial_key, INITIAL) == sealed
        and p3._json(tester, endpoint + "/reveal", 200, reveal_key, {"expected_version": 2})
        == revealed
        and p3._json(tester, endpoint + "/labels", 200, label_key, label) == labeled,
        "P5_REPLAY_OR_REFRESH_MISMATCH",
    )
    p3._code(
        p3._json(tester, endpoint + "/initial", 409, p3._key(report, "p5_overwrite"), INITIAL),
        "BLIND_REVIEW_STAGE_CHANGED",
    )
    return [labeled[field]["sha256"] for field in ("initial", "exposure", "labels")]


def _execute(report):
    stack = _open_stack(report)
    api = owned_container(stack, "api")
    require(
        stack.json_exec("P5_TEST_PROFILE", api, RUNTIME + "print(json.dumps(settings.profile))")
        == "test",
        "P5_TEST_PROFILE_REQUIRED",
    )
    credentials = {row["role"]: row for row in _private_json(stack.credentials_path)}
    with ExitStack() as resources:
        clients = {
            role: _client(resources, credentials[role]["token"])
            for role in (
                "customer",
                "support",
                "tester",
            )
        }
        customer, support, tester = (clients[role] for role in ("customer", "support", "tester"))
        for role, client in clients.items():
            who = read(client, "me")
            require(
                who["role"] == role and who["user_id"] == credentials[role]["user_id"],
                "P5_ACTOR_MISMATCH",
            )
            require(read(client, "runtime")["mode"] == "offline", "P5_OFFLINE_RUNTIME_REQUIRED")
        require(
            read(tester, "evaluation/capabilities")["chat_blind_review"] is True,
            "P5_CAPABILITY_REQUIRED",
        )
        eligible = [
            row
            for row in read(customer, "orders")["items"]
            if row["status"] == "DELIVERED"
            and row["paid_at"]
            and row["delivered_at"]
            and len(row["items"]) == 1
            and row["paid_cents"] > 0
            and row["refunded_cents"] == 0
        ]
        require(len(eligible) >= 2, "P5_TWO_OWN_ORDERS_REQUIRED")
        report["stage"] = "freeze_real_chat_snapshots"
        names = ("fixture", "browser")
        cases = [
            _source(customer, support, order, report, name)
            for order, name in zip(
                eligible[:2],
                names,
                strict=True,
            )
        ]
        sources = [
            (f"P5-{name.upper()}-01", source_id)
            for name, (source_id, _) in zip(names, cases, strict=True)
        ]
        reviewer = credentials["tester"]["user_id"]
        report["stage"] = "assign_and_verify_blind_input"
        assigned = stack.json_exec(
            "P5_TRUSTED_ASSIGN",
            api,
            RUNTIME + f"reviewer = {reviewer!r}\nsources = {sources!r}\n" + ASSIGN,
        )
        foreign = _client(resources, assigned["other_token"])
        require(
            read(foreign, "me")["role"] == "tester" and not read(foreign, BASE)["items"],
            "P5_FOREIGN_TESTER_SCOPE",
        )
        tasks = assigned["tasks"]
        require(
            {item["id"] for item in read(tester, BASE)["items"]} == {item["id"] for item in tasks},
            "P5_ASSIGNED_TASK_LIST_MISMATCH",
        )
        report["fixture_task_id"], report["browser_task_id"] = (row["id"] for row in tasks)
        for task, (_, originals) in zip(tasks, cases, strict=True):
            _blind(tester, foreign, support, task, originals)
        report["stage"] = "seal_reveal_label_controlled_fixture"
        hashes = _complete(tester, tasks[0], report)
        report["stage"] = "read_only_durable_audit"
        task_ids = [row["id"] for row in tasks]
        source_ids = [row[1] for row in sources]
        audit = stack.json_exec(
            "P5_DURABLE_AUDIT",
            api,
            RUNTIME
            + f"reviewer = {reviewer!r}\ntask_ids = {task_ids!r}\nsource_ids = {source_ids!r}\n"
            + AUDIT,
        )
        require(
            audit["hashes"] == hashes
            and audit["counts"]
            == {
                "tasks": 2,
                "initials": 1,
                "exposures": 1,
                "labels": 1,
                "agent_model_attempts": 0,
                "chat_model_attempts": 0,
                "intake_model_attempts": 0,
                "executions": 0,
            },
            "P5_DURABLE_COUNT_OR_HASH_MISMATCH",
        )
        report["database_audit"] = audit
        report["stage"] = "verified"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image-id", required=True)
    parser.add_argument("--frontend-image-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    images = {BACKEND: args.backend_image_id, FRONTEND: args.frontend_image_id}
    if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", sha) for sha in images.values()):
        parser.error("supply both full immutable sha256 image IDs")
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires the exact preflight digest and a new report path")
    plan = _plan(images)
    checksum = digest(plan)
    if not args.execute:
        print(
            json.dumps(
                {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
            )
        )
        return 0
    require(checksum == args.expected_plan_sha256, "P5_PLAN_CHANGED")
    report = {
        "status": "IN_PROGRESS",
        "plan_sha256": checksum,
        "plan": plan,
        "request_ids": {},
        "sources": {},
    }
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        with offline_demo.lifecycle_lock(STATE):
            _execute(report)
        report["status"] = "PASSED"
    except VerificationError as error:
        report.update(status="FAILED", failure_code=str(error))
    except httpx.TimeoutException:
        report.update(status="FAILED", failure_code="HTTP_TIMEOUT_OUTCOME_UNKNOWN")
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, subprocess.TimeoutExpired):
        report.update(status="FAILED", failure_code="P5_ISOLATED_EXECUTION_FAILED")
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, OSError, ValueError, subprocess.TimeoutExpired):
        print("P5_ACCEPTANCE_BLOCKED")
        raise SystemExit(1) from None
