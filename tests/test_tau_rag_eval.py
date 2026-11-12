from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import evaluate_tau_rag as evaluation
from scripts.tau_documents import extract
from scripts.tau_rag import build_chunks


def fixture_document():
    _, document = extract("synthetic_policy.md", fixture=True)
    chunks = build_chunks(
        document, strategy="structure", policy_key="test", roles=["test"]
    )
    return document, chunks


def label(chunk):
    location = chunk["citations"][0]["location"]
    return {"line": location["line_start"], "byte_start": location["byte_start"],
            "excerpt": chunk["text"]}


def test_full_partial_duplicate_and_invalid_citations():
    document, chunks = fixture_document()
    a, b = chunks[:2]
    a_partial = deepcopy(a)
    a_partial["citations"][0]["char_end_exclusive"] -= 1
    fake_b = deepcopy(b)
    fake_b["citations"][0]["location"]["line_start"] += 1
    labels = [label(a), label(b)]
    result = evaluation.score([a_partial, fake_b, a, a, b], labels, document)
    assert result["citation_total"] == 5
    assert result["citation_valid"] == 4
    assert result["metrics"]["recall@1"] == 0
    assert result["metrics"]["recall@3"] == 0.5
    assert result["metrics"]["recall@5"] == 1
    assert result["metrics"]["mrr@5"] == pytest.approx(1/3)
    assert result["metrics"]["ndcg@5"] == pytest.approx((0.5 + 1 / 2.584962500721156) / 2)


def test_parent_context_and_wrong_document_cannot_supply_evidence():
    document, chunks = fixture_document()
    a = deepcopy(chunks[0])
    a["parent_citations"] = a["citations"]
    a["citations"] = []
    b = deepcopy(chunks[0])
    b["document_version"] = "invented"
    result = evaluation.score([a, b], [label(chunks[0])], document)
    assert result["metrics"]["recall@5"] == 0
    assert result["citation_valid"] == 0


def test_frozen_fixtures_are_separated_and_tampering_is_rejected(monkeypatch, tmp_path):
    queries = evaluation.frozen_json("queries.json")
    labels = evaluation.frozen_json("labels.json")
    assert {query["id"] for query in queries} == set(labels)
    assert all(query["synthetic_query"] and set(query) == {
        "id", "language", "synthetic_query", "query",
    } for query in queries)
    assert sum(query["language"] == "zh" for query in queries) == 4
    monkeypatch.setattr(evaluation, "FIXTURES", tmp_path)
    (tmp_path / "queries.json").write_text("[]")
    with pytest.raises(ValueError, match="frozen development fixture changed"):
        evaluation.frozen_json("queries.json")


def test_access_probes_call_vector_store_only_when_eligible():
    document, _ = fixture_document()
    chunks = build_chunks(
        document, strategy="structure", policy_key="synthetic_access",
        roles=["test_policy_reader"], effective_from="2026-01-01", effective_until="2027-01-01",
    )
    called = []

    def rank(_, allowed, limit):
        called.append(allowed)
        return [(key, 1.0) for key in sorted(allowed)[:limit]]

    rows = evaluation.access_probes(
        chunks, [("stub", "dense", {"encode_query": lambda _: [1.0], "dense_rank": rank})],
    )
    assert all(row["passed"] for row in rows)
    assert called == [{chunk["id"] for chunk in chunks}]


def test_pinned_source_labels_when_local_bundle_is_present():
    policy = Path(".runtime/tau-retail/source/policy.md")
    if not policy.exists():
        pytest.skip("requires the local P1 policy bundle")
    raw, _ = extract(policy)
    assert len(evaluation.load_labels(raw)) == 18


def test_full_runner_keeps_failed_store_and_uses_shared_vectors(monkeypatch):
    document, _ = fixture_document()
    query = {"id": "test", "query": "test policy", "language": "en", "synthetic_query": True}
    zh_query = {**query, "id": "zh", "query": "测试政策", "language": "zh"}
    monkeypatch.setattr(evaluation, "extract", lambda _: (b"", document))
    monkeypatch.setattr(evaluation, "frozen_json", lambda _: [query, zh_query])
    _, chunks = fixture_document()
    monkeypatch.setattr(evaluation, "load_labels", lambda _: {
        "test": [label(chunks[0])], "zh": [label(chunks[0])],
    })
    embedded = []

    def embed(text):
        embedded.append(text)
        return [1.0, 0.5]

    def failing_factory(_chunks, _vectors):
        raise OSError("store offline")

    @contextmanager
    def failing_probe_factory(chunks, vectors):
        if chunks[0]["policy_key"] == "synthetic_access":
            raise OSError("probe store offline")
        yield evaluation.cosine_rank(chunks, vectors)
        raise OSError("cleanup failed")

    report = evaluation.evaluate(
        "unused", embed=embed, rerank=lambda _, texts: [0.0] * len(texts),
        index_factories={
            "memory": evaluation.cosine_rank,
            "offline": failing_factory,
            "probe_offline": failing_probe_factory,
        },
    )
    assert report["chunk_config"] == {"chunk_size": 450, "overlap": 70}
    assert embedded.count(query["query"]) == embedded.count(zh_query["query"]) == 1
    assert set(report["vector_hashes"]) == {"queries", "fixed", "structure"}
    assert len(report["index_failures"]) == 6
    assert {row["purpose"] for row in report["index_failures"]} == {
        "retrieval", "access_probes", "cleanup",
    }
    unrun = [row for row in report["access_probes"] if row["backend"] in {"offline", "probe_offline"}]
    assert len(unrun) == 72
    assert all(row["status"] == "unrun" and not row["passed"] for row in unrun)
    good_runs = [row for row in report["comparisons"]
                 if row["backend"] in {"memory", "probe_offline"}]
    failed_runs = [row for row in report["comparisons"] if row["backend"] == "offline"]
    assert len(good_runs) == 12 and len(failed_runs) == 6
    assert all(row["overall"]["failure_count"] == 0 for row in good_runs)
    assert all(row["overall"]["failure_count"] == 2 for row in failed_runs)
    assert all(row["overall"]["metrics"]["recall@5"] == 0 for row in failed_runs)
    for row in report["same_vector_backend_agreement"]:
        assert row["comparable_query_count"] == (0 if "offline" in row["backends"] else 2)
