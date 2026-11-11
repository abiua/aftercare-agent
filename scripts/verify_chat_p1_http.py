#!/usr/bin/env python3
"""Exercise P1 over HTTP on the dedicated offline localhost demo stack."""

import argparse
import hashlib
import json
import os
import struct
import zlib
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


class VerificationError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def api_url(value):
    url = urlsplit(value)
    require(
        url.scheme == "http"
        and url.netloc in {"localhost:5190", "127.0.0.1:5190"}
        and url.path.rstrip("/") in {"", "/api"}
        and not url.query
        and not url.fragment,
        "ISOLATED_LOOPBACK_5190_REQUIRED",
    )
    return f"http://{url.netloc}/api/"


def png(color):
    """Generate a small synthetic image with a valid PNG envelope."""

    def chunk(kind, body):
        data = kind + body
        return struct.pack(">I", len(body)) + data + struct.pack(">I", zlib.crc32(data))

    pixels = (b"\0" + bytes(color) * 8) * 8
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def call(client, method, path, expected, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code != expected:
        raise VerificationError(f"HTTP_EXPECTED_{expected}_GOT_{response.status_code}")
    return response


def with_key(key):
    return {"Idempotency-Key": key}


def read(client, path, **kwargs):
    return call(client, "GET", path, 200, **kwargs).json()


def message(client, conversation, text, ids, photos=(), *, expected=201):
    parts = [
        ("client_message_id", (None, ids["client_message_id"])),
        ("text", (None, text)),
        *[("files", (name, content, "image/png")) for name, content in photos],
    ]
    return call(
        client,
        "POST",
        f"conversations/{conversation}/messages",
        expected,
        headers=with_key(ids["idempotency_key"]),
        files=parts,
    ).json()


def request_ids(report, name, *, message_id=True):
    ids = {"idempotency_key": str(uuid4())}
    if message_id:
        ids["client_message_id"] = str(uuid4())
    report["request_ids"][name] = ids
    return ids


def create(client, report, name):
    ids = request_ids(report, name, message_id=False)
    first = call(
        client, "POST", "conversations", 201, headers=with_key(ids["idempotency_key"]), json={}
    ).json()
    report[name] = first["id"]
    again = call(
        client, "POST", "conversations", 201, headers=with_key(ids["idempotency_key"]), json={}
    ).json()
    require(first == again, "CREATE_IDEMPOTENCY")
    return first["id"]


def actor_clients(base, filename, stack):
    rows = json.loads(filename.read_text())
    selected = {}
    customer_owners = set()
    for row in rows:
        role = row["role"]
        if role == "customer" and row.get("customer_id") not in customer_owners:
            require(bool(row.get("customer_id")), "CUSTOMER_OWNER_REQUIRED")
            key = "other_customer" if customer_owners else "customer"
            customer_owners.add(row["customer_id"])
        elif role in {"support", "tester"}:
            key = role
        else:
            continue
        if key in selected:
            continue
        client = stack.enter_context(
            httpx.Client(
                base_url=base,
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                headers={"Authorization": f"Bearer {row['token']}"},
            )
        )
        user = read(client, "me")
        require(
            all(user.get(field) == row.get(field) for field in ("role", "user_id", "customer_id")),
            "CREDENTIAL_ACTOR_MISMATCH",
        )
        selected[key] = client
    require(
        set(selected) == {"customer", "other_customer", "support", "tester"},
        "TWO_CUSTOMERS_SUPPORT_AND_TESTER_REQUIRED",
    )
    return selected


def status_cards(reply):
    return [
        card["data"]
        for row in reply["messages"]
        if row["role"] == "assistant"
        for card in row["cards"]
        if card["type"] == "status"
    ]


def run(args, report):
    base = api_url(args.base_url)
    report["phase"] = "offline_and_auth"
    with ExitStack() as stack:
        actors = actor_clients(base, args.credentials_file, stack)
        customer, other, support, tester = (
            actors[key] for key in ("customer", "other_customer", "support", "tester")
        )
        runtime = read(customer, "runtime")
        require(
            runtime.get("mode") == "offline" and runtime.get("models") == [],
            "EXPLICIT_OFFLINE_RUNTIME_REQUIRED",
        )
        orders = read(customer, "orders", params={"limit": 100})["items"]
        order = next((row for row in orders if row["status"] == "DELIVERED" and row["items"]), None)
        require(order is not None, "OWN_DELIVERED_ORDER_REQUIRED")
        report["order_id"] = order["id"]

        report["phase"] = "refund"
        refund = create(customer, report, "refund_conversation_id")
        ids = request_ids(report, "refund_message")
        question = f"请帮我查一下订单 {order['id']} 的退款进度。"
        reply = message(customer, refund, question, ids)
        require(reply == message(customer, refund, question, ids), "REFUND_MESSAGE_IDEMPOTENCY")
        refund_status = [
            row["code"]
            for row in status_cards(reply)
            if row["code"]
            in {
                "REFUND_LEDGER",
                "REFUND_NOT_FOUND",
            }
        ]
        require(len(refund_status) == 1, "DETERMINISTIC_REFUND_CARD_REQUIRED")
        require(
            reply["conversation"]["active_order_id"] == order["id"]
            and reply["conversation"]["active_case_id"] is None,
            "REFUND_QUERY_SCOPE",
        )
        report["refund_status"] = refund_status[0]

        report["phase"] = "photos_before_order"
        chat = create(customer, report, "photo_conversation_id")
        photos = [
            ("synthetic-blue.png", png((20, 40, 200))),
            ("synthetic-red.png", png((200, 40, 20))),
        ]
        ids = request_ids(report, "photo_message")
        caption = "这是合成验收照片，收到的商品有破损。"
        uploaded = message(customer, chat, caption, ids, photos)
        require(uploaded["conversation"]["active_order_id"] is None, "PHOTO_PRECEDES_ORDER")
        require(uploaded == message(customer, chat, caption, ids, photos), "UPLOAD_IDEMPOTENCY")
        message(customer, chat, caption + " 不同内容", ids, photos, expected=409)
        source = next(row for row in uploaded["messages"] if row["role"] == "customer")
        attachments = source["attachments"]
        require(len(attachments) == 2, "TWO_ATTACHMENTS_REQUIRED")
        report["photo_message_id"] = source["id"]
        report["attachment_ids"] = [item["id"] for item in attachments]
        for (name, raw), item in zip(photos, attachments, strict=True):
            path = f"conversations/{chat}/attachments/{item['id']}/content"
            require(
                item["content_path"] == f"/api/{path}"
                and item["filename"] == name
                and item["size"] == len(raw)
                and item["sha256"] == hashlib.sha256(raw).hexdigest(),
                "ATTACHMENT_METADATA_MISMATCH",
            )
            image = call(customer, "GET", path, 200)
            require(
                image.content == raw and image.headers.get("content-type") == "image/png",
                "ORIGINAL_IMAGE_MISMATCH",
            )
            call(other, "GET", path, 404)
            call(tester, "GET", path, 403)

        report["phase"] = "confirm_order_and_support_reply"
        ids = request_ids(report, "order_confirmation", message_id=False)
        body = {
            "order_id": order["id"],
            "line_ids": [order["items"][0]["line_id"]],
            "expected_revision": uploaded["conversation"]["revision"],
            "new_issue": False,
        }
        confirmed = call(
            customer,
            "POST",
            f"conversations/{chat}/context",
            200,
            headers=with_key(ids["idempotency_key"]),
            json=body,
        ).json()
        require(
            confirmed["conversation"]["active_order_id"] == order["id"], "ORDER_CONFIRMATION_SCOPE"
        )
        case_id = confirmed["conversation"]["active_case_id"]
        require(isinstance(case_id, str) and bool(case_id), "CASE_LINK_REQUIRED")
        report["case_id"] = case_id
        require(read(customer, f"cases/{case_id}/runs") == [], "UNEXPECTED_AGENT_RUN")
        require(
            read(support, f"conversations/{chat}")["conversation"]["id"] == chat,
            "SUPPORT_CANNOT_READ",
        )
        note = "客服已收到这两张照片。此消息用于隔离会话验收。"
        reply = message(support, chat, note, request_ids(report, "support_message"))
        require(
            any(row["role"] == "support" and row["text"] == note for row in reply["messages"]),
            "PUBLIC_SUPPORT_REPLY_REQUIRED",
        )

        report["phase"] = "pagination_and_permissions"
        path = f"conversations/{chat}"
        full = read(customer, path)["messages"]
        first = read(customer, path, params={"limit": 1})
        require(first["has_more"] is True and len(first["messages"]) == 1, "FIRST_PAGE_INVALID")
        rest = read(customer, path, params={"after": first["next_after"]})
        require(
            not rest["has_more"]
            and first["messages"] + rest["messages"] == full
            and len({row["sequence"] for row in full}) == len(full)
            and any(row["role"] == "support" and row["text"] == note for row in full)
            and sum(row["id"] == source["id"] for row in full) == 1,
            "CUSTOMER_RELOAD_OR_PAGINATION_MISMATCH",
        )
        call(other, "GET", path, 404)
        call(tester, "GET", path, 403)
        message(other, chat, "越权验收", request_ids(report, "foreign_message"), expected=404)
        message(tester, chat, "越权验收", request_ids(report, "tester_message"), expected=403)
        require(read(customer, path)["messages"] == full, "DENIED_WRITES_CHANGED_MESSAGES")
        require(read(customer, f"cases/{case_id}/runs") == [], "UNEXPECTED_AGENT_RUN")
        report.update(
            phase="completed", case_agent_runs=0, public_message_count=len(full), attachment_count=2
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5190")
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "status": "FAILED",
        "scope": "P1_HTTP_OFFLINE_SYNTHETIC_IMAGES",
        "phase": "initialization",
        "request_ids": {},
    }
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        try:
            run(args, report)
            report["status"] = "PASSED"
        except VerificationError as error:
            report["failure_code"] = str(error)
        except httpx.TimeoutException:
            report["failure_code"] = "HTTP_TIMEOUT_OUTCOME_UNKNOWN"
        except httpx.HTTPError:
            report["failure_code"] = "HTTP_TRANSPORT_FAILED"
        except (OSError, ValueError, TypeError, KeyError, StopIteration):
            report["failure_code"] = "INPUT_OR_RESPONSE_FORMAT_INVALID"
        finally:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
