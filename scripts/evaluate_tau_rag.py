"""Score retrieval of frozen source clauses in the τ-bench retail policy."""

import argparse
import hashlib
import json
import math
from contextlib import ExitStack
from itertools import combinations
from pathlib import Path
from statistics import mean
from time import perf_counter

if __package__:
    from .tau_documents import extract
    from .tau_rag import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, build_chunks, search
else:
    from tau_documents import extract
    from tau_rag import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, build_chunks, search

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "tau-rag-eval"
FROZEN = {
    "queries.json": "8441fb4f78e338116a3c37697641c46ad43ebb03cfcc098bd6ff6d91455457f9",
    "labels.json": "c79bb2a32ee1a5f7a58386e7db52cda02c1b8e2dcfe176aed855ce38958cf1eb",
}
CUTOFFS = (1, 3, 5)


def frozen_json(name):
    raw = (FIXTURES / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN[name]:
        raise ValueError(f"frozen development fixture changed: {name}")
    return json.loads(raw)


def load_labels(raw):
    labels = frozen_json("labels.json")
    lines = raw.splitlines(keepends=True)
    for clauses in labels.values():
        for clause in clauses:
            index = clause["line"] - 1
            start = sum(map(len, lines[:index]))
            end = start + len(lines[index])
            if (
                start != clause["byte_start"]
                or end != clause["byte_end_exclusive"]
                or raw[start:end].decode().rstrip("\r\n") != clause["excerpt"]
            ):
                raise ValueError(f"frozen label does not match source line {index + 1}")
    return labels


def valid_citations(hit, document, blocks):
    if (
        hit.get("document_id") != document["id"]
        or hit.get("document_version") != document["source"]["document_version"]
        or hit.get("source", {}).get("sha256") != document["source"]["sha256"]
    ):
        return []
    locators = []
    for citation in hit.get("citations", []):
        block = blocks.get(citation.get("block_id"))
        if block is None or any(citation.get(key) != block[key]
                                for key in ("heading_path", "kind", "location")):
            continue
        start = citation.get("char_start")
        end = citation.get("char_end_exclusive")
        text = block["text"]
        if (
            type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or text[start:end] not in hit.get("text", "")
        ):
            continue
        locators.append((block["location"], text, start, end))
    return locators


def score(results, clauses, document):
    blocks = {block["id"]: block for block in document["blocks"]}
    ranks = {}
    citation_total = 0
    citation_valid = 0
    hits = []
    for rank, hit in enumerate(results[:max(CUTOFFS)], 1):
        citations = valid_citations(hit, document, blocks)
        citation_total += len(hit.get("citations", []))
        citation_valid += len(citations)
        matched = [
            clause["line"] for clause in clauses
            if any(
                location["byte_start"] + len(text[:start].encode()) <= clause["byte_start"]
                and location["byte_start"] + len(text[:end].encode())
                >= clause["byte_start"] + len(clause["excerpt"].encode())
                for location, text, start, end in citations
            )
        ]
        for line in matched:
            ranks.setdefault(line, rank)
        hits.append({
            "chunk_id": hit["id"], "rank": rank, "matched_lines": matched,
            "ranking": {key: value for key, value in hit.items()
                        if key.endswith(("_rank", "_score"))},
            "citations": hit.get("citations", []),
            "parent_citations": hit.get("parent_citations", []),
        })
    metrics = {}
    for cutoff in CUTOFFS:
        observed = [rank for rank in ranks.values() if rank <= cutoff]
        metrics[f"recall@{cutoff}"] = len(observed) / len(clauses)
        metrics[f"mrr@{cutoff}"] = 1 / min(observed) if observed else 0.0
        metrics[f"ndcg@{cutoff}"] = sum(
            1 / math.log2(rank + 1) for rank in observed
        ) / len(clauses)
    return {
        "metrics": metrics,
        "citation_total": citation_total,
        "citation_valid": citation_valid,
        "missed_lines": [clause["line"] for clause in clauses if clause["line"] not in ranks],
        "hits": hits,
    }


def vector_digest(vectors):
    payload = json.dumps(
        {key: [float(value) for value in vector] for key, vector in vectors.items()},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def cosine_rank(_chunks, vectors):
    norms = {key: math.sqrt(sum(x*x for x in vector)) for key, vector in vectors.items()}

    def rank(vector, allowed_ids, limit):
        norm = math.sqrt(sum(x*x for x in vector))
        if not norm:
            raise ValueError("zero-norm query vector")
        values = [
            (key, sum(a*b for a, b in zip(vector, vectors[key], strict=True)) / (norm * norms[key]))
            for key in allowed_ids
        ]
        return sorted(values, key=lambda pair: (-pair[1], pair[0]))[:limit]

    return rank


def summarize(rows):
    durations = sorted(row["wall_ms"] for row in rows)
    combined = sorted(row["wall_ms"] + (row["shared_query_embedding_ms"] or 0) for row in rows)
    citations = sum(row["citation_total"] for row in rows)
    verified = sum(row["citation_valid"] for row in rows)
    return {
        "query_count": len(rows),
        "failure_count": sum(row["status"] != "ok" for row in rows),
        "metrics": {
            key: mean(row["metrics"][key] for row in rows) for key in rows[0]["metrics"]
        },
        "citation_precision": verified / citations if citations else None,
        "citation_total": citations,
        "citation_valid": verified,
        "latency_ms": {
            "mean": mean(durations), "p95": durations[math.ceil(.95 * len(rows)) - 1],
            "mean_with_shared_embedding": mean(combined),
            "p95_with_shared_embedding": combined[math.ceil(.95 * len(rows)) - 1],
        },
    }


def access_probes(chunks, modes, *, setup_error=None):
    cases = (
        ("allowed_start_inclusive", ["test_policy_reader"], "2026-01-01", None, True),
        ("wrong_role", ["test_other_role"], "2026-03-01", None, False),
        ("before_start", ["test_policy_reader"], "2025-12-31", None, False),
        ("end_exclusive", ["test_policy_reader"], "2027-01-01", None, False),
        ("missing_date", ["test_policy_reader"], None, None, False),
        ("wrong_version", ["test_policy_reader"], "2026-03-01", {"synthetic_access": "old"}, False),
    )
    rows = []
    for backend, mode, options in modes:
        for name, roles, as_of, versions, allowed in cases:
            error = setup_error
            result_count = None
            if error is None:
                try:
                    response = search(
                        "cancel pending order", chunks, roles=roles, as_of=as_of,
                        policy_keys=["synthetic_access"], versions=versions, mode=mode,
                        candidate_limit=20, limit=5, **options,
                    )
                    result_count = len(response["results"])
                except Exception as exc:  # noqa: BLE001 - Keep driver failures visible in the access denominator.
                    error = f"{type(exc).__name__}: {exc}"
            rows.append({
                "id": name, "backend": backend, "mode": mode, "expected_allowed": allowed,
                "result_count": result_count, "error": error,
                "status": "unrun" if setup_error else "failed" if error else "ok",
                "passed": error is None and bool(result_count) == allowed,
            })
    return rows


def compare_vector_backends(runs):
    pairs = []
    for left, right in combinations(runs, 2):
        if (
            left["backend"] == right["backend"]
            or (left["strategy"], left["mode"]) != (right["strategy"], right["mode"])
        ):
            continue
        rows = []
        for a, b in zip(left["queries"], right["queries"], strict=True):
            if a["id"] != b["id"]:
                raise ValueError("cannot align vector backend query results")
            if a["status"] != "ok" or b["status"] != "ok":
                rows.append({"id": a["id"], "status": "unavailable"})
                continue
            ids_a = [hit["chunk_id"] for hit in a["hits"]]
            ids_b = [hit["chunk_id"] for hit in b["hits"]]
            union = set(ids_a) | set(ids_b)
            rows.append({
                "id": a["id"], "status": "ok", "exact_rank_agreement": ids_a == ids_b,
                "top5_jaccard": len(set(ids_a) & set(ids_b)) / len(union) if union else None,
            })
        usable = [row for row in rows if row["status"] == "ok"]
        pairs.append({
            "strategy": left["strategy"], "mode": left["mode"],
            "backends": [left["backend"], right["backend"]], "queries": rows,
            "comparable_query_count": len(usable),
            "exact_rank_agreement_count": sum(row["exact_rank_agreement"] for row in usable),
        })
    return pairs


def evaluate(
    policy_path, *, embed=None, rerank=None, index_factories=None,
    chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP,
):
    """Run selected retrievers. Index factories return a ranker or a managed ranker."""
    raw, document = extract(policy_path)
    queries = frozen_json("queries.json")
    query_vectors = {}
    query_embedding_ms = {}
    query_embedding_errors = {}

    def encode_query(query):
        if query in query_embedding_errors:
            raise ValueError(query_embedding_errors[query])
        return query_vectors[query]

    factories = index_factories if index_factories is not None else {"memory_cosine": cosine_rank}
    if embed is None and (index_factories or rerank is not None):
        raise ValueError("index factories and reranker require an embedding provider")
    report = {
        "scope": "synthetic development queries on the pinned τ-bench retail policy",
        "source": document["source"], "frozen_fixtures": FROZEN, "candidate_limit": 20,
        "limit": 5, "chunk_config": {"chunk_size": chunk_size, "overlap": overlap},
        "comparisons": [], "bad_cases": [], "access_probes": [], "index_failures": [],
        "chunk_counts": {}, "vector_hashes": {},
        "unrun": ([] if embed else ["dense", "hybrid", "rerank"]) +
                 (["rerank"] if embed and rerank is None else []),
    }
    if embed is not None:
        for query in [row["query"] for row in queries] + ["cancel pending order"]:
            started = perf_counter()
            try:
                query_vectors[query] = embed(query)
            except Exception as exc:  # noqa: BLE001 - Embedding providers expose different runtime exceptions.
                query_embedding_errors[query] = f"{type(exc).__name__}: {exc}"
            query_embedding_ms[query] = (perf_counter() - started) * 1000
        report["vector_hashes"]["queries"] = vector_digest(query_vectors)
    unscored = []
    for strategy in ("fixed", "structure"):
        chunks = build_chunks(
            document, strategy=strategy, policy_key="tau_retail", roles=["policy_reader"],
            **report["chunk_config"],
        )
        report["chunk_counts"][strategy] = len(chunks)
        probe_chunks = build_chunks(
            document, strategy=strategy, policy_key="synthetic_access", roles=["test_policy_reader"],
            effective_from="2026-01-01", effective_until="2027-01-01",
            **report["chunk_config"],
        )
        vectors = {}
        probe_vectors = {}
        vector_error = None
        if embed is not None:
            try:
                vectors = {chunk["id"]: embed(chunk["text"]) for chunk in chunks}
                report["vector_hashes"][strategy] = vector_digest(vectors)
                probe_vectors = {
                    chunk["id"]: vectors[original["id"]]
                    for chunk, original in zip(probe_chunks, chunks, strict=True)
                }
            except Exception as exc:  # noqa: BLE001 - Keep failed corpus embedding in every affected comparison.
                vector_error = f"{type(exc).__name__}: {exc}"
        for backend, factory in [("lexical", None), *list(factories.items() if embed else ())]:
            modes = ("dense", "hybrid", "rerank") if rerank else ("dense", "hybrid")
            modes = modes if factory else ("bm25",)
            with ExitStack() as stack:
                options = {}
                probe_options = {}
                setup_error = vector_error if factory else None
                probe_error = setup_error
                if factory and setup_error is None:
                    model_options = {"encode_query": encode_query, "rerank": rerank}
                    for purpose, selected_chunks, selected_vectors in (
                        ("retrieval", chunks, vectors), ("access_probes", probe_chunks, probe_vectors),
                    ):
                        try:
                            ranker = factory(selected_chunks, selected_vectors)
                            if hasattr(ranker, "__enter__"):
                                ranker = stack.enter_context(ranker)
                            selected_options = {**model_options, "dense_rank": ranker}
                            if purpose == "retrieval":
                                options = selected_options
                            else:
                                probe_options = selected_options
                        except Exception as exc:  # noqa: BLE001 - Drivers expose different connection and indexing exceptions.
                            error = f"{type(exc).__name__}: {exc}"
                            if purpose == "retrieval":
                                setup_error = error
                            else:
                                probe_error = error
                for purpose, error in (("retrieval", setup_error), ("access_probes", probe_error)):
                    if error and (purpose == "retrieval" or error != setup_error):
                        report["index_failures"].append({
                            "strategy": strategy, "backend": backend, "purpose": purpose,
                            "error": error,
                        })
                probes = access_probes(
                    probe_chunks, [(backend, mode, probe_options) for mode in modes],
                    setup_error=probe_error,
                )
                report["access_probes"].extend({"strategy": strategy, **row} for row in probes)
                for mode in modes:
                    rows = []
                    for query in queries:
                        started = perf_counter()
                        response = {}
                        error = setup_error
                        if not error:
                            try:
                                response = search(
                                    query["query"], chunks, roles=["policy_reader"], mode=mode,
                                    policy_keys=["tau_retail"], candidate_limit=20, limit=5,
                                    versions={"tau_retail": document["source"]["document_version"]},
                                    **options,
                                )
                            except Exception as exc:  # noqa: BLE001 - Preserve each failed backend/model query in its denominator.
                                error = f"{type(exc).__name__}: {exc}"
                        rows.append({
                            **query, "error": error, "status": "failed" if error else "ok",
                            "wall_ms": (perf_counter() - started) * 1000,
                            "stage_ms": response.get("timings_ms", {}),
                            "shared_query_embedding_ms": (
                                query_embedding_ms[query["query"]] if factory else None
                            ),
                            "results": response.get("results", []),
                        })
                    unscored.append((strategy, backend, mode, rows))
                try:
                    stack.close()
                except Exception as exc:  # noqa: BLE001 - Cleanup errors must not discard completed comparisons.
                    report["index_failures"].append({
                        "strategy": strategy, "backend": backend, "purpose": "cleanup",
                        "error": f"{type(exc).__name__}: {exc}",
                    })

    labels = load_labels(raw)
    if set(labels) != {query["id"] for query in queries}:
        raise ValueError("query IDs and labels do not match")
    for strategy, backend, mode, rows in unscored:
        comparison = {"strategy": strategy, "backend": backend, "mode": mode}
        for row in rows:
            clauses = labels[row["id"]]
            row.update(score(row.pop("results"), clauses, document))
            if row["error"] or row["missed_lines"] or row["citation_total"] != row["citation_valid"]:
                report["bad_cases"].append({
                    **comparison, **row, "expected_clauses": clauses,
                })
        report["comparisons"].append({
            **comparison,
            "overall": summarize(rows),
            "by_language": {
                lang: summarize([row for row in rows if row["language"] == lang])
                for lang in ("en", "zh")
            },
            "queries": rows,
        })
    report["same_vector_backend_agreement"] = compare_vector_backends(
        [run for run in report["comparisons"] if run["mode"] != "bm25"]
    )
    return report


def main():
    parser = argparse.ArgumentParser(description="Run frozen τ-bench retrieval comparisons")
    parser.add_argument("policy_path", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--model-cache", type=Path, help="Enable the pinned embedding and reranker")
    parser.add_argument("--database-stores", action="store_true",
                        help="Use Milvus and pgvector for the vector comparisons")
    args = parser.parse_args()
    if args.database_stores and args.model_cache is None:
        parser.error("--database-stores requires --model-cache")
    chunk_config = {"chunk_size": args.chunk_size, "overlap": args.overlap}
    if args.model_cache is None:
        report = evaluate(args.policy_path, **chunk_config)
    else:
        if __package__:
            from .tau_rag_store import index_factories, load_models
        else:
            from tau_rag_store import index_factories, load_models

        embed, rerank, provenance = load_models(args.model_cache)
        report = evaluate(
            args.policy_path, embed=embed, rerank=rerank,
            index_factories=index_factories() if args.database_stores else None,
            **chunk_config,
        )
        report["model_provenance"] = provenance
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.output)
    return int(bool(
        report["index_failures"]
        or any(not row["passed"] for row in report["access_probes"])
        or any(comparison["overall"]["failure_count"] for comparison in report["comparisons"])
    ))


if __name__ == "__main__":
    raise SystemExit(main())
