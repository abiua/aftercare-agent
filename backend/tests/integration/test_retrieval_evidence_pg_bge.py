"""Actual local BGE and pgvector evidence with disposable evaluation schemas."""

import math
import os
import struct
from collections import Counter
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.models import Policy
from aftercare.config import load_settings
from aftercare.db import User, guarded_engine
from aftercare.evaluation import retrieval_evidence as evidence
from evaluation_technical_fixtures import TechnicalCatalogue
from aftercare.evaluation.formal.native import read_isolated
from aftercare.evaluation.runner import EvaluationRunner
from aftercare.evaluation.service import EvaluationService, ExperimentInput
from aftercare.evaluation.tables import (
    EvaluationMutation,
    EvaluationProgress,
    EvaluationResult,
    EvaluationReview,
    Experiment,
)
from aftercare.intelligence import retrieval_evidence as encoding
from aftercare.intelligence.graph import INTENT_TERMS
from aftercare.intelligence.policies import PolicyVector, document_key

pytestmark = [
    pytest.mark.pg,
    pytest.mark.skipif(
        os.environ.get("AFTERCARE_RETRIEVAL_EVIDENCE_PG") != "1",
        reason="requires explicitly enabled isolated PostgreSQL and installed local BGE",
    ),
]
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
TESTER = UserContext(user_id="retrieval-audit", role="tester")


class NoObjects:
    def put(self, *_):
        pytest.fail("retrieval evidence case tried to upload an object")

    def get(self, *_):
        pytest.fail("retrieval evidence case tried to read an object")

    def presign(self, *_):
        pytest.fail("retrieval evidence case tried to sign an object")


def run_fixture(scoped, settings, case_id, schemas, *, mode="single"):
    service = EvaluationService(scoped, settings, execution_driver="inline", catalog=TechnicalCatalogue())
    runner = EvaluationRunner(scoped, settings, InMemorySaver(), storage=NoObjects())
    request = ExperimentInput(
        name="retrieval evidence integration",
        dataset_id="v0.2",
        split="dev",
        mode=mode,
        strategy="routed",
        repetitions=1,
        concurrency=1,
        max_calls=1,
        budget_usd=0,
        source="fixture",
        case_ids=[case_id],
    )
    plan = service.create(TESTER, request, str(uuid4()))
    (planned_row,) = plan["results"]
    namespace = f"eval_{UUID(planned_row['id']).hex}"
    with scoped.connect() as conn:
        assert conn.scalar(text("SELECT to_regnamespace(:name) IS NULL"), {"name": namespace})
    schemas.append(namespace)
    # Use the production runner without the outer service's hard-coded public
    # publication admission check. That check has no matching experiment in this schema.
    case = next(item for item in TechnicalCatalogue().load_cases("dev") if item["id"] == case_id)
    outcome = runner.run(
        planned_row["id"],
        case,
        planned_row["expected"],
        plan["config"],
        planned_row["quota"],
        cancelled=lambda: False,
    )
    row = {**planned_row, **outcome}
    assert row["actual"]["model_attempts"] == []
    return plan["config"], row


def events(row, kind):
    return [event["payload"] for event in row["trace"] if event.get("kind") == kind]


def verify_native_read(conn, config, row):
    raw, retrieval = read_isolated(conn, row["id"])
    assert raw is not None and raw[1] == []
    findings = Counter()
    case = evidence.native_retrieval_evidence(row, config, retrieval, findings)
    assert case["catalogue_sha256"] == row["quota"]["policy_catalogue_sha256"]
    return retrieval, findings


def cosine_distance(left, right):
    numerator = math.fsum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    return 1 - numerator / (left_norm * right_norm)


def verify_real_hybrid_rank(conn, model_path, config, row, observed, expected_roles):
    traces = events(row, "retrieval")
    assert [event["role"] for event in traces] == expected_roles
    whole_index = row["actual"]["retrieval_evidence"]["index"]
    assert whole_index["state"] == "BUILT"
    assert whole_index["keys"] == observed["catalogue"]["keys"]
    assert whole_index["content_sha256"] == observed["index"]["content_sha256"]
    manifest = config["retrieval_snapshot"]["encoder"]
    assert set(manifest["files"]) == set(encoding.BGE_FILES)
    assert manifest["sha256"] == row["actual"]["retrieval_evidence"]["encoder_manifest_sha256"]

    c081 = next(case for case in TechnicalCatalogue().load_cases("dev") if case["id"] == "UNIT-STATUS")
    query = f"{INTENT_TERMS['refund_status']} {c081['message'][:600]}"
    (embedding,) = encoding.bound_encoder(model_path, manifest)([encoding.QUERY_PREFIX + query])
    assert len(embedding) == 512
    query_bytes = struct.pack("!512f", *embedding)
    query32 = struct.unpack("!512f", query_bytes)
    namespace = f"eval_{UUID(row['id']).hex}"
    options = {"schema_translate_map": {None: namespace}}
    catalogue = {
        document_key(document): document
        for document in conn.scalars(select(Policy.data), execution_options=options)
    }
    vectors = {key: tuple(map(float, vector)) for key, _, vector in observed["index"]["rows"]}
    assert all(len(vector) == 512 for vector in vectors.values())
    for retrieval in traces:
        assert retrieval["mode"] == "HYBRID", (
            retrieval["mode"],
            retrieval["blockers"],
            whole_index["state"],
        )
        assert "INCOMPLETE_VECTOR_INDEX" not in retrieval["blockers"]
        proof = retrieval["retrieval_evidence"]
        assert proof["candidate_keys"] and proof["vector_index"]["keys"] == proof["candidate_keys"]
        assert proof["query_sha256"] == sha256(query.encode()).hexdigest()
        assert proof["query_vector_sha256"] == sha256(query_bytes).hexdigest()
        keys = list(
            conn.scalars(
                select(PolicyVector.key)
                .where(
                    PolicyVector.key.in_(proof["candidate_keys"]),
                    PolicyVector.encoder == f"{encoding.MODEL_ID}@{manifest['sha256']}",
                )
                .order_by(PolicyVector.embedding.cosine_distance(embedding), PolicyVector.key),
                execution_options=options,
            )
        )
        assert sorted(keys) == proof["candidate_keys"]
        assert retrieval["semantic_versions"] == [
            {"id": catalogue[key]["id"], "version": catalogue[key]["version"]} for key in keys
        ]
        distances = {key: cosine_distance(query32, vectors[key]) for key in keys}
        # Database float32 accumulation may swap nearly tied distances. For every ordered
        # pair, require no inversion beyond an absolute cosine-distance tolerance of 1e-6.
        assert all(
            distances[earlier] <= distances[later] + 1e-6
            for index, earlier in enumerate(keys)
            for later in keys[index + 1 :]
        )
    print(
        f"HYBRID {config['mode']}: {len(whole_index['keys'])} stored float32 vectors; "
        f"{len(traces)} queries match native, SQL and independent cosine (absolute 1e-6)."
    )


def test_retrieval_evidence_with_real_postgresql_and_local_bge(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    model_path = Path(
        os.environ.get(
            "AFTERCARE_RETRIEVAL_BGE_DIR",
            str(Path.home() / ".cache/aftercare-agent/models" / MODEL_REVISION),
        )
    )
    settings = load_settings("test").model_copy(
        update={
            "model_enabled": False,
            "embedding_enabled": False,
            "embedding_model_path": model_path,
        }
    )
    hybrid_settings = settings.model_copy(update={"embedding_enabled": True})
    engine = guarded_engine(settings, test_only=True)
    schemas = []
    outer = f"retrieval_acceptance_{uuid4().hex}"
    try:
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT to_regtype('vector') IS NOT NULL"))
            conn.execute(text(f'CREATE SCHEMA "{outer}"'))
        schemas.append(outer)
        scoped = engine.execution_options(schema_translate_map={None: outer})
        for model in (
            User,
            Experiment,
            EvaluationResult,
            EvaluationMutation,
            EvaluationProgress,
            EvaluationReview,
        ):
            model.__table__.create(scoped)
        with Session(scoped) as db, db.begin():
            db.add(
                User(
                    id=TESTER.user_id,
                    role="tester",
                    display_name=TESTER.user_id,
                    enabled=True,
                    token_hash=uuid4().hex,
                )
            )

        with monkeypatch.context() as patch:
            patch.setattr(
                evidence, "encoder_manifest", lambda *_: pytest.fail("BM25 read the model path")
            )
            patch.setattr(
                encoding, "_bound_model", lambda *_: pytest.fail("BM25 loaded an encoder")
            )
            bm25_config, bm25_row = run_fixture(scoped, settings, "UNIT-STATUS", schemas)

        with monkeypatch.context() as patch:
            patch.setattr(
                encoding, "_bound_model", lambda *_: pytest.fail("early pause loaded BGE")
            )
            early_config, early_row = run_fixture(scoped, hybrid_settings, "UNIT-WRONG", schemas)

        hybrid_config, hybrid_row = run_fixture(scoped, hybrid_settings, "UNIT-STATUS", schemas)
        multi_config, multi_row = run_fixture(
            scoped,
            hybrid_settings,
            "UNIT-STATUS",
            schemas,
            mode="multi",
        )

        with (
            engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
            assert (
                conn.exec_driver_sql("SHOW transaction_isolation").scalar_one() == "repeatable read"
            )

            bm25, bm25_findings = verify_native_read(conn, bm25_config, bm25_row)
            assert bm25["index"]["keys"] == [] and not bm25_findings
            assert bm25_config["retrieval_snapshot"]["encoder"] is None
            assert bm25_row["actual"]["retrieval_evidence"]["index"]["state"] == "NOT_APPLICABLE"
            (lexical,) = events(bm25_row, "retrieval")
            assert (
                lexical["mode"] == "BM25" and lexical["retrieval_evidence"]["vector_index"] is None
            )
            print(
                "BM25: encoder manifest/load prohibited by test, zero vector rows, real query evidence."
            )

            early, early_findings = verify_native_read(conn, early_config, early_row)
            assert early["index"]["keys"] == []
            assert early_row["actual"]["retrieval_evidence"]["index"]["state"] == "NOT_USED"
            assert not events(early_row, "retrieval")
            assert early_row["actual"]["events"]["e02"]["status"] == "WAITING_INPUT"
            assert early_findings == {"NO_RETRIEVAL_OBSERVED": 1}
            print("HYBRID configured, C041 pauses before retrieval: NOT_USED, zero vector rows.")

            hybrid, hybrid_findings = verify_native_read(conn, hybrid_config, hybrid_row)
            verify_real_hybrid_rank(conn, model_path, hybrid_config, hybrid_row, hybrid, ["single"])
            multi, multi_findings = verify_native_read(conn, multi_config, multi_row)
            verify_real_hybrid_rank(
                conn, model_path, multi_config, multi_row, multi, ["policy", "verifier"]
            )
            assert not hybrid_findings and not multi_findings
            assert hybrid["index"]["content_sha256"] == multi["index"]["content_sha256"]
            assert bm25["catalogue"]["catalogue_sha256"] == hybrid["catalogue"]["catalogue_sha256"]
            print(
                "Native RR/READ ONLY policy and query evidence agree; fixture source, zero provider calls."
            )
    finally:
        try:
            with engine.begin() as conn:
                for schema in reversed(schemas):
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                    assert conn.scalar(
                        text("SELECT to_regnamespace(:name) IS NULL"), {"name": schema}
                    )
        finally:
            engine.dispose()
            encoding._bound_model.cache_clear()
