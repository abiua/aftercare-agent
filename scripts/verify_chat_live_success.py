"""One bounded real-model ordinary or complex attempt on a fresh isolated test stack."""

import argparse
import json
import os
import re
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import offline_demo
import verify_chat_live_business_path as driver
from aftercare.business.rules import digest
from verify_chat_p1_http import call, message, read, request_ids
from verify_chat_p2_offline import owned_container
from verify_chat_p3_offline import _ref
from verify_fresh_bootstrap import VerificationError, require
from verify_material_review_migration import _hash
from verify_streaming_compatibility import _save

ROOT = driver.ROOT
CASES = ROOT / "data/chat-eval-v1/development/cases.json"
COMPLEX_ID = "DAMAGE-TWO-LINE-PACKAGE-CONFLICT"
SCENARIOS = {
    "ordinary": {
        "port": 5204,
        "object_port": 58366,
        "net": "10.253.252.32/28",
        "ingress": "10.253.252.48/28",
        "order": "O9902",
        "lines": ["L01"],
        "route": "SINGLE",
    },
    "complex": {
        "port": 5205,
        "object_port": 58367,
        "net": "10.253.252.64/28",
        "ingress": "10.253.252.80/28",
        "order": "DEV-ORDER-04",
        "lines": ["L01", "L02"],
        "route": "MULTI",
    },
}
ORDINARY_REPEATS = {
    2: {
        "port": 5206,
        "object_port": 58368,
        "net": "10.253.252.96/28",
        "ingress": "10.253.252.112/28",
    },
    3: {
        "port": 5207,
        "object_port": 58369,
        "net": "10.253.252.128/28",
        "ingress": "10.253.252.144/28",
    },
    4: {
        "port": 5208,
        "object_port": 58370,
        "net": "10.253.252.160/28",
        "ingress": "10.253.252.176/28",
    },
    5: {
        "port": 5209,
        "object_port": 58371,
        "net": "10.253.252.192/28",
        "ingress": "10.253.252.208/28",
    },
    6: {
        "port": 5210,
        "object_port": 58372,
        "net": "10.253.252.224/28",
        "ingress": "10.253.252.240/28",
    },
    7: {
        "port": 5211,
        "object_port": 58373,
        "net": "10.253.253.0/28",
        "ingress": "10.253.253.16/28",
    },
    8: {
        "port": 5212,
        "object_port": 58374,
        "net": "10.253.253.32/28",
        "ingress": "10.253.253.48/28",
    },
    9: {
        "port": 5214,
        "object_port": 58376,
        "net": "10.253.253.96/28",
        "ingress": "10.253.253.112/28",
    },
    10: {
        "port": 5215,
        "object_port": 58377,
        "net": "10.253.253.128/28",
        "ingress": "10.253.253.144/28",
    },
}
COMPLEX_REPEATS = {
    2: {
        "port": 5213,
        "object_port": 58375,
        "net": "10.253.253.64/28",
        "ingress": "10.253.253.80/28",
    },
}
PROTOCOL = driver.WIRE_V3
CONTROL_OBSERVATION = {
    "provenance": "AUTOMATED_SYNTHETIC_CONTROL",
    "kind": "ITEM",
    "finding": "VISIBLE_DAMAGE",
    "line_id": "L01",
    "affected_quantity": 1,
}
FINANCIAL = (
    "business_proposals",
    "business_approvals",
    "business_executions",
    "business_simulated_payments",
)


@contextmanager
def _target(name, attempt=1):
    repeats = ORDINARY_REPEATS if name == "ordinary" else COMPLEX_REPEATS
    require(attempt == 1 or attempt in repeats, "LIVE_SUCCESS_ATTEMPT_UNSUPPORTED")
    scenario = SCENARIOS[name] if attempt == 1 else {**SCENARIOS[name], **repeats[attempt]}
    suffix = name if attempt == 1 else f"{name}-r{attempt}"
    state = ROOT / f".runtime/offline-browser-demo-live-success-{suffix}.json"
    with (
        patch.multiple(
            driver,
            PORT=scenario["port"],
            OBJECT_PORT=scenario["object_port"],
            URL=f"http://127.0.0.1:{scenario['port']}",
            STATE=state,
            SUBNETS={key: scenario[key] for key in ("net", "ingress")},
        ),
        patch.object(offline_demo, "STATE", state),
    ):
        yield state


def _fixture(name):
    seed, assets, originals, photos = driver._inputs()
    if name == "ordinary":
        return seed, assets, originals, photos, None
    pack = json.loads(CASES.read_bytes())
    (case,) = [row for row in pack["cases"] if row["id"] == COMPLEX_ID]
    require(
        case["order_id"] == SCENARIOS[name]["order"]
        and case["attachments"] == ["VIS-01", "VIS-03"]
        and case["expected_default_route"] == "MULTI",
        "LIVE_SUCCESS_COMPLEX_FIXTURE_CHANGED",
    )
    (ordinary,) = [row for row in seed["orders"] if row["id"] == driver.ORDER]
    seed["orders"].remove(ordinary)
    (mug,) = [row for row in seed["products"] if row["id"] == driver.PRODUCT]
    seed["products"].remove(mug)
    order = deepcopy(ordinary)
    order["id"] = case["order_id"]
    items = []
    for index, spec in enumerate((case, case["additional_line"]), 1):
        line_id = f"L{index:02}"
        product_id = f"DEV-MUG-{index}"
        title = ("白色马克杯", "带盖马克杯")[index - 1]
        require(
            not any(row["id"] == product_id for row in seed["products"]),
            "LIVE_SUCCESS_COMPLEX_PRODUCT_COLLISION",
        )
        seed["products"].append(
            {
                **mug,
                "id": product_id,
                "name": title,
                "unit_price_cents": spec["unit_paid_cents"],
            }
        )
        items.append(
            {
                **ordinary["items"][0],
                "line_id": line_id,
                "product_id": product_id,
                "title": title,
                "package_id": spec["package_id"],
                "quantity": spec["quantity"],
                **dict.fromkeys(
                    ("unit_price_cents", "payable_cents", "paid_cents"), spec["unit_paid_cents"]
                ),
            }
        )
    require(
        [(row["line_id"], row["package_id"], row["quantity"], row["paid_cents"]) for row in items]
        == [("L01", "TEST-PARCEL-A17", 1, 6000), ("L02", "TEST-PARCEL-B17", 1, 7200)]
        and not any(row["id"] == order["id"] for row in seed["orders"]),
        "LIVE_SUCCESS_COMPLEX_ORDER_COLLISION",
    )
    total = sum(row["paid_cents"] for row in items)
    order.update(
        items=items, **dict.fromkeys(("subtotal_cents", "payable_cents", "paid_cents"), total)
    )
    seed["orders"].append(order)
    return seed, assets, originals, photos, case


def _preflight(name, image_receipt, *, attempt=1):
    plan = driver._preflight(image_receipt, control_modify=False, wire_protocol=PROTOCOL)
    seed, assets, _, photos, case = _fixture(name)
    scenario = SCENARIOS[name]
    plan["protocol"] = "chat-live-success-plan-v1"
    plan["scenario"] = name
    plan["attempt"] = attempt
    plan["wire_protocol"] = PROTOCOL
    plan["case_scope"] = {
        "customer_id": "C001",
        "order_id": scenario["order"],
        "line_ids": scenario["lines"],
        "order_version": 1,
    }
    plan["expected_route"] = scenario["route"]
    plan["fixture"].update(
        order=scenario["order"],
        seed_sha256=digest(seed),
        product=["DEV-MUG-1", "DEV-MUG-2"] if case else driver.PRODUCT,
        original_sha256s=[row["sha256"] for row in assets],
        sent_jpeg_sha256s=[row.sent_sha256 for row in photos],
    )
    plan["protected_state"] = ["5202_WAITING_INDEPENDENT_REVIEW", "5203_FAILED_AND_TAKEOVER"]
    if name == "ordinary":
        if attempt >= 2:
            plan["protected_state"].append("5204_ORIGINAL_ORDINARY_UNKNOWN_NO_REPLAY")
        if attempt >= 3:
            plan["protected_state"].append("5206_ORDINARY_R2_PARTIAL_NO_REPLAY")
        if attempt >= 4:
            plan["protected_state"].append("5207_ORDINARY_R3_ADAPTER_FAILURE_NO_REPLAY")
        if attempt >= 5:
            plan["protected_state"].append("5208_ORDINARY_R4_PREWIRE_DENIAL_NO_REPLAY")
        if attempt >= 6:
            plan["protected_state"].append("5209_ORDINARY_R5_OUTPUT_TRUNCATED_NO_REPLAY")
        if attempt >= 7:
            plan["protected_state"].append("5210_ORDINARY_R6_INVALID_MODEL_OUTPUT_NO_REPLAY")
        if attempt in (8, 9, 10):
            plan["protected_state"].append("5211_ORDINARY_R7_HUMAN_HANDOFF_NO_REPLAY")
            plan["control_observation"] = CONTROL_OBSERVATION
            plan["phases"][2]["success"] = "SINGLE_ITEM_TYPED_SYNTHETIC_CONTROL_OBSERVATION"
        if attempt >= 9:
            plan["protected_state"].append("5212_ORDINARY_R8_BOOT_WORKER_NOT_READY_NO_REPLAY")
        if attempt == 10:
            plan["protected_state"].append(
                "5214_ORDINARY_R9_PROGRAM_OPTION_SCOPE_REJECTED_NO_REPLAY"
            )
    elif attempt == 2:
        plan["protected_state"].append("5205_COMPLEX_R1_VERIFIER_PROMPT_LIMIT_NO_REPLAY")
    plan["gates"]["success_driver_sha256"] = _hash(Path(__file__))
    plan["complex_fixture_sha256"] = _hash(CASES) if case else None
    plan["completion"] = (
        "ACTUAL_MULTI_FACT_POLICY_VERIFIER_AND_PACKAGE_CONFLICT; LOCAL_RECHECK_REPORTED_SEPARATELY"
        if case
        else "VALIDATED_EXECUTABLE_AGENT_ORIGINAL_ACCEPTED_WITHOUT_MODIFICATION"
    )
    if case:
        plan["phases"] = plan["phases"][:4]
    return plan


def _public_case(clients, report, output, assets, originals, case, scope):
    customer, support = clients["customer"], clients["support"]
    order = read(customer, "orders/" + scope["order_id"])
    require(
        read(customer, "runtime")["mode"] == "offline"
        and order["id"] == scope["order_id"]
        and order["version"] == scope["order_version"]
        and order["status"] == "DELIVERED"
        and order["payment_status"] == "CONFIRMED"
        and [row["line_id"] for row in order["items"]] == scope["line_ids"]
        and order["refunded_cents"] == 0
        and (
            [(row["line_id"], row.get("package_id"), row["paid_cents"]) for row in order["items"]]
            == [("L01", "TEST-PARCEL-A17", 6000), ("L02", "TEST-PARCEL-B17", 7200)]
            if case
            else order["paid_cents"] == 4900 and order["items"][0]["product_id"] == driver.PRODUCT
        ),
        "LIVE_SUCCESS_COMPLEX_HTTP_ORDER_MISMATCH",
    )
    created, _ = driver._mutation(
        customer, report, output, "CREATE_CONVERSATION", "conversations", 201, {}
    )
    ident = str(UUID(created["id"]))
    report["conversation_id"] = ident
    selected, _ = driver._mutation(
        customer,
        report,
        output,
        "CONFIRM_CONTEXT",
        f"conversations/{ident}/context",
        200,
        {
            "order_id": scope["order_id"],
            "line_ids": scope["line_ids"],
            "expected_revision": created["revision"],
            "new_issue": False,
        },
    )
    require(
        selected["conversation"]["active_order_id"] == scope["order_id"],
        "LIVE_SUCCESS_COMPLEX_CONTEXT_CHANGED",
    )
    ids = request_ids(report, "initial_text")
    driver._checkpoint(report, output, "CUSTOMER_TEXT")
    text = (
        case["customer_message"]
        if case
        else "订单 O9902 的 L01 白色马克杯杯口破损，我申请退这一只已实付的49元。"
    )
    report["customer_request"] = text
    first = message(customer, ident, text, ids)
    case_id = str(UUID(first["conversation"]["active_case_id"]))
    report["case_id"] = case_id
    ids = request_ids(report, "two_originals")
    driver._checkpoint(report, output, "CUSTOMER_TWO_ORIGINALS")
    second = message(
        customer, ident, "", ids, [(asset["file"], originals[asset["id"]]) for asset in assets]
    )
    (uploaded,) = [row for row in second["messages"] if row["role"] == "customer"]
    attachments = uploaded["attachments"]
    require(
        second["conversation"]["active_case_id"] == case_id and len(attachments) == 2,
        "LIVE_SUCCESS_COMPLEX_PHOTO_SCOPE_CHANGED",
    )
    for asset, attachment in zip(assets, attachments, strict=True):
        actual = call(customer, "GET", attachment["content_path"].removeprefix("/api/"), 200)
        require(
            attachment["sha256"] == asset["sha256"] and actual.content == originals[asset["id"]],
            "LIVE_SUCCESS_COMPLEX_ORIGINAL_CHANGED",
        )
    page = read(support, f"conversations/{ident}/decision-materials")
    require(
        page["case_id"] == case_id
        and {row["attachment_id"] for row in page["items"]} == {row["id"] for row in attachments},
        "LIVE_SUCCESS_COMPLEX_MATERIAL_SCOPE_CHANGED",
    )
    report["issue_id"] = str(UUID(page["issue_id"]))
    report["attachment_ids"] = [row["id"] for row in attachments]
    driver._checkpoint(report, output, "PUBLIC_ORIGINALS_VERIFIED")
    return ident, case_id, attachments


def _trace(stack, report, run):
    """Extract native receipts and model-authored claims for the private 0600 report.

    Structured claim values can contain free text. Keep them private with the
    selected tool receipt fields, and omit provider request/response bodies.
    """
    return stack.json_exec(
        "LIVE_SUCCESS_NATIVE_TRACE",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import text
from aftercare.business.rules import digest
from aftercare.config import load_settings
from aftercare.db import guarded_engine
engine=guarded_engine(load_settings('test'),test_only=True)
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()'))=={report["system_identifier"]!r}
    events=db.execute(text(
        "SELECT seq,kind,payload FROM public.agent_run_events WHERE run_id=:run AND kind IN "
        "('investigation_route','investigation_input','investigation_claims','investigation_assessment',"
        "'investigation_receipt','investigation_recheck','investigation_result') ORDER BY seq"
    ),{{'run':{str(UUID(run))!r}}}).mappings().all()
    audit={{'routes':[],'inputs':[],'claims':[],'assessments':[],'receipts':[],'rechecks':[],'results':[]}}
    fields={{'ORDER_LINE':('line_id','product_id','package_id','paid_cents','refunded_cents','quantity'),
             'REVIEW':('decision','material_ids','materials_version'),
             'CONDITION':('value','order_id','paid_cents','payment_status','paid_at'),
             'MATERIAL':('kind','sha256')}}
    def claim(row):
        return {{k:row[k] for k in ('id','role','subject','predicate','state','value','supporting_refs','conflicting_refs') if k in row}}
    for event in events:
        seq, kind, p = event['seq'], event['kind'], event['payload']
        if kind == 'investigation_route':
            audit['routes'].append({{k:p[k] for k in ('selected_path','arm','complexity_features','prompt_version') if k in p}})
        elif kind == 'investigation_input':
            audit['inputs'].append({{k:p[k] for k in ('role','phase_id','upstream_claim_ids') if k in p}})
        elif kind == 'investigation_claims':
            audit['claims'].append(dict(seq=seq,role=p['role'],phase_id=p['phase_id'],claims=[claim(row) for row in p['claims']]))
        elif kind == 'investigation_assessment':
            audit['assessments'].append(dict(seq=seq,recheck_count=p['recheck_count'],
                assessments=[{{k:row[k] for k in ('claim_id','verdict','evidence_refs','contradicted_field') if k in row}} for row in p['assessments']]))
        elif kind == 'investigation_recheck':
            audit['rechecks'].append({{k:p[k] for k in ('target_role','claim_ids','stale_claim_ids','tool','evidence_ids','receipt_sha256')}})
        elif kind == 'investigation_result':
            audit['results'].append(dict(seq=seq,selected_option_ids=p['selected_option_ids'],program_checks=p['program_checks']))
        elif kind == 'investigation_receipt':
            receipt=p['receipt']; assert p['receipt_sha256']==digest(receipt)
            safe=[]
            for record in receipt['data'].get('records',[]):
                ref=record['ref']; data=record['data']; kind=ref['source_kind']
                if kind=='POLICY':
                    clause=data['clause']; data={{k:clause[k] for k in ('id','version') if k in clause}}
                else:
                    observation_rows=data.get('observations',[]) if kind=='REVIEW' else []
                    data={{k:data[k] for k in fields.get(kind,()) if k in data}}
                    if observation_rows:
                        keys=('material_id','finding','line_id','affected_quantity','source','material_sha256')
                        data['observations']=[{{k:row[k] for k in keys if k in row}} for row in observation_rows]
                safe.append(dict(ref=ref,data=data))
            audit['receipts'].append(dict(seq=seq,role=p['role'],phase_id=p['phase_id'],tool=receipt['name'],
                receipt_sha256=p['receipt_sha256'],refs=receipt['refs'],records=safe))
print(json.dumps(audit))
engine.dispose()
""",
    )


def _records(trace, *, role=None, kind=None):
    return [
        record
        for receipt in trace["receipts"]
        if role is None or receipt["role"] == role
        for record in receipt["records"]
        if kind is None or record["ref"]["source_kind"] == kind
    ]


def _ordinary_ready(trace, snapshot):
    review = driver._review_choice(snapshot, {}, [], False)
    if review is None:
        return False
    option = next(
        row for row in snapshot["candidate"]["options"] if row["id"] == review["option_id"]
    )
    ref_ids = set(option["evidence_ids"])
    lines = _records(trace, role="strong_single", kind="ORDER_LINE")
    clauses = _records(trace, role="strong_single", kind="POLICY")
    return (
        option["lines"] == [{"line_id": "L01", "amount_cents": 4900}]
        and bool(option["policy_ids"])
        and any(
            row["ref"]["source_id"] == "L01"
            and row["data"].get("paid_cents") == 4900
            and row["data"].get("refunded_cents") == 0
            for row in lines
        )
        and set(option["policy_ids"])
        <= {row["data"]["id"] for row in clauses if row["ref"]["source_id"] in ref_ids}
    )


def _complex_outcome(trace, *, customer_statement_bound):
    inputs = {(row["role"], row["phase_id"]): row["upstream_claim_ids"] for row in trace["inputs"]}
    facts = _records(trace, role="fact", kind="ORDER_LINE")
    mapping = {row["ref"]["source_id"]: row["data"].get("package_id") for row in facts}
    independent = (
        inputs.get(("fact", "fact")) == []
        and inputs.get(("policy", "policy")) == []
        and bool(inputs.get(("verifier", "verify-0")))
        and bool(_records(trace, role="policy", kind="POLICY"))
        and bool(trace["assessments"])
    )
    claims = {
        row["id"]: row
        for batch in trace["claims"]
        if batch["phase_id"] == "fact"
        for row in batch["claims"]
    }
    disputed = {
        row["claim_id"]
        for assessment in trace["assessments"]
        for row in assessment["assessments"]
        if row["verdict"] == "CONTRADICTED" and "L02" in row.get("evidence_refs", [])
    }
    # A conflicting or contradicted L02 package claim must identify the actual
    # package-label mismatch. A generic material or policy dispute is insufficient.
    package_conflicts = set()
    for ident, row in claims.items():
        wording = json.dumps(
            [row.get(key) for key in ("subject", "predicate", "value")], ensure_ascii=False
        )
        refs = set(row.get("supporting_refs", []) + row.get("conflicting_refs", []))
        if (
            customer_statement_bound
            and mapping.get("L02") == "TEST-PARCEL-B17"
            and ("L02" in wording or "L02" in refs)
            and "TEST-PARCEL-A17" in wording
            and (
                ident in disputed
                or (row.get("state") == "CONTRADICTED" and "L02" in row.get("conflicting_refs", []))
            )
        ):
            package_conflicts.add(ident)
    rechecks = trace["rechecks"]
    narrow = (
        bool(package_conflicts)
        and len(rechecks) == 1
        and (
            rechecks[0]["target_role"] == "fact"
            and bool(rechecks[0]["claim_ids"])
            and set(rechecks[0]["claim_ids"]) <= package_conflicts
            and set(rechecks[0]["evidence_ids"]) <= {"L01", "L02"}
            and "L02" in rechecks[0]["evidence_ids"]
            and any(
                batch["role"] == "fact" and batch["phase_id"] == "recheck"
                for batch in trace["claims"]
            )
            and any(assessment["recheck_count"] == 1 for assessment in trace["assessments"])
        )
    )
    return {
        "independent_fact_policy_and_verifier": independent,
        "native_package_mapping_read": mapping
        == {"L01": "TEST-PARCEL-A17", "L02": "TEST-PARCEL-B17"},
        "model_identified_package_conflict": bool(package_conflicts),
        "targeted_recheck": "VERIFIED"
        if narrow
        else ("NOT_OBSERVED" if not rechecks else "UNVERIFIED"),
        "customer_label_provenance": "CUSTOMER_STATEMENT_ONLY"
        if customer_statement_bound
        else "UNVERIFIED",
    }


def _verify_typed_control(snapshot, trace, report):
    control = report["material_review_control"]
    readback = report["material_review_control_readback"]
    expected = {
        **readback["observations"][0],
        "material_sha256": control["original_sha256"],
    }
    (review,) = [
        row
        for row in snapshot["input"]["frozen_context"]["reviews"]
        if row["effective"] and row["id"] == control["review_id"]
    ]
    require(
        review["materials_version"] == readback["materials_version"]
        and review["observations"]
        == [
            {
                **expected,
                "original_attachments": [
                    {
                        "attachment_id": control["attachment_id"],
                        "message_id": readback["message_id"],
                        "sha256": control["original_sha256"],
                    }
                ],
            }
        ],
        "LIVE_SUCCESS_TYPED_CONTROL_FROZEN_BINDING_CHANGED",
    )
    return any(
        row["ref"]["source_id"] == control["review_id"]
        and row["data"].get("observations") == [expected]
        for row in _records(trace, role="strong_single", kind="REVIEW")
    )


def _execute(plan, report, output):
    require(
        all(_hash(ROOT / "scripts" / key) == val for key, val in plan["helpers"].items())
        and _hash(Path(__file__)) == plan["gates"]["success_driver_sha256"]
        and _hash(Path(driver.__file__)) == plan["gates"]["script_sha256"]
        and (
            plan["complex_fixture_sha256"] is None or _hash(CASES) == plan["complex_fixture_sha256"]
        ),
        "LIVE_SUCCESS_EXECUTION_INPUT_CHANGED",
    )
    seed, assets, originals, photos, complex_case = _fixture(plan["scenario"])
    stack, state = driver._boot(plan, report, output, seed)
    try:
        with ExitStack() as resources:
            clients = driver._actor_clients(resources, stack)
            conversation, case, attachments = _public_case(
                clients, report, output, assets, originals, complex_case, plan["case_scope"]
            )
            pending = driver._wait_turn(stack, report)
            require(
                [row["id"] for row in pending["attachments"]] == report["attachment_ids"],
                "LIVE_SUCCESS_VISION_TURN_CHANGED",
            )
            vision = driver._paid_phase(
                stack, state, plan, report, output, "VISION", pending["id"], photos
            )
            driver._verify_vision(vision, report, plan)
            driver._checkpoint(report, output, "ENABLE_TEXT_OWNED_OFFLINE_CONFIG")
            driver._enable_investigation(stack)
            turn = driver._material_control(
                stack,
                clients["support"],
                report,
                output,
                case,
                conversation,
                attachments,
                assets,
                originals,
                control_observation=plan.get("control_observation"),
            )
            text = driver._paid_phase(
                stack, state, plan, report, output, "TEXT", turn, photos, project=True
            )
            wire = driver._assert_wire(report, "TEXT")
            (run,) = text["runs"]
            require(
                run["run_id"]
                and run["snapshot_id"]
                and run["projection"] == "PUBLISHED"
                and text["counts"]["agent_runs"] == 1
                and len(wire) == len(text["attempts"]) == text["counts"]["agent_model_attempts"]
                and {
                    (row["run_id"], row["native_logical_id"], row["native_ordinal"]) for row in wire
                }
                == {(row["run_id"], row["logical_id"], row["ordinal"]) for row in text["attempts"]}
                and all(
                    row["run_id"] == run["run_id"]
                    and row["case_scope"] == plan["case_scope"]
                    and row["expected_route"] == plan["expected_route"]
                    for row in wire
                )
                and all(row["status"] not in {"STARTED", "UNKNOWN"} for row in text["attempts"]),
                "LIVE_SUCCESS_NATIVE_CANDIDATE_NOT_PUBLISHED",
            )
            trace = _trace(stack, report, run["run_id"])
            report["native_text_attempts"] = text["attempts"]
            report["trace"] = trace
            driver._checkpoint(report, output, "NATIVE_TEXT_TRACE_RECORDED")
            require(
                len(trace["routes"]) == 1
                and trace["routes"][0]["selected_path"] == plan["expected_route"]
                and trace["routes"][0]["arm"] == "auto",
                "LIVE_SUCCESS_NATIVE_ROUTE_MISMATCH",
            )
            current = read(clients["support"], f"conversations/{conversation}/decisions")
            snapshot = current["snapshot"]
            require(
                current["issue_id"] == report["issue_id"]
                and snapshot["id"] == run["snapshot_id"]
                and snapshot["origin"] == "AGENT"
                and snapshot["original_run_id"] == run["run_id"]
                and snapshot["input"]["case_id"] == case
                and snapshot["input"]["line_ids"] == plan["case_scope"]["line_ids"]
                and snapshot["input"]["order_id"] == plan["case_scope"]["order_id"]
                and {
                    row["id"]
                    for row in snapshot["input"]["frozen_context"]["reviews"]
                    if row["effective"]
                }
                == {report["material_review_id"]}
                and {
                    row["attachment_id"]
                    for row in snapshot["input"]["frozen_context"]["observations"]
                }
                == set(report["attachment_ids"]),
                "LIVE_SUCCESS_CANDIDATE_SCOPE_CHANGED",
            )
            report["candidate"] = {
                "ref": _ref(snapshot),
                "run_id": run["run_id"],
                "options": [
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "action",
                            "lines",
                            "target_line_ids",
                            "policy_ids",
                            "evidence_ids",
                        )
                        if key in row
                    }
                    for row in snapshot["candidate"]["options"]
                ],
                "checks": snapshot["checks"],
            }
            model_candidate = driver._model_candidate(text, snapshot)
            report["model_validated_candidate"] = model_candidate
            if plan.get("control_observation"):
                report["typed_control_actually_read_by_model"] = _verify_typed_control(
                    snapshot, trace, report
                )
                report["typed_control_frozen"] = True
            driver._checkpoint(report, output, "REAL_NATIVE_CANDIDATE_READBACK")
            if complex_case:
                customer_statement_bound = any(
                    row["text"] == complex_case["customer_message"]
                    for row in snapshot["input"]["frozen_context"]["messages"]
                )
                complex_result = report["complex_evidence"] = _complex_outcome(
                    trace,
                    customer_statement_bound=customer_statement_bound,
                )
                require(
                    not any(text["counts"][key] for key in FINANCIAL),
                    "LIVE_SUCCESS_COMPLEX_FINANCIAL_WRITE",
                )
                return (
                    "PASSED"
                    if model_candidate
                    and run["run_status"] in {"COMPLETED", "NEEDS_REVIEW"}
                    and run["run_reason"] in {"ELIGIBLE", "HUMAN_REVIEW"}
                    and all(
                        complex_result[key]
                        for key in (
                            "independent_fact_policy_and_verifier",
                            "native_package_mapping_read",
                            "model_identified_package_conflict",
                        )
                    )
                    else "PARTIAL"
                )
            if not model_candidate or not _ordinary_ready(trace, snapshot):
                report["business_completion"] = "NOT_RUN_NO_FULLY_EVIDENCED_AGENT_REFUND"
                return "PARTIAL"
            if (
                plan.get("control_observation")
                and not report["typed_control_actually_read_by_model"]
            ):
                report["business_completion"] = "NOT_RUN_NO_NATIVE_TYPED_CONTROL_TOOL_READ"
                return "PARTIAL"
            completed = driver._complete_business(
                clients,
                case,
                conversation,
                snapshot,
                report,
                output,
                control_modify=False,
            )
            final = driver._native(stack, report)
            require(
                completed
                and report["agent_executable"]
                and not report["support_control_modified"]
                and all(
                    final[key] == text[key]
                    for key in ("vision", "observations", "attempts", "runs", "customer_messages")
                )
                and all(final["counts"][key] == 1 for key in FINANCIAL),
                "LIVE_SUCCESS_FINAL_BUSINESS_READBACK_CHANGED",
            )
            report["final_native_counts"] = final["counts"]
            report["business_completion"] = "SIMULATED_RECEIPT_VERIFIED"
            return "PASSED"
    except Exception:
        if report.get("case_id") and report.get("conversation_id"):
            try:
                report["failure_native_readback"] = driver._native(stack, report)
            except Exception as error:  # noqa: BLE001 - never print raw storage or provider errors
                report["failure_readback_error"] = type(error).__name__
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--attempt", type=int, choices=(1, *ORDINARY_REPEATS), default=1)
    parser.add_argument("--image-receipt", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.report and args.report.parent.resolve() != (ROOT / ".runtime").resolve():
        parser.error("--report must be a private file directly under .runtime")
    if args.execute and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute requires an exact --expected-plan-sha256 and a new --report")
    with (
        _target(args.scenario, args.attempt) as state,
        offline_demo.lifecycle_lock(state, shared=not args.execute),
    ):
        plan = _preflight(args.scenario, args.image_receipt, attempt=args.attempt)
        checksum = digest(plan)
        if not args.execute:
            print(json.dumps({"status": "PREFLIGHT_ONLY", "plan": plan, "plan_sha256": checksum}))
            return 0
        require(checksum == args.expected_plan_sha256, "LIVE_SUCCESS_PLAN_CHANGED")
        report = {
            "status": "IN_PROGRESS",
            "stage": "STARTING",
            "plan": plan,
            "plan_sha256": checksum,
            "review_provenance": "AUTOMATED_SYNTHETIC_CONTROL",
            "request_ids": {},
            "mutations": {},
            "queue": {},
            "paid_phases": {},
        }
        fd = os.open(args.report, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            report["status"] = _execute(plan, report, args.report)
        except Exception as error:  # noqa: BLE001 - safe bounded reporting
            code = str(error)
            report.update(
                status="UNKNOWN" if driver._unknown(report, error) else "FAILED",
                failure_code=code if re.fullmatch(r"[A-Z0-9_]+", code) else type(error).__name__,
            )
        finally:
            _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "PASSED")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, ValueError, OSError) as error:
        code = str(error)
        print(code if re.fullmatch(r"[A-Z0-9_]+", code) else "LIVE_SUCCESS_PREFLIGHT_BLOCKED")
        raise SystemExit(1) from None
