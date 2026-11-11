"""Frozen and observed policy/index evidence for isolated evaluation."""

import re
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.business.models import Policy
from aftercare.business.rules import digest
from aftercare.intelligence import policy_versions
from aftercare.intelligence.policies import (
    BM25_B,
    BM25_K1,
    DIMENSIONS,
    MODEL_ID,
    QUERY_EVIDENCE_VERSION,
    QUERY_PREFIX,
    RETRIEVAL_LIMIT,
    RRF_K,
    PolicyVector,
    document_key,
)
from aftercare.intelligence.retrieval_evidence import encoder_manifest, vector_index_evidence
from aftercare.intelligence.tables import RunEvent

SCHEMA_VERSION = "ac-retrieval-evidence-v1"


def catalogue_keys(documents):
    return sorted(document_key(document) for document in documents)


def catalogue_sha256(documents):
    return digest(catalogue_keys(documents))


def fixture_policies(fixture):
    return [*fixture["business"]["policies"], *fixture.get("policy_overrides", [])]


def policy_snapshot(case_hashes):
    selector_paths = (
        Path(policy_versions.__file__),
        Path(__file__).parents[1] / "business" / "rules.py",
        Path(__file__).parents[1] / "business" / "service.py",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "selector_sha256": digest(
            [sha256(path.read_bytes()).hexdigest() for path in selector_paths]
        ),
        "cases_sha256": digest(case_hashes),
    }


def retrieval_configuration(settings):
    hybrid = bool(getattr(settings, "embedding_enabled", False))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "HYBRID" if hybrid else "BM25",
        "encoder": encoder_manifest(getattr(settings, "embedding_model_path", None))
        if hybrid
        else None,
        "query_prefix_sha256": sha256(QUERY_PREFIX.encode()).hexdigest() if hybrid else None,
        "dimensions": DIMENSIONS if hybrid else None,
        "distance": "cosine" if hybrid else None,
        "lexical": {"tokenizer": "jieba.cut_for_search", "k1": BM25_K1, "b": BM25_B},
        "fusion": {"algorithm": "rrf", "k": RRF_K, "limit": RETRIEVAL_LIMIT},
    }


def _index_query():
    return select(PolicyVector.key, PolicyVector.encoder, PolicyVector.embedding)


def read_observed_index(engine):
    with Session(engine) as session:
        return vector_index_evidence(session.execute(_index_query()).all())


def read_native_retrieval(connection, inspector, namespace, execution):
    catalogue = None
    if inspector.has_table(Policy.__tablename__, schema=namespace):
        documents = list(connection.scalars(select(Policy.data), execution_options=execution))
        catalogue = {
            "catalogue_sha256": catalogue_sha256(documents),
            "keys": catalogue_keys(documents),
        }
        if inspector.has_table(RunEvent.__tablename__, schema=namespace):
            rows = connection.execute(
                select(RunEvent.run_id, RunEvent.seq, RunEvent.payload)
                .where(RunEvent.kind == "context_binding")
                .order_by(RunEvent.run_id, RunEvent.seq),
                execution_options=execution,
            )
            catalogue["bindings"] = [
                {"run_id": run_id, "seq": seq, **_binding_payload(payload)}
                for run_id, seq, payload in rows
            ]
    index = None
    if inspector.has_table(PolicyVector.__tablename__, schema=namespace):
        rows = connection.execute(_index_query(), execution_options=execution).all()
        index = {
            **vector_index_evidence(rows),
            "rows": rows,  # Internal only, for verification of the exact query subset.
        }
    return catalogue, index


def _binding_payload(payload):
    return {
        key: payload.get(key)
        for key in ("cycle", "policy_resolution_sha256", "selected_document_keys")
    }


def _verified_bindings(trace, actual_catalogue):
    native = defaultdict(list)
    for actual in (actual_catalogue or {}).get("bindings", []):
        run_id, seq, cycle = actual["run_id"], actual["seq"], actual["cycle"]
        if (
            not isinstance(run_id, str)
            or type(seq) is not int
            or type(cycle) is not int
            or not isinstance(actual["policy_resolution_sha256"], str)
            or not isinstance(actual["selected_document_keys"], list)
            or not all(_sha256(key) for key in actual["selected_document_keys"])
        ):
            continue
        native[seq].append(actual)
    bindings = defaultdict(list)
    for event in trace:
        if event.get("kind") != "context_binding":
            continue
        matched = [
            actual
            for actual in native[event.get("seq")]
            if event.get("run_id", actual["run_id"]) == actual["run_id"]
            and _binding_payload(event.get("payload") or {}) == _binding_payload(actual)
        ]
        if len(matched) == 1:
            actual = matched[0]
            bindings[actual["run_id"], actual["cycle"]].append(actual)
    return bindings


def _sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _record_findings(row, config, isolated, findings):
    observed = (row.get("actual") or {}).get("retrieval_evidence")
    if not isinstance(observed, dict) or observed.get("schema_version") != SCHEMA_VERSION:
        if row["started_at"] is not None:
            findings["RETRIEVAL_EVIDENCE_UNAVAILABLE"] += 1
            if config.get("embedding_enabled"):
                findings["INDEX_PROVENANCE_UNAVAILABLE"] += 1
                if not isolated or not (isolated.get("index") or {}).get("keys"):
                    findings["INDEX_ROWS_UNAVAILABLE"] += 1
        return None
    policy = config.get("policy_snapshot") or {}
    retrieval = config.get("retrieval_snapshot") or {}
    actual_catalogue = isolated.get("catalogue") if isolated else None
    actual_index = isolated.get("index") if isolated else None
    if not actual_catalogue:
        findings["POLICY_CATALOGUE_UNAVAILABLE"] += 1
    elif (
        actual_catalogue["catalogue_sha256"] != observed.get("catalogue_sha256")
        or row["quota"].get("policy_catalogue_sha256") != observed.get("catalogue_sha256")
        or policy.get("selector_sha256") != observed.get("selector_sha256")
    ):
        findings["POLICY_CATALOGUE_MISMATCH"] += 1
    encoder = retrieval.get("encoder")
    if retrieval.get("mode") != observed.get("configured_mode") or (
        encoder and encoder.get("sha256")
    ) != observed.get("encoder_manifest_sha256"):
        findings["RETRIEVAL_CONFIGURATION_MISMATCH"] += 1

    reported_index = observed.get("index") or {}
    state = reported_index.get("state")
    if not actual_index:
        if state not in {"NOT_USED", "NOT_APPLICABLE"}:
            findings["INDEX_ROWS_UNAVAILABLE"] += 1
    elif any(
        actual_index[key] != reported_index.get(key)
        for key in ("keys", "encoders", "content_sha256")
    ):
        findings["INDEX_CONTENT_MISMATCH"] += 1
    if state == "BUILT":
        if (
            not actual_catalogue
            or not actual_index
            or actual_index["keys"] != actual_catalogue["keys"]
            or not encoder
            or actual_index["encoders"] != [f"{MODEL_ID}@{encoder.get('sha256')}"]
        ):
            findings["INDEX_CONTENT_MISMATCH"] += 1
    elif state in {"NOT_USED", "NOT_APPLICABLE"}:
        expected_state = "NOT_APPLICABLE" if retrieval.get("mode") == "BM25" else "NOT_USED"
        if state != expected_state or (actual_index and actual_index["keys"]):
            findings["INDEX_CONTENT_MISMATCH"] += 1
    elif state == "FAILED":
        findings["INDEX_BUILD_FAILED"] += 1
    else:
        findings["INDEX_PROVENANCE_UNAVAILABLE"] += 1

    comparable_queries = defaultdict(list)
    trace = row.get("trace", [])
    bindings = _verified_bindings(trace, actual_catalogue)
    if not bindings:
        findings["POLICY_SELECTION_NOT_OBSERVED"] += 1
    elif actual_catalogue and any(
        len(matches) != 1
        or not set(matches[0]["selected_document_keys"]) <= set(actual_catalogue["keys"])
        for matches in bindings.values()
    ):
        findings["POLICY_SELECTION_MISMATCH"] += 1
    for event in trace:
        if event.get("kind") != "retrieval":
            continue
        payload = event.get("payload") or {}
        proof = payload.get("retrieval_evidence") or {}
        candidate_keys = proof.get("candidate_keys")
        run_id, cycle = payload.get("run_id"), payload.get("cycle")
        stage, rework = payload.get("stage"), payload.get("rework")
        seq = event.get("seq")
        if (
            proof.get("schema_version") != QUERY_EVIDENCE_VERSION
            or not isinstance(candidate_keys, list)
            or not all(_sha256(key) for key in candidate_keys)
            or not _sha256(proof.get("query_sha256"))
            or not isinstance(run_id, str)
            or type(cycle) is not int
            or type(seq) is not int
            or event.get("run_id", run_id) != run_id
            or (payload.get("role"), stage)
            not in (("single", "policy"), ("policy", "policy"), ("verifier", "verification"))
            or type(rework) is not int
            or not isinstance(payload.get("snapshot"), str)
            or not actual_catalogue
            or not set(candidate_keys) <= set(actual_catalogue["keys"])
        ):
            findings["QUERY_EVIDENCE_UNAVAILABLE"] += 1
            continue
        matching = bindings.get((run_id, cycle), [])
        if (
            len(matching) != 1
            or matching[0]["seq"] >= seq
            or matching[0]["policy_resolution_sha256"] != payload["snapshot"]
            or matching[0]["selected_document_keys"] != candidate_keys
        ):
            findings["QUERY_POLICY_BINDING_MISMATCH"] += 1
        mode = payload.get("mode")
        if mode == "HYBRID":
            rows = (
                [
                    item
                    for item in actual_index["rows"]
                    if item[0] in candidate_keys and item[1] in reported_index.get("encoders", [])
                ]
                if actual_index
                else []
            )
            subset = vector_index_evidence(rows)
            if set(subset["keys"]) != set(candidate_keys) or subset != proof.get("vector_index"):
                findings["QUERY_INDEX_MISMATCH"] += 1
            if not _sha256(proof.get("query_vector_sha256")):
                findings["QUERY_VECTOR_EVIDENCE_UNAVAILABLE"] += 1
        elif mode == "BM25" and proof.get("vector_index") is None:
            if not candidate_keys:
                findings["NO_RETRIEVAL_CANDIDATES"] += 1
            elif retrieval.get("mode") == "HYBRID":
                findings["HYBRID_FELL_BACK_TO_BM25"] += 1
        else:
            findings["QUERY_INDEX_MISMATCH"] += 1
        comparison = digest([cycle, stage, rework, proof["query_sha256"]])
        comparable_queries[comparison].append(
            {
                "selection": digest([payload["snapshot"], candidate_keys]),
                "index": digest([mode, proof.get("vector_index")]),
                "query_vector": proof.get("query_vector_sha256"),
            }
        )
    if not comparable_queries:
        findings["NO_RETRIEVAL_OBSERVED"] += 1
    return {
        "catalogue_sha256": observed.get("catalogue_sha256"),
        "index": reported_index,
        "queries": dict(comparable_queries),
    }


def native_retrieval_evidence(row, config, isolated, findings):
    # Legacy native test readers return their historical list of index identities.
    if not isinstance(isolated, dict):
        if isinstance((row.get("actual") or {}).get("retrieval_evidence"), dict):
            return _record_findings(row, config, None, findings)
        if row["started_at"] is not None:
            findings["RETRIEVAL_EVIDENCE_UNAVAILABLE"] += 1
            if config.get("embedding_enabled"):
                findings["INDEX_PROVENANCE_UNAVAILABLE"] += 1
                if not isolated:
                    findings["INDEX_ROWS_UNAVAILABLE"] += 1
        return None
    return _record_findings(row, config, isolated, findings)


def compare_case_retrieval(left, right):
    if left is None or right is None:
        return []
    mismatches = []
    if left["catalogue_sha256"] != right["catalogue_sha256"]:
        mismatches.append("PAIRED_POLICY_CATALOGUE_MISMATCH")
    if (
        left["index"].get("state") == right["index"].get("state") == "BUILT"
        and left["index"] != right["index"]
    ):
        mismatches.append("PAIRED_INDEX_CONTENT_MISMATCH")
    common = left["queries"].keys() & right["queries"].keys()
    paired = [pair for key in common for pair in zip(left["queries"][key], right["queries"][key])]
    for field, finding in (
        ("selection", "PAIRED_QUERY_POLICY_SELECTION_MISMATCH"),
        ("index", "PAIRED_QUERY_INDEX_MISMATCH"),
        ("query_vector", "PAIRED_QUERY_VECTOR_MISMATCH"),
    ):
        if any(first[field] != second[field] for first, second in paired):
            mismatches.append(finding)
    return mismatches
