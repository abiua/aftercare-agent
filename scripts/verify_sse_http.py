"""Verify run event replay over real HTTP with isolated PostgreSQL test identities.

From the project root run:
    PYTHONPATH=backend/src /tmp/aftercare-c-venv/bin/python scripts/verify_sse_http.py \
        --output reports/sse-http.json
"""

import argparse
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from aftercare.auth import token_hash
from aftercare.business.models import Case
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

BASE = "http://127.0.0.1:8022"
PAGE_SIZE = 200


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


@contextmanager
def temporary_customers(engine, customer_id):
    principals = [
        (str(uuid4()), secrets.token_urlsafe(32), customer)
        for customer in (customer_id, str(uuid4()))
    ]
    with Session(engine) as db, db.begin():
        db.add_all(
            User(
                id=user_id,
                role="customer",
                customer_id=customer,
                display_name="SSE temporary verification identity",
                token_hash=token_hash(token),
                enabled=True,
            )
            for user_id, token, customer in principals
        )
    try:
        yield principals
    finally:
        with Session(engine) as db, db.begin():
            for user_id, token, _ in principals:
                db.execute(
                    delete(User).where(User.id == user_id, User.token_hash == token_hash(token))
                )


def disable_temporary_customer(engine, principal):
    user_id, token, _ = principal
    with Session(engine) as db, db.begin():
        disabled = db.scalar(
            update(User)
            .where(User.id == user_id, User.token_hash == token_hash(token), User.enabled.is_(True))
            .values(enabled=False)
            .returning(User.id)
        )
        require(disabled == user_id, "ISOLATED_TOKEN_DISABLE_FAILED")


def client_for(principal):
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {principal[1]}"},
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(8),
    )


def verify_identity(client, principal):
    response = client.get("/api/me")
    require(response.status_code == 200, "TEST_HTTP_IDENTITY_UNAVAILABLE")
    identity = response.json()
    require(
        (identity.get("user_id"), identity.get("role"), identity.get("customer_id"))
        == (principal[0], "customer", principal[2]),
        "HTTP_AND_TEST_DATABASE_IDENTITY_MISMATCH",
    )


def expect_error(response, status, code=None):
    require(response.status_code == status, f"EXPECTED_HTTP_{status}_GOT_{response.status_code}")
    require(
        response.headers.get("content-type", "").startswith("application/json"),
        "ERROR_DID_NOT_PRECEDE_SSE_START",
    )
    if code is not None:
        require(response.json().get("detail", {}).get("code") == code, "UNEXPECTED_ERROR_CODE")


def read_event_pages(client, path):
    rows = []
    while len(rows) < 2000:
        response = client.get(f"{path}/events", params={"after": len(rows)})
        require(response.status_code == 200, "EVENT_PAGE_HTTP_FAILED")
        page = response.json()
        require(isinstance(page, list) and len(page) <= PAGE_SIZE, "EVENT_PAGE_LIMIT_EXCEEDED")
        require(
            [row["seq"] for row in page] == list(range(len(rows) + 1, len(rows) + len(page) + 1)),
            "EVENT_PAGE_SEQUENCE_MISMATCH",
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
    raise CheckFailed("EVENT_PAGE_FIXTURE_TOO_LARGE")


def sse_frames(response):
    event_id = None
    event_type = "message"
    data = []
    for line in response.iter_lines():
        if line.startswith(":"):
            yield "comment", None, line
        elif line.startswith("id:"):
            event_id = int(line[3:].strip())
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            yield event_type, event_id, json.loads("\n".join(data))
            event_id, event_type, data = None, "message", []


def replay(client, path, *, after=0, header=None, stop_after=None):
    headers = {"Last-Event-ID": str(header)} if header is not None else {}
    with client.stream(
        "GET", f"{path}/stream", params={"after": after}, headers=headers
    ) as response:
        require(response.status_code == 200, "STREAM_OPEN_FAILED")
        require(
            response.headers.get("content-type", "").startswith("text/event-stream"),
            "STREAM_CONTENT_TYPE_MISMATCH",
        )
        rows, idle = [], None
        for event_type, event_id, payload in sse_frames(response):
            if event_type == "idle":
                idle = payload["status"]
                break
            if event_type == "comment":
                continue
            require(event_id == payload.get("seq"), "STREAM_EVENT_ID_MISMATCH")
            rows.append(payload)
            if stop_after is not None and len(rows) == stop_after:
                break
        return rows, idle


def verify_history(owner, foreign, path, report):
    state = owner.get(path)
    require(state.status_code == 200, "RUN_LOOKUP_FAILED")
    status = state.json()["status"]
    require(status not in {"QUEUED", "RUNNING"}, "REPLAY_TARGET_NOT_IDLE")
    rows = read_event_pages(owner, path)
    require(len(rows) >= 2, "REPLAY_TARGET_INSUFFICIENT_HISTORY")
    require(not any(row["kind"] == "attempt" for row in rows), "REPLAY_TARGET_HAS_MODEL_ATTEMPTS")
    report.update(run_status=status, events=len(rows), model_attempts_observed=0)
    page_after_first = owner.get(f"{path}/events", params={"after": rows[0]["seq"]})
    require(
        page_after_first.status_code == 200 and page_after_first.json() == rows[1 : PAGE_SIZE + 1],
        "CURSOR_PAGE_MISMATCH",
    )

    prefix, _ = replay(owner, path, stop_after=1)
    require(prefix == rows[:1], "INITIAL_STREAM_PREFIX_MISMATCH")
    suffix, idle = replay(owner, path, header=prefix[0]["seq"])
    require(suffix == rows[1:] and idle == status, "RECONNECT_SEQUENCE_MISMATCH")
    empty, idle = replay(owner, path, after=rows[-1]["seq"])
    require(not empty and idle == status, "HEAD_CURSOR_SHOULD_RETURN_EMPTY_HISTORY")
    report["checks"]["tcp_disconnect_and_reconnect"] = "PASSED"
    report["checks"]["page_order_and_limit"] = "PASSED"
    report["checks"]["page_boundary_observed"] = "PASSED" if len(rows) > PAGE_SIZE else "NOT_RUN"
    if len(rows) <= PAGE_SIZE:
        report["not_run_reasons"]["page_boundary_observed"] = "FIXTURE_HAS_AT_MOST_200_EVENTS"

    future = rows[-1]["seq"] + 1
    for route in ("events", "stream"):
        expect_error(
            owner.get(f"{path}/{route}", params={"after": future}), 409, "INVALID_EVENT_CURSOR"
        )
        expect_error(owner.get(f"{path}/{route}", params={"after": -1}), 422)
    expect_error(
        owner.get(f"{path}/stream", headers={"Last-Event-ID": str(future)}),
        409,
        "INVALID_EVENT_CURSOR",
    )
    for value in ("-1", "NaN", "1.0", ""):
        expect_error(
            owner.get(f"{path}/stream", headers={"Last-Event-ID": value}), 400, "INVALID_CURSOR"
        )
    report["checks"]["future_and_invalid_cursor"] = "PASSED"

    for route in ("events", "stream"):
        expect_error(foreign.get(f"{path}/{route}"), 404, "NOT_FOUND")
        expect_error(foreign.get(f"{path}/{route}", params={"after": future}), 404, "NOT_FOUND")
    report["checks"]["customer_ownership"] = "PASSED"
    after = owner.get(path)
    require(
        after.status_code == 200
        and after.json() == state.json()
        and read_event_pages(owner, path) == rows,
        "RUN_OR_EVENT_HISTORY_CHANGED_DURING_REPLAY",
    )
    report["checks"]["read_only_replay_no_new_attempts"] = "PASSED"
    return rows[-1]["seq"]


def verify(engine, case_id, run_id, report):
    with Session(engine) as db:
        case = db.get(Case, case_id)
        require(case is not None, "REPLAY_TARGET_NOT_IN_TEST_DATABASE")
        customer_id = case.customer_id

    path = f"/api/cases/{case_id}/runs/{run_id}"
    with (
        temporary_customers(engine, customer_id) as (owner, foreign),
        client_for(owner) as owner_client,
        client_for(foreign) as foreign_client,
    ):
        verify_identity(owner_client, owner)
        verify_identity(foreign_client, foreign)
        last_event = verify_history(owner_client, foreign_client, path, report)
        disable_temporary_customer(engine, owner)
        expect_error(owner_client.get("/api/me"), 401, "AUTH_REQUIRED")
        expect_error(
            owner_client.get(f"{path}/stream", headers={"Last-Event-ID": str(last_event)}),
            401,
            "AUTH_REQUIRED",
        )
        verify_identity(foreign_client, foreign)
        report["checks"]["isolated_token_revoked_reconnect"] = "PASSED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-report", type=Path, default=ROOT / "reports/intake-queue-http.json"
    )
    parser.add_argument("--output", type=Path, required=True, help="New evidence JSON file")
    args = parser.parse_args()

    report = {
        "status": "FAILED",
        "source": "real_loopback_tcp_test_postgresql",
        "not_run_reasons": {
            "already_connected_token_revocation": "EXISTING_ZERO_MODEL_RUN_GOES_IDLE_AFTER_REPLAY"
        },
        "checks": {
            name: "NOT_RUN"
            for name in (
                "tcp_disconnect_and_reconnect",
                "page_order_and_limit",
                "page_boundary_observed",
                "future_and_invalid_cursor",
                "customer_ownership",
                "read_only_replay_no_new_attempts",
                "isolated_token_revoked_reconnect",
                "already_connected_token_revocation",
            )
        },
    }
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        parser.exit(1, "Could not create a new private evidence file.\n")
    with os.fdopen(descriptor, "w") as stream:
        try:
            fixture = json.loads(args.fixture_report.read_text())
            case_id = str(UUID(fixture["case_id"]))
            run_id = str(UUID(fixture["run_id"]))
            report.update(case_id=case_id, run_id=run_id)
            settings = load_settings("test")
            require(not settings.container, "HOST_TEST_PROFILE_REQUIRED")
            engine = guarded_engine(settings, test_only=True)
            try:
                verify(engine, case_id, run_id, report)
            finally:
                engine.dispose()
            report["status"] = "PARTIAL" if "NOT_RUN" in report["checks"].values() else "PASSED"
        except CheckFailed as error:
            report["failure"] = str(error)
        except Exception as error:  # noqa: BLE001
            # Provider URLs, database exception text and response bodies are intentionally private.
            report["failure"] = type(error).__name__
        finally:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(report, ensure_ascii=False))
    return int(report["status"] == "FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
