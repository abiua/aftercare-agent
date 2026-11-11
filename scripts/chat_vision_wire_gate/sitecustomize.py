"""Test-only, fail-closed guard for the pinned P2 acceptance worker."""

import base64
import hashlib
import json
import os
from pathlib import Path

from langchain_openai._compat import httpx as provider_httpx
from openai import _base_client

STATE = Path("/vision-wire")
PLAN = json.loads((STATE / "plan.json").read_text())
CLIENT_SEND = provider_httpx.Client.send
HANDLE = provider_httpx.HTTPTransport.handle_request
URL = "https://api.openai.com/v1/responses"


def _record(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _send(client, request, *args, **kwargs):
    if (
        str(request.url) != URL
        or type(client._transport_for_url(request.url)) is not provider_httpx.HTTPTransport
    ):
        raise RuntimeError("P2_WIRE_TARGET_REJECTED")
    kwargs["follow_redirects"] = False
    return CLIENT_SEND(client, request, *args, **kwargs)


def _handle(transport, request):
    if request.method != "POST" or str(request.url) != URL or len(request.content) > 300_000:
        raise RuntimeError("P2_WIRE_REQUEST_REJECTED")
    payload = json.loads(request.content)
    if (
        payload.get("model") != PLAN["model"]
        or payload.get("max_output_tokens") != PLAN["output_tokens"]
        or payload.get("stream") is True
        or payload.get("tools")
        or payload.get("store") is not False
        or request.headers.get("x-stainless-retry-count") != "0"
    ):
        raise RuntimeError("P2_WIRE_REQUEST_REJECTED")
    images = [
        block
        for message in payload["input"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "input_image"
    ]
    if len(images) != 1 or images[0].get("detail") != "low":
        raise RuntimeError("P2_WIRE_IMAGE_REJECTED")
    prefix = "data:image/jpeg;base64,"
    image_url = images[0].get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith(prefix):
        raise RuntimeError("P2_WIRE_IMAGE_REJECTED")
    image = base64.b64decode(image_url.removeprefix(prefix), validate=True)
    image_sha = hashlib.sha256(image).hexdigest()
    if image_sha != PLAN["sent_sha256"]:
        raise RuntimeError("P2_WIRE_IMAGE_DIGEST_MISMATCH")
    _record(
        STATE / "admission.json",
        {
            "status": "DELIVERY_UNKNOWN",
            "model": PLAN["model"],
            "sent_sha256": image_sha,
            "output_tokens": PLAN["output_tokens"],
            "retry_index": 0,
            "serialized_body_bytes": len(request.content),
        },
    )
    response = HANDLE(transport, request)
    _record(STATE / "response.json", {"http_status": response.status_code})
    return response


if getattr(_base_client, "httpx2", None) is not provider_httpx:
    raise RuntimeError("P2_UNVERIFIED_SDK_HTTP_TRANSPORT")
provider_httpx.Client.send = _send
provider_httpx.HTTPTransport.handle_request = _handle
READY = True
