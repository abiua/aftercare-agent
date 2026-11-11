"""Verify automatic material-review reassessment on the owned 5201 HTTP and Celery stack."""

import argparse
import json
import os
import re
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID

import offline_demo
import verify_material_review_migration as migration
from verify_chat_p1_http import call, message, read, request_ids, with_key
from verify_chat_p2_offline import owned_container
from verify_chat_p4_path import _bridge
from verify_streaming_compatibility import _private_json, _save

STACK_ID = "315cfced887148e3b4221b777887e929"
SYSTEM_ID = "7706834943065174055"
BACKEND_ID = "sha256:7319ccca9d062c57d8b2f91db388473db2cedc594f3661605bc81b6d17c43ea8"
FRONTEND_ID = "sha256:adc940831d70a679ff559aeff461309ad60dd0cb6f21589b640ccd8e86751c98"
ROOT = Path(__file__).resolve().parents[1]
JOURNAL = "p5-material-reassessment-started.json"
CUSTOMER_CLIENT = "other_customer"
CUSTOMER_ID, ORDER_ID, LINE_ID = "C002", "O0288", "L01"
HELPERS = (
    "offline_demo.py",
    "setup_runtime.py",
    "verify_fresh_bootstrap.py",
    "verify_material_review_migration.py",
    "verify_chat_p1_http.py",
    "verify_chat_p2_offline.py",
    "verify_chat_p3_offline.py",
    "verify_chat_p4_path.py",
    "verify_chat_p5_pg.py",
    "verify_chat_backup_restore.py",
    "verify_streaming_compatibility.py",
)
IMAGES = (
    ("ITEM", ROOT / "data/vision-v1/synthetic-cracked-mug.png"),
    ("PARCEL", ROOT / "data/vision-v1/synthetic-torn-parcel.png"),
)


def _check(condition, suffix):
    if not condition:
        raise ValueError(f"P5_MATERIAL_REASSESS_{suffix}")


def _runtime(stack):
    code = """
import json
from aftercare.config import load_settings
s = load_settings('test')
names = ('chat_analysis_enabled','chat_decisions_enabled','chat_investigation_enabled',
         'model_enabled','embedding_enabled','chat_vision_enabled','chat_investigation_protocol')
print(json.dumps({key: getattr(s, key) for key in names}))
"""
    flags = {}
    for role in ("api", "beat", "worker"):
        container = owned_container(stack, role)
        running = stack.docker(
            "P5_MATERIAL_REASSESS_PROCESS",
            "container",
            "inspect",
            "--format",
            "{{.State.Running}} {{.State.Paused}}",
            container,
            log_output=False,
        ).stdout.strip()
        _check(running == "true false", "RUNTIME_NOT_RUNNING")
        flags[role] = stack.json_exec("P5_MATERIAL_REASSESS_FLAGS", container, code)
    api = flags["api"]
    _check(
        all(value == api for value in flags.values())
        and all(
            api[key]
            for key in (
                "chat_analysis_enabled",
                "chat_decisions_enabled",
                "chat_investigation_enabled",
            )
        )
        and not any(
            api[key] for key in ("model_enabled", "embedding_enabled", "chat_vision_enabled")
        ),
        "OFFLINE_FEATURE_FLAGS",
    )
    return flags


def _readback(stack, *, conversation=None, case=None, customer=None):
    arguments = {
        "conversation": str(UUID(conversation)) if conversation else None,
        "case": str(UUID(case)) if case else None,
        "customer": customer,
    }
    return stack.json_exec(
        "P5_MATERIAL_REASSESS_READONLY",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import select, text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.business.rules import eligibility, now
from aftercare.business.models import Case, Order
from aftercare.business.service import BusinessService
from aftercare.intelligence.decision_candidate import load_binding
from aftercare.intelligence.tables import AgentRun
from sqlalchemy.orm import Session
P = {arguments!r}
settings = load_settings('test')
engine = guarded_engine(settings, test_only=True)
with Session(engine) as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    def many(sql): return [dict(row) for row in db.execute(text(sql), P).mappings()]
    def scalar(sql): return db.scalar(text(sql), P)
    assert scalar('SELECT system_identifier::text FROM pg_control_system()') == {SYSTEM_ID!r}
    result = {{'financial_and_provider_counts': {{
        name: scalar(f'SELECT count(*) FROM {{name}}') for name in
        ('business_proposals','business_approvals','business_executions','agent_model_attempts',
         'chat_model_attempts','intake_model_audits')
    }}}}
    if P['customer']:
        clock = now()
        business = BusinessService(engine, settings, clock=lambda: clock)
        candidates = db.scalars(select(Order).where(
            Order.customer_id == P['customer'],
            ~select(Case.id).where(Case.order_id == Order.id).exists(),
        ).order_by(Order.id))
        result['eligible_orders'] = []
        for order in candidates:
            data = business._order_json(db, order)
            if data.get('status') != 'DELIVERED':
                continue
            for line in data['items']:
                transient = Case(intent='damaged', line_ids=[line['line_id']], created_at=clock)
                resolution, policies = business._case_policies(db, transient, data, decision_at=clock)
                balances = business._balances(db, transient, data, business._ledger(db, order))
                def assess(materials, review):
                    return eligibility(
                        case={{'intent': 'damaged', 'line_ids': transient.line_ids, 'created_at': clock.isoformat()}},
                        order=data, policies=policies, materials=materials, review=review,
                        facts={{}}, balances=balances, policy_blockers=resolution['blockers'],
                    )
                initial = assess([], None)
                approved = assess([{{'kind':'ITEM'}}, {{'kind':'PARCEL'}}], {{'decision':'APPROVED'}})
                if (
                    initial['status'] == 'WAITING_INPUT'
                    and set(initial['missing_materials']) == {{'ITEM', 'PARCEL'}}
                    and approved['status'] == 'ELIGIBLE' and 'REFUND' in approved['actions']
                ):
                    result['eligible_orders'].append({{
                        'id': order.id, 'version': order.version,
                        'line_id': line['line_id'], 'policy_snapshot': resolution['snapshot'],
                        'initial_status': initial['status'], 'initial_reason_code': initial['reason_code'],
                        'initial_expected_route_reason': 'EVIDENCE_REQUIRED',
                        'approved_status': approved['status'], 'approved_reason_code': approved['reason_code'],
                    }})
    if P['conversation']:
        result['turns'] = many('''
            SELECT id,generation,revision,input_through_seq,state,reason
            FROM chat_analysis_turns WHERE conversation_id=:conversation ORDER BY generation''')
        result['requests'] = many('''
            SELECT q.turn_id,q.state,q.run_id,q.reason,q.scope,q.previous,r.status AS run_status,
                   r.reason AS run_reason
            FROM chat_investigation_requests q JOIN chat_analysis_turns t ON t.id=q.turn_id
            LEFT JOIN agent_runs r ON r.id=q.run_id
            WHERE q.conversation_id=:conversation ORDER BY t.generation''')
        for request in result['requests']:
            scope = request.pop('scope') or {{}}
            request['dependency_sha256'] = scope.get('dependency_sha256')
            request['frozen_review_ids'] = [
                row['id'] for row in scope.get('frozen_context',{{}}).get('reviews',[])
                if row.get('effective')
            ]
            binding = (
                load_binding(db, db.get_one(AgentRun, request['run_id'])) or {{}}
                if request['run_id'] else {{}}
            )
            request['bound_turn_id'] = binding.get('decision_turn_id')
            request['bound_previous'] = binding.get('expected_previous')
        result['snapshots'] = many('''
            SELECT s.id,s.run_id,s.parent_id,s.version,s.sha256,p.state AS projection
            FROM chat_decision_snapshots s LEFT JOIN chat_decision_projections p ON p.snapshot_id=s.id
            WHERE s.conversation_id=:conversation ORDER BY s.version''')
        result['customer_messages'] = scalar('''
            SELECT count(*) FROM chat_messages WHERE conversation_id=:conversation AND role='customer' ''')
        result['material_sources'] = many('''
            SELECT material_id,attachment_id,kind,sha256 FROM chat_material_sources
            WHERE conversation_id=:conversation ORDER BY kind''')
        result['reviews'] = many('''
            SELECT id,decision,observations IS NULL AS no_observations FROM business_material_reviews
            WHERE case_id=:case ORDER BY created_at''')
print(json.dumps(result))
""",
    )


def _plan(backend, frontend):
    _check((backend, frontend) == (BACKEND_ID, FRONTEND_ID), "IMAGE_PIN")
    state = _private_json(migration.STATE)
    stack = offline_demo.restore(state)
    _check(
        state["run_id"] == stack.run_id == STACK_ID
        and state["url"] == migration.URL
        and not (stack.directory / JOURNAL).exists()
        and state["status"] == "READY"
        and state["profile"] == "test",
        "STACK_ID",
    )
    network, identity = migration._topology(stack, backend, frontend)
    _check(identity == SYSTEM_ID, "SYSTEM_ID")
    flags = _runtime(stack)
    with ExitStack() as managers:
        clients = migration._clients(stack, managers)
        customer = clients[CUSTOMER_CLIENT]
        actor = read(customer, "me")
        _check(actor["customer_id"] == CUSTOMER_ID, "CUSTOMER_PIN")
        _check(read(customer, "runtime")["mode"] == "offline", "HTTP_NOT_OFFLINE")
        candidates = _readback(stack, customer=actor["customer_id"])["eligible_orders"]
        orders = {row["id"]: row for row in read(customer, "orders?limit=300")["items"]}
        matching = [
            row
            for row in candidates
            if (row["id"], row["line_id"]) == (ORDER_ID, LINE_ID)
            and row["id"] in orders
            and orders[row["id"]]["version"] == row["version"]
            and row["line_id"] in {line["line_id"] for line in orders[row["id"]]["items"]}
        ]
    _check(bool(matching), "NO_ELIGIBLE_UNUSED_SYNTHETIC_ORDER")
    order = min(matching, key=lambda row: row["id"])
    return {
        "stack_id": stack.run_id,
        "system_id": identity,
        "network": network,
        "backend": backend,
        "frontend": frontend,
        "flags": flags,
        "customer_id": actor["customer_id"],
        "customer_user_id": actor["user_id"],
        "customer_client": CUSTOMER_CLIENT,
        "order": {**order, "items": [{"line_id": order["line_id"]}]},
        "images": {kind: sha256(path.read_bytes()).hexdigest() for kind, path in IMAGES},
        "driver_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "helper_sha256s": {
            name: sha256((ROOT / "scripts" / name).read_bytes()).hexdigest() for name in HELPERS
        },
        "maximum_provider_calls": 0,
        "maximum_approvals": 0,
        "maximum_executions": 0,
        "boundary": "CONTROL_HTTP_CELERY_NO_REAL_IMAGE_UNDERSTANDING",
    }


def _wait_revision(customer, support, path, prior, *, seconds=90):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        snapshot = read(support, path + "/decisions")["snapshot"]
        progress = read(customer, path).get("investigation_progress")
        if snapshot and snapshot["id"] != prior and progress == {"state": "PENDING_SUPPORT"}:
            return snapshot, progress["state"]
        sleep(0.5)
    _check(False, "AUTOMATIC_CANDIDATE_TIMEOUT")


def _stable_reassessment(before, after, approval, updated):
    turns, requests, snapshots = (after[name] for name in ("turns", "requests", "snapshots"))
    turn = turns[-1]
    request = requests[-1]
    snapshot = snapshots[-1]
    previous = {key: before["snapshots"][-1][key] for key in ("id", "version", "sha256")}
    return (
        len(turns) == len(before["turns"]) + 1
        and len(requests) == len(before["requests"]) + 1
        and len(snapshots) == len(before["snapshots"]) + 1
        and turn["state"] == "PUBLISHED"
        and turn["generation"] == before["turns"][-1]["generation"] + 1
        and turn["input_through_seq"] >= before["turns"][-1]["input_through_seq"]
        and request["turn_id"] == request["bound_turn_id"] == turn["id"]
        and request["state"] == "STARTED"
        and request["dependency_sha256"] != before["requests"][-1]["dependency_sha256"]
        and request["frozen_review_ids"] == [approval]
        and request["run_id"] == snapshot["run_id"]
        and snapshot["projection"] == "PUBLISHED"
        and snapshot["id"] == updated
        and request["previous"] == request["bound_previous"] == previous
        and snapshot["version"] == previous["version"] + 1
        and before["snapshots"] == snapshots[:-1]
        and before["customer_messages"] == after["customer_messages"]
        and after["reviews"] == [{"id": approval, "decision": "APPROVED", "no_observations": True}]
    )


def _start_case(customer, order, report, save):
    key = request_ids(report, "create_conversation", message_id=False)["idempotency_key"]
    save("CREATE_CONVERSATION")
    created = call(customer, "POST", "conversations", 201, headers=with_key(key), json={}).json()
    ident = created["id"]
    report["conversation_id"] = ident
    key = request_ids(report, "confirm_context", message_id=False)["idempotency_key"]
    save("CONFIRM_CONTEXT")
    selected = call(
        customer,
        "POST",
        f"conversations/{ident}/context",
        200,
        headers=with_key(key),
        json={
            "order_id": order["id"],
            "line_ids": [order["line_id"]],
            "expected_revision": created["revision"],
            "new_issue": False,
        },
    ).json()
    _check(
        selected["conversation"]["active_order_id"] == order["id"]
        and not selected["conversation"]["active_case_id"],
        "ORDER_CONTEXT_CHANGED",
    )
    ids = request_ids(report, "initial_text")
    save("CUSTOMER_TEXT")
    initial = message(
        customer,
        ident,
        f"订单 {order['id']} 的商品行 {order['line_id']} 破损，我申请处理这一个商品行。",
        ids,
    )
    case = initial["conversation"]["active_case_id"]
    _check(bool(case), "CASE_NOT_BOUND")
    report["case_id"] = case
    save("INITIAL")
    return ident, case


def _execute(plan, report, report_path):
    stack = offline_demo.restore(_private_json(migration.STATE))
    marker = stack.directory / JOURNAL
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {"report": str(report_path.resolve()), "plan_sha256": report["plan_sha256"]}, stream
        )
        stream.flush()
        os.fsync(stream.fileno())

    def save(stage):
        report["stage"] = stage
        _save(report_path, report)

    pre = _readback(stack)["financial_and_provider_counts"]
    with ExitStack() as managers:
        clients = migration._clients(stack, managers)
        customer, support = clients[plan["customer_client"]], clients["support"]
        actor = read(customer, "me")
        _check(
            (actor["customer_id"], actor["user_id"])
            == (plan["customer_id"], plan["customer_user_id"]),
            "CUSTOMER_CHANGED",
        )
        order = plan["order"]
        conversation, case = _start_case(customer, order, report, save)
        path = f"conversations/{conversation}"
        initial, initial_progress = _wait_revision(customer, support, path, None)
        native_initial = _readback(stack, conversation=conversation, case=case)
        _check(
            initial["input"]["line_ids"] == [order["line_id"]]
            and initial["input"]["order_id"] == order["id"]
            and initial["input"]["business_versions"]["order"] == order["version"]
            and initial["input"]["business_versions"]["policies"] == order["policy_snapshot"]
            and initial["input"]["frozen_context"]["eligibility"]["status"]
            == order["initial_status"]
            and native_initial["requests"][-1]["run_reason"]
            == order["initial_expected_route_reason"]
            and any(
                option["action"] == "REQUEST_EVIDENCE" for option in initial["candidate"]["options"]
            ),
            "INITIAL_EVIDENCE_REQUEST_MISSING",
        )
        initial_messages = native_initial["customer_messages"]
        report["initial_snapshot_id"] = initial["id"]
        photo_request = request_ids(report, "photos")
        save("PHOTOS")
        originals = [file.read_bytes() for _, file in IMAGES]
        _check(
            [sha256(raw).hexdigest() for raw in originals]
            == [plan["images"][kind] for kind, _ in IMAGES],
            "PHOTO_HASH_CHANGED",
        )
        uploaded = message(
            customer,
            conversation,
            "",
            photo_request,
            [(file.name, raw) for (_, file), raw in zip(IMAGES, originals, strict=True)],
        )
        photos = [
            item
            for row in uploaded["messages"]
            if row["role"] == "customer"
            for item in row["attachments"]
        ]
        _check(len(photos) == 2, "PHOTO_COUNT")
        photo_snapshot, photo_progress = _wait_revision(customer, support, path, initial["id"])
        report["photo_snapshot_id"] = photo_snapshot["id"]
        report["attachment_ids"] = [photo["id"] for photo in photos]
        save("LINK_MATERIALS")
        links = []
        for (kind, _), photo, raw in zip(IMAGES, photos, originals, strict=True):
            links.append(_bridge(customer, support, report, conversation, case, photo, raw, kind))
            report["material_links"] = links
            save("LINK_MATERIALS")
        before = _readback(stack, conversation=conversation, case=case)
        _check(
            before["snapshots"][-1]["id"] == photo_snapshot["id"]
            and before["customer_messages"] == initial_messages + 1
            and not before["reviews"]
            and len(before["material_sources"]) == 2
            and before["financial_and_provider_counts"] == pre,
            "PRE_REVIEW_BASELINE",
        )
        review_path = f"cases/{case}/material-reviews"
        review_body = {
            "expected_version": read(support, f"cases/{case}")["case"]["version"],
            "decision": "APPROVED",
            "notes": "CONTROL：自动化流程验收使用的模拟审核，非真人材料判断。",
        }
        key = request_ids(report, "material_review", message_id=False)["idempotency_key"]
        from aftercare.business.rules import digest

        report["material_review_body"] = review_body
        report["pre_review_native_sha256"] = digest(before)
        save("REVIEW")
        reviewed = call(
            support, "POST", review_path, 201, headers=with_key(key), json=review_body
        ).json()
        report["review_id"] = reviewed["id"]
        save("WAIT_FOR_CELERY")
        updated, updated_progress = _wait_revision(customer, support, path, photo_snapshot["id"])
        after = _readback(stack, conversation=conversation, case=case)
        _check(_stable_reassessment(before, after, reviewed["id"], updated["id"]), "NATIVE_CHAIN")
        _check(
            updated["input"]["business_versions"]["order"] == order["version"]
            and updated["input"]["business_versions"]["policies"] == order["policy_snapshot"]
            and updated["input"]["frozen_context"]["eligibility"]["status"]
            == order["approved_status"],
            "ORDER_RULES_CHANGED",
        )
        duplicate = call(
            support, "POST", review_path, 201, headers=with_key(key), json=review_body
        ).json()
        _check(duplicate == reviewed, "REVIEW_IDEMPOTENCY")
        sleep(4)
        _check(
            _readback(stack, conversation=conversation, case=case) == after,
            "REVIEW_REPLAY_SCHEDULED",
        )
        for prior in (initial, photo_snapshot):
            history = read(support, path + f"/decisions/history/{prior['id']}")
            _check(history == prior, "OLD_SNAPSHOT_CHANGED")
        _check(after["financial_and_provider_counts"] == pre, "FINANCIAL_OR_PROVIDER_WRITES")
    report.update(
        stage="RECORDED",
        review_id=reviewed["id"],
        material_links=links,
        new_turn=after["turns"][-1],
        new_request=after["requests"][-1],
        new_snapshot_id=updated["id"],
        new_candidate_actions=[item["action"] for item in updated["candidate"]["options"]],
        customer_progress=[initial_progress, photo_progress, updated_progress],
        old_snapshots_preserved=True,
        customer_message_count_unchanged_after_review=True,
        replay_no_new_turn_request_or_candidate=True,
        provider_calls=0,
        approvals=0,
        executions=0,
        real_image_understanding="NOT_RUN",
        independent_human_labels=False,
        material_review_provenance="CONTROL_SIMULATED_NO_HUMAN_IMAGE_JUDGMENT",
    )


def main():
    from aftercare.business.rules import digest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image-id", required=True)
    parser.add_argument("--frontend-image-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not all(
        re.fullmatch(r"sha256:[a-f0-9]{64}", item)
        for item in (args.backend_image_id, args.frontend_image_id)
    ):
        parser.error("supply complete immutable image IDs")
    if args.execute and not (args.expected_plan_sha256 and args.report):
        parser.error("--execute requires reviewed plan SHA and a new report")
    plan = _plan(args.backend_image_id, args.frontend_image_id)
    checksum = digest(plan)
    if not args.execute:
        print(json.dumps({"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}))
        return 0
    _check(checksum == args.expected_plan_sha256, "PLAN_CHANGED")
    report = {
        "status": "IN_PROGRESS",
        "stage": "PREFLIGHT",
        "plan_sha256": checksum,
        "plan": plan,
        "request_ids": {},
    }
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        with offline_demo.lifecycle_lock(migration.STATE):
            _check(_plan(args.backend_image_id, args.frontend_image_id) == plan, "PLAN_CHANGED")
            _execute(plan, report, args.report)
        report["status"] = "PASSED"
    except Exception as error:  # noqa: BLE001
        code = str(error)
        report.update(
            status="FAILED",
            failure_code=code
            if re.fullmatch(r"P5_MATERIAL_REASSESS_[A-Z0-9_]+", code)
            else type(error).__name__,
        )
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
