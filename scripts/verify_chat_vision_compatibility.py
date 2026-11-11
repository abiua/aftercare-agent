"""Probe the project's existing models with two controlled synthetic image batches."""

import argparse
import base64
import hashlib
import io
import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from aftercare.intelligence import models, vision
from langchain_openai._compat import httpx as provider_httpx
from PIL import Image, ImageFilter
from prepare_browser_financial_case import synthetic_photos
from verify_streaming_compatibility import (
    API_URL,
    ROOT,
    _configuration,
    _private_json,
    _save,
    _usage,
)

MAX_CALLS = 2
MAX_COST = Decimal("0.10")
OUTPUT = 900
TIMEOUT = 30
PRIOR_REPORT = ROOT / "reports" / "chat-vision-compatibility-live-20261111.json"
PRIOR_SHA = "46c80bc0bf22aa78f0e5e058a545c31261659a90f3391546b7f4cc3989672832"
ADMISSION = ROOT / ".runtime" / "chat-vision-demo-probe-20261111-admission"


class ProbeError(RuntimeError):
    pass


def _demo_images():
    images = {}
    for label in ("ITEM", "PARCEL"):
        path = ROOT / ".runtime" / "demo-assets" / f"{label.lower()}-synthetic.png"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > 4_000_000
            ):
                raise ProbeError("DEMO_ASSET_REJECTED")
            raw = stream.read(4_000_001)
        if len(raw) != info.st_size:
            raise ProbeError("DEMO_ASSET_REJECTED")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (640, 480):
                raise ProbeError("DEMO_ASSET_DIMENSIONS_REJECTED")
        images[label] = raw
    return images


def _photos(demo_assets=False):
    images = _demo_images() if demo_assets else synthetic_photos()
    first = list(images.items())
    blurred = []
    for label, original in reversed(first):
        with Image.open(io.BytesIO(original)) as image, io.BytesIO() as buffer:
            image.filter(ImageFilter.GaussianBlur(9)).save(buffer, format="PNG")
            blurred.append((f"{label}-blur", buffer.getvalue()))
    result = {}
    for name, batch in (("CLEAR_MARKINGS", first), ("BLURRED_REVERSED", blurred)):
        originals = dict(batch)
        sources = [
            vision.PhotoSource(label, name, hashlib.sha256(raw).hexdigest(), "image/png", len(raw))
            for label, raw in batch
        ]
        result[name] = vision.prepare_photos(sources, originals.__getitem__)
    return result


def _verify_wire(request, spec, expected):
    if request.method != "POST" or str(request.url) != f"{API_URL}/responses":
        raise ProbeError("REQUEST_TARGET_REJECTED")
    if len(request.content) > 1_000_000:
        raise ProbeError("REQUEST_SIZE_REJECTED")
    payload = json.loads(request.content)
    if (
        payload.get("model") != spec.model
        or payload.get("max_output_tokens") != OUTPUT
        or payload.get("stream") is True
        or payload.get("tools")
        or request.headers.get("x-stainless-retry-count") != "0"
    ):
        raise ProbeError("REQUEST_SCOPE_REJECTED")
    images = [
        block
        for message in payload["input"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "input_image"
    ]
    digests = []
    for item in images:
        url = item.get("image_url")
        if (
            item.get("detail") != "low"
            or not isinstance(url, str)
            or not url.startswith(models.VISION_INPUT_PREFIX)
        ):
            raise ProbeError("REQUEST_IMAGE_REJECTED")
        raw = base64.b64decode(url.removeprefix(models.VISION_INPUT_PREFIX), validate=True)
        digests.append(hashlib.sha256(raw).hexdigest())
    if digests != expected:
        raise ProbeError("REQUEST_IMAGE_DIGEST_MISMATCH")
    return digests


def _attempt(spec, messages, row, report, output):
    expected = [photo["sent_sha256"] for photo in row["photos"]]
    original_send = provider_httpx.Client.send
    original_handle = provider_httpx.HTTPTransport.handle_request

    def guarded_send(client, request, *args, **kwargs):
        if type(client._transport_for_url(request.url)) is not provider_httpx.HTTPTransport:
            raise ProbeError("UNOBSERVED_HTTP_TRANSPORT_REJECTED")
        kwargs["follow_redirects"] = False
        return original_send(client, request, *args, **kwargs)

    def guarded_handle(transport, request):
        if row["wire_requests"] or report["wire_requests"] >= MAX_CALLS:
            raise ProbeError("REQUEST_LIMIT_REJECTED")
        row["wire_image_sha256"] = _verify_wire(request, spec, expected)
        row["wire_requests"] = 1
        report["wire_requests"] += 1
        row["transport_state"] = "DELIVERY_UNKNOWN"
        _save(output, report)
        response = original_handle(transport, request)
        row["http_status"] = response.status_code
        row["transport_state"] = "HTTP_RESPONSE_OBSERVED"
        _save(output, report)
        return response

    try:
        with (
            patch.object(provider_httpx.Client, "send", guarded_send),
            patch.object(provider_httpx.HTTPTransport, "handle_request", guarded_handle),
        ):
            response = models._transport(spec, messages, [], TIMEOUT, OUTPUT)
    except Exception as error:  # noqa: BLE001 - expose classifications only
        row["failure_code"] = (
            str(error) if isinstance(error, ProbeError) else models._failure(error)[0]
        )
        row["status"] = "UNKNOWN" if row["transport_state"] == "DELIVERY_UNKNOWN" else "FAILED"
        return
    if row["transport_state"] != "HTTP_RESPONSE_OBSERVED":
        row.update(status="FAILED", failure_code="WIRE_NOT_OBSERVED")
        return
    row["usage"] = _usage(response.usage_metadata)
    cost = models._estimated_price(spec, row["usage"] or {})
    row["estimated_cost_usd"] = str(cost) if cost is not None else None
    if response.response_metadata.get("status") in {"incomplete", "failed", "cancelled"}:
        row.update(status="FAILED", failure_code="MODEL_INCOMPLETE")
        return
    try:
        result = vision._validated(response.tool_calls, models._content(response), len(expected))
    except (ValueError, TypeError):
        row.update(status="FAILED", failure_code="MODEL_OUTPUT_INVALID")
        return
    row["observations"] = result["result"]["photos"]
    row["status"] = "PARSED" if cost is not None else "USAGE_OR_COST_UNKNOWN"


def _prior_cost():
    raw = PRIOR_REPORT.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PRIOR_SHA:
        raise ProbeError("PRIOR_REPORT_DIGEST_MISMATCH")
    report = json.loads(raw)
    if report["wire_requests"] != 2 or len(report["attempts"]) != 2:
        raise ProbeError("PRIOR_USAGE_UNAVAILABLE")
    return sum(Decimal(row["estimated_cost_usd"]) for row in report["attempts"])


def _plan(demo_assets=False):
    specs = _configuration()
    photos = _photos(demo_assets)
    requests = []
    spec = specs["gpt-6.1-terra"]
    if spec.alias != "economy":
        raise ProbeError("ECONOMY_TERRA_REQUIRED")
    if (
        spec.provider == "openai"
        and spec.model in models.CACHE_WRITE_MODELS
        and spec.cache_write_usd_per_million is None
    ):
        raise ProbeError("CACHE_PRICE_REQUIRED")
    for name, images in photos.items():
        messages = vision._messages(images)
        input_upper = spec.context_tokens
        cost = models._reserve_price(spec, input_upper, OUTPUT)
        if cost is None or spec.output_tokens < OUTPUT:
            raise ProbeError("FROZEN_PRICE_OR_CONTEXT_UNAVAILABLE")
        row = {
            "model": spec.model,
            "batch": name,
            "status": "NOT_RUN",
            "transport_state": "NOT_SENT",
            "wire_requests": 0,
            "usage": None,
            "estimated_cost_usd": None,
            "observations": None,
            "estimated_reservation_usd": str(cost),
            "registered_context_tokens": input_upper,
            "photos": [image.audit_ref() for image in images],
        }
        requests.append((spec, messages, row))
    total = sum(Decimal(row["estimated_reservation_usd"]) for _, _, row in requests)
    prior = _prior_cost() if demo_assets else Decimal(0)
    if len(requests) > MAX_CALLS or total + prior > MAX_COST:
        raise ProbeError("ESTIMATED_BUDGET_REJECTED")
    frozen = {
        "demo_assets": demo_assets,
        "prior_report_sha256": PRIOR_SHA if demo_assets else None,
        "model_config_sha256": models.digest(spec.model_dump(mode="json")),
        "prompt_version": vision.PROMPT_VERSION,
        "output_cap_per_call": OUTPUT,
        "attempts": [row for _, _, row in requests],
    }
    return requests, {
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PREFLIGHT_ONLY",
        "scope": "SYNTHETIC_VISION_COMPATIBILITY",
        "interpretation": "Wire and structured-response checks. Human interpretation pending.",
        "fixture": "Synthetic ITEM/PARCEL printed cards and blurred reversed derivatives. Scope is image transport and printed/blurred observations. Product identity and damage recognition are outside this fixture.",
        "reservation_basis": "Registered context_tokens plus 900 output tokens at registered prices. This is a conservative estimate, not a provider billing limit.",
        "max_calls": MAX_CALLS,
        "output_cap_per_call": OUTPUT,
        "automatic_retries": 0,
        "estimated_reservation_total_usd": str(total),
        "prior_estimated_cost_usd": str(prior),
        "estimated_combined_upper_usd": str(total + prior),
        "estimated_cap_usd": str(MAX_COST),
        "frozen_plan_sha256": models.digest(frozen),
        "fixture_profile": "LOCAL_DEMO_CARDS_640X480"
        if demo_assets
        else "SMALL_CONTROL_CARDS_96X64",
        "wire_requests": 0,
        "attempts": [row for _, _, row in requests],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--demo-assets", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()
    if args.execute_paid and (
        not args.demo_assets or not args.expected_plan_sha256 or args.report is None
    ):
        parser.error(
            "--execute-paid requires --demo-assets, --expected-plan-sha256 and a new --report"
        )
    try:
        requests, report = _plan(args.demo_assets)
        if not args.execute_paid:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if report["frozen_plan_sha256"] != args.expected_plan_sha256:
            raise ProbeError("FROZEN_PLAN_DIGEST_MISMATCH")
        secret = _private_json(ROOT / ".runtime" / "model-secret.json")
        key = secret.get("api_key")
        if not isinstance(key, str) or not key.strip():
            raise ProbeError("MISSING_PROJECT_MODEL_KEY")
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            report["status"] = "IN_PROGRESS"
            json.dump(report, stream)
        fd = os.open(ADMISSION, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(report["frozen_plan_sha256"] + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        with patch.dict(
            os.environ,
            {
                "AFTERCARE_OPENAI_KEY": key,
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
            },
        ):
            for spec, messages, row in requests:
                _attempt(spec, messages, row, report, args.report)
                if row["wire_requests"] and row["estimated_cost_usd"] is None:
                    report["stop_reason"] = "ATTEMPT_COST_UNKNOWN"
                    break
                if (
                    row["usage"]
                    and row["usage"].get("input_tokens", 0) > row["registered_context_tokens"]
                ):
                    report["stop_reason"] = "INPUT_TOKEN_ALLOWANCE_EXCEEDED"
                    break
                if row["wire_requests"] and Decimal(row["estimated_cost_usd"]) > Decimal(
                    row["estimated_reservation_usd"]
                ):
                    report["stop_reason"] = "ESTIMATED_COST_EXCEEDED_RESERVATION"
                    break
                _save(args.report, report)
        report["status"] = (
            "PARSED_ALL"
            if not report.get("stop_reason")
            and all(row["status"] == "PARSED" for row in report["attempts"])
            else "INCOMPLETE"
        )
        _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "PARSED_ALL")
    except (ProbeError, OSError, ValueError, KeyError, TypeError):
        print("VISION_PROBE_BLOCKED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
