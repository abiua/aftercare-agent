"""Controlled scheduling and transport tests, without shared services."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aftercare.business.errors import BusinessError
from aftercare.business.models import Case, Customer, Order
from aftercare.conversation_analysis.audit import ChatAnalysisAudit
from aftercare.conversation_analysis.service import AnalysisLimits, AnalysisService
from aftercare.conversation_analysis.tables import ChatAnalysisTurn, ChatModelAttempt, Observation
from aftercare.conversations.tables import Attachment, Conversation, Message
from aftercare.db import User
from aftercare.intelligence.models import ModelFailure

OWNER = "customer-one"
CONVERSATION = "conversation-one"


@pytest.fixture
def fixture():
    engine = create_engine("sqlite://")
    for table in (
        User,
        Customer,
        Order,
        Case,
        Conversation,
        Message,
        Attachment,
        ChatAnalysisTurn,
        ChatModelAttempt,
        Observation,
    ):
        table.__table__.create(engine)
    clock = SimpleNamespace(value=datetime(2026, 11, 11, tzinfo=UTC))
    service = AnalysisService(
        engine, AnalysisLimits(2, 100, 2, 6, 10, 20, 5), clock=lambda: clock.value
    )
    with Session(engine) as db, db.begin():
        db.add(
            User(
                id=OWNER,
                role="customer",
                customer_id=OWNER,
                enabled=True,
                token_hash="t" * 64,
                display_name="fixture",
            )
        )
        db.add(
            Conversation(
                id=CONVERSATION,
                customer_id=OWNER,
                status="OPEN",
                revision=0,
                sequence=0,
                active_order_id=None,
                active_case_id=None,
                line_ids=[],
                input_floor=0,
                require_new_case=False,
                created_at=clock.value,
                updated_at=clock.value,
            )
        )
    yield SimpleNamespace(engine=engine, service=service, clock=clock)
    engine.dispose()


def _advance(fixture, seconds):
    fixture.clock.value += timedelta(seconds=seconds)


def _message(fixture, *, pending=False, count=1, stage=True):
    with Session(fixture.engine) as db, db.begin():
        conversation = db.get_one(Conversation, CONVERSATION)
        ident = str(uuid4())
        if not pending:
            conversation.sequence += 1
            conversation.revision += 1
        db.add(
            Message(
                id=ident,
                conversation_id=CONVERSATION,
                actor_id=OWNER,
                sequence=None if pending else conversation.sequence,
                role="customer",
                text="  已收到，有问题。  ",
                cards=[],
                created_at=fixture.clock.value,
            )
        )
        for pos in range(count):
            db.add(
                Attachment(
                    id=str(uuid4()),
                    message_id=ident,
                    position=pos,
                    filename=f"fixture-{pos}.png",
                    object_key=str(uuid4()),
                    content_type="image/png",
                    size=10,
                    sha256="a" * 64,
                )
            )
        if pending or not stage:
            return
        turn = fixture.service.stage(db, conversation, input_from_seq=1)
        db.flush()
        return turn.id


def _claim(fixture):
    ident = _message(fixture)
    _advance(fixture, 2)
    assert fixture.service.due() == [ident]
    lease = fixture.service.claim(ident)
    assert lease and fixture.service.claim(ident) is None
    return lease


def _bound(fixture, lease):
    attachment = fixture.service.input_snapshot(lease)["attachments"][-1]
    data = {
        "attachment_id": attachment["id"],
        "message_id": attachment["message_id"],
        "original_sha256": attachment["sha256"],
        "sent_sha256": "b" * 64,
        "transform_version": "fixture-v1",
    }
    fixture.service.bind_vision_input(
        lease.turn_id, lease.token, {"logical_id": "vision:fixture", "photos": [data]}
    )
    return {key: value for key, value in data.items() if key != "message_id"} | {
        "source_kind": "MODEL_OBSERVATION",
        "verification_state": "UNVERIFIED",
        "candidate_kind": "ITEM",
        "uncertainty": "LIMITED",
        "findings": ["POSSIBLE_CRACK"],
        "logical_id": "vision:fixture",
    }


def _reserve(fixture, lease, cost):
    return fixture.service.reserve(
        lease.turn_id, lease.token, cost, {"logical_id": "vision:fixture"}
    )[0]


def _validated(observation):
    return {
        "result": {
            "photos": [
                {
                    "image_index": 1,
                    **{
                        key: observation[key]
                        for key in ("candidate_kind", "uncertainty", "findings")
                    },
                }
            ]
        }
    }


def test_committed_generations_group_photos_without_sliding_the_hard_deadline(fixture):
    first = _message(fixture, count=0)
    _advance(fixture, 2)
    second = _message(fixture, count=2)
    _advance(fixture, 3)
    third = _message(fixture, count=1)
    assert fixture.service.due() == []
    _advance(fixture, 1)
    latest = _message(fixture, count=0)
    assert fixture.service.due() == [latest]
    assert (
        fixture.service.claim(first)
        is fixture.service.claim(second)
        is fixture.service.claim(third)
        is None
    )
    lease = fixture.service.claim(latest)
    snapshot = fixture.service.input_snapshot(lease)
    assert snapshot["generation"] == 4
    assert [(photo["sequence"], photo["position"]) for photo in snapshot["attachments"]] == [
        (2, 0),
        (2, 1),
        (3, 0),
    ]


def test_only_committed_uploads_enter_input_and_wait_for_staged_batch_is_bounded(fixture):
    first = _message(fixture)
    _advance(fixture, 1)
    _message(fixture, pending=True, count=2)
    _advance(fixture, 2)
    assert fixture.service.due() == []
    assert fixture.service.claim(first) is None
    _advance(fixture, 7)
    assert fixture.service.due() == [first]
    assert len(fixture.service.input_snapshot(fixture.service.claim(first))["attachments"]) == 1


def test_stale_response_stays_in_audit_and_cannot_publish(fixture):
    lease = _claim(fixture)
    observation = _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 90)
    new_turn = _message(fixture)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={"input_tokens": 2},
        estimated_cost_microusd=20,
        response_evidence={"response": "SDK_RETURNED"},
    )
    with pytest.raises(BusinessError, match="无法继续"):
        fixture.service.publish(
            lease.turn_id, lease.token, [observation], lambda *_: pytest.fail("stale publish")
        )
    with Session(fixture.engine) as db:
        assert db.get_one(ChatModelAttempt, attempt).estimated_cost_microusd == 20
        assert db.scalars(select(Observation)).all() == []
    _advance(fixture, 2)
    new_lease = fixture.service.claim(new_turn)
    _bound(fixture, new_lease)
    with pytest.raises(BusinessError) as error:
        _reserve(fixture, new_lease, 11)
    assert error.value.code == "ANALYSIS_BUDGET_EXCEEDED"


def test_expired_started_attempt_is_unknown_and_never_replayed(fixture):
    lease = _claim(fixture)
    _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 40)
    _advance(fixture, 5)
    assert fixture.service.due() == [lease.turn_id]
    assert fixture.service.claim(lease.turn_id) is None
    assert fixture.service.claim(lease.turn_id) is None
    with Session(fixture.engine) as db:
        interrupted = db.get_one(ChatModelAttempt, attempt)
        assert interrupted.state == "UNKNOWN"
        interrupted_at = interrupted.finished_at
    _advance(fixture, 2)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={"input_tokens": 3, "output_tokens": 1},
        estimated_cost_microusd=41,
        response_evidence={"response": "SDK_RETURNED"},
    )
    with Session(fixture.engine) as db:
        row = db.get_one(ChatModelAttempt, attempt)
        assert row.state == "UNKNOWN" and row.finished_at == interrupted_at
        assert row.response_evidence["late_settlement"]["estimated_cost_microusd"] == 41
        assert row.budget_violation


def test_observations_and_public_projection_commit_together(fixture):
    lease = _claim(fixture)
    observation = _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 40)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={"input_tokens": 2, "output_tokens": 1},
        estimated_cost_microusd=12,
        response_evidence={},
        validated_result=_validated(observation),
    )
    with pytest.raises(BusinessError):
        fixture.service.publish(
            lease.turn_id, lease.token, [{**observation, "sent_sha256": "c" * 64}], lambda *_: None
        )
    with pytest.raises(BusinessError) as altered:
        fixture.service.publish(
            lease.turn_id, lease.token, [{**observation, "findings": ["BLUR"]}], lambda *_: None
        )
    assert altered.value.code == "ANALYSIS_RESULT_UNBOUND"
    with pytest.raises(RuntimeError):
        fixture.service.publish(
            lease.turn_id,
            lease.token,
            [observation],
            lambda *_: (_ for _ in ()).throw(RuntimeError()),
        )
    with Session(fixture.engine) as db:
        assert db.scalars(select(Observation)).all() == []
    result = fixture.service.publish(
        lease.turn_id, lease.token, [observation], lambda *_: "published"
    )
    assert result == "published"
    with Session(fixture.engine) as db:
        assert db.scalars(select(Observation)).one().summary == "疑似裂纹"
        assert db.get_one(ChatAnalysisTurn, lease.turn_id).state == "PUBLISHED"


@pytest.mark.parametrize(
    ("cost", "code"),
    [
        (None, "ANALYSIS_COST_UNKNOWN"),
        (41, "ANALYSIS_BUDGET_VIOLATION"),
    ],
)
def test_unsettled_cost_cannot_publish(fixture, cost, code):
    lease = _claim(fixture)
    observation = _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 40)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={"input_tokens": 2, "output_tokens": 1},
        estimated_cost_microusd=cost,
        response_evidence={},
        validated_result=_validated(observation),
    )
    with pytest.raises(BusinessError) as denied:
        fixture.service.publish(
            lease.turn_id,
            lease.token,
            [observation],
            lambda *_: pytest.fail("cost-invalid publish"),
        )
    assert denied.value.code == code


def test_published_observations_must_cover_every_sent_photo(fixture):
    turn = _message(fixture, count=2)
    _advance(fixture, 2)
    lease = fixture.service.claim(turn)
    photos = [
        {
            "attachment_id": row["id"],
            "message_id": row["message_id"],
            "original_sha256": row["sha256"],
            "sent_sha256": "b" * 64,
            "transform_version": "fixture-v1",
        }
        for row in fixture.service.input_snapshot(lease)["attachments"]
    ]
    fixture.service.bind_vision_input(
        turn, lease.token, {"logical_id": "vision:fixture", "photos": photos}
    )
    attempt = _reserve(fixture, lease, 40)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={"input_tokens": 2, "output_tokens": 1},
        estimated_cost_microusd=12,
        response_evidence={},
    )
    with pytest.raises(BusinessError) as denied:
        fixture.service.publish(
            turn, lease.token, [photos[0]], lambda *_: pytest.fail("partial publish")
        )
    assert denied.value.code == "ANALYSIS_OBSERVATIONS_INVALID"


def test_modelrunner_audit_reserves_only_once_and_rounds_up_microusd(fixture):
    lease = _claim(fixture)
    _bound(fixture, lease)
    audit = ChatAnalysisAudit(fixture.service, lease)
    spec = SimpleNamespace(alias="vision", model="fixture", provider="fixture")
    registry = SimpleNamespace(
        max_usd=Decimal("0.1"), max_run_usd=None, version="v1", deadline_seconds=10
    )
    assert audit.replay("vision:fixture") is None
    assert audit.begin_attempt("vision:fixture", 1, spec, Decimal("0.0000011"), registry) == 5
    with pytest.raises(ModelFailure, match="ANALYSIS_ATTEMPT_NOT_REPLAYED"):
        audit.replay("vision:fixture")
    with pytest.raises(ModelFailure, match="ANALYSIS_BUDGET_EXCEEDED"):
        audit.begin_attempt("vision:fixture", 2, spec, Decimal("0.000001"), registry)
    audit.finish_attempt("vision:fixture", 1, "SUCCEEDED", None, {}, Decimal("0.0000001"), {})
    with Session(fixture.engine) as db:
        attempt = db.get_one(ChatModelAttempt, audit.attempt_id)
        assert (attempt.state, attempt.reserved_microusd, attempt.estimated_cost_microusd) == (
            "SUCCEEDED",
            2,
            1,
        )


def test_over_reservation_is_persistent_and_blocks_even_when_budget_remains(fixture):
    lease = _claim(fixture)
    _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 10)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={},
        estimated_cost_microusd=11,
        response_evidence={},
    )
    next_id = _message(fixture)
    _advance(fixture, 2)
    restarted = AnalysisService(
        fixture.engine, fixture.service.limits, clock=lambda: fixture.clock.value
    )
    next_lease = restarted.claim(next_id)
    _bound(fixture, next_lease)
    with pytest.raises(BusinessError) as error:
        restarted.reserve(next_id, next_lease.token, 1, {"logical_id": "vision:fixture"})
    assert error.value.code == "ANALYSIS_BUDGET_VIOLATION"
    with Session(fixture.engine) as db:
        assert db.get_one(ChatModelAttempt, attempt).budget_violation is True


def test_restart_and_changed_vision_binding_cannot_reopen_same_unknown_revision(fixture):
    lease = _claim(fixture)
    _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 10)
    _advance(fixture, 5)
    restarted = AnalysisService(
        fixture.engine, fixture.service.limits, clock=lambda: fixture.clock.value
    )
    assert restarted.claim(lease.turn_id) is None
    with Session(fixture.engine) as db, db.begin():
        conversation = db.get_one(Conversation, CONVERSATION)
        assert restarted.stage(db, conversation, input_from_seq=1).id == lease.turn_id
    with pytest.raises(BusinessError):
        restarted.bind_vision_input(lease.turn_id, lease.token, {"logical_id": "vision:other"})
    with Session(fixture.engine) as db:
        assert db.get_one(ChatModelAttempt, attempt).state == "UNKNOWN"
        assert len(db.scalars(select(ChatAnalysisTurn)).all()) == 1


@pytest.mark.parametrize("invalidate", ["handoff", "scope", "actor"])
def test_observation_cannot_publish_after_authority_or_scope_changes(fixture, invalidate):
    lease = _claim(fixture)
    observation = _bound(fixture, lease)
    attempt = _reserve(fixture, lease, 10)
    fixture.service.finish_attempt(
        attempt,
        status="SUCCEEDED",
        usage={},
        estimated_cost_microusd=10,
        response_evidence={},
    )
    with Session(fixture.engine) as db, db.begin():
        conversation = db.get_one(Conversation, CONVERSATION)
        if invalidate == "handoff":
            conversation.status = "HUMAN_HANDOFF"
            fixture.service.invalidate(db, conversation)
        elif invalidate == "scope":
            conversation.line_ids = ["different"]
        else:
            db.get_one(User, OWNER).enabled = False
    with pytest.raises(BusinessError):
        fixture.service.publish(
            lease.turn_id, lease.token, [observation], lambda *_: pytest.fail("invalid publish")
        )


def test_actual_vision_adapter_publishes_only_public_projection(fixture):
    import json
    from datetime import date

    from langchain_core.messages import AIMessage

    from aftercare.intelligence.models import ModelRunner, ModelSpec, Registry
    from aftercare.intelligence.vision import PhotoSource, PreparedPhoto, observe

    lease = _claim(fixture)
    snapshot = fixture.service.input_snapshot(lease)
    source = snapshot["attachments"][0]
    photo = PreparedPhoto(
        PhotoSource(source["id"], source["message_id"], source["sha256"], "image/png", 10),
        "b" * 64,
        2,
        2,
        b"controlled payload",
    )
    date_stamp = date(2026, 11, 11)
    registry = Registry(
        version="fixture",
        max_usd=Decimal("0.0001"),
        models=[
            ModelSpec(
                alias="economy",
                provider="fixture",
                model="fixture",
                endpoint="https://example.invalid",
                api_key_env="AFTERCARE_TEST_VISION",
                compatibility="VERIFIED",
                compatibility_version="fixture",
                checked_at=date_stamp,
                price_date=date_stamp,
                source="controlled",
                vision=True,
                vision_checked_at=date_stamp,
                vision_compatibility_version="fixture",
                vision_input_tokens_per_image=3000,
                input_usd_per_million=Decimal("0.001"),
                output_usd_per_million=Decimal("0.001"),
                data_scopes=["simulated_case"],
            ),
        ],
    )
    calls = []

    def transport(*_):
        calls.append("controlled")
        return AIMessage(
            content=json.dumps(
                {
                    "photos": [
                        {
                            "image_index": 1,
                            "candidate_kind": "ITEM",
                            "uncertainty": "LIMITED",
                            "findings": ["POSSIBLE_CRACK"],
                        }
                    ]
                }
            ),
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )

    model = ModelRunner(registry, transport=transport)
    audit = ChatAnalysisAudit(fixture.service, lease)

    def analyze():
        return observe(
            model,
            audit,
            (photo,),
            preferred="economy",
            input_binding=snapshot["scope_sha256"],
            data_scope="simulated_case",
        )

    result = analyze()
    with pytest.raises(ModelFailure, match="ANALYSIS_ATTEMPT_NOT_REPLAYED"):
        analyze()
    assert calls == ["controlled"]
    public = fixture.service.publish(
        lease.turn_id,
        lease.token,
        result["observations"],
        lambda _db, _conversation, rows: rows,
    )
    assert public == [
        {
            "attachment_id": source["id"],
            "certainty": "UNCERTAIN",
            "summary": "疑似裂纹",
        }
    ]
