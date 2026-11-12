"""Offline retrieval over admitted P2 document exports."""

import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_REVIEW = {"source_verified", "native_extracted"}
MODES = ("bm25", "dense", "hybrid", "rerank")
DEFAULT_CHUNK_SIZE = 450
DEFAULT_OVERLAP = 70
TOKEN = re.compile(r"[\u3400-\u9fff]|[^\W_]+", re.UNICODE)


class RagError(ValueError):
    pass


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RagError("policy dates must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _role_set(roles):
    if isinstance(roles, str):
        raise RagError("roles and policy keys must be collections of nonempty strings")
    items = tuple(roles)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise RagError("roles and policy keys must be collections of nonempty strings")
    return set(items)


def _check_document(document):
    if (document.get("schema_version") != 1
            or document.get("extraction_status") != "parsed"
            or document.get("review_status") not in TRUSTED_REVIEW):
        raise RagError("only trusted, fully parsed P2 documents can be indexed")
    if any(page.get("review_status") not in TRUSTED_REVIEW or page.get("method") != "native"
           for page in document["pages"]):
        raise RagError("untrusted or OCR page")
    source = document["source"]
    if source["source_kind"] == "tau2_retail_policy":
        manifest_name = "data/tau/manifest.json"
        manifest = json.loads((ROOT / manifest_name).read_text())
        entry = manifest["files"]["policy.md"]
        version = manifest["revision"]
        if (source.get("name") != "policy.md" or source.get("source_revision") != version
                or source.get("license") != manifest["license"]):
            raise RagError("policy provenance mismatch")
    elif source["source_kind"] == "synthetic_format_fixture":
        manifest_name = "data/tau-documents/manifest.json"
        manifest = json.loads((ROOT / manifest_name).read_text())
        entry = next((item for item in manifest["fixtures"] if item["path"] == source.get("name")), None)
        if (entry is None or entry["expected_status"] not in TRUSTED_REVIEW
                or source.get("fixture_id") != entry["fixture_id"]
                or source.get("format_variant") != entry["format_variant"]):
            raise RagError("fixture is not approved for indexing")
        version = entry.get("document_version", manifest["document_version"])
    else:
        raise RagError("source kind is not approved for indexing")
    if (source.get("document_version") != version or source.get("manifest") != manifest_name
            or any(source.get(field) != entry.get(field) for field in ("sha256", "size_bytes", "source_url"))):
        raise RagError("source does not match the pinned manifest")
    identity = f"{source['source_kind']}:{version}:{source['name']}:{source['sha256']}"
    if document.get("id") != hashlib.sha256(identity.encode()).hexdigest():
        raise RagError("document identity mismatch")
    blocks = document["blocks"]
    if not blocks or any(not b.get("id") or not b.get("text") or not b.get("location") for b in blocks):
        raise RagError("document contains invalid blocks")


def load_document(path):
    """Verify the P2 export against its pinned original and re-extraction."""
    if __package__:
        from .tau_documents import extract
    else:
        from tau_documents import extract

    path = Path(path)
    if path.is_dir():
        path = path / "document.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    _check_document(document)
    source = document["source"]
    original = path.parent / "original" / source["name"]
    fixture = source["source_kind"] == "synthetic_format_fixture"
    raw, expected = extract(source["name"] if fixture else original, fixture=fixture)
    if original.read_bytes() != raw or document != expected:
        raise RagError("P2 export does not match its original and pinned extraction")
    return document


def _cite(block, start=0, end=None):
    return {
        "block_id": block["id"],
        "location": block["location"],
        "heading_path": block.get("heading_path", []),
        "kind": block["kind"],
        "char_start": start,
        "char_end_exclusive": len(block["text"]) if end is None else end,
    }


def _windows(length, size, overlap):
    start = 0
    while start < length:
        end = min(start + size, length)
        yield start, end
        if end == length:
            break
        start = end - overlap


def _sections(blocks):
    sections = defaultdict(list)
    for index, block in enumerate(blocks):
        heading = tuple((h["level"], h["title"]) for h in block.get("heading_path", []))
        # A repeated heading is a new section even when its label matches.
        if block["kind"] == "heading" or block.get("heading_candidate") or index == 0:
            section_start = index
        key = (section_start, block["location"].get("page"), heading)
        sections[key].append(index)
    return {index: section for section in sections.values() for index in section}


def build_chunks(document, *, strategy, policy_key, roles, effective_from=None,
                 effective_until=None, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP):
    """Build deterministic spans from an in-memory P2 extraction or verified export.

    Use load_document at the file boundary to authenticate the text against the original.
    """
    _check_document(document)
    if (strategy not in {"fixed", "structure"} or not isinstance(policy_key, str)
            or not policy_key.strip() or not 0 <= overlap < chunk_size):
        raise RagError("invalid chunking configuration")
    access = sorted(_role_set(roles))
    if not access:
        raise RagError("at least one access role is required")
    source = document["source"]
    source_date = source.get("effective_date")
    if source_date and effective_from not in (None, source_date):
        raise RagError("applicability conflicts with the source date")
    effective_from = source_date or effective_from
    first, last = _date(effective_from), _date(effective_until)
    if last is not None and (first is None or first >= last):
        raise RagError("effective_until must follow effective_from")
    blocks = document["blocks"]
    sections = _sections(blocks)
    texts = [block["text"] for block in blocks]
    start = 0
    spans = []
    for value in texts:
        spans.append((start, start + len(value)))
        start += len(value) + 2
    stream = "\n\n".join(texts)
    chunks = []

    def append(body, cited, span_key):
        if not body.strip():
            return
        parents = set()
        for index, _, _ in cited:
            section = sections[index]
            if blocks[section[0]]["kind"] == "heading" or blocks[section[0]].get("heading_candidate"):
                parents.add(section[0])
            position = section.index(index)
            parents.update(section[max(0, position - 1):position + 2])
        parents = sorted(parents)
        identity = [document["id"], policy_key, strategy, chunk_size, overlap, span_key]
        chunk_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        chunks.append({
            "id": chunk_id,
            "text": body,
            "parent_text": "\n\n".join(texts[i] for i in parents),
            "citations": [_cite(blocks[i], start, end) for i, start, end in cited],
            "parent_citations": [_cite(blocks[i]) for i in parents],
            "document_id": document["id"],
            "document_version": source["document_version"],
            "policy_key": policy_key,
            "roles": access,
            "effective_from": effective_from,
            "effective_until": effective_until,
            "strategy": strategy,
            "source": source,
        })

    if strategy == "fixed":
        for start, end in _windows(len(stream), chunk_size, overlap):
            cited = [(i, max(0, start - left), min(right, end) - left)
                     for i, (left, right) in enumerate(spans) if left < end and start < right]
            append(stream[start:end], cited, [start, end])
    else:
        for i, value in enumerate(texts):
            # Preserve table rows and whole clauses when they exceed the fixed window.
            append(value, [(i, 0, len(value))], [i])
    return chunks


def tokenize(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    # Separate Han characters from adjacent Latin words in either direction.
    normalized = re.sub(r"([\u3400-\u9fff])", r" \1 ", normalized)
    return TOKEN.findall(normalized)


def _bm25(query, chunks, limit):
    term_counts = {chunk["id"]: Counter(tokenize(chunk["text"])) for chunk in chunks}
    if not term_counts:
        return []
    count = len(term_counts)
    mean_length = sum(sum(tokens.values()) for tokens in term_counts.values()) / count
    if not mean_length:
        return []
    terms = set(tokenize(query))
    frequencies = {term: sum(term in tokens for tokens in term_counts.values()) for term in terms}
    idf = {term: math.log1p((count - frequency + 0.5) / (frequency + 0.5))
           for term, frequency in frequencies.items()}
    scores = []
    for chunk_id, tokens in term_counts.items():
        length_weight = 1.5 * (0.25 + 0.75 * sum(tokens.values()) / mean_length)
        score = sum(idf[t] * tokens[t] * 2.5 / (tokens[t] + length_weight) for t in sorted(terms))
        if score > 0:
            scores.append((chunk_id, score))
    return sorted(scores, key=lambda item: (-item[1], item[0]))[:limit]


def _eligible(chunks, roles, policy_keys, as_of, versions):
    """Check applicability independently of ACL; ambiguous versions fail closed."""
    roles = _role_set(roles)
    scope = _role_set(policy_keys)
    day = _date(as_of)
    applicable = []
    versions_by_policy = defaultdict(set)
    for chunk in chunks:
        policy = chunk["policy_key"]
        if policy not in scope:
            continue
        first = _date(chunk["effective_from"])
        last = _date(chunk["effective_until"])
        if day is None:
            if first is not None or last is not None or versions is None:
                continue
        elif first is None or day < first or (last is not None and day >= last):
            continue
        if versions is not None and versions.get(policy) != chunk["document_version"]:
            continue
        versions_by_policy[policy].add(chunk["document_version"])
        applicable.append(chunk)
    eligible = {}
    for chunk in applicable:
        if len(versions_by_policy[chunk["policy_key"]]) != 1 or not roles.intersection(chunk["roles"]):
            continue
        chunk_id = chunk["id"]
        if chunk_id in eligible and eligible[chunk_id] != chunk:
            raise RagError("conflicting metadata for a chunk ID")
        eligible[chunk_id] = chunk
    return eligible


def _ms(start):
    return (perf_counter() - start) * 1000


def search(query, chunks, *, roles, policy_keys, as_of=None, versions=None, encode_query=None,
           dense_rank=None, rerank=None, mode="hybrid", candidate_limit=20, limit=5):
    if not isinstance(query, str) or not query.strip() or mode not in MODES or not 0 < limit <= candidate_limit:
        raise RagError("invalid query, mode or retrieval limits")
    if mode != "bm25" and (encode_query is None or dense_rank is None):
        raise RagError("this mode requires a dense encoder and store")
    if mode == "rerank" and rerank is None:
        raise RagError("rerank mode requires a cross-encoder")
    start = perf_counter()
    allowed = _eligible(chunks, roles, policy_keys, as_of, versions)
    timings = {"filter": _ms(start)}
    ranking = {}
    if allowed and mode != "dense":
        stage = perf_counter()
        ranking["bm25"] = _bm25(query, allowed.values(), candidate_limit)
        timings["bm25"] = _ms(stage)
    if allowed and mode != "bm25":
        stage = perf_counter()
        vector = encode_query(query)
        timings["encode_query"] = _ms(stage)
        stage = perf_counter()
        dense = list(dense_rank(vector, set(allowed), candidate_limit))
        if (len(dense) > candidate_limit or len({key for key, _ in dense}) != len(dense)
                or any(key not in allowed or not math.isfinite(score) for key, score in dense)):
            raise RagError("dense store returned invalid or unauthorized candidates")
        ranking["dense"] = dense
        timings["dense"] = _ms(stage)
    stage = perf_counter()
    metadata = defaultdict(dict)
    for method, hits in ranking.items():
        for position, (chunk_id, score) in enumerate(hits, 1):
            metadata[chunk_id].update({f"{method}_rank": position, f"{method}_score": score})
    scores = {}
    for chunk_id, info in metadata.items():
        if mode in ("hybrid", "rerank"):
            score = sum(1 / (60 + info[f"{name}_rank"]) for name in ranking if f"{name}_rank" in info)
            info["rrf_score"] = score
        else:
            score = info[f"{mode}_score"]
        scores[chunk_id] = score
    ranked = sorted(scores, key=lambda key: (-scores[key], key))[:candidate_limit]
    timings["rank"] = _ms(stage)
    if mode == "rerank" and ranked:
        stage = perf_counter()
        relevance = list(rerank(query, [allowed[key]["text"] for key in ranked]))
        if len(relevance) != len(ranked) or any(not math.isfinite(score) for score in relevance):
            raise RagError("cross-encoder returned invalid scores")
        for key, score in zip(ranked, relevance, strict=True):
            metadata[key]["rerank_score"] = score
        ranked.sort(key=lambda key: -metadata[key]["rerank_score"])
        timings["rerank"] = _ms(stage)
    results = [{**allowed[key], **metadata[key], "rank": rank,
                "date_applicability": "matched" if as_of is not None else "unknown"}
               for rank, key in enumerate(ranked[:limit], 1)]
    timings["total"] = _ms(start)
    return {"results": results, "timings_ms": timings}


def _local_dense_rank(vectors):
    def rank(query_vector, allowed_ids, limit):
        query_norm = math.hypot(*query_vector)
        scores = []
        for chunk_id in allowed_ids:
            vector = vectors[chunk_id]
            norm = query_norm * math.hypot(*vector)
            score = math.fsum(a * b for a, b in zip(query_vector, vector, strict=True)) / norm
            scores.append((chunk_id, score))
        return sorted(scores, key=lambda pair: (-pair[1], pair[0]))[:limit]

    return rank


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search a verified P2 export offline.")
    parser.add_argument("document", type=Path, help="P2 export directory or its document.json")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--strategy", choices=("fixed", "structure"), default="structure")
    parser.add_argument("--mode", choices=MODES, default="bm25")
    parser.add_argument("--policy-key", required=True)
    parser.add_argument("--role", required=True, help="role assigned to the local evaluation corpus")
    parser.add_argument("--version", required=True, help="explicit pinned policy document version")
    parser.add_argument("--as-of", help="date whose applicability is required (YYYY-MM-DD)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help="fixed window size in characters (default: %(default)s)")
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP,
                        help="fixed window overlap in characters (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--candidate-limit", type=int, default=20)
    parser.add_argument("--model-cache", type=Path, help="Hugging Face snapshot cache for dense modes")
    parser.add_argument("--store", choices=("local", "milvus", "pgvector"), default="local")
    parser.add_argument("--catalog", type=Path, help="path to a persistent index catalog")
    parser.add_argument("--build-index", action="store_true",
                        help="build a new database index and publish its catalog; omit query")
    args = parser.parse_args(argv)
    if args.build_index == (args.query is not None):
        parser.error("provide a query or use --build-index")
    persistent = args.store != "local"
    if persistent != (args.catalog is not None) or (args.build_index and not persistent):
        parser.error("database stores require --catalog; --build-index requires a database store")
    if persistent and not args.build_index and args.mode == "bm25":
        parser.error("database search requires --mode dense, hybrid, or rerank")
    try:
        document = load_document(args.document)
        chunks = build_chunks(
            document, strategy=args.strategy, policy_key=args.policy_key, roles=[args.role],
            chunk_size=args.chunk_size, overlap=args.overlap,
        )
        model_provenance = {}
        model_options = {}
        index = nullcontext(None)
        if args.mode != "bm25" or args.build_index:
            if args.model_cache is None:
                raise RagError("dense modes and index builds require --model-cache")
            if __package__:
                from .tau_rag_catalog import build_catalog, open_catalog
                from .tau_rag_store import load_models
            else:
                from tau_rag_catalog import build_catalog, open_catalog
                from tau_rag_store import load_models
            embed, rerank, model_provenance = load_models(args.model_cache)
            if args.build_index:
                if args.version != document["source"]["document_version"]:
                    raise RagError("index build requires the document's pinned version")
                catalog = build_catalog(args.catalog, args.store, chunks, embed, model_provenance)
                print(json.dumps(catalog, ensure_ascii=False, indent=2))
                return 0
            if persistent:
                index = open_catalog(args.catalog, args.store, chunks, model_provenance)
            else:
                vectors = {chunk["id"]: embed(chunk["text"]) for chunk in chunks}
                index = nullcontext(_local_dense_rank(vectors))
            model_options = {"encode_query": embed, "rerank": rerank}
        with index as rank:
            response = search(
                args.query, chunks, roles=[args.role], policy_keys=[args.policy_key],
                as_of=args.as_of, versions={args.policy_key: args.version},
                mode=args.mode, limit=args.limit, candidate_limit=args.candidate_limit,
                dense_rank=rank, **model_options,
            )
    except (ImportError, OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"RAG error: {exc}\n")
    response.update({
        "strategy": args.strategy,
        "mode": args.mode,
        "store": args.store if persistent else ("local_exact" if model_options else "local_lexical"),
        "indexed_chunks": len(chunks),
        "chunk_config": {"chunk_size": args.chunk_size, "overlap": args.overlap},
        "models": model_provenance,
    })
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
