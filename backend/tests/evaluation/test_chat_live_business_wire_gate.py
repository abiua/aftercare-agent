"""The owned wire gate uses native serialization without ever reaching a provider."""

import base64
import hashlib
import importlib.util
import json
import sys
from contextvars import Context
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_openai._compat import httpx
from openai import RateLimitError

DIRECTORY = Path(__file__).resolve().parents[3] / "scripts/chat_live_business_wire_gate"
SPEC = importlib.util.spec_from_file_location("live_business_wire_policy", DIRECTORY / "policy.py")
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
KEY = "test-only-wire-key"
IMAGES = [b"first fixture image", b"second fixture image"]
REF = ["ORDER_LINE", "L01", "a" * 64]


def _fixture(kind):
    posts, total, reserve, output = policy.CAPS[kind]
    common = {
        "kind": kind,
        "conversation_id": "conversation",
        "case_id": "case",
        "issue_id": "issue",
        "turn_id": "turn",
    }
    plan = {
        **common,
        "protocol": policy.PROTOCOL,
        "campaign_id": "campaign",
        "model": policy.MODEL,
        "maximum_provider_posts": posts,
        "maximum_reservation_microusd": total,
        "per_post_reserved_microusd": reserve,
        "output_tokens": output,
    }
    binding = {
        **common,
        "native_logical_id": (
            "vision:" + "a" * 64 if kind == "VISION" else "p4:compact-v7:single:0"
        ),
        "native_ordinal": 1,
        "reserved_microusd": reserve,
    }
    if kind == "VISION":
        hashes = {
            "original_sha256s": ["b" * 64, "c" * 64],
            "sent_sha256s": [hashlib.sha256(raw).hexdigest() for raw in IMAGES],
        }
        plan.update(hashes)
        binding.update(hashes)
    else:
        binding.update(
            run_id="run",
            scope_sha256="b" * 64,
            relevant_input_sha256="c" * 64,
            policy_catalogue_sha256="d" * 64,
            role_evidence={name: [REF] for name in policy.TOOLS},
        )
    return plan, binding


def _messages(plan, binding):
    if plan["kind"] == "VISION":
        content = []
        for index, image in enumerate(IMAGES, 1):
            content.append({"type": "text", "text": f"照片 {index}"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(image).decode(),
                        "detail": "low",
                    },
                }
            )
        return [
            SystemMessage(content="仅观察当前编号照片，返回一个 JSON 对象。"),
            HumanMessage(content=content),
        ]
    view = {
        "input_digest": binding["relevant_input_sha256"],
        "scope": [plan["conversation_id"], plan["issue_id"], plan["case_id"]],
        "evidence_encoding": "compact-v7",
        "available_evidence": [REF],
        "policy_context": {"catalogue_sha256": binding["policy_catalogue_sha256"]},
    }
    return [
        SystemMessage(content="调查角色 strong_single。"),
        HumanMessage(content=json.dumps({"frozen_input": view, "upstream_claims": []})),
    ]


def _request(plan, binding):
    content = _messages(plan, binding)[1].content
    if isinstance(content, list):
        content = [
            {"type": "input_text", "text": item["text"]}
            if item["type"] == "text"
            else {"type": "input_image", **item["image_url"]}
            for item in content
        ]
        for item in content:
            if item["type"] == "input_image":
                item["image_url"] = item.pop("url")
    return httpx.Request(
        "POST",
        policy.URL,
        json={
            "model": plan["model"],
            "max_output_tokens": plan["output_tokens"],
            "store": False,
            "input": [
                {"role": "system", "content": _messages(plan, binding)[0].content},
                {"role": "user", "content": content},
            ],
        },
        headers={"authorization": "Bearer " + KEY, "x-stainless-retry-count": "0"},
    )


def _lock(directory):
    (directory / "lock").touch(mode=0o600)


@pytest.mark.parametrize("kind", ["VISION", "TEXT"])
def test_real_sdk_serialization_is_admitted_only_under_native_binding(kind, tmp_path, monkeypatch):
    plan, binding = _fixture(kind)
    _lock(tmp_path)
    raw = json.dumps(plan).encode()
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(raw)
    plan_path.chmod(0o600)
    monkeypatch.setenv("P5_LIVE_BUSINESS_PLAN_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("AFTERCARE_OPENAI_KEY", KEY)
    monkeypatch.setitem(sys.modules, "policy", policy)
    sends = []

    def offline_handle(_transport, request):
        journal = json.loads((tmp_path / f"admission-{len(sends) + 1:02}.json").read_bytes())
        assert journal["status"] == "DELIVERY_UNKNOWN"
        assert journal["native_logical_id"] == (
            binding["native_logical_id"] if not sends else "p4:compact-v7:single:1"
        )
        sends.append(request)
        return httpx.Response(429, json={"error": {"message": "fixture"}})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline_handle)
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", ChatOpenAI._get_request_payload)
    monkeypatch.setattr(ChatOpenAI, "_generate", ChatOpenAI._generate)
    code = (DIRECTORY / "sitecustomize.py").read_text()
    namespace = {}
    exec(code.replace('Path("/live-business-wire")', f"Path({str(tmp_path)!r})"), namespace)  # noqa: S102
    assert namespace["READY"]
    sdk = ChatOpenAI(
        model=plan["model"],
        base_url="https://api.openai.com/v1",
        api_key=KEY,
        max_retries=0,
        max_tokens=plan["output_tokens"],
        use_responses_api=True,
        store=False,
    )
    with pytest.raises(policy.WireDenied, match="TRANSPORT_TARGET"):
        sdk.invoke(_messages(plan, binding))
    assert not sends
    with namespace["attempt"](binding), pytest.raises(RateLimitError):
        if kind == "VISION":
            Context().run(sdk.invoke, _messages(plan, binding))
        else:
            sdk.invoke(_messages(plan, binding))
    assert len(sends) == 1
    receipt = json.loads((tmp_path / "response-01.json").read_bytes())
    assert receipt["http_status"] == 429
    assert KEY.encode() not in (tmp_path / "admission-01.json").read_bytes()
    if kind == "TEXT":
        messages = [
            *_messages(plan, binding),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_fixture",
                        "name": "read_order_lines",
                        "args": {"line_ids": ["L01"]},
                    }
                ],
            ),
            ToolMessage(content="{}", tool_call_id="call_fixture"),
        ]
        next_binding = {**binding, "native_logical_id": "p4:compact-v7:single:1"}
        with namespace["attempt"](next_binding), pytest.raises(RateLimitError):
            sdk.invoke(messages)
        assert len(sends) == 2


def test_unknown_response_blocks_every_new_logical_attempt(tmp_path):
    plan, binding = _fixture("TEXT")
    _lock(tmp_path)
    sends = []

    def dropped(request):
        assert (tmp_path / "admission-01.json").exists()
        sends.append(request)
        raise httpx.ReadError("offline dropped response")

    with pytest.raises(httpx.ReadError):
        policy.send(tmp_path, plan, binding, _request(plan, binding), KEY, dropped)
    with pytest.raises(policy.WireDenied, match="UNKNOWN_DELIVERY"):
        policy.send(
            tmp_path,
            plan,
            {**binding, "native_logical_id": "p4:compact-v7:single:1"},
            _request(plan, binding),
            KEY,
            dropped,
        )
    assert len(sends) == 1 and not (tmp_path / "response-01.json").exists()


def test_known_response_cannot_be_resent_and_native_scope_stays_pinned(tmp_path):
    plan, binding = _fixture("TEXT")
    _lock(tmp_path)

    def send(_request):
        return httpx.Response(200, json={})

    request = _request(plan, binding)
    policy.send(tmp_path, plan, binding, request, KEY, send)
    with pytest.raises(policy.WireDenied, match="REPLAY"):
        policy.send(tmp_path, plan, binding, request, KEY, send)
    moved = {
        **binding,
        "native_logical_id": "p4:compact-v7:single:1",
        "run_id": "another-run",
        "relevant_input_sha256": "e" * 64,
    }
    with pytest.raises(policy.WireDenied, match="NATIVE_SCOPE_CHANGED"):
        policy.send(tmp_path, plan, moved, _request(plan, moved), KEY, send)
    assert len(list(tmp_path.glob("admission-*.json"))) == 1


def test_wrong_key_retry_ordinal_scope_tool_and_image_order_are_denied():
    plan, binding = _fixture("TEXT")
    request = _request(plan, binding)
    with pytest.raises(policy.WireDenied, match="AUTHORIZATION"):
        policy.verify_request(plan, binding, request, "wrong-key")
    with pytest.raises(policy.WireDenied, match="NATIVE_ATTEMPT_BINDING"):
        policy.verify_request(plan, {**binding, "native_ordinal": 2}, request, KEY)
    with pytest.raises(policy.WireDenied, match="NATIVE_ATTEMPT_BINDING"):
        policy.verify_request(plan, {**binding, "case_id": "another-case"}, request, KEY)
    payload = json.loads(request.content)
    payload["tools"] = [{"type": "function", "name": "approve_refund"}]
    with pytest.raises(policy.WireDenied, match="TEXT_TOOLS"):
        policy.verify_request(
            plan,
            binding,
            httpx.Request("POST", policy.URL, json=payload, headers=request.headers),
            KEY,
        )
    plan, binding = _fixture("VISION")
    request = _request(plan, binding)
    payload = json.loads(request.content)
    content = payload["input"][1]["content"]
    content[1], content[3] = content[3], content[1]
    with pytest.raises(policy.WireDenied, match="VISION_IMAGE_DIGEST"):
        policy.verify_request(
            plan,
            binding,
            httpx.Request("POST", policy.URL, json=payload, headers=request.headers),
            KEY,
        )


def test_text_gate_binds_fixed_wire_reserve_and_denies_ninth_post(tmp_path):
    plan, binding = _fixture("TEXT")
    _lock(tmp_path)
    native = {**binding, "reserved_microusd": 1000}
    posts = []

    def transport(request):
        posts.append(request)
        return httpx.Response(200, json={})

    for index in range(8):
        attempt = {**native, "native_logical_id": f"p4:compact-v7:single:{index}"}
        request = _request(plan, attempt)
        body = json.loads(request.content)
        body["input"][0]["content"] += str(index)
        policy.send(
            tmp_path,
            plan,
            attempt,
            httpx.Request("POST", policy.URL, json=body, headers=request.headers),
            KEY,
            transport,
        )
    with pytest.raises(policy.WireDenied, match="POST_CAP"):
        policy.send(tmp_path, plan, native, _request(plan, native), KEY, transport)
    assert len(posts) == 8
    receipts = [json.loads(path.read_bytes()) for path in tmp_path.glob("admission-*.json")]
    assert sum(row["reserved_microusd"] for row in receipts) == 8_000
    assert sum(row["wire_reserved_microusd"] for row in receipts) == 317_760


def test_v7_wrong_evidence_and_unpaired_tool_output_are_denied():
    plan, binding = _fixture("TEXT")
    request = _request(plan, binding)
    body = json.loads(request.content)
    message = body["input"][1]
    view = json.loads(message["content"])
    view["frozen_input"]["available_evidence"] = [["ORDER_LINE", "L02", "e" * 64]]
    message["content"] = json.dumps(view)
    with pytest.raises(policy.WireDenied, match="TEXT_EVIDENCE"):
        policy.verify_request(
            plan,
            binding,
            httpx.Request("POST", policy.URL, json=body, headers=request.headers),
            KEY,
        )
    body = json.loads(request.content)
    body["input"].append({"type": "function_call_output", "call_id": "unbound", "output": "{}"})
    with pytest.raises(policy.WireDenied, match="TEXT_TRANSCRIPT_TOOL"):
        policy.verify_request(
            plan,
            binding,
            httpx.Request("POST", policy.URL, json=body, headers=request.headers),
            KEY,
        )


def _scoped(plan, binding, route):
    customer, order, lines = policy.SCOPES[route]
    scope = {
        "customer_id": customer,
        "order_id": order,
        "line_ids": lines,
        "order_version": 1,
    }
    return (
        {**plan, "protocol": policy.SCOPED_PROTOCOL, "case_scope": scope, "expected_route": route},
        {**binding, "case_scope": scope, "expected_route": route},
    )


def test_v2_requires_pinned_fixture_order_version_lines_and_route():
    plan, binding = _fixture("TEXT")
    scoped, native = _scoped(plan, binding, "MULTI")
    policy.validate_plan(scoped)
    with pytest.raises(policy.WireDenied, match="NATIVE_ROUTE_PHASE"):
        policy.validate_binding(scoped, native)
    fact = {**native, "native_logical_id": "p4:compact-v7:fact:0"}
    policy.validate_binding(scoped, fact)
    with pytest.raises(policy.WireDenied, match="NATIVE_CASE_SCOPE_CHANGED"):
        policy.validate_binding(
            scoped, {**fact, "case_scope": {**fact["case_scope"], "order_version": 2}}
        )
    with pytest.raises(policy.WireDenied, match="PLAN_NATIVE_CASE_SCOPE"):
        policy.validate_plan({**scoped, "expected_route": "SINGLE"})
    with pytest.raises(policy.WireDenied, match="PLAN_NATIVE_CASE_SCOPE"):
        policy.validate_plan({**scoped, "protocol": policy.PROTOCOL})
    ordinary, native = _scoped(plan, binding, "SINGLE")
    policy.validate_plan(ordinary)
    policy.validate_binding(ordinary, native)
    with pytest.raises(policy.WireDenied, match="NATIVE_ROUTE_PHASE"):
        policy.validate_binding(
            ordinary, {**native, "native_logical_id": "p4:compact-v7:verify-0:0"}
        )


def test_v2_complex_fact_uses_same_wire_caps_and_journals_scoped_route(tmp_path):
    plan, binding = _scoped(*_fixture("TEXT"), "MULTI")
    native = {**binding, "native_logical_id": "p4:compact-v7:fact:0"}
    request = _request(plan, native)
    body = json.loads(request.content)
    body["input"][0]["content"] = "调查角色 fact。"
    content = json.loads(body["input"][1]["content"])
    del content["frozen_input"]["policy_context"]
    body["input"][1]["content"] = json.dumps(content)
    _lock(tmp_path)
    policy.send(
        tmp_path,
        plan,
        native,
        httpx.Request("POST", policy.URL, json=body, headers=request.headers),
        KEY,
        lambda _: httpx.Response(200, json={}),
    )
    receipt = json.loads((tmp_path / "admission-01.json").read_bytes())
    assert receipt["case_scope"] == plan["case_scope"]
    assert (receipt["expected_route"], receipt["role"], receipt["phase"]) == (
        "MULTI",
        "fact",
        "fact",
    )
    assert receipt["wire_reserved_microusd"] == 39_720


@pytest.mark.parametrize(
    ("stage", "exception_type", "admitted"),
    [
        ("serialize", ValueError, False),
        ("validate", policy.WireDenied, False),
        ("send", httpx.ConnectError, True),
        ("read", httpx.ReadError, True),
    ],
)
def test_scoped_fault_journal_classifies_safe_stage_without_raw_data(
    stage, exception_type, admitted, tmp_path, monkeypatch
):
    plan, binding = _fixture("TEXT")
    _lock(tmp_path)
    raw = json.dumps(plan).encode()
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(raw)
    plan_path.chmod(0o600)
    monkeypatch.setenv("P5_LIVE_BUSINESS_PLAN_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("AFTERCARE_OPENAI_KEY", KEY)
    monkeypatch.setitem(sys.modules, "policy", policy)
    mock_calls = []
    raw_sensitive = "dummy-sensitive-message-never-journal"

    def broken_payload(*_args, **_kwargs):
        raise ValueError(raw_sensitive)

    class UnreadableResponse:
        def read(self):
            raise httpx.ReadError(raw_sensitive)

    def offline_handle(_transport, _request):
        mock_calls.append(True)
        if stage == "send":
            raise httpx.ConnectError(raw_sensitive)
        if stage == "read":
            return UnreadableResponse()
        pytest.fail("validation or serialization sent a provider request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline_handle)
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", broken_payload)
    monkeypatch.setattr(ChatOpenAI, "_generate", ChatOpenAI._generate)
    code = (DIRECTORY / "sitecustomize.py").read_text()
    namespace = {}
    exec(code.replace('Path("/live-business-wire")', f"Path({str(tmp_path)!r})"), namespace)  # noqa: S102

    with namespace["attempt"](binding), pytest.raises(exception_type):
        if stage == "serialize":
            namespace["_payload"](None)
        else:
            request = _request(plan, binding)
            if stage == "validate":
                request.headers["authorization"] = "Bearer " + raw_sensitive
            namespace["_handle"](None, request)

    (fault,) = list(tmp_path.glob("fault-*.json"))
    saved = fault.read_bytes()
    assert raw_sensitive.encode() not in saved and KEY.encode() not in saved
    record = json.loads(saved)
    assert record == {
        "kind": "TEXT",
        "native_logical_id": binding["native_logical_id"],
        "native_ordinal": 1,
        "stage": stage,
        "exception_type": exception_type.__name__,
        "wire_denial_code": "LIVE_BUSINESS_WIRE_REQUEST_AUTHORIZATION"
        if stage == "validate"
        else None,
        "admitted": admitted,
        "admission_index": 1 if admitted else None,
        "admission_file_present": admitted,
    }
    assert bool(mock_calls) is admitted
    assert not list(tmp_path.glob("response-*.json"))


@pytest.mark.parametrize("isolate_cookies", [False, True])
def test_default_private_sdk_reuses_response_cookie_across_rounds(
    isolate_cookies, tmp_path, monkeypatch
):
    from langchain_openai.chat_models import _client_utils

    from aftercare.intelligence.models import ModelSpec, _private_transport

    plan, binding = _fixture("TEXT")
    if isolate_cookies:
        plan, binding = _scoped(plan, binding, "SINGLE")
    _lock(tmp_path)
    raw = json.dumps(plan).encode()
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(raw)
    plan_path.chmod(0o600)
    monkeypatch.setenv("P5_LIVE_BUSINESS_PLAN_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv("AFTERCARE_OPENAI_KEY", KEY)
    monkeypatch.setitem(sys.modules, "policy", policy)
    cached = _client_utils._cached_sync_httpx_client
    # Isolate test cache without clearing the process-global SDK client cache.
    from functools import lru_cache

    monkeypatch.setattr(_client_utils, "_cached_sync_httpx_client", lru_cache()(cached.__wrapped__))
    provider_calls = []

    def offline_handle(_transport, request):
        provider_calls.append("cookie" in request.headers)
        return httpx.Response(
            200,
            headers={"set-cookie": "synthetic_session=fixture; Secure; Path=/"},
            json={
                "id": "resp_fixture",
                "object": "response",
                "created_at": 0,
                "model": plan["model"],
                "status": "completed",
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "{}", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline_handle)
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(ChatOpenAI, "_get_request_payload", ChatOpenAI._get_request_payload)
    monkeypatch.setattr(ChatOpenAI, "_generate", ChatOpenAI._generate)
    code = (DIRECTORY / "sitecustomize.py").read_text()
    namespace = {}
    exec(code.replace('Path("/live-business-wire")', f"Path({str(tmp_path)!r})"), namespace)  # noqa: S102
    spec = ModelSpec.model_validate(
        {
            "alias": "analysis",
            "provider": "openai",
            "model": plan["model"],
            "endpoint": "https://api.openai.com/v1",
            "interface": "responses",
            "api_key_env": "AFTERCARE_OPENAI_KEY",
            "context_tokens": 32000,
            "output_tokens": 1800,
            "input_usd_per_million": "1",
            "output_usd_per_million": "1",
            "price_date": "2026-11-11",
            "source": "offline fixture",
            "data_scopes": ["customer_conversation"],
        }
    )
    with namespace["attempt"](binding):
        response = _private_transport(spec, _messages(plan, binding), [], 5, 1800)
        assert response.content[0]["text"] == "{}"
    followup = [
        *_messages(plan, binding),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_fixture", "name": "read_order_lines", "args": {"line_ids": ["L01"]}}
            ],
        ),
        ToolMessage(content="{}", tool_call_id="call_fixture"),
    ]
    next_binding = {**binding, "native_logical_id": "p4:compact-v7:single:1"}
    if isolate_cookies:
        with namespace["attempt"](next_binding):
            response = _private_transport(spec, followup, [], 5, 1800)
            assert response.content[0]["text"] == "{}"
            assert response.usage_metadata["total_tokens"] == 15
        assert provider_calls == [False, False]
        for index in (1, 2):
            row = json.loads((tmp_path / f"response-{index:02}.json").read_bytes())
            assert row["http_status"] == 200 and row["set_cookie_discarded"] is True

        request = _request(plan, binding)
        request.headers["cookie"] = "synthetic_outgoing=fixture"
        with pytest.raises(policy.WireDenied, match="REQUEST_COOKIE"):
            policy.verify_request(plan, binding, request, KEY)
    else:
        with (
            namespace["attempt"](next_binding),
            pytest.raises(policy.WireDenied, match="REQUEST_COOKIE"),
        ):
            _private_transport(spec, followup, [], 5, 1800)
        assert provider_calls == [False]


@pytest.mark.parametrize(
    ("header", "value", "code"),
    [
        ("x-stainless-retry-count", "1", "REQUEST_RETRY_HEADER"),
        ("content-type", "text/plain", "REQUEST_CONTENT_TYPE"),
        ("cookie", "", "REQUEST_COOKIE"),
        ("proxy-authorization", "fixture", "REQUEST_PROXY_AUTHORIZATION"),
        ("openai-project", "fixture", "REQUEST_PROJECT"),
        ("openai-organization", "fixture", "REQUEST_ORGANIZATION"),
    ],
)
def test_request_header_failures_have_distinct_safe_codes(header, value, code):
    plan, binding = _fixture("TEXT")
    request = _request(plan, binding)
    request.headers[header] = value
    with pytest.raises(policy.WireDenied, match=f"^LIVE_BUSINESS_WIRE_{code}$"):
        policy.verify_request(plan, binding, request, KEY)
