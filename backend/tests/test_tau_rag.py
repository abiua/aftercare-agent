import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import tau_documents as documents
from scripts import tau_rag as rag


@pytest.fixture
def document(tmp_path):
    output = tmp_path / "export"
    documents.export("synthetic_policy.md", output, fixture=True)
    return rag.load_document(output)


def build(document, **options):
    settings = {"strategy": "structure", "policy_key": "shop-a", "roles": ["support"]}
    return rag.build_chunks(document, **(settings | options))


def search(document, chunks, **options):
    settings = {
        "roles": ["support"],
        "policy_keys": ["shop-a"],
        "versions": {"shop-a": document["source"]["document_version"]},
        "mode": "bm25",
    }
    return rag.search("return credit receipt", chunks, **(settings | options))["results"]


def test_chunking_and_citations(document):
    blocks = {block["id"]: block for block in document["blocks"]}
    for strategy in ("fixed", "structure"):
        chunks = build(document, strategy=strategy, chunk_size=47, overlap=13)
        assert chunks == build(document, strategy=strategy, chunk_size=47, overlap=13)
        assert chunks
        for chunk in chunks:
            pieces = []
            for citation in chunk["citations"]:
                block = blocks[citation["block_id"]]
                assert citation["location"] == block["location"]
                pieces.append(block["text"][citation["char_start"]:citation["char_end_exclusive"]])
            assert "\n\n".join(pieces) in chunk["text"]
            assert chunk["parent_text"] == "\n\n".join(
                blocks[cite["block_id"]]["text"] for cite in chunk["parent_citations"]
            )
        if strategy == "fixed":
            assert all(len(chunk["text"]) <= 47 for chunk in chunks)
        else:
            table = next(chunk for chunk in chunks if chunk["citations"][0]["kind"] == "table")
            assert table["text"] == next(block["text"] for block in blocks.values() if block["kind"] == "table")


def test_untrusted_exports_and_input_provenance(document, tmp_path):
    path = tmp_path / "export" / "document.json"
    altered = deepcopy(document)
    altered["blocks"][0]["text"] = "hidden task"
    path.write_text(json.dumps(altered))
    with pytest.raises(rag.RagError, match="pinned extraction"):
        rag.load_document(path)
    altered = deepcopy(document)
    altered["review_status"] = "needs_review"
    with pytest.raises(rag.RagError, match="trusted"):
        build(altered)
    altered = deepcopy(document)
    altered["pages"] = [{"method": "ocr", "review_status": "native_extracted"}]
    with pytest.raises(rag.RagError, match="OCR"):
        build(altered)
    altered = deepcopy(document)
    altered["source"]["source_kind"] = "tau2_hidden_task"
    with pytest.raises(rag.RagError, match="source kind"):
        build(altered)


def test_access_version_and_date_filters(document):
    public = build(document)
    other_shop = build(document, policy_key="shop-b")
    protected = build(document, policy_key="restricted", roles=["manager"])
    assert search(document, public + other_shop + protected) == search(document, public)
    assert not search(document, public, versions=None)
    assert not search(document, public, versions={"shop-a": "mismatched-version"})
    assert not search(document, public, roles=["customer"])
    assert not search(document, protected, policy_keys=["restricted"], versions={
        "restricted": document["source"]["document_version"],
    })
    assert not search(document, public, as_of="2026-03-03")
    assert all(row["date_applicability"] == "unknown" for row in search(document, public))

    dated = build(document, effective_from="2026-03-03", effective_until="2026-04-02")
    assert not search(document, dated, as_of="2026-03-02")
    assert not search(document, dated, as_of="2026-04-02")
    assert not search(document, dated)
    assert all(row["date_applicability"] == "matched"
               for row in search(document, dated, as_of="2026-03-03"))
    conflicting = [{**chunk, "document_version": "overlapping-version"} for chunk in dated]
    assert not search(document, dated + conflicting, versions=None, as_of="2026-03-03")
    assert search(document, dated + conflicting, as_of="2026-03-03")


def test_dense_allowlist_and_fusion(document):
    chunks = build(document)
    foreign = build(document, policy_key="shop-b")
    lexical = search(document, chunks)
    first, second = lexical[:2]

    def dense(vector, allowed, limit):
        assert vector == [0.0, 1.0]
        assert allowed == {chunk["id"] for chunk in chunks}
        assert limit == 5
        return [(second["id"], 0.9), (first["id"], 0.8)]

    options = {"encode_query": lambda _: [0.0, 1.0], "dense_rank": dense, "candidate_limit": 5}
    fused = search(document, chunks + foreign, mode="hybrid", **options)
    assert {row["id"] for row in fused[:2]} == {first["id"], second["id"]}
    assert fused[0]["rrf_score"] == pytest.approx(1 / 61 + 1 / 62)

    def rerank(query, texts):
        assert query == "return credit receipt"
        return [float(text == second["text"]) for text in texts]

    ranked = search(document, chunks + foreign, mode="rerank", rerank=rerank, **options)
    assert ranked[0]["id"] == second["id"]
    assert ranked[0]["rerank_score"] == 1.0
    options["dense_rank"] = lambda *_: [(foreign[0]["id"], 1.0)]
    with pytest.raises(rag.RagError, match="unauthorized"):
        search(document, chunks + foreign, mode="dense", **options)


def test_fixed_defaults_and_cli_override(document, tmp_path, capsys):
    fixed = build(document, strategy="fixed")
    assert fixed == build(document, strategy="fixed", chunk_size=450, overlap=70)
    cli = [
        str(tmp_path / "export"), "return credit", "--strategy", "fixed",
        "--policy-key", "shop-a", "--role", "support",
        "--version", document["source"]["document_version"],
        "--chunk-size", "90", "--overlap", "20",
    ]
    assert rag.main(cli) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["chunk_config"] == {"chunk_size": 90, "overlap": 20}
    assert report["indexed_chunks"] > len(fixed)
    assert all(len(result["text"]) <= 90 for result in report["results"])


def test_database_cli_build_then_open_without_reembedding(document, tmp_path, capsys, monkeypatch):
    from scripts import tau_rag_catalog as catalog
    from scripts import tau_rag_store as models

    rows = {}
    events = []
    names = []
    embedded = []

    class Store:
        def create(self):
            events.append("create")

        def upsert(self, chunks, vectors):
            assert set(vectors) == {chunk["id"] for chunk in chunks}
            events.append("upsert")
            rows.update({chunk["id"]: deepcopy(chunk) for chunk in chunks})

        def get(self, key):
            return rows[key]

        def rank(self, vector, allowed_ids, limit):
            events.append("rank")
            assert allowed_ids == set(rows)
            return [(key, 1.0) for key in sorted(allowed_ids)[:limit]]

        def close(self):
            events.append("close")

    def store(backend, name, dimension):
        assert backend == "pgvector" and dimension == 2
        names.append(name)
        return Store()

    def embed(text):
        embedded.append(text)
        return [1.0, 0.0]

    monkeypatch.setattr(catalog, "_store", store)
    monkeypatch.setattr(models, "load_models", lambda _: (embed, None, {"embedding": {"rev": "a"}}))
    flags = [
        "--policy-key", "shop-a", "--role", "support", "--store", "pgvector",
        "--version", document["source"]["document_version"], "--model-cache", str(tmp_path),
        "--catalog", str(tmp_path / "new" / "catalog.json"),
    ]
    source = str(tmp_path / "export")
    assert rag.main([source, *flags, "--build-index"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["count"] == len(embedded) == len(rows)

    embedded.clear()
    assert rag.main([source, "what is return credit", *flags, "--mode", "hybrid"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["store"] == "pgvector" and result["results"]
    assert embedded == ["what is return credit"]
    assert names == [created["name"], created["name"]]
    assert events == ["create", "upsert", "close", "rank", "close"]

    chunk = next(iter(rows.values()))
    chunk["roles"] = ["different"]
    with pytest.raises(SystemExit) as failure:
        rag.main([source, "what is return credit", *flags, "--mode", "dense"])
    assert failure.value.code == 2
    assert "stored snapshot differs" in capsys.readouterr().err
    assert events[-1] == "close"
    assert events.count("rank") == 1
