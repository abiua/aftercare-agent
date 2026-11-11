from decimal import Decimal

import pytest
from aftercare.auth import UserContext, current_user
from aftercare.business.errors import BusinessError
from aftercare.intake import (
    BoundedIntakeClassifier,
    ConfirmInput,
    IntakeService,
    PreviewInput,
    router,
    rule_intents,
)
from aftercare.intelligence.models import ModelFailure, ModelRunner, Registry
from fastapi import FastAPI
from fastapi.testclient import TestClient

CUSTOMER = UserContext(user_id="u1", role="customer", customer_id="C001")
OUTSIDER = UserContext(user_id="u2", role="customer", customer_id="C002")
SUPPORT = UserContext(user_id="support", role="support")
KEY = "stable-intake"
OWN = {"id": "O0001", "version": 3, "status": "DELIVERED", "customer_id": "C001"}
SECOND = {"id": "O0002", "version": 1, "status": "SHIPPED", "customer_id": "C001"}
FOREIGN = {"id": "O0099", "version": 5, "status": "DELIVERED", "customer_id": "C099"}


class Business:
    def __init__(self, orders=(OWN,)):
        self.visible = orders
        self.created = []

    def orders(self, user, *, limit, offset):
        assert user.role == "customer"
        return self.visible[offset : offset + limit]

    def create_case(self, user, **args):
        self.created.append((user, args))
        return {"id": "case-1", "intent": args["intent"]}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("商品开箱发现破损", ["damaged"]),
        ("包裹收到了，型号不对", ["wrong_item"]),
        ("帮我查一下退款状态", ["refund_status"]),
        ("货没有破损，退款到哪了", ["refund_status"]),
        ("收到的是错发商品，退款进度如何", ["wrong_item", "refund_status"]),
        ("订单商品怎么样了", []),
    ],
)
def test_rule_classification_is_explicit_and_conservative(text, expected):
    assert rule_intents(text) == expected


def test_preview_matches_authorized_order_without_creating_case():
    business = Business((OWN, FOREIGN))
    service = IntakeService(business)
    result = service.preview(CUSTOMER, PreviewInput(message="我收到订单o0001的商品裂了"))
    assert result["status"] == "READY"
    assert result["intent"] == "damaged" and result["classifier_source"] == "rules"
    assert result["order"] == {"id": "O0001", "status": "DELIVERED", "version": 3}
    assert result["order_candidates"] == [result["order"]]
    assert business.created == []


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("商品破损但我没填单号", "ORDER_REQUIRED"),
        ("订单O0001和O0002的商品都破损", "MULTIPLE_ORDERS"),
        ("订单O0099的商品破损", "ORDER_NOT_AVAILABLE"),
        ("订单O0001和O0099的商品都破损", "MULTIPLE_ORDERS"),
    ],
)
def test_missing_multiple_or_unauthorized_order_requires_clarification(message, reason):
    business = Business((OWN, SECOND, FOREIGN))
    result = IntakeService(business).preview(CUSTOMER, PreviewInput(message=message))
    assert result["order"] is None and result["reason"] == reason
    assert result["status"] == "CLARIFICATION_REQUIRED" and "order" in result["required"]
    assert {item["id"] for item in result["order_candidates"]} == {"O0001", "O0002"}
    assert not business.created


def test_intake_does_not_assume_single_owned_order_and_does_not_read_other_customer():
    service = IntakeService(Business((OWN, FOREIGN)))
    single = service.preview(CUSTOMER, PreviewInput(message="收到的商品有明显破损"))
    assert single["reason"] == "ORDER_REQUIRED" and len(single["order_candidates"]) == 1
    outsider = service.preview(OUTSIDER, PreviewInput(message="订单O0001的商品有破损"))
    assert outsider["order"] is None and outsider["order_candidates"] == []
    with pytest.raises(BusinessError, match="本人"):
        service.preview(SUPPORT, PreviewInput(message="订单O0001商品破损"))


def test_confirmation_delegates_only_explicit_authorized_case_creation():
    business = Business((OWN, FOREIGN))
    service = IntakeService(business)
    data = ConfirmInput(
        message="我收到的商品型号不对",
        order_id="O0001",
        intent="wrong_item",
        expected_version=3,
        confirmed=True,
        line_ids=["L01"],
    )
    assert service.confirm(CUSTOMER, data, KEY)["id"] == "case-1"
    assert business.created[0][1] == {
        "order_id": "O0001",
        "intent": "wrong_item",
        "description": "我收到的商品型号不对",
        "line_ids": ["L01"],
        "expected_version": 3,
        "idempotency_key": KEY,
    }
    with pytest.raises(BusinessError, match="订单不存在"):
        service.confirm(CUSTOMER, data.model_copy(update={"order_id": "O0099"}), KEY)
    assert len(business.created) == 1


def test_optional_model_gets_only_ambiguous_text_and_actor_key():
    calls = []

    def classify(**kwargs):
        calls.append(kwargs)
        return "refund_status"

    service = IntakeService(Business(), classifier=classify)
    ambiguous = PreviewInput(message="订单O0001这事处理到哪了", classifier="model")
    result = service.preview(CUSTOMER, ambiguous, KEY)
    assert result["classifier_source"] == "model" and result["intent"] == "refund_status"
    assert calls == [
        {"text": ambiguous.message, "actor_id": "u1", "customer_id": "C001", "request_key": KEY}
    ]
    service.preview(CUSTOMER, PreviewInput(message="订单O0001的商品破损", classifier="model"), KEY)
    assert len(calls) == 1


def test_failed_model_returns_explicit_intent_clarification():
    def fail(**_):
        raise ModelFailure("MODEL_BUDGET_EXHAUSTED")

    result = IntakeService(Business(), classifier=fail).preview(
        CUSTOMER,
        PreviewInput(message="订单O0001该怎么处理", classifier="model"),
        KEY,
    )
    assert result["classifier_source"] == "model_unavailable"
    assert result["required"] == ["intent"] and result["intent"] is None


def test_http_intake_requires_literal_confirmation_and_keeps_preview_read_only():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: CUSTOMER
    app.state.business = Business()
    with TestClient(app) as client:
        response = client.post("/api/intake/preview", json={"message": "订单O0001商品破损"})
        assert response.status_code == 200 and response.json()["status"] == "READY"
        assert app.state.business.created == []
        body = {
            "message": "订单O0001商品破损",
            "order_id": "O0001",
            "intent": "damaged",
            "expected_version": 3,
            "confirmed": 1,
        }
        headers = {"Idempotency-Key": KEY}
        assert client.post("/api/intake/confirm", json=body, headers=headers).status_code == 422
        body["confirmed"] = True
        assert client.post("/api/intake/confirm", json=body, headers=headers).status_code == 201
    assert len(app.state.business.created) == 1


def test_bounded_live_classifier_requires_global_provider_limits():
    plain = ModelRunner(Registry(version="controlled", models=[]))
    with pytest.raises(ValueError, match="provider limits"):
        BoundedIntakeClassifier(plain, lambda *_: None, max_usd=Decimal("0.01"))


def test_bounded_intake_runs_one_tool_free_attempt_and_replays_the_audit():
    from langchain_core.messages import AIMessage

    attempts = []
    scope = Registry.model_validate(
        {
            "version": "controlled",
            "allow_unverified": True,
            "provider_limits": {
                "fixture": {"scope": "account", "max_in_flight": 1, "requests_per_minute": 2},
            },
            "models": [
                {
                    "alias": "economy",
                    "provider": "fixture",
                    "model": "fixture-a",
                    "api_key_env": "TEST_KEY",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "input_usd_per_million": "1",
                    "output_usd_per_million": "1",
                    "price_date": "2026-11-10",
                    "source": "controlled fixture",
                }
            ],
        }
    )

    class Audit:
        result = None

        def replay(self, _logical):
            return self.result

        def cooldown_aliases(self, _seconds):
            return set()

        def budget(self, registry):
            return {"attempts_limit": registry.max_attempts}

        def event(self, *_):
            pass

        def claim_provider(self, spec, registry):
            assert spec.provider in registry.provider_limits
            return "fixture-slot"

        def release_provider(self, slot, *, attempted, **_):
            assert slot == "fixture-slot" and attempted

        def begin_attempt(
            self,
            logical,
            ordinal,
            _spec,
            reserve,
            registry,
            *,
            transport_evidence=None,
            base_registry_digest=None,
        ):
            assert logical == "intake:classify" and ordinal == 1
            assert registry.max_attempts == registry.max_run_attempts == 1
            assert reserve < registry.max_usd == registry.max_run_usd == Decimal("0.01")
            return 8

        def finish_attempt(
            self,
            _logical,
            _ordinal,
            status,
            _code,
            _usage,
            _price,
            result,
            *,
            transport_evidence=None,
        ):
            assert status == "SUCCEEDED"
            self.result = result

        def confirm_admission(self):
            pass

    audit = Audit()
    messages = "订单O0001这次的事情还在处理吗"

    def transport(_spec, transcript, tools, timeout, _output):
        assert not tools and timeout <= 8
        assert transcript[-1].content == messages
        assert len(transcript) == 2
        attempts.append(transcript[-1].content)
        return AIMessage(content='{"intent":"refund_status"}')

    classifier = BoundedIntakeClassifier(
        ModelRunner(scope, transport=transport),
        lambda *_: audit,
        max_usd=Decimal("0.01"),
    )
    kwargs = {"text": messages, "actor_id": "u1", "customer_id": "C001", "request_key": KEY}
    assert classifier(**kwargs) == classifier(**kwargs) == "refund_status"
    assert attempts == [messages]


def _audit_fixture():
    from aftercare.db import User
    from aftercare.intake_audit import IntakeAuditFactory, IntakeModelAudit
    from aftercare.intelligence.model_limits import ProviderRateLease, ProviderRateScope
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite://")
    for table in (User, IntakeModelAudit, ProviderRateScope, ProviderRateLease):
        table.__table__.create(engine)
    with Session(engine) as db, db.begin():
        db.add(
            User(
                id=CUSTOMER.user_id,
                role="customer",
                token_hash="fixture",
                customer_id="C001",
                display_name="Fixture",
                enabled=True,
            )
        )
    registry = Registry.model_validate(
        {
            "version": "controlled",
            "allow_unverified": True,
            "provider_limits": {
                "fixture": {"scope": "account", "max_in_flight": 1, "requests_per_minute": 5},
            },
            "models": [
                {
                    "alias": "economy",
                    "provider": "fixture",
                    "model": "fixture-a",
                    "api_key_env": "TEST_KEY",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "input_usd_per_million": "1",
                    "output_usd_per_million": "1",
                    "price_date": "2026-11-10",
                    "source": "controlled fixture",
                }
            ],
        }
    )
    return engine, registry, IntakeAuditFactory(engine)


@pytest.mark.parametrize("fails", [False, True])
def test_durable_intake_replays_success_or_retains_unknown_without_new_call(fails):
    from aftercare.intake_audit import IntakeModelAudit
    from langchain_core.messages import AIMessage
    from sqlalchemy.orm import Session

    engine, registry, factory = _audit_fixture()
    requests = []
    message = "订单O0001现在还在处理吗"

    def transport(_spec, transcript, tools, *_):
        assert not tools and transcript[-1].content == message
        requests.append(1)
        if fails:
            raise TimeoutError
        return AIMessage(
            content='{"intent":null}',
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )

    classifier = BoundedIntakeClassifier(
        ModelRunner(registry, transport=transport),
        factory,
        max_usd=Decimal("0.01"),
    )
    service = IntakeService(Business(), classifier)
    data = PreviewInput(message=message, classifier="model")
    first = service.preview(CUSTOMER, data, KEY)
    repeated = service.preview(CUSTOMER, data, KEY)
    assert first["intent"] is repeated["intent"] is None
    assert repeated["classifier_source"] == ("model_unavailable" if fails else "model")
    assert requests == [1]
    with Session(engine) as db:
        row = db.get_one(IntakeModelAudit, (CUSTOMER.user_id, KEY))
        assert row.status == ("UNKNOWN" if fails else "SUCCEEDED")
        assert row.estimated_cost_usd == (None if fails else "0.00001")
        assert row.usage is None if fails else row.usage["total_tokens"] == 10
        assert message not in str(row.metadata_json) and message not in str(row.trace)
    with pytest.raises(BusinessError) as error:
        service.preview(
            CUSTOMER, PreviewInput(message="订单O0001这事有人管吗", classifier="model"), KEY
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert requests == [1]


def test_interrupted_intake_reservation_cannot_restart_and_auth_revocation_is_checked():
    from datetime import UTC, datetime, timedelta

    from aftercare.db import User
    from aftercare.intake_audit import IntakeModelAudit
    from sqlalchemy.orm import Session

    engine, registry, factory = _audit_fixture()
    audit = factory("u1", "C001", KEY, "fixture-digest")
    model = BoundedIntakeClassifier(
        ModelRunner(registry),
        factory,
        max_usd=Decimal("0.01"),
    ).model
    audit.begin_attempt("intake:classify", 1, registry.models[0], Decimal("0.001"), model.registry)
    with pytest.raises(ModelFailure, match="INTAKE_ALREADY_RUNNING"):
        audit.replay("intake:classify")
    with Session(engine) as db, db.begin():
        row = db.get_one(IntakeModelAudit, ("u1", KEY))
        row.deadline = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ModelFailure, match="INTAKE_INTERRUPTED"):
        audit.replay("intake:classify")
    with Session(engine) as db, db.begin():
        row = db.get_one(IntakeModelAudit, ("u1", KEY))
        assert row.status == "UNKNOWN" and row.usage is None
        db.get_one(User, "u1").enabled = False
    with pytest.raises(ModelFailure, match="INTAKE_ACTOR_REVOKED"):
        factory("u1", "C001", KEY, "fixture-digest")
