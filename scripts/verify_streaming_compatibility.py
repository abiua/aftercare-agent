"""Run four one-shot official Responses checks through the native adapter."""

import argparse
import json
import os
import stat
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import mkstemp
from time import monotonic
from unittest.mock import patch
from urllib.parse import urlsplit

from aftercare.intelligence import models
from aftercare.intelligence.native_stream import StreamInterrupted
from langchain_core.messages import HumanMessage
from langchain_openai._compat import httpx as provider_httpx
from langchain_openai.chat_models import base as langchain_openai_base
from openai import _base_client as openai_base_client

ROOT = Path(__file__).resolve().parents[1]
MODELS = ("gpt-6.1-terra", "gpt-6.1-sol")
API_URL = "https://api.openai.com/v1"
CAP = 256
MAX_REQUESTS = len(MODELS) * 2
TIMEOUT = 30
MAX_BODY_BYTES = 2048
ESTIMATED_RESERVATION_LIMIT_USD = Decimal("0.03")
TEXT = "售后流式就绪"
TOOL_NAME = "lookup_fixture_order"
FIXTURE_ARGS = {"order_id": "STREAM-001"}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Return a synthetic streaming compatibility fixture.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
    }
]
STREAM_EVENTS = {
    "response.created",
    "response.output_text.delta",
    "response.output_text.done",
    "response.function_call_arguments.delta",
    "response.function_call_arguments.done",
    "response.output_item.done",
    "response.completed",
    "response.incomplete",
    "response.failed",
}


class ProbeError(RuntimeError):
    pass


def _private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ProbeError("PRIVATE_CONFIG_PERMISSIONS_REJECTED")
    return json.loads(path.read_text())


def _configuration():
    config = _private_json(ROOT / ".runtime" / "models.json")
    specs = {}
    for row in config.get("models", []):
        if row.get("model") not in MODELS:
            continue
        endpoint = urlsplit(row["endpoint"])
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != "api.openai.com"
            or endpoint.path.rstrip("/") != "/v1"
            or endpoint.query
            or endpoint.fragment
            or row["interface"] != "responses"
            or row["provider"] != "openai"
            or row["api_key_env"] != "AFTERCARE_OPENAI_KEY"
        ):
            raise ProbeError("OFFICIAL_RESPONSES_CONFIG_REQUIRED")
        spec = models.ModelSpec.model_validate(row)
        if spec.model in specs:
            raise ProbeError("DUPLICATE_MODEL")
        specs[spec.model] = spec
    if set(specs) != set(MODELS):
        raise ProbeError("AUTHORIZED_MODELS_REQUIRED")
    if getattr(openai_base_client, "httpx2", None) is not provider_httpx:
        raise ProbeError("ACTIVE_HTTP_TRANSPORT_NOT_VERIFIED")
    return specs


def _save(output_path, report):
    descriptor, temp = mkstemp(prefix=".streaming-compatibility-", dir=output_path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, output_path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _usage(usage):
    if usage is None:
        return None
    fields = {
        key: usage[key]
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if type(usage.get(key)) is int and usage[key] >= 0
    }
    for key, valid in (
        ("input_token_details", ("cache_read", "cache_creation")),
        ("output_token_details", ("reasoning",)),
    ):
        counts = usage.get(key) or {}
        subset = {k: counts[k] for k in valid if type(counts.get(k)) is int and counts[k] >= 0}
        if subset:
            fields[key] = subset
    return fields or None


def _attempt(spec, kind, row, report, output_path):
    observed = Counter()
    draft = []
    tool_done_matches = False
    wire_count = 0
    original_send = provider_httpx.Client.send
    original_handle = provider_httpx.HTTPTransport.handle_request
    original_converter = langchain_openai_base._convert_responses_chunk_to_generation_chunk

    def guarded_send(client, request, *args, **kwargs):
        if type(client._transport_for_url(request.url)) is not provider_httpx.HTTPTransport:
            raise ProbeError("UNOBSERVED_HTTP_TRANSPORT_REJECTED")
        kwargs["follow_redirects"] = False
        return original_send(client, request, *args, **kwargs)

    def observed_handle(transport, request):
        nonlocal wire_count
        if len(request.content) > MAX_BODY_BYTES:
            raise ProbeError("REQUEST_BODY_LIMIT_REJECTED")
        payload = json.loads(request.content)
        if (
            request.method != "POST"
            or str(request.url) != API_URL + "/responses"
            or payload.get("stream") is not True
            or payload.get("model") != spec.model
            or payload.get("max_output_tokens") != CAP
            or request.headers.get("x-stainless-retry-count") != "0"
        ):
            raise ProbeError("REQUEST_SCOPE_REJECTED")
        if wire_count >= 1 or report["wire_requests"] >= MAX_REQUESTS:
            raise ProbeError("REQUEST_LIMIT_REJECTED")
        wire_count += 1
        report["wire_requests"] += 1
        row["wire_requests"] = wire_count
        row["transport_status"] = "DELIVERY_UNKNOWN"
        row["sent_max_output_tokens"] = payload["max_output_tokens"]
        row["sdk_retry_index"] = 0
        row["serialized_body_bytes"] = len(request.content)
        _save(output_path, report)
        response = original_handle(transport, request)
        row["http_status"] = response.status_code
        row["transport_status"] = "HTTP_RESPONSE_OBSERVED"
        _save(output_path, report)
        return response

    def observed_converter(chunk, *args, **kwargs):
        nonlocal tool_done_matches
        event_type = chunk.type
        if event_type in STREAM_EVENTS:
            observed[event_type] += 1
        if event_type == "response.function_call_arguments.done":
            try:
                tool_done_matches = (
                    getattr(chunk, "name", TOOL_NAME) == TOOL_NAME
                    and json.loads(chunk.arguments) == FIXTURE_ARGS
                )
            except (TypeError, ValueError):
                tool_done_matches = False
        return original_converter(chunk, *args, **kwargs)

    if kind == "text":
        prompt = f"兼容测试。只原样输出以下文字：{TEXT}。不要输出引号、标点或其他文字。"
        tools = []
    else:
        prompt = (
            f"这是兼容测试，必须调用一次 {TOOL_NAME}，"
            '参数恰好为 {"order_id":"STREAM-001"}。不要添加参数，不要用文字代替工具调用。'
        )
        tools = TOOLS
    started = monotonic()
    try:
        with (
            patch.object(provider_httpx.Client, "send", guarded_send),
            patch.object(provider_httpx.HTTPTransport, "handle_request", observed_handle),
            patch.object(
                langchain_openai_base,
                "_convert_responses_chunk_to_generation_chunk",
                observed_converter,
            ),
        ):
            result = models._stream_transport(
                spec,
                [HumanMessage(content=prompt)],
                tools,
                TIMEOUT,
                CAP,
                draft.append,
            )
        row["usage"] = _usage(result.usage_metadata)
        checks = {
            "exact_wire_request": wire_count == 1,
            "exact_serialized_output_cap": row["sent_max_output_tokens"] == CAP,
            "provider_completed": (
                observed["response.completed"] == 1
                and result.response_metadata.get("status") == "completed"
            ),
            "complete_usage_within_output_cap": bool(
                row["usage"]
                and {
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                }
                <= row["usage"].keys()
                and row["usage"]["output_tokens"] <= CAP
            ),
        }
        if kind == "text":
            checks["native_text_delta"] = observed["response.output_text.delta"] >= 1
            checks["text_matches_and_no_tool_calls"] = (
                "".join(draft) == result.text == TEXT
                and not result.tool_calls
                and not result.invalid_tool_calls
            )
        else:
            checks["native_argument_deltas"] = (
                observed["response.function_call_arguments.delta"] >= 1
            )
            checks["native_argument_done_matches_fixture"] = (
                observed["response.function_call_arguments.done"] == 1 and tool_done_matches
            )
            checks["one_completed_native_tool"] = (
                len(result.tool_calls) == 1
                and not result.invalid_tool_calls
                and result.tool_calls[0]["name"] == TOOL_NAME
                and result.tool_calls[0]["args"] == FIXTURE_ARGS
                and bool(result.tool_calls[0]["id"])
            )
        row["checks"] = checks
        row["status"] = "PASSED" if all(checks.values()) else "FAILED"
    except StreamInterrupted as error:
        row.update(
            status="UNKNOWN", error_code="MODEL_STREAM_INTERRUPTED", usage=_usage(error.usage)
        )
    except ProbeError as error:
        row.update(status="FAILED", error_code=str(error))
    except Exception as error:  # noqa: BLE001 - external exception messages may contain private data
        code, _, uncertain = models._failure(error)
        row.update(status="UNKNOWN" if uncertain else "FAILED", error_code=code)
        status_code = getattr(error, "status_code", None)
        if type(status_code) is int:
            row["http_status"] = status_code
    row["elapsed_ms"] = round((monotonic() - started) * 1000)
    row["stream_events"] = dict(sorted(observed.items()))
    row["draft_delta_batches"] = len(draft)
    row["usage_status"] = (
        "PROVIDER_REPORTED"
        if row["usage"] is not None
        and {"input_tokens", "output_tokens", "total_tokens"} <= row["usage"].keys()
        else "UNKNOWN"
    )
    if row["usage_status"] == "PROVIDER_REPORTED":
        estimated = models._estimated_price(spec, row["usage"])
        row["estimated_cost_usd"] = str(estimated) if estimated is not None else None
    _save(output_path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-paid", action="store_true", help="Allow four actual provider requests"
    )
    parser.add_argument(
        "--output", type=Path, help="New private JSON report path for paid execution"
    )
    args = parser.parse_args()
    if args.execute_paid and args.output is None:
        parser.error("--execute-paid requires --output")
    specs = _configuration()
    reservations = {
        name: models._reserve_price(spec, MAX_BODY_BYTES, CAP) for name, spec in specs.items()
    }
    if any(value is None for value in reservations.values()):
        raise ProbeError("MISSING_FROZEN_PRICE")
    estimated_max = sum(reservations.values()) * 2
    if estimated_max > ESTIMATED_RESERVATION_LIMIT_USD:
        raise ProbeError("ESTIMATED_RESERVATION_LIMIT_REJECTED")
    scope = {
        "maximum_requests": MAX_REQUESTS,
        "output_token_cap_per_request": CAP,
        "maximum_requested_output_tokens": MAX_REQUESTS * CAP,
        "maximum_serialized_body_bytes_per_request": MAX_BODY_BYTES,
        "estimated_reservation_total_usd": str(estimated_max),
        "estimated_reservation_basis": "2048 input token allowance per request and frozen prices; estimate only",
        "timeout_seconds_per_request": TIMEOUT,
        "automatic_retries": 0,
        "transport": "openai_sdk_httpx2.HTTPTransport.handle_request",
    }
    if not args.execute_paid:
        print(json.dumps({"status": "PREFLIGHT_ONLY", "models": list(MODELS), **scope}))
        return 0

    output_path = args.output
    secret = _private_json(ROOT / ".runtime" / "model-secret.json")
    key = secret.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ProbeError("MISSING_PROJECT_MODEL_KEY")
    report = {
        "at": datetime.now(UTC).isoformat(),
        "status": "IN_PROGRESS",
        "source": "live_project_native_adapter",
        "provider": "openai",
        "interface": "responses",
        **scope,
        "wire_requests": 0,
        "interruption_acceptance": "controlled_offline_only",
        "models": [
            {
                "model": name,
                "status": "NOT_RUN",
                "attempts": [
                    {
                        "kind": kind,
                        "status": "NOT_RUN",
                        "usage": None,
                        "usage_status": "UNKNOWN",
                        "estimated_cost_usd": None,
                        "estimated_reservation_usd": str(reservations[name]),
                        "wire_requests": 0,
                        "transport_status": "NOT_SENT",
                        "sent_max_output_tokens": None,
                    }
                    for kind in ("text", "tool_arguments")
                ],
            }
            for name in MODELS
        ],
    }
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(report, output, indent=2)
    with patch.dict(
        os.environ,
        {
            "AFTERCARE_OPENAI_KEY": key,
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        },
    ):
        for model in report["models"]:
            for row in model["attempts"]:
                row["status"] = "STARTED"
                _save(output_path, report)
                _attempt(specs[model["model"]], row["kind"], row, report, output_path)
            model["status"] = (
                "PASSED"
                if all(a["status"] == "PASSED" for a in model["attempts"])
                else "FAILED"
                if any(a["status"] == "FAILED" for a in model["attempts"])
                else "UNKNOWN"
            )
            _save(output_path, report)
    statuses = {row["status"] for row in report["models"]}
    report["status"] = (
        "PASSED" if statuses == {"PASSED"} else ("FAILED" if "FAILED" in statuses else "UNKNOWN")
    )
    attempts = [attempt for model in report["models"] for attempt in model["attempts"]]
    report["http_responses_observed"] = sum(
        row["transport_status"] == "HTTP_RESPONSE_OBSERVED" for row in attempts
    )
    report["unknown_cost_dispatched_attempts"] = sum(
        bool(row["wire_requests"]) and row["estimated_cost_usd"] is None for row in attempts
    )
    report["estimated_known_cost_usd"] = str(
        sum(
            Decimal(row["estimated_cost_usd"])
            for row in attempts
            if row["estimated_cost_usd"] is not None
        )
    )
    _save(output_path, report)
    print(
        json.dumps(
            {
                "wire_requests": report["wire_requests"],
                "models": [
                    {
                        "model": row["model"],
                        "status": row["status"],
                        "attempts": [
                            {"kind": a["kind"], "status": a["status"], "usage": a["usage"]}
                            for a in row["attempts"]
                        ],
                    }
                    for row in report["models"]
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0 if all(row["status"] == "PASSED" for row in report["models"]) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileExistsError:
        print("STREAMING_REPORT_ALREADY_EXISTS")
        raise SystemExit(1) from None
    except ProbeError as error:
        print(str(error))
        raise SystemExit(1) from None
    except Exception:  # noqa: BLE001 - avoid leaking errors from project credentials or SDK
        print("STREAMING_COMPATIBILITY_SETUP_FAILED")
        raise SystemExit(1) from None
