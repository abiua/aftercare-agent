"""Controlled model responses and a real local audit. No provider requests."""

import base64
import hashlib
import io
import json
from datetime import date
from decimal import Decimal

import pytest
import test_transport_evidence
from langchain_core.messages import AIMessage
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.rules import digest
from aftercare.intelligence.models import ModelFailure, ModelRunner, ModelSpec, Registry
from aftercare.intelligence.tables import ModelAttempt, RunEvent
from aftercare.intelligence.vision import PhotoSource, observe, prepare_photos

STAMP = date(2026, 1, 1)
vision_ledger = test_transport_evidence.ledger


def _registry(*, enabled=True, scope="simulated_case", context_tokens=16000):
    return Registry(
        version="vision-fixture",
        max_attempts=12,
        max_usd=Decimal("0.1"),
        native_streaming=True,
        models=[
            ModelSpec(
                alias="economy",
                provider="fixture",
                model="test",
                endpoint="https://example.invalid",
                api_key_env="AFTERCARE_TEST_TRANSPORT_KEY",
                context_tokens=context_tokens,
                compatibility="VERIFIED",
                compatibility_version="text-fixture",
                checked_at=STAMP,
                source="controlled fixture",
                input_usd_per_million=1,
                output_usd_per_million=1,
                price_date=STAMP,
                vision=enabled,
                vision_checked_at=STAMP if enabled else None,
                vision_compatibility_version="fixture" if enabled else None,
                vision_input_tokens_per_image=3000 if enabled else None,
                data_scopes=[scope],
            )
        ],
    )


def _inputs():
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[270] = "private metadata"
    Image.new("RGB", (920, 420), "blue").save(buffer, format="PNG", exif=exif)
    data = buffer.getvalue()
    source = PhotoSource(
        "asset", "message", hashlib.sha256(data).hexdigest(), "image/png", len(data)
    )
    return source, data


def _photos():
    source, data = _inputs()
    return prepare_photos([source], lambda key: data if key == source.attachment_id else b"")


def _observe(model, audit, photos=None, scope="simulated_case"):
    return observe(
        model,
        audit,
        photos or _photos(),
        preferred="economy",
        input_binding="turn-generation-1",
        data_scope=scope,
    )


def _response():
    return {
        "photos": [
            {
                "image_index": 1,
                "candidate_kind": "ITEM",
                "uncertainty": "LIMITED",
                "findings": ["POSSIBLE_CRACK"],
            }
        ]
    }


def test_prepared_jpeg_references_verified_original_and_strips_metadata():
    (photo,) = _photos()
    assert photo.width == 768 and photo.height < 420
    assert photo.audit_ref()["sent_sha256"] == hashlib.sha256(photo.content).hexdigest()
    with Image.open(io.BytesIO(photo.content)) as image:
        assert image.format == "JPEG"
        assert not image.getexif() and not image.info.get("icc_profile")
    source, data = _inputs()
    with pytest.raises(BusinessError, match="对象与登记"):
        prepare_photos([source], lambda _: data[:-1])
    with pytest.raises(BusinessError, match="类型与登记"):
        changed = PhotoSource("asset", "message", source.original_sha256, "image/webp", source.size)
        prepare_photos([changed], lambda _: data)


def test_legacy_registry_digest_unchanged():
    text_only = _registry(enabled=False)
    current = text_only.model_dump(mode="json")
    assert all(not key.startswith("vision") for key in current["models"][0])
    old = text_only.model_dump(
        mode="json",
        exclude={
            "models": {
                0: {
                    "vision",
                    "vision_checked_at",
                    "vision_compatibility_version",
                    "vision_input_tokens_per_image",
                }
            }
        },
    )
    assert digest(current) == digest(old) == ModelRunner(text_only).base_registry_digest
    assert digest(current) == "e850fd2ea15fc393bfbc6e675c95310bc0bb6d87b36040df1632106cbccde88d"


def test_vision_audit_and_replay_bind_actual_bytes_and_reservation(vision_ledger):
    engine, audit = vision_ledger
    transmitted = []

    def transport(spec, messages, tools, timeout, output):
        url = messages[-1].content[1]["image_url"]["url"]
        transmitted.append(base64.b64decode(url.split(",", 1)[1]))
        assert tools == [] and output == 900
        return AIMessage(
            content=json.dumps(_response()),
            usage_metadata={"input_tokens": 200, "output_tokens": 30, "total_tokens": 230},
        )

    model = ModelRunner(
        _registry(), transport=transport, stream_transport=lambda *_: pytest.fail("stream")
    )
    result = _observe(model, audit)
    assert result == _observe(model, audit)
    (row,) = result["observations"]
    assert row["verification_state"] == "UNVERIFIED" and row["source_kind"] == "MODEL_OBSERVATION"
    assert row["sent_sha256"] == hashlib.sha256(transmitted[0]).hexdigest()
    assert len(transmitted) == 1
    with Session(engine) as session:
        attempt = session.scalars(select(ModelAttempt)).one()
        assert Decimal(attempt.metadata_json["reserved_cost_usd"]) >= Decimal("0.0039")
        assert attempt.status == "SUCCEEDED"
        assert attempt.usage["total_tokens"] == 230
        events = session.scalars(select(RunEvent)).all()
        assert not any("base64" in repr(event.payload) for event in events)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("bad-output", "INVALID_MODEL_OUTPUT"),
        ("unknown", "MODEL_TIMEOUT"),
    ],
)
def test_vision_failure_has_one_attempt_and_no_automatic_retry(vision_ledger, status, code):
    engine, audit = vision_ledger

    def transport(*args):
        if status == "unknown":
            raise TimeoutError
        data = _response()
        data["photos"][0]["image_index"] = 2
        return AIMessage(content=json.dumps(data))

    with pytest.raises(ModelFailure) as exc:
        _observe(ModelRunner(_registry(), transport=transport), audit)
    assert exc.value.code == code
    with pytest.raises(ModelFailure) as repeat:
        _observe(ModelRunner(_registry(), transport=transport), audit)
    assert repeat.value.code == code
    with Session(engine) as session:
        attempt = session.scalars(select(ModelAttempt)).one()
        assert attempt.status == ("UNKNOWN" if status == "unknown" else "FAILED")


@pytest.mark.parametrize(
    ("settings", "scope"),
    [
        ({"enabled": False}, "simulated_case"),
        ({}, "customer_attachment"),
        ({"context_tokens": 3000}, "simulated_case"),
    ],
)
def test_vision_unverified_scope_or_context_never_admits(vision_ledger, settings, scope):
    engine, audit = vision_ledger
    model = ModelRunner(
        _registry(**settings), transport=lambda *_: pytest.fail("unexpected provider")
    )
    with pytest.raises(ModelFailure, match="NO_COMPATIBLE_MODEL"):
        _observe(model, audit, scope=scope)
    with Session(engine) as session:
        assert session.scalars(select(ModelAttempt)).first() is None


@pytest.mark.parametrize("cap", [None, Decimal("0.000001")])
def test_vision_budget_blocks_transport(vision_ledger, cap):
    engine, audit = vision_ledger
    registry = _registry().model_copy(update={"max_usd": cap})
    model = ModelRunner(registry, transport=lambda *_: pytest.fail("unexpected provider"))
    with pytest.raises(ModelFailure) as exc:
        _observe(model, audit)
    assert exc.value.code == (
        "VISION_BUDGET_REQUIRED" if cap is None else "MODEL_COST_BUDGET_EXHAUSTED"
    )
    with Session(engine) as session:
        assert session.scalars(select(ModelAttempt)).first() is None


@pytest.mark.parametrize("interface", ["chat", "responses"])
def test_langchain_serializes_jpeg_as_provider_image(interface, monkeypatch, capsys):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.globals import get_debug, set_debug
    from langchain_core.runnables.config import var_child_runnable_config

    from aftercare.intelligence.models import _transport
    from aftercare.intelligence.vision import _messages

    captured = []
    callbacks = []

    class ExternalCallback(BaseCallbackHandler):
        def on_chat_model_start(self, *args, **kwargs):
            callbacks.append("called")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            content = json.dumps(_response())
            if interface == "chat":
                result = {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content},
                        }
                    ]
                }
            else:
                result = {
                    "id": "resp_fixture",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg_fixture",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": content, "annotations": []},
                            ],
                        }
                    ],
                }
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    monkeypatch.setenv("AFTERCARE_TEST_TRANSPORT_KEY", "local-fixture-only")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    photos = _photos()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            spec = (
                _registry()
                .models[0]
                .model_copy(
                    update={
                        "interface": interface,
                        "endpoint": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                )
            )
            token = var_child_runnable_config.set({"callbacks": [ExternalCallback()]})
            debug = get_debug()
            try:
                set_debug(True)
                result = _transport(spec, _messages(photos), [], 2, 900)
            finally:
                set_debug(debug)
                var_child_runnable_config.reset(token)
            assert json.loads(result.text) == _response()
        finally:
            server.shutdown()
            thread.join(timeout=3)
    (request,) = captured
    if interface == "chat":
        block = request["messages"][-1]["content"][-1]
        assert block["type"] == "image_url"
        value = block["image_url"]
        assert value["detail"] == "low"
        data_url = value["url"]
    else:
        block = request["input"][-1]["content"][-1]
        assert block["type"] == "input_image" and block["detail"] == "low"
        data_url = block["image_url"]
    assert base64.b64decode(data_url.split(",", 1)[1]) == photos[0].content
    assert not callbacks
    assert "base64" not in repr(capsys.readouterr())
