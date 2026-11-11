#!/usr/bin/env python3
"""Compare the production policy retriever's three rankings on a fixed dev set."""

import argparse
import hashlib
import json
from pathlib import Path
from statistics import fmean
from uuid import uuid4

from aftercare.business.rules import scoped_policy_documents
from aftercare.business.seed import DAMAGE_REMEDY_POLICY, WRONG_ITEM_POLICY
from aftercare.config import load_settings
from aftercare.db import guarded_engine
from aftercare.intelligence.graph import INTENT_TERMS
from aftercare.intelligence.policies import (
    DIMENSIONS,
    MODEL_ID,
    PolicySearch,
    PolicyVector,
    bge_encoder,
    document_key,
)
from aftercare.intelligence.policy_versions import resolve_policies
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ROOT / "data/retrieval/queries.v1.json"
BUSINESS = ROOT / "data/v0.1/agent/business.json"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
METHODS = ("bm25", "semantic", "rrf")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def key(document):
    return document["id"], document["version"]


def prepare():
    fixture = json.loads(QUERIES.read_text())
    baseline = json.loads(BUSINESS.read_text())
    base = baseline["policies"] + [WRONG_ITEM_POLICY, DAMAGE_REMEDY_POLICY]
    products = {row["id"]: row for row in baseline["products"]}
    prepared = []
    require(
        len({row["id"] for row in fixture["queries"]}) == len(fixture["queries"]),
        "Duplicate query ID",
    )
    for row in fixture["queries"]:
        versions = fixture["synthetic_versions"][row["variants"]] if "variants" in row else []
        product = products[row.get("product", fixture["default_product"])]
        scope = resolve_policies(
            base + versions,
            intent=row["intent"],
            products=[product],
            timestamps={"decision_at": fixture["decision_at"]},
        ).to_dict()
        scoped_policy_documents(scope, row["intent"], "DELIVERED")
        selected = {key(document) for document in scope["documents"]}
        expected = set(map(tuple, row["relevant"]))
        forbidden = set(map(tuple, row.get("forbidden", [])))
        require(
            expected <= selected, f"{row['id']}: expected relevance is outside eligible documents"
        )
        require(not forbidden & selected, f"{row['id']}: forbidden version entered candidates")
        require(
            not {policy_id for policy_id, _ in selected} & set(row.get("forbidden_ids", [])),
            f"{row['id']}: forbidden policy entered candidates",
        )
        require(
            sorted(set(scope["blockers"])) == sorted(row.get("expected_blockers", [])),
            f"{row['id']}: unexpected applicability blockers {scope['blockers']}",
        )
        require(expected or scope["blockers"], f"{row['id']}: unscorable query without a blocker")
        prepared.append(
            {
                "case": row,
                "query": f"{INTENT_TERMS[row['intent']]} {row['message'][:600]}",
                "product": product["id"],
                "expected": expected,
                "scope": scope,
            }
        )
    return fixture, prepared


def compare(search, prepared, cutoffs):
    rows = []
    for entry in prepared:
        case, scope = entry["case"], entry["scope"]
        result = search.search_selected(
            entry["query"], scope["documents"], blockers=scope["blockers"], limit=max(cutoffs)
        )
        require(
            result.mode == "HYBRID" and "INCOMPLETE_VECTOR_INDEX" not in result.blockers,
            f"{case['id']}: semantic ranking unavailable",
        )
        rankings = {
            "bm25": list(map(key, result.lexical_versions)),
            "semantic": list(map(key, result.semantic_versions)),
            "rrf": list(map(key, result.documents)),
        }
        selected = {key(document) for document in scope["documents"]}
        require(set(rankings["semantic"]) == selected, f"{case['id']}: incomplete semantic rank")
        require(
            all(set(ranked) <= selected for ranked in rankings.values()),
            f"{case['id']}: ranking escaped policy scope",
        )
        expected = entry["expected"]
        rows.append(
            {
                "id": case["id"],
                "intent": case["intent"],
                "kind": case["kind"],
                "message": case["message"],
                "retrieval_query": entry["query"],
                "product": entry["product"],
                "relevant": case["relevant"],
                "candidate_versions": sorted(selected),
                "blockers": result.blockers,
                "rankings": rankings,
                "recall": {
                    method: {
                        f"@{k}": len(expected & set(ranked[:k])) / len(expected) for k in cutoffs
                    }
                    for method, ranked in rankings.items()
                }
                if expected
                else None,
            }
        )
    return rows


def summarize(rows, cutoffs):
    paired = [row for row in rows if row["recall"] is not None]
    result = {"paired_queries": len(paired), "applicability_only_queries": len(rows) - len(paired)}
    result["by_cutoff"] = {}
    for k in cutoffs:
        label = f"@{k}"
        scores = {
            method: fmean(row["recall"][method][label] for row in paired) for method in METHODS
        }
        result["by_cutoff"][label] = {
            "macro_recall": scores,
            "rrf_minus_bm25": scores["rrf"] - scores["bm25"],
            "rrf_minus_semantic": scores["rrf"] - scores["semantic"],
            "semantic_minus_bm25": scores["semantic"] - scores["bm25"],
        }
    return result


def run(fixture, prepared, model_dir):
    require(
        model_dir.is_dir() and model_dir.name == REVISION,
        "Pinned local BGE revision directory required",
    )
    files = sorted(path for path in model_dir.rglob("*") if path.is_file())
    require(
        (model_dir / "model.safetensors").is_file() and (model_dir / "config.json").is_file(),
        "Incomplete BGE snapshot",
    )
    model_hashes = {path.relative_to(model_dir).as_posix(): sha(path) for path in files}
    engine = guarded_engine(load_settings("test"), test_only=True)
    schema = "retrieval_benchmark_" + uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            PolicyVector.__table__.create(scoped)
            search = PolicySearch(
                scoped, encode=bge_encoder(str(model_dir), None), encoder=f"{MODEL_ID}@{REVISION}"
            )
            documents = {
                document_key(doc): doc for entry in prepared for doc in entry["scope"]["documents"]
            }
            indexed = search.index(list(documents.values()))
            require(indexed == len(documents), "Isolated benchmark vector index was not populated")
            rows = compare(search, prepared, fixture["k"])
        finally:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        engine.dispose()
    return {
        "status": "COMPLETED",
        "source": "real_test_postgresql_pgvector_local_bge",
        "benchmark_version": fixture["version"],
        "relevance_source": fixture["relevance_source"],
        "query_file_sha256": sha(QUERIES),
        "business_file_sha256": sha(BUSINESS),
        "model": {
            "id": MODEL_ID,
            "revision": REVISION,
            "dimensions": DIMENSIONS,
            "file_sha256": model_hashes,
        },
        "indexed_documents": indexed,
        "overall": summarize(rows, fixture["k"]),
        "by_intent": {
            intent: summarize([row for row in rows if row["intent"] == intent], fixture["k"])
            for intent in INTENT_TERMS
        },
        "queries": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models" / REVISION)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Check fixed queries and scopes without PostgreSQL or BGE",
    )
    parser.add_argument(
        "--output", type=Path, help="New JSON file. Existing reports are preserved."
    )
    args = parser.parse_args()
    fixture, prepared = prepare()
    if args.validate_only:
        print(
            json.dumps(
                {"status": "SCOPE_VALIDATED", "query_count": len(prepared)}, ensure_ascii=False
            )
        )
        return
    if args.output is None or args.output.exists():
        parser.error("Use --output with a new JSON path")
    result = run(fixture, prepared, args.model_dir.resolve())
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result["overall"], ensure_ascii=False))


if __name__ == "__main__":
    main()
