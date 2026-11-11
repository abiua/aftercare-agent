"""Run P2's real queue and photo storage in the owned offline 5191 demo."""

import argparse
import fcntl
import json
import os
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID

import httpx
import offline_demo
from verify_chat_p1_http import call, create, message, png, read, request_ids, with_key
from verify_fresh_bootstrap import FreshStack, VerificationError, require

STATE = offline_demo.STATE.with_name("offline-browser-demo-p2.json")
TAG = "chat-p2-20261111"
PORT = 5191
OBJECT_PORT = 58353
TASK = "aftercare.chat_analysis"

STATS = """
import json
from aftercare.queue import make_queue
values = make_queue('app').control.inspect(timeout=2).stats() or {}
print(json.dumps({'workers': len(values), 'tasks': sum(row.get('total', {}).get('aftercare.chat_analysis', 0) for row in values.values())}))
"""


class P2Stack(FreshStack):
    def __init__(self, directory):
        super().__init__(directory)
        target = self.runtime / "local.json"
        config = json.loads(target.read_text())
        config["app"].update(
            chat_analysis_enabled=True,
            chat_vision_enabled=False,
            model_enabled=False,
            embedding_enabled=False,
        )
        target.write_text(json.dumps(config))


def current():
    require(STATE.exists(), "P2_DEMO_NOT_STARTED")
    state = json.loads(STATE.read_text())
    require(state["url"] == f"http://127.0.0.1:{PORT}", "P2_LOOPBACK_REQUIRED")
    require(offline_demo.status(state)["healthy_now"], "P2_DEMO_UNAVAILABLE")
    stack = offline_demo.restore(state)
    config = json.loads((stack.runtime / "local.json").read_text())["app"]
    require(
        config["chat_analysis_enabled"] is True and config["chat_vision_enabled"] is False,
        "P2_OFFLINE_SETTINGS_REQUIRED",
    )
    return state, stack


def owned_container(stack, suffix):
    name = f"aftercare-fresh-{stack.run_id}-{suffix}"
    require(
        ("container", name) in stack.resources and stack.owned("container", name),
        "P2_CONTAINER_OWNERSHIP_MISMATCH",
    )
    return name


def _stage_db(stack, api, conversation_id):
    ident = str(UUID(conversation_id))
    return stack.json_exec(
        "CHAT_OUTBOX_READ",
        api,
        f"""
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
engine = guarded_engine(load_settings('app'))
with engine.connect() as conn, conn.begin():
    conn.execute(text('SET TRANSACTION READ ONLY'))
    params = {{'id': {ident!r}}}
    turns = [dict(row) for row in conn.execute(text('SELECT id, state, generation FROM chat_analysis_turns WHERE conversation_id=:id ORDER BY generation'), params).mappings()]
    attempts = conn.scalar(text('SELECT count(*) FROM chat_model_attempts WHERE conversation_id=:id'), params)
    links = conn.scalar(text('SELECT count(*) FROM chat_case_links WHERE conversation_id=:id'), params)
    all_attempts = conn.scalar(text('SELECT (SELECT count(*) FROM chat_model_attempts) + (SELECT count(*) FROM agent_model_attempts) + (SELECT count(*) FROM intake_model_audits)'))
    print(json.dumps({{'turns': turns, 'attempts': attempts, 'links': links, 'all_model_attempts': all_attempts}}))
engine.dispose()
""",
    )


def _stats(stack, api):
    stats = stack.json_exec("CHAT_WORKER_STATS", api, STATS)
    require(stats["workers"] == 1, "P2_SINGLE_WORKER_REQUIRED")
    return stats["tasks"]


def _cards(data, kind=None):
    return [
        card
        for item in data["messages"]
        for card in item["cards"]
        if kind is None or card["type"] == kind
    ]


def _wait(client, path, predicate, *, seconds=30):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        data = read(client, path)
        if predicate(data):
            return data
        sleep(0.25)
    raise VerificationError("P2_EXPECTED_STATE_TIMEOUT")


def _begin(client, report, name, order, text):
    ident = create(client, report, name)
    data = message(client, ident, text, request_ids(report, name + "_text"))
    case_id = data["conversation"]["active_case_id"]
    if case_id is None:
        body = {
            "order_id": order["id"],
            "line_ids": [order["items"][0]["line_id"]],
            "expected_revision": data["conversation"]["revision"],
            "new_issue": False,
        }
        data = call(
            client,
            "POST",
            f"conversations/{ident}/context",
            200,
            headers=with_key(
                request_ids(report, name + "_context", message_id=False)["idempotency_key"]
            ),
            json=body,
        ).json()
        case_id = data["conversation"]["active_case_id"]
    require(bool(case_id), "P2_CASE_LINK_MISSING")
    return ident, case_id, data


def _verify(client, report, stack, api, beat):
    require(read(client, "runtime")["mode"] == "offline", "OFFLINE_MODEL_MODE_REQUIRED")
    current_cases = read(client, "cases?limit=300")["items"]
    used = {
        case["order_id"] for case in current_cases if case["intent"] in {"wrong_item", "damaged"}
    }
    order = next(
        (
            item
            for item in read(client, "orders?limit=300")["items"]
            if item["id"] not in used and item["items"] and item["status"] == "DELIVERED"
        ),
        None,
    )
    require(order is not None, "UNUSED_DELIVERED_DEMO_ORDER_REQUIRED")
    report["order_id"] = order["id"]
    original_count = _stats(stack, api)
    stack.docker("PAUSE_OWN_BEAT", "pause", beat)
    try:
        ident, case_id, initial = _begin(
            client, report, "wrong_item_conversation", order, f"订单 {order['id']} 发错了商品"
        )
        report["wrong_item_case_id"] = case_id
        require(initial["analysis"]["state"] == "COLLECTING", "QUIET_STATE_REQUIRED")
        require(not _cards(initial, "evidence_request"), "EVIDENCE_BEFORE_QUIET")
        pending = _stage_db(stack, api, ident)
        require(
            pending["turns"][-1]["state"] == "PENDING" and pending["links"] == 1,
            "OUTBOX_NOT_DURABLE",
        )
        pending_id = pending["turns"][-1]["id"]
        sleep(4)
        pending_page = read(client, f"conversations/{ident}")
        require(not _cards(pending_page, "evidence_request"), "PUBLISHED_WHILE_BEAT_PAUSED")
        require(
            _stage_db(stack, api, ident)["turns"][-1]["state"] == "PENDING",
            "MISSED_WAKEUP_OUTBOX_LOST",
        )
        report["outbox"] = {"turn_id": pending_id, "pending_after_quiet_with_beat_paused": True}
    finally:
        stack.docker("RESUME_OWN_BEAT", "unpause", beat)

    path = f"conversations/{ident}"
    released = _wait(
        client, path, lambda data: data.get("analysis", {}).get("state") == "WAITING_CUSTOMER"
    )
    prompts = _cards(released, "evidence_request")
    require(
        len(prompts) == 1 and "包裹外侧标签" in prompts[0]["data"]["prompt"],
        "TARGETED_EVIDENCE_MISSING",
    )
    require(
        _stage_db(stack, api, ident)["turns"][-1]["id"] == pending_id,
        "OUTBOX_REPLACED_DURING_PICKUP",
    )
    report["outbox"].update(
        {"picked_up_by_beat": True, "public_prompt": prompts[0]["data"]["prompt"]}
    )
    before_duplicates = _stats(stack, api)
    stack.json_exec(
        "CHAT_DUPLICATE_PUBLISH",
        api,
        f"""
import json
from aftercare.queue import make_queue
app = make_queue('app')
for _ in range(2):
    app.send_task({TASK!r}, args=[{str(UUID(pending_id))!r}])
print(json.dumps({{'repeated_publications': 2}}))
""",
    )
    deadline = monotonic() + 18
    while _stats(stack, api) < before_duplicates + 2 and monotonic() < deadline:
        sleep(0.25)
    after_duplicates = _stats(stack, api)
    require(
        after_duplicates >= before_duplicates + 2 > original_count,
        "DUPLICATE_DELIVERY_NOT_OBSERVED",
    )
    same = read(client, path)
    require(same["messages"] == released["messages"], "DUPLICATE_PUBLIC_REPLY")
    report["outbox"]["duplicate_worker_task_delta"] = after_duplicates - before_duplicates
    declined = message(client, ident, "暂时无法提供", request_ids(report, "decline"))
    require(
        declined["conversation"]["status"] == "HUMAN_HANDOFF", "EVIDENCE_DECLINE_DID_NOT_HANDOFF"
    )

    photo_id, photo_case, _ = _begin(
        client, report, "damaged_conversation", order, f"订单 {order['id']} 的商品有破损"
    )
    report["damaged_case_id"] = photo_case
    photos = (("one.png", png((255, 0, 0))), ("two.png", png((0, 0, 255))))
    ids = request_ids(report, "photo_batch")
    uploaded = message(client, photo_id, "", ids, photos)
    repeated = message(client, photo_id, "", ids, photos)
    require(uploaded == repeated, "PHOTO_REPLAY_CHANGED_RESPONSE")
    photo_message = next(item for item in uploaded["messages"] if item["role"] == "customer")
    require(len(photo_message["attachments"]) == 2, "TWO_PHOTOS_REQUIRED")
    for attachment, (_, original) in zip(photo_message["attachments"], photos, strict=True):
        content_path = attachment["content_path"]
        require(
            content_path.startswith(f"/api/conversations/{photo_id}/attachments/"),
            "PHOTO_SCOPE_MISMATCH",
        )
        response = call(client, "GET", content_path.removeprefix("/api/"), 200)
        require(response.content == original, "ORIGINAL_PHOTO_MISMATCH")
    photo_page = _wait(
        client,
        f"conversations/{photo_id}",
        lambda data: data.get("analysis", {}).get("state") == "UNAVAILABLE",
    )
    require(photo_page["conversation"]["active_case_id"] == photo_case, "PHOTO_REPLACED_CASE")
    require(
        any(
            card["data"].get("code") == "PHOTO_ANALYSIS_UNAVAILABLE" for card in _cards(photo_page)
        ),
        "OFFLINE_PHOTO_STATUS_MISSING",
    )
    require(not _cards(photo_page, "photo_observation"), "OFFLINE_PHOTO_FALSE_OBSERVATION")
    photo_audit = _stage_db(stack, api, photo_id)
    declined_audit = _stage_db(stack, api, ident)
    require(photo_audit["links"] == declined_audit["links"] == 1, "DUPLICATE_CASE_LINK")
    require(photo_audit["all_model_attempts"] == 0, "OFFLINE_MODEL_ATTEMPT_OBSERVED")
    report.update(
        provider_calls=0,
        model_attempt_rows=photo_audit["all_model_attempts"],
        photo_attachment_bytes=[len(data) for _, data in photos],
        customer_handoff=True,
        photo_analysis_state="UNAVAILABLE",
        case_links=[declined_audit["links"], photo_audit["links"]],
    )


def verify(output):
    require(output is not None, "NEW_REPORT_PATH_REQUIRED")
    state, stack = current()
    api = owned_container(stack, "api")
    beat = owned_container(stack, "beat")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    report = {
        "status": "FAILED",
        "run_id": state["run_id"],
        "url": state["url"],
        "request_ids": {},
        "scope": "real HTTP/PostgreSQL/S3/Celery/Redis, model disabled",
    }
    with os.fdopen(fd, "w") as stream:
        try:
            credentials = json.loads(stack.credentials_path.read_text())
            customer = next(item for item in credentials if item["role"] == "customer")
            with httpx.Client(
                base_url=state["url"] + "/api/",
                trust_env=False,
                timeout=10,
                headers={"Authorization": "Bearer " + customer["token"]},
            ) as client:
                require(
                    read(client, "me")["customer_id"] == customer["customer_id"],
                    "CUSTOMER_TOKEN_MISMATCH",
                )
                _verify(client, report, stack, api, beat)
            report["status"] = "PASSED"
        except (VerificationError, RuntimeError) as error:
            report["failure_code"] = str(error)
        except Exception as error:  # noqa: BLE001 - never print arbitrary exception details
            report["failure_code"] = type(error).__name__
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "verify", "stop"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    offline_demo.STATE = STATE
    offline_demo.FreshStack = P2Stack
    STATE.parent.mkdir(exist_ok=True)
    fd = os.open(STATE.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "start":
            result = offline_demo.start(
                SimpleNamespace(
                    port=PORT,
                    object_port=OBJECT_PORT,
                    backend_image=f"aftercare-backend:{TAG}",
                    frontend_image=f"aftercare-frontend:{TAG}",
                )
            )
        elif args.command == "verify":
            result = verify(args.output)
        elif not STATE.exists():
            result = {"status": "NOT_STARTED"}
        else:
            state = json.loads(STATE.read_text())
            if args.command == "stop":
                require(state["url"] == f"http://127.0.0.1:{PORT}", "P2_LOOPBACK_REQUIRED")
                state["cleanup_leftovers"] = offline_demo.restore(state).cleanup(False)
                state["status"] = (
                    "STOPPED" if not state["cleanup_leftovers"] else "CLEANUP_INCOMPLETE"
                )
                offline_demo.save(state)
            result = offline_demo.status(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] in {"FAILED", "CLEANUP_INCOMPLETE"})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BlockingIOError, VerificationError) as error:
        print(json.dumps({"status": "FAILED", "code": str(error)}))
        raise SystemExit(1) from None
