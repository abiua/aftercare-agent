"""Verify customer policy retrieval and same-case correction over the isolated API."""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path

import httpx
from tau_p4_runtime import API_PORT, RUNTIME
from verify_chat_material_review_reassessment import _start_case
from verify_chat_p1_http import call, create, message, png, read, request_ids
from verify_chat_p2_offline import _cards, _wait
from verify_chat_p3_offline import _json, _key
from verify_fresh_bootstrap import require
from verify_streaming_compatibility import _save


def consult(customer, report):
    conversation = create(customer, report, "policy_conversation")
    path = f"conversations/{conversation}"
    data = message(
        customer,
        conversation,
        "退货政策是什么？",
        request_ids(report, "policy_question"),
    )
    require(data["conversation"]["active_case_id"] is None, "POLICY_CREATED_CASE")
    data = _wait(customer, path, lambda page: _cards(page, "policy_consultation"), seconds=90)
    answer = _cards(data, "policy_consultation")[-1]["data"]
    require(
        answer["retrieval"]["status"] == "RETRIEVED"
        and bool(answer["excerpts"])
        and all(
            excerpt["policy_id"] and excerpt["version"] and excerpt["chunk_id"]
            for excerpt in answer["excerpts"]
        )
        and data["conversation"]["active_case_id"] is None,
        "POLICY_SOURCE_MISSING_OR_CASE_CREATED",
    )
    report["policy_consultation"] = answer


def correct(clients, report, save):
    customer, support = clients["customer"], clients["support"]
    used = {item["order_id"] for item in read(customer, "cases?limit=300")["items"]}
    order = next(
        item
        for item in read(customer, "orders?limit=300")["items"]
        if item["id"] not in used | {"O9902"}
        and item["status"] == "DELIVERED"
        and len(item["items"]) == 1
    )
    conversation, case = _start_case(
        customer, {"id": order["id"], "line_id": order["items"][0]["line_id"]}, report, save
    )
    path = f"conversations/{conversation}"
    prior_content = png((10, 60, 220))
    page = message(
        customer,
        conversation,
        "先附上一张商品照片。",
        request_ids(report, "before_correction_photo"),
        [("before.png", prior_content)],
    )
    prior_attachment = page["memory"]["recent_attachments"][0]["attachment_id"]
    before = page["memory"]
    require(before["confirmed_context"]["case_id"] == case, "MEMORY_CASE_UNBOUND")
    data = message(
        customer,
        conversation,
        "更正一下，我说的不是外箱破损，而是商品本身有裂纹。",
        request_ids(report, "correction"),
    )
    correction = data["memory"]["correction"]
    require(
        data["memory"]["state"] == "CORRECTION_NEEDS_SUPPORT"
        and data["conversation"]["active_case_id"] == case
        and data["memory"]["issue_id"] == before["issue_id"]
        and _cards(data, "status"),
        "CORRECTION_CHANGED_CASE_OR_WAS_NOT_HELD",
    )
    confirmed = _json(
        support,
        path + "/correction/confirm",
        200,
        _key(report, "confirm_correction"),
        {
            "message_id": correction["message_id"],
            "expected_revision": data["conversation"]["revision"],
            "retained_attachment_ids": [prior_attachment],
            "evidence_reason": "客服核对该照片对应同一个已确认商品行，保留作为来源。",
        },
    )
    require(
        confirmed["conversation"]["active_case_id"] == case
        and confirmed["memory"]["confirmed_correction"]["source_sequence"]
        == correction["sequence"],
        "CORRECTION_CONFIRMATION_CHANGED_CASE",
    )
    supplement_content = png((230, 60, 10))
    data = message(
        customer,
        conversation,
        "补充说明，裂纹在杯身的边缘。",
        request_ids(report, "supplement"),
        [("supplement.png", supplement_content)],
    )
    current_attachments = {
        item["attachment_id"] for item in data["memory"]["recent_attachments"] if item["current"]
    }
    supplemental = next(
        attachment
        for item in data["messages"]
        if item["text"] == "补充说明，裂纹在杯身的边缘。"
        for attachment in item["attachments"]
    )
    fetched = call(customer, "GET", supplemental["content_path"].removeprefix("/api/"), 200)
    require(
        fetched.content == supplement_content
        and current_attachments == {prior_attachment, supplemental["id"]},
        "S3_SUPPLEMENT_OR_RETAINED_EVIDENCE_UNBOUND",
    )
    require(
        data["conversation"]["active_case_id"] == case
        and data["memory"]["issue_id"] == before["issue_id"]
        and data["memory"]["latest_update"]["text"] == "补充说明，裂纹在杯身的边缘。",
        "SUPPLEMENT_DID_NOT_UPDATE_SAME_CASE",
    )
    require(
        len(
            [
                item
                for item in read(customer, "cases?limit=300")["items"]
                if item["order_id"] == order["id"]
            ]
        )
        == 1,
        "CORRECTION_OR_SUPPLEMENT_CREATED_ANOTHER_CASE",
    )
    report["memory"] = {
        "order_id": order["id"],
        "conversation_id": conversation,
        "case_id": case,
        "before": before,
        "corrected": confirmed["memory"],
        "supplemented": data["memory"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    report = {
        "status": "RUNNING",
        "request_ids": {},
        "business_data": "approved synthetic Aftercare fixture with O9902",
    }

    def save(stage):
        report["stage"] = stage
        _save(args.output, report)

    save("start")
    credentials = json.loads((RUNTIME / ".runtime/users-test.json").read_text())
    try:
        with ExitStack() as resources:
            clients = {
                row["role"]: resources.enter_context(
                    httpx.Client(
                        base_url=f"http://127.0.0.1:{API_PORT}/api/",
                        headers={"Authorization": f"Bearer {row['token']}"},
                        timeout=15,
                        trust_env=False,
                    )
                )
                for row in credentials
            }
            consult(clients["customer"], report)
            save("consultation_verified")
            correct(clients, report, save)
            save("correction_verified")
            report["status"] = "PASSED"
            save("verified")
    except Exception as error:
        report.update(status="FAILED", error_type=type(error).__name__, code=str(error)[:120])
        _save(args.output, report)
        raise


if __name__ == "__main__":
    main()
