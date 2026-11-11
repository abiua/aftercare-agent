"""Fail-closed HTTP admission for the isolated P4 synthetic compatibility worker."""

import fcntl
import hashlib
import json
import os
from pathlib import Path

from langchain_openai._compat import httpx
from openai import _base_client

STATE = Path("/p4-wire")
PLAN = json.loads((STATE / "plan.json").read_text())
PROTOCOL = PLAN.get("protocol", "original")
P5_PROTOCOL = "p5-paid-text-v1"
if PROTOCOL == P5_PROTOCOL:
    import p5_policy
OUTPUT_CAPS = {
    "original": 900,
    "compact-v2": 900,
    "catalogue-v3": 1800,
    "references-v4": 1800,
    P5_PROTOCOL: 1800,
}
PROMPTS = {
    "compact-v2": "compact-v2",
    "catalogue-v3": "compact-v3",
    "references-v4": "compact-v4",
    P5_PROTOCOL: "compact-v5",
}
URL = "https://api.openai.com/v1/responses"
SEND, HANDLE = httpx.Client.send, httpx.HTTPTransport.handle_request
ALLOWED = {
    "read_order_lines",
    "read_material_evidence",
    "read_refund_execution",
    "search_applicable_policy",
    "read_policy_clause",
    "request_targeted_check",
}


def _write(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _send(client, request, *args, **kwargs):
    if (
        str(request.url) != URL
        or type(client._transport_for_url(request.url)) is not httpx.HTTPTransport
    ):
        raise RuntimeError("P4_WIRE_TARGET_REJECTED")
    kwargs["follow_redirects"] = False
    return SEND(client, request, *args, **kwargs)


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _handle(transport, request):
    if request.method != "POST" or str(request.url) != URL or len(request.content) > 200_000:
        raise RuntimeError("P4_WIRE_TARGET_REJECTED")
    payload = json.loads(request.content)
    tools = payload.get("tools", [])
    if (
        payload.get("model") != PLAN["model"]
        or payload.get("max_output_tokens") != PLAN["output_tokens"]
        or payload.get("store") is not False
        or payload.get("stream") is True
        or payload.get("previous_response_id")
        or payload.get("prompt_cache_options")
        or request.headers.get("x-stainless-retry-count") != "0"
        or any(tool.get("type") != "function" or tool.get("name") not in ALLOWED for tool in tools)
    ):
        raise RuntimeError("P4_WIRE_OPTIONS_REJECTED")
    messages = payload.get("input", [])
    objects = list(_objects(messages))
    if any(row.get("type") in {"input_image", "image_url", "input_file"} for row in objects):
        raise RuntimeError("P4_WIRE_TEXT_ONLY")
    inputs = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if item.get("type") == "input_text"
            )
        try:
            inputs.append(json.loads(content))
        except (ValueError, TypeError):
            continue
    views = [
        entry["frozen_input"]
        for entry in inputs
        if isinstance(entry, dict) and isinstance(entry.get("frozen_input"), dict)
    ]
    if len(views) != 1 or views[0].get("input_digest") != PLAN["relevant_input_sha256"]:
        raise RuntimeError("P4_WIRE_SCOPE_REJECTED")
    if PROTOCOL in PROMPTS and (
        views[0].get("evidence_encoding") != PROMPTS[PROTOCOL]
        or views[0].get("scope") != [PLAN["conversation_id"], PLAN["issue_id"], PLAN["case_id"]]
    ):
        raise RuntimeError("P4_WIRE_COMPACT_ENCODING_REQUIRED")
    if PROTOCOL in {"catalogue-v3", "references-v4"} and (
        views[0].get("policy_context", {}).get("catalogue_sha256")
        != PLAN["policy_catalogue_sha256"]
    ):
        raise RuntimeError("P4_WIRE_CATALOGUE_BINDING_REQUIRED")
    for row in [*objects, *_objects(views)]:
        scope = row.get("scope")
        if isinstance(scope, list) and (
            len(scope) != 3
            or scope[0] != PLAN["conversation_id"]
            or scope[1] != PLAN["issue_id"]
            or scope[2] != PLAN["case_id"]
        ):
            raise RuntimeError("P4_WIRE_EVIDENCE_SCOPE_REJECTED")
    data = request.content.decode()
    if "data:image/" in data or "base64," in data:
        raise RuntimeError("P4_WIRE_TEXT_ONLY")
    if PROTOCOL == P5_PROTOCOL:
        role, recheck = p5_policy.request_role(messages, views[0], PLAN["policy_catalogue_sha256"])
        index, row_id = p5_policy.admit(
            STATE,
            PLAN,
            _write,
            request.content,
            [tool["name"] for tool in tools],
            role=role,
            recheck=recheck,
        )
        response = HANDLE(transport, request)
        _write(
            STATE / f"response-{index:02}.json",
            {"call_index": index, "row_id": row_id, "http_status": response.status_code},
        )
        return response
    # The initial user object retains the digest on follow-up tool turns.
    # Only the digest-bound synthetic issue is admitted by this worker.
    with (STATE / "lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = len(list(STATE.glob("admission-*.json"))) + 1
        if index > PLAN["maximum_provider_posts"]:
            raise RuntimeError("P4_WIRE_POST_LIMIT")
        _write(
            STATE / f"admission-{index:02}.json",
            {
                "call_index": index,
                "status": "DELIVERY_UNKNOWN",
                "model": PLAN["model"],
                "output_tokens": PLAN["output_tokens"],
                "request_sha256": hashlib.sha256(request.content).hexdigest(),
                "serialized_body_bytes": len(request.content),
                "tool_names": [tool["name"] for tool in tools],
                "retry_index": 0,
            },
        )
    response = HANDLE(transport, request)
    _write(
        STATE / f"response-{index:02}.json",
        {"call_index": index, "http_status": response.status_code},
    )
    return response


if (
    PROTOCOL not in OUTPUT_CAPS
    or type(PLAN["output_tokens"]) is not int
    or PLAN["output_tokens"] != OUTPUT_CAPS[PROTOCOL]
):
    raise RuntimeError("P4_WIRE_PLAN_OUTPUT_REJECTED")
if getattr(_base_client, "httpx2", None) is not httpx:
    raise RuntimeError("P4_UNVERIFIED_SDK_HTTP_TRANSPORT")
httpx.Client.send = _send
httpx.HTTPTransport.handle_request = _handle
READY = True
