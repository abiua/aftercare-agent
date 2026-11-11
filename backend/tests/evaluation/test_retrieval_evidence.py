import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from aftercare.evaluation import retrieval_evidence as evidence
from aftercare.intelligence import policies
from aftercare.intelligence import retrieval_evidence as encoding
from aftercare.intelligence.policies import PolicySearch, document_key


def _snapshot(path):
    for name in encoding.BGE_FILES:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"fixture file")
    (path / "modules.json").write_text(
        json.dumps([{"path": name, "type": kind} for name, kind in encoding.MODULES])
    )
    (path / ".env").write_text("not an encoder input")


def test_content_bound_encoder_uses_only_staged_allowlist_and_never_reuses_changed_bytes(
    tmp_path, monkeypatch
):
    path = tmp_path / "local-snapshot"
    _snapshot(path)
    loads = []

    class Encoder:
        def __init__(self, staged, **kwargs):
            assert kwargs == {
                "device": "cpu",
                "local_files_only": True,
                "trust_remote_code": False,
                "model_kwargs": {"use_safetensors": True},
            }
            root = Path(staged)
            assert sorted(
                str(item.relative_to(root)) for item in root.rglob("*") if item.is_file()
            ) == list(encoding.BGE_FILES)
            weights = (root / "model.safetensors").read_bytes()
            loads.append(weights)
            self.marker = 1.0 if weights == b"fixture file" else 2.0

        def encode(self, texts, **kwargs):
            assert self.max_seq_length == 512
            assert kwargs == {
                "prompt": "",
                "batch_size": 32,
                "precision": "float32",
                "normalize_embeddings": True,
                "show_progress_bar": False,
            }
            return SimpleNamespace(tolist=lambda: [[self.marker] * 512 for _ in texts])

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Encoder)
    )
    try:
        first = encoding.encoder_manifest(path)
        assert set(first["files"]) == set(encoding.BGE_FILES)
        assert str(path) not in json.dumps(first) and ".env" not in first["files"]
        old = encoding.bound_encoder(path, first)
        assert not loads  # Manifest capture and construction do not load the model.
        assert old(["doc"])[0][0] == 1
        (path / "model.safetensors").write_bytes(b"updated fixture bytes")
        second = encoding.encoder_manifest(path)
        assert second["sha256"] != first["sha256"]
        assert encoding.bound_encoder(path, second)(["doc"])[0][0] == 2
        assert old(["doc"])[0][0] == 1
        assert loads == [b"fixture file", b"updated fixture bytes"]
        encoding._bound_model.cache_clear()
        with pytest.raises(ValueError, match="BGE_BOUND_LOAD_FAILED"):
            old(["doc"])
    finally:
        encoding._bound_model.cache_clear()


def test_manifest_rejects_symlinks_within_profile(tmp_path):
    path = tmp_path / "local-snapshot"
    _snapshot(path)
    (path / "model.safetensors").unlink()
    (path / "model.safetensors").symlink_to(path / ".env")
    with pytest.raises(ValueError, match="BGE_SNAPSHOT_UNAVAILABLE"):
        encoding.encoder_manifest(path)


def test_bm25_does_not_read_model_path_or_vectors(monkeypatch):
    monkeypatch.setattr(evidence, "encoder_manifest", lambda _: pytest.fail("read model files"))
    settings = SimpleNamespace(embedding_enabled=False, embedding_model_path="/unused")
    config = evidence.retrieval_configuration(settings)
    assert config["mode"] == "BM25" and config["encoder"] is None
    policy = {"id": "P1", "version": "1", "text": "退款"}
    search = PolicySearch(None, evaluation_catalogue=lambda: pytest.fail("built index"))
    result = search.search_selected("退款", [policy])
    assert search.index_state == "NOT_APPLICABLE"
    assert result.mode == "BM25" and result.evidence["vector_index"] is None
    assert result.evidence["candidate_keys"] == [document_key(policy)]
    assert "退款" not in str(result.evidence)


def test_hybrid_missing_evidence_or_fallback_is_not_verified():
    key = "a" * 64
    config = {
        "embedding_enabled": True,
        "policy_snapshot": {"selector_sha256": "selector"},
        "retrieval_snapshot": {"mode": "HYBRID", "encoder": {"sha256": "encoder"}},
    }
    index = encoding.vector_index_evidence([])
    binding = {"cycle": 0, "policy_resolution_sha256": "scope", "selected_document_keys": [key]}
    actual = {
        "catalogue": {
            "catalogue_sha256": "catalogue",
            "keys": [key],
            "bindings": [{"run_id": "run", "seq": 1, **binding}],
        },
        "index": {**index, "rows": []},
    }
    row = {
        "started_at": "started",
        "quota": {"policy_catalogue_sha256": "catalogue"},
        "actual": {
            "retrieval_evidence": {
                "schema_version": evidence.SCHEMA_VERSION,
                "catalogue_sha256": "catalogue",
                "selector_sha256": "selector",
                "configured_mode": "HYBRID",
                "encoder_manifest_sha256": "encoder",
                "index": {**index, "state": "FAILED"},
            }
        },
        "trace": [
            {
                "kind": "context_binding",
                "seq": 1,
                "payload": binding,
            },
            {
                "kind": "retrieval",
                "seq": 2,
                "payload": {
                    "mode": "BM25",
                    "run_id": "run",
                    "cycle": 0,
                    "role": "single",
                    "stage": "policy",
                    "rework": 0,
                    "snapshot": "scope",
                    "retrieval_evidence": {
                        "schema_version": policies.QUERY_EVIDENCE_VERSION,
                        "query_sha256": "b" * 64,
                        "candidate_keys": [key],
                        "vector_index": None,
                    },
                },
            },
        ],
    }
    findings = Counter()
    baseline = evidence.native_retrieval_evidence(row, config, actual, findings)
    assert baseline
    assert findings == {"INDEX_BUILD_FAILED": 1, "HYBRID_FELL_BACK_TO_BM25": 1}
    other_row, other_actual = deepcopy(row), deepcopy(actual)
    other_row["trace"][0]["payload"]["policy_resolution_sha256"] = "another-scope"
    other_actual["catalogue"]["bindings"][0]["policy_resolution_sha256"] = "another-scope"
    other_row["trace"][1]["payload"].update(snapshot="another-scope", role="policy")
    other = evidence.native_retrieval_evidence(other_row, config, other_actual, Counter())
    assert evidence.compare_case_retrieval(baseline, other) == [
        "PAIRED_QUERY_POLICY_SELECTION_MISMATCH"
    ]
    other_row["trace"][1]["payload"].update(role="verifier", stage="verification")
    other = evidence.native_retrieval_evidence(other_row, config, other_actual, Counter())
    assert evidence.compare_case_retrieval(baseline, other) == []
    findings.clear()
    row["trace"][-1]["payload"]["cycle"] = 1
    evidence.native_retrieval_evidence(row, config, actual, findings)
    assert findings["QUERY_POLICY_BINDING_MISMATCH"] == 1
    findings.clear()
    row["trace"][-1]["payload"]["cycle"] = 0
    row["trace"][-1]["payload"]["run_id"] = "other"
    evidence.native_retrieval_evidence(row, config, actual, findings)
    assert findings["QUERY_POLICY_BINDING_MISMATCH"] == 1
    findings.clear()
    row["trace"][-1]["payload"]["retrieval_evidence"]["schema_version"] = "ac-retrieval-query-v1"
    evidence.native_retrieval_evidence(row, config, actual, findings)
    assert findings["QUERY_EVIDENCE_UNAVAILABLE"] == findings["NO_RETRIEVAL_OBSERVED"] == 1
    findings.clear()
    row["actual"] = {}
    assert evidence.native_retrieval_evidence(row, config, actual, findings) is None
    assert findings == {
        "RETRIEVAL_EVIDENCE_UNAVAILABLE": 1,
        "INDEX_PROVENANCE_UNAVAILABLE": 1,
        "INDEX_ROWS_UNAVAILABLE": 1,
    }


def test_vector_rows_are_hashed_as_float32_and_only_common_queries_compare():
    row = ("d" * 64, "encoder", [1.0] * 512)
    other_row = ("e" * 64, "encoder", [2.0] * 512)
    first = encoding.vector_index_evidence([row, other_row])
    assert first == encoding.vector_index_evidence([other_row, row])
    changed = encoding.vector_index_evidence([row, (*other_row[:2], [3.0] * 512)])
    assert first["keys"] == changed["keys"] and first["content_sha256"] != changed["content_sha256"]
    query = {"selection": "scope-a", "index": "index-a", "query_vector": "vector-a"}
    baseline = {
        "catalogue_sha256": "cat",
        "index": {**first, "state": "BUILT"},
        "queries": {"only-left": [query], "common": [query]},
    }
    comparison = {
        "catalogue_sha256": "cat",
        "index": {**changed, "state": "BUILT"},
        "queries": {"only-right": [query], "common": [query, query]},
    }
    assert evidence.compare_case_retrieval(baseline, comparison) == [
        "PAIRED_INDEX_CONTENT_MISMATCH"
    ]
    comparison["queries"]["common"] = [
        dict(query, selection="scope-b", index="index-b", query_vector="vector-b")
    ]
    assert evidence.compare_case_retrieval(baseline, comparison) == [
        "PAIRED_INDEX_CONTENT_MISMATCH",
        "PAIRED_QUERY_POLICY_SELECTION_MISMATCH",
        "PAIRED_QUERY_INDEX_MISMATCH",
        "PAIRED_QUERY_VECTOR_MISMATCH",
    ]


def test_query_vector_evidence_hashes_the_vector_bound_to_cosine_sql(monkeypatch):
    from collections import namedtuple

    policy = {"id": "P1", "version": "1", "text": "退款"}
    key = document_key(policy)
    vector = [0.123456789] * 512
    Row = namedtuple("Row", ["key", "encoder", "embedding"])
    selected = Row(key, "fixture-encoder", [1.0] * 512)
    sql_vectors = []

    class Session:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def scalars(self, _statement):
            return [key]

        def execute(self, statement):
            params = statement.compile().params
            sql_vectors.extend(value for value in params.values() if value == vector)
            return SimpleNamespace(all=lambda: [selected])

    monkeypatch.setattr(policies, "Session", Session)
    search = PolicySearch(
        None,
        encode=lambda _: [vector],
        encoder="fixture-encoder",
        evaluation_catalogue=lambda: [policy],
    )
    search.index_state = "BUILT"
    result = search.search_selected("退款", [policy])
    assert result.mode == "HYBRID" and sql_vectors == [vector]
    assert result.evidence["query_vector_sha256"] == encoding.vector_digest(sql_vectors[0])
    assert result.evidence["vector_index"] == encoding.vector_index_evidence([selected])
