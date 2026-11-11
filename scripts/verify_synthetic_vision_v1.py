"""One-shot development probe of the frozen synthetic vision-v1 image batch."""

import argparse
import hashlib
import io
import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import mkstemp
from unittest.mock import patch

from aftercare.intelligence import models, vision
from langchain_openai._compat import httpx as provider_httpx
from PIL import Image
from verify_chat_vision_compatibility import ProbeError, _verify_wire
from verify_streaming_compatibility import ROOT, _configuration, _private_json, _usage

ASSETS = ROOT / "data" / "vision-v1"
MANIFEST_SHA256 = "9e3069ccfeb95b70bc5c2814893edb3d7686f51ecd660bfb4c54ac452e411dfe"
ADMISSION = ROOT / ".runtime" / "synthetic-vision-v1-development-admission"
CAP = Decimal("0.05")
OUTPUT = 900


def _read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ProbeError("ASSET_SIZE_REJECTED")
        raw = stream.read(limit + 1)
        if len(raw) != info.st_size:
            raise ProbeError("ASSET_CHANGED")
        return raw


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _prepare():
    raw = _read(ASSETS / "manifest.json", 32_768)
    if _sha(raw) != MANIFEST_SHA256:
        raise ProbeError("MANIFEST_DIGEST_MISMATCH")
    entries = json.loads(raw)["assets"]
    if [row["id"] for row in entries] != ["VIS-01", "VIS-02", "VIS-03", "VIS-04"]:
        raise ProbeError("ASSET_ORDER_REJECTED")
    originals, sources = {}, []
    for index, row in enumerate(entries, 1):
        name = row["file"]
        if Path(name).name != name or not name.endswith(".png"):
            raise ProbeError("ASSET_NAME_REJECTED")
        data = _read(ASSETS / name, 8 * 1024 * 1024)
        if (len(data), _sha(data)) != (row["bytes"], row["sha256"]):
            raise ProbeError("ASSET_DIGEST_MISMATCH")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or list(image.size) != row["dimensions"]:
                raise ProbeError("ASSET_DIMENSIONS_REJECTED")
        ident = str(index)
        originals[ident] = data
        sources.append(
            vision.PhotoSource(
                ident, "synthetic-development", row["sha256"], "image/png", len(data)
            )
        )
    if sum(map(len, originals.values())) != 8_236_015:
        raise ProbeError("BATCH_SIZE_MISMATCH")
    return vision.prepare_photos(sources, originals.__getitem__)


def _plan():
    spec = _configuration()["gpt-6.1-terra"]
    if spec.alias != "economy" or spec.output_tokens < OUTPUT:
        raise ProbeError("ECONOMY_TERRA_REQUIRED")
    if spec.cache_write_ttl and spec.cache_write_usd_per_million is None:
        raise ProbeError("CACHE_PRICE_REQUIRED")
    reserve = models._reserve_price(spec, spec.context_tokens, OUTPUT)
    if reserve is None or not 0 < reserve <= CAP:
        raise ProbeError("ESTIMATED_BUDGET_REJECTED")
    photos = _prepare()
    messages = vision._messages(photos)
    plan = {
        "scope": "SYNTHETIC_VISION_V1_DEVELOPMENT",
        "manifest_sha256": MANIFEST_SHA256,
        "model": spec.model,
        "model_config_sha256": models.digest(spec.model_dump(mode="json")),
        "prompt_version": vision.PROMPT_VERSION,
        "messages_sha256": models.digest([message.model_dump(mode="json") for message in messages]),
        "registered_context_tokens": spec.context_tokens,
        "estimated_reservation_usd": str(reserve),
        "estimated_cap_usd": str(CAP),
        "reservation_basis": "Full registered context and 900 output tokens at registered maximum applicable rates.",
        "max_wire_requests": 1,
        "max_output_tokens": OUTPUT,
        "automatic_retries": 0,
        "photos": [photo.audit_ref() for photo in photos],
    }
    return (
        spec,
        messages,
        {
            **plan,
            "frozen_plan_sha256": models.digest(plan),
            "status": "PREFLIGHT_ONLY",
            "wire_requests": 0,
            "transport_state": "NOT_SENT",
            "usage": None,
            "estimated_cost_usd": None,
            "observations": None,
            "assessment": "Development observations on AI-generated pictures. Independent review not performed.",
        },
    )


def _verify_exact_input(request, spec, messages, photos):
    expected = [photo["sent_sha256"] for photo in photos]
    digests = _verify_wire(request, spec, expected)
    payload = json.loads(request.content)
    if payload.get("store") is not False or payload.get("instructions") is not None:
        raise ProbeError("WIRE_PRIVACY_SCOPE_REJECTED")
    actual = []
    for entry in payload["input"]:
        content = entry["content"]
        if isinstance(content, str):
            actual.append(content)
            continue
        for block in content:
            if block["type"] == "input_text":
                actual.append(block["text"])
            elif block["type"] == "input_image":
                actual.append(None)
            else:
                raise ProbeError("UNEXPECTED_WIRE_INPUT")
    intended = [
        messages[0].content,
        *(block["text"] if block["type"] == "text" else None for block in messages[1].content),
    ]
    if actual != intended:
        raise ProbeError("WIRE_TEXT_OR_IMAGE_ORDER_MISMATCH")
    return digests


def _write(stream, report):
    json.dump(report, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())


def _save(path, report):
    fd, temporary = mkstemp(prefix=".synthetic-vision-v1-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            _write(stream, report)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _attempt(spec, messages, report, output):
    original_send = provider_httpx.Client.send
    original_handle = provider_httpx.HTTPTransport.handle_request

    def guarded_send(client, request, *args, **kwargs):
        if type(client._transport_for_url(request.url)) is not provider_httpx.HTTPTransport:
            raise ProbeError("UNOBSERVED_HTTP_TRANSPORT_REJECTED")
        kwargs["follow_redirects"] = False
        return original_send(client, request, *args, **kwargs)

    def guarded_handle(transport, request):
        if report["wire_requests"]:
            raise ProbeError("REQUEST_LIMIT_REJECTED")
        report["wire_image_sha256"] = _verify_exact_input(request, spec, messages, report["photos"])
        report["wire_requests"] = 1
        report["transport_state"] = "DELIVERY_UNKNOWN"
        _save(output, report)
        response = original_handle(transport, request)
        report["http_status"] = response.status_code
        report["transport_state"] = "HTTP_RESPONSE_OBSERVED"
        _save(output, report)
        return response

    try:
        with (
            patch.object(provider_httpx.Client, "send", guarded_send),
            patch.object(provider_httpx.HTTPTransport, "handle_request", guarded_handle),
        ):
            response = models._transport(spec, messages, [], 30, OUTPUT)
    except Exception as error:  # noqa: BLE001 - persist classified errors only
        report["failure_code"] = (
            str(error) if isinstance(error, ProbeError) else models._failure(error)[0]
        )
        report["status"] = (
            "UNKNOWN" if report["transport_state"] == "DELIVERY_UNKNOWN" else "FAILED"
        )
        return

    if report["transport_state"] != "HTTP_RESPONSE_OBSERVED":
        report.update(status="FAILED", failure_code="WIRE_NOT_OBSERVED")
        return
    usage = response.usage_metadata
    report["usage"] = _usage(usage)
    cost = models._estimated_price(spec, usage or {})
    report["estimated_cost_usd"] = str(cost) if cost is not None else None
    if response.response_metadata.get("status") in {
        "incomplete",
        "failed",
        "cancelled",
    } or response.response_metadata.get("finish_reason") in {"length", "content_filter"}:
        report.update(status="FAILED", failure_code="MODEL_INCOMPLETE")
        return
    try:
        result = vision._validated(
            response.tool_calls, models._content(response), len(report["photos"])
        )
    except (ValueError, TypeError):
        report.update(status="FAILED", failure_code="MODEL_OUTPUT_INVALID")
        return
    report["observations"] = result["result"]["photos"]
    if cost is None:
        report.update(status="INCOMPLETE", failure_code="ATTEMPT_COST_UNKNOWN")
    elif usage["input_tokens"] > spec.context_tokens or usage["output_tokens"] > OUTPUT:
        report.update(status="INCOMPLETE", failure_code="TOKEN_ALLOWANCE_EXCEEDED")
    elif cost > Decimal(report["estimated_reservation_usd"]):
        report.update(status="INCOMPLETE", failure_code="ESTIMATED_COST_EXCEEDED_RESERVATION")
    else:
        report["status"] = "PARSED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preflight", action="store_true", help="Read and hash the inputs without sending"
    )
    mode.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute_paid and (not args.expected_plan_sha256 or args.report is None):
        parser.error("--execute-paid requires --expected-plan-sha256 and a new --report")
    try:
        spec, messages, report = _plan()
        if not args.execute_paid:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.expected_plan_sha256 != report["frozen_plan_sha256"]:
            raise ProbeError("FROZEN_PLAN_DIGEST_MISMATCH")
        key = _private_json(ROOT / ".runtime" / "model-secret.json").get("api_key")
        if not isinstance(key, str) or not key.strip():
            raise ProbeError("MISSING_PROJECT_MODEL_KEY")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(args.report, flags, 0o600), "w") as stream:
            report.update(status="IN_PROGRESS", created_at=datetime.now(UTC).isoformat())
            _write(stream, report)
        _sync_directory(args.report.parent)
        try:
            with os.fdopen(os.open(ADMISSION, flags, 0o600), "w") as stream:
                _write(stream, {"frozen_plan_sha256": report["frozen_plan_sha256"]})
            _sync_directory(ADMISSION.parent)
        except OSError:
            report.update(status="FAILED", failure_code="ADMISSION_UNAVAILABLE")
            _save(args.report, report)
            raise ProbeError("ADMISSION_UNAVAILABLE") from None
        with patch.dict(
            os.environ,
            {
                "AFTERCARE_OPENAI_KEY": key,
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
            },
        ):
            _attempt(spec, messages, report, args.report)
        _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "PARSED")
    except (ProbeError, OSError, ValueError, KeyError, TypeError):
        print("SYNTHETIC_VISION_PROBE_BLOCKED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
