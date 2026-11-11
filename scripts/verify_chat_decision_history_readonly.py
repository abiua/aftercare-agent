"""Read back the existing owned 5201 automatic decision history through HTTP and PG."""

import argparse
import json
import os
import re
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path

import offline_demo
import verify_chat_material_review_reassessment as reassess
import verify_material_review_migration as migration
from verify_chat_p1_http import call, read
from verify_chat_p2_offline import owned_container
from verify_streaming_compatibility import _private_json, _save

CONVERSATION = "0b1cce8e-2954-4477-9118-be01fb0b55a1"
CASE = "fe8c0602-1a46-4c2d-a666-127088f5d084"
BASELINE = reassess.ROOT / "reports/chat-material-review-reassessment-reconciliation-20261111.json"


def _check(condition, code):
    if not condition:
        raise ValueError(f"P5_HISTORY_{code}")


def _ref(row):
    return {key: row[key] for key in ("id", "version", "sha256")}


def _native(stack):
    return stack.json_exec(
        "P5_HISTORY_NATIVE_READ_ONLY",
        owned_container(stack, "api"),
        f"""
import json
from sqlalchemy import text
from aftercare.business.rules import utc
from aftercare.config import load_settings
from aftercare.db import guarded_engine

engine = guarded_engine(load_settings('test'), test_only=True)
params = {{'id': {CONVERSATION!r}, 'case': {CASE!r}}}
with engine.connect() as db, db.begin():
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.scalar(text('SELECT system_identifier::text FROM pg_control_system()')) == {reassess.SYSTEM_ID!r}
    result = dict(db.execute(text('''
        SELECT c.analysis_issue_id AS issue_id, c.customer_id, h.snapshot_id
        FROM chat_conversations c
        JOIN business_cases b ON b.id=c.active_case_id
        JOIN chat_decision_heads h ON h.conversation_id=c.id AND h.issue_id=c.analysis_issue_id
        WHERE c.id=:id AND b.id=:case AND b.decision_conversation_id=c.id
        AND b.decision_issue_id=c.analysis_issue_id
    '''), params).mappings().one())
    result['snapshots'] = [
        {{**row, 'created_at': utc(row['created_at']).isoformat()}}
        for row in db.execute(text('''
            SELECT s.id,s.version,s.sha256,s.origin,s.created_at
            FROM chat_decision_snapshots s JOIN chat_conversations c
              ON c.id=s.conversation_id AND c.analysis_issue_id=s.issue_id
            WHERE c.id=:id ORDER BY s.version DESC
        '''), params).mappings()
    ]
print(json.dumps(result))
""",
    )


def _verify_pages(fetch, native):
    snapshots = native["snapshots"]
    _check([row["version"] for row in snapshots] == [3, 2, 1], "NATIVE_VERSIONS")
    cursor = None
    for index, snapshot in enumerate(snapshots):
        params = {"limit": 1}
        if cursor is not None:
            params["before_version"] = cursor
        page = fetch(params)
        expected = {key: snapshot[key] for key in ("id", "version", "origin", "created_at")}
        cursor = snapshot["version"] if index < len(snapshots) - 1 else None
        _check(
            page["issue_id"] == native["issue_id"]
            and page["items"] == [expected]
            and page["next_before_version"] == cursor,
            "PAGINATION_MISMATCH",
        )
    empty = fetch({"limit": 1, "before_version": 1})
    _check(
        empty == {"issue_id": native["issue_id"], "items": [], "next_before_version": None},
        "CURSOR_END",
    )


def _verify(backend, frontend):
    state = _private_json(migration.STATE)
    stack = offline_demo.restore(state)
    _check(
        state["run_id"] == stack.run_id == reassess.STACK_ID
        and state["profile"] == "test"
        and state["status"] == "READY"
        and state["url"] == migration.URL,
        "OWNED_STACK",
    )
    _, system_id = migration._topology(stack, backend, frontend)
    _check(system_id == reassess.SYSTEM_ID, "OWNED_DATABASE")
    baseline_bytes = BASELINE.read_bytes()
    baseline = json.loads(baseline_bytes)
    _check(
        (baseline["conversation_id"], baseline["case_id"]) == (CONVERSATION, CASE),
        "BASELINE_SCOPE",
    )
    native = _native(stack)
    _check(
        list(reversed([_ref(row) for row in native["snapshots"]])) == baseline["snapshot_refs"]
        and native["snapshot_id"] == native["snapshots"][0]["id"]
        and native["customer_id"] == reassess.CUSTOMER_ID,
        "NATIVE_BASELINE",
    )
    path = f"conversations/{CONVERSATION}/decisions"
    with ExitStack() as managers:
        clients = migration._clients(stack, managers)
        support, customer = clients["support"], clients[reassess.CUSTOMER_CLIENT]
        _check(read(customer, "me")["customer_id"] == native["customer_id"], "CUSTOMER_SCOPE")
        _verify_pages(lambda params: read(support, path + "/history", params=params), native)
        call(customer, "GET", path + "/history", 404, params={"limit": 1})
        for limit in (0, 51):
            call(support, "GET", path + "/history", 422, params={"limit": limit})
        current = read(support, path)
        _check(
            current["issue_id"] == native["issue_id"]
            and _ref(current["snapshot"]) == _ref(native["snapshots"][0]),
            "CURRENT_HEAD",
        )
        for expected in native["snapshots"]:
            actual = read(support, path + "/history/" + expected["id"])
            _check(
                _ref(actual) == _ref(expected)
                and actual["input"]["conversation_id"] == CONVERSATION
                and actual["input"]["case_id"] == CASE
                and actual["input"]["issue_id"] == native["issue_id"],
                "SINGLE_HISTORY",
            )
    _check(_native(stack) == native, "NATIVE_CHANGED")
    return {
        "status": "PASSED",
        "boundary": "HTTP_GET_AND_POSTGRES_READ_ONLY",
        "stack_id": stack.run_id,
        "system_id": system_id,
        "backend_image": backend,
        "frontend_image": frontend,
        "baseline_sha256": sha256(baseline_bytes).hexdigest(),
        "conversation_id": CONVERSATION,
        "case_id": CASE,
        "issue_id": native["issue_id"],
        "snapshot_refs": [_ref(row) for row in native["snapshots"]],
        "page_size": 1,
        "customer_history_http_status": 404,
        "limit_validation_http_status": 422,
        "native_unchanged": True,
        "post_requests": 0,
        "offline_hint_duplicate_regression": "LOCAL_TEST_ONLY_NOT_TRIGGERED_ON_RUNNING_STACK",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image-id", required=True)
    parser.add_argument("--frontend-image-id", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if not all(
        re.fullmatch(r"sha256:[a-f0-9]{64}", image)
        for image in (args.backend_image_id, args.frontend_image_id)
    ):
        parser.error("supply complete immutable image IDs")
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"status": "IN_PROGRESS"}, stream)
    try:
        report = _verify(args.backend_image_id, args.frontend_image_id)
    except Exception as error:  # noqa: BLE001 - safe diagnostic only
        code = str(error)
        report = {
            "status": "FAILED",
            "failure_code": code
            if re.fullmatch(r"P5_HISTORY_[A-Z0-9_]+", code)
            else type(error).__name__,
            "post_requests": 0,
        }
    _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
