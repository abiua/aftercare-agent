"""Structured review is a support observation, separate from approval and execution."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from test_business import OWNER, SUPPORT, _change, _create, _photo
from test_business import system as _system

from aftercare.auth import current_user
from aftercare.business.api import router
from aftercare.business.errors import BusinessError
from aftercare.business.models import Approval, Execution, Idempotency, MaterialReview
from aftercare.business.rules import digest
from aftercare.evaluation.chat.runner import _material_images, _populate

system = _system


@pytest.fixture
def review(system):
    service = system.service
    case_id = _create(service)
    materials = [
        _change(
            service,
            case_id,
            OWNER,
            service.upload,
            f"material-{kind}",
            kind=kind,
            content=_photo(color),
        )
        for kind, color in (("ITEM", "green"), ("PARCEL", "orange"))
    ]
    version = service.context(case_id, SUPPORT)["case"]["version"]
    request = {
        "decision": "APPROVED",
        "notes": "内部备注不作为结构化观察，也不意味着财务批准。",
        "expected_version": version,
    }
    return system, case_id, materials, request


def _observe(material, **overrides):
    return {
        "material_id": material["id"],
        "finding": "VISIBLE_DAMAGE",
        "line_id": "L01",
        "affected_quantity": 2,
        **overrides,
    }


def test_structured_material_review_support_api_and_idempotency(review):
    system, case_id, materials, request = review
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.business = system.service
    app.dependency_overrides[current_user] = lambda: SUPPORT
    observations = [_observe(material) for material in materials]
    headers = {"Idempotency-Key": "typed-material-review"}
    case_url = f"/api/cases/{case_id}"
    path = f"{case_url}/material-reviews"
    with TestClient(app) as client:
        result = client.post(path, json={**request, "observations": observations}, headers=headers)
        assert result.status_code == 201, result.text
        body = result.json()
        expected = [
            {**item, "source": "HUMAN_MATERIAL_REVIEW"}
            for item in sorted(observations, key=lambda item: item["material_id"])
        ]
        assert body["observations"] == expected
        assert (
            client.post(
                path,
                json={**request, "observations": list(reversed(observations))},
                headers=headers,
            ).json()
            == body
        )
        changed = client.post(
            path,
            json={
                **request,
                "observations": [{**item, "affected_quantity": 1} for item in observations],
            },
            headers=headers,
        )
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert client.get(path).json()["items"][0]["observations"] == expected
        assert client.get(case_url).json()["reviews"][0]["observations"] == expected
        app.dependency_overrides[current_user] = lambda: OWNER
        assert "observations" not in client.get(path).json()["items"][0]
        assert "observations" not in client.get(case_url).json()["reviews"][0]

    assert "observations" not in system.service.context(case_id, SUPPORT)["reviews"][0]
    with Session(system.engine) as db:
        row = db.get_one(MaterialReview, body["id"])
        assert row.observations == expected and row.notes == request["notes"]
        assert not db.scalar(select(func.count()).select_from(Approval))
        assert not db.scalar(select(func.count()).select_from(Execution))
    assert system.service.context(case_id, SUPPORT)["eligibility"]["status"] == "ELIGIBLE"
    _change(
        system.service,
        case_id,
        OWNER,
        system.service.upload,
        "later-photo",
        kind="ITEM",
        content=_photo("blue"),
    )
    historical = system.service.detail(case_id, SUPPORT)["reviews"][0]
    assert historical["effective"] is False and historical["observations"] == expected


@pytest.mark.parametrize(
    "invalid",
    [
        lambda material: [],
        lambda material: [_observe(material[0], material_id="other-material")],
        lambda material: [_observe(material[0], line_id="L02")],
        lambda material: [_observe(material[0], affected_quantity=3)],
        lambda material: [_observe(material[0], affected_quantity=True)],
        lambda material: [_observe(material[0], line_id=None)],
        lambda material: [_observe(material[0], finding="UNCERTAIN")],
        lambda material: [_observe(material[0], source="HUMAN_MATERIAL_REVIEW")],
        lambda material: [_observe(material[0]), _observe(material[0])],
        lambda material: [_observe(material[0]), _observe(material[1], affected_quantity=1)],
    ],
)
def test_invalid_observation_does_not_create_or_advance_review(review, invalid):
    system, case_id, materials, request = review
    with pytest.raises(BusinessError) as rejected:
        system.service.review_materials(
            case_id,
            SUPPORT,
            **request,
            observations=invalid(materials),
            idempotency_key="invalid",
        )
    assert rejected.value.code == "INVALID_REVIEW_OBSERVATIONS"
    assert rejected.value.status == 422
    detail = system.service.detail(case_id, SUPPORT)
    assert detail["case"]["version"] == request["expected_version"]
    assert detail["reviews"] == []


def test_legacy_review_signature_and_sql_null_are_preserved(review):
    system, case_id, _, request = review
    body = system.service.review_materials(case_id, SUPPORT, **request, idempotency_key="legacy")
    assert "observations" not in body
    assert body == system.service.review_materials(
        case_id, SUPPORT, **request, observations=None, idempotency_key="legacy"
    )
    with Session(system.engine) as db:
        row = db.get_one(Idempotency, (SUPPORT.user_id, f"material-review:{case_id}", "legacy"))
        assert row.request_hash == digest(
            [
                request["decision"],
                request["notes"],
                request["expected_version"],
            ]
        )
        assert db.scalar(text("SELECT observations IS NULL FROM business_material_reviews")) == 1
    with system.engine.begin() as db:
        db.execute(text("ALTER TABLE business_material_reviews DROP COLUMN observations"))
    # Historical evaluation schemas retain their old physical structure.
    assert system.service.context(case_id, SUPPORT)["reviews"] == [{**body, "effective": True}]


@pytest.mark.parametrize("shape", ["legacy", "typed", "missing-required"])
def test_evaluation_clone_copies_only_the_known_optional_historical_column(review, tmp_path, shape):
    system, case_id, materials, request = review
    observations = [_observe(materials[0])] if shape == "typed" else None
    original_review = system.service.review_materials(
        case_id,
        SUPPORT,
        **request,
        observations=observations,
        idempotency_key="clone-review",
    )
    review_schema = text("SELECT sql FROM sqlite_schema WHERE name='business_material_reviews'")
    with system.engine.begin() as db:
        if shape == "legacy":
            db.execute(text("ALTER TABLE business_material_reviews DROP COLUMN observations"))
        if shape == "missing-required":
            db.execute(text("ALTER TABLE business_material_reviews DROP COLUMN notes"))
        original_columns = db.scalar(review_schema)
        original_rows = db.execute(text("SELECT * FROM business_material_reviews")).all()
        material_objects = _material_images(db, system.objects)
    source_engine = (
        system.engine.execution_options(schema_translate_map={None: "main"})
        if shape == "legacy"
        else system.engine
    )
    source = SimpleNamespace(
        definition=SimpleNamespace(
            source=SimpleNamespace(engine=source_engine, storage=system.objects), actor=SUPPORT
        ),
        images=(),
        materials=material_objects,
    )
    target = create_engine(f"sqlite:///{tmp_path / 'cloned.db'}")
    storage = type(system.objects)()
    try:
        if shape == "missing-required":
            with pytest.raises(ValueError, match="CHAT_EVAL_MATERIAL_REVIEW_SCHEMA_MISMATCH"):
                _populate(source, target, storage)
        else:
            _populate(source, target, storage)
            with Session(target) as db:
                row = db.get_one(MaterialReview, original_review["id"])
                assert row.notes == original_review["notes"]
                assert row.material_ids == original_review["material_ids"]
                assert row.observations == original_review.get("observations")
            assert storage.objects == system.objects.objects
        with system.engine.connect() as db:
            assert db.scalar(review_schema) == original_columns
            assert (
                db.execute(text("SELECT * FROM business_material_reviews")).all() == original_rows
            )
    finally:
        target.dispose()
