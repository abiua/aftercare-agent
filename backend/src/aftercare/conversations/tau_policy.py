"""P3 reference retrieval for the isolated chat workflow."""

import logging
import re
from functools import lru_cache
from hashlib import sha256

POLICY = "tau_retail"
READER_ROLE = "policy_reader"
REVISION = "da0835f6c67baef58bdda18f2a3b037dfb3a1754"
SCOPE = "TAU_RETAIL_BENCHMARK_REFERENCE"
_CONSULTATION_TOPICS = (
    (r"退货|\breturn\b", {"POL05", "POL07"}),
    (r"退款|\brefund\b", {"POL02", "POL07", "POL09", "POL10"}),
    (r"取消|\bcancel\b", {"POL03", "POL04"}),
    (r"保修|质保|\bwarranty\b", {"POL11"}),
    (r"破损|损坏|\bdamag(?:e|ed)\b", {"POL06"}),
    (r"错发|发错|\bwrong item\b", {"POL13"}),
)
_POLICY_QUESTION = re.compile(
    r"(政策|规则|条款|条件|policy|policies|rules|eligibility)", re.IGNORECASE
)
_QUESTION_FORM = re.compile(r"[?？]|^(?:什么是|请问|如何|(?:what|how|can|when)\b)", re.IGNORECASE)
_ACTION = re.compile(
    r"(帮我|请给我|我要|我想申请|请取消|please (?:process|cancel)|I want to)", re.IGNORECASE
)
_LOG = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def _models(cache):
    from scripts.tau_rag_store import load_models

    return load_models(cache, local_files_only=True)


def search_reference(settings, query):
    """Return source-bound matches and an auditable retrieval outcome."""
    audit = {
        "scope": SCOPE,
        "policy_key": POLICY,
        "role": READER_ROLE,
        "version": REVISION,
        "date_applicability": "unknown",
        "query_sha256": sha256(query.encode()).hexdigest(),
        "store": "milvus",
        "mode": "rerank",
    }
    try:
        from scripts.tau_rag import build_chunks, load_document, search
        from scripts.tau_rag_catalog import open_catalog

        document = load_document(settings.chat_tau_policy_document)
        if document["source"]["document_version"] != REVISION:
            return {**audit, "status": "SOURCE_VERSION_MISMATCH"}, []
        chunks = build_chunks(
            document, strategy="structure", policy_key=POLICY, roles=[READER_ROLE]
        )
        embed, rerank, models = _models(settings.chat_tau_policy_model_cache)
        with open_catalog(settings.chat_tau_policy_catalog, "milvus", chunks, models) as rank:
            result = search(
                query,
                chunks,
                roles=[READER_ROLE],
                policy_keys=[POLICY],
                versions={POLICY: REVISION},
                mode="rerank",
                limit=5,
                dense_rank=rank,
                encode_query=embed,
                rerank=rerank,
            )
    except Exception as error:  # noqa: BLE001 - SDK and model failures share one public outcome.
        _LOG.warning("Tau reference retrieval failed (%s)", type(error).__name__)
        return {**audit, "status": "UNAVAILABLE"}, []
    results = result["results"]
    if not results:
        return {**audit, "status": "NO_MATCH", "timings_ms": result["timings_ms"]}, []
    return {
        **audit,
        "status": "RETRIEVED",
        "source_sha256": document["source"]["sha256"],
        "source_url": document["source"]["source_url"],
        "model_revisions": {name: models[name]["revision"] for name in ("embedding", "reranker")},
        "timings_ms": result["timings_ms"],
        "hits": [
            {
                "chunk_id": hit["id"],
                "rank": hit["rank"],
                "location": hit["citations"][0]["location"],
            }
            for hit in results
        ],
    }, results


def consultation_pending(settings, query, *, available):
    if not (
        getattr(settings, "chat_tau_policy_enabled", False)
        and _consultation_topics(query)
        and _POLICY_QUESTION.search(query)
        and _QUESTION_FORM.search(query.strip())
        and not _ACTION.search(query)
    ):
        return None
    if not available or settings.chat_tau_authorized_policy_catalog is None:
        return "政策查询暂不可用。你可以请客服核对适用规则。", [
            {"type": "status", "data": {"code": "POLICY_CONSULTATION_UNAVAILABLE"}}
        ]
    return "正在查找适用的售后条款。", [
        {"type": "status", "data": {"code": "POLICY_CONSULTATION_PENDING"}}
    ]


def _consultation_topics(query):
    return {
        policy_id
        for pattern, policy_ids in _CONSULTATION_TOPICS
        if re.search(pattern, query, re.IGNORECASE)
        for policy_id in policy_ids
    }


def consultation_reply(settings, query, documents, decision_at):
    from aftercare.intelligence.policies import document_key
    from aftercare.intelligence.policy_versions import resolve_policies

    resolution = resolve_policies(
        documents,
        intent=None,
        products=[],
        timestamps={"decision_at": decision_at},
    )
    topic = _consultation_topics(query)
    eligible = {
        document_key(row): row
        for row in resolution.documents
        if row["id"] in topic
        and not row.get("product_ids")
        and not row.get("categories")
        and row.get("time_basis", "decision_at") == "decision_at"
    }
    ranking, audit = search_authorized_policy(settings, query, documents, selected_keys=eligible)
    hit_ids = {hit["policy_ref"]: hit["chunk_id"] for hit in audit.get("hits", [])}
    excerpts = []
    for key in ranking[:2]:
        row = eligible[key]
        ref = f"{row['id']}:{row['version']}"
        excerpts.append(
            {
                "text": row["text"],
                "title": row.get("title") or row["id"],
                "policy_id": row["id"],
                "version": row["version"],
                "effective_at": row["effective_at"],
                "chunk_id": hit_ids[ref],
            }
        )
    message = (
        "".join(row["text"] for row in sorted(excerpts, key=lambda row: row["policy_id"]))
        + "需要核对具体商品时，请先确认订单。"
        if excerpts
        else "本次无法确认适用的售后条款。请确认订单和商品，或请客服核对。"
    )
    return message, [
        {
            "type": "policy_consultation",
            "data": {"retrieval": audit, "excerpts": excerpts},
        }
    ]


def _authorized_chunks(documents):
    from aftercare.business.rules import digest
    from aftercare.intelligence.policies import document_key
    from aftercare.investigation.catalogue import valid_documents

    if not valid_documents(documents):
        raise ValueError("INVALID_FROZEN_POLICY_CATALOGUE")
    corpus_sha256 = digest(documents)
    chunks = []
    for document in documents:
        key = document_key(document)
        policy_ref = f"{document['id']}:{document['version']}"
        text = document["text"]
        for start in range(0, len(text), 80):
            end = min(start + 100, len(text))
            chunk = {
                "document_id": key,
                "document_version": document["version"],
                "policy_key": key,
                "roles": ["investigation"],
                "effective_from": None,
                "effective_until": None,
                "text": text[start:end],
                "source": {"corpus_sha256": corpus_sha256, "policy_ref": policy_ref},
                "citations": [{"char_start": start, "char_end_exclusive": end}],
            }
            chunks.append({"id": digest(chunk), **chunk})
            if end == len(text):
                break
    return chunks


def build_authorized_catalog(settings, documents):
    from scripts.tau_rag_catalog import build_catalog

    if settings.chat_tau_authorized_policy_catalog is None:
        raise ValueError("Configure a new isolated authorized catalog path.")
    chunks = _authorized_chunks(documents)
    embed, _, models = _models(settings.chat_tau_policy_model_cache)
    return build_catalog(
        settings.chat_tau_authorized_policy_catalog, "milvus", chunks, embed, models
    )


def search_authorized_policy(settings, query, documents, *, selected_keys=None):
    """Rank exact frozen business policy versions within the caller’s validated scope."""
    from aftercare.business.rules import digest

    audit = {
        "mode": "rerank",
        "store": "milvus",
        "candidate_scope": "FROZEN_RAW_CATALOGUE"
        if selected_keys is None
        else "CURRENT_STORE_WIDE",
        "applicability": "NOT_ASSESSED" if selected_keys is None else "STORE_WIDE_POLICY",
        "query_sha256": sha256(query.encode()).hexdigest(),
        "corpus_sha256": digest(documents),
    }
    if selected_keys is not None and not selected_keys:
        return [], {**audit, "status": "NO_MATCH"}

    try:
        from scripts.tau_rag import search
        from scripts.tau_rag_catalog import open_catalog

        chunks = _authorized_chunks(documents)
        versions = {
            chunk["policy_key"]: chunk["document_version"]
            for chunk in chunks
            if selected_keys is None or chunk["document_id"] in selected_keys
        }
        embed, rerank, models = _models(settings.chat_tau_policy_model_cache)
        with open_catalog(
            settings.chat_tau_authorized_policy_catalog, "milvus", chunks, models
        ) as rank:
            result = search(
                query,
                chunks,
                roles=["investigation"],
                policy_keys=versions,
                versions=versions,
                mode="rerank",
                limit=20,
                dense_rank=rank,
                encode_query=embed,
                rerank=rerank,
            )
    except Exception as error:  # noqa: BLE001 - SDK and model failures share one public outcome.
        _LOG.warning("Authorized policy retrieval failed (%s)", type(error).__name__)
        return [], {**audit, "status": "UNAVAILABLE"}

    ranking = []
    hits = []
    for hit in result["results"]:
        if hit["document_id"] in ranking:
            continue
        ranking.append(hit["document_id"])
        hits.append(
            {
                "policy_ref": hit["source"]["policy_ref"],
                "chunk_id": hit["id"],
                "rank": hit["rank"],
                **hit["citations"][0],
            }
        )
        if len(ranking) == 5:
            break
    return ranking, {
        **audit,
        "status": "RETRIEVED" if ranking else "NO_MATCH",
        "model_revisions": {name: models[name]["revision"] for name in ("embedding", "reranker")},
        "timings_ms": result["timings_ms"],
        "hits": hits,
    }
