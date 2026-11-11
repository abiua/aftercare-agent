"""Versioned policy retrieval and applicability checks."""

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

import jieba
from pgvector.sqlalchemy import Vector
from sqlalchemy import String, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from aftercare.db import Base
from aftercare.intelligence.retrieval_evidence import (
    DIMENSIONS,
    MODEL_ID,
    QUERY_PREFIX,
    vector_digest,
    vector_index_evidence,
)

BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
RETRIEVAL_LIMIT = 5
QUERY_EVIDENCE_VERSION = "ac-retrieval-query-v2"


class PolicyVector(Base):
    __tablename__ = "policy_vectors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    encoder: Mapped[str] = mapped_column(String(256), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(DIMENSIONS))


@dataclass(frozen=True)
class SearchResult:
    documents: list[dict]
    lexical: list[str]
    semantic: list[str]
    mode: str
    blockers: list[str]
    lexical_versions: list[dict] = field(default_factory=list)
    semantic_versions: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def document_key(policy: dict) -> str:
    payload = json.dumps(policy, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _timestamp(value) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Policy timestamp must include an offset")
    return stamp


def applicable(policies: list[dict], *, intent: str, as_of) -> tuple[list[dict], list[str]]:
    try:
        effective_time = _timestamp(as_of)
    except (ValueError, TypeError):
        return [], ["UNKNOWN_CASE_TIME"]

    active = defaultdict(list)
    blockers = []
    for policy in policies:
        intents = policy.get("intents")
        if intents and intent not in intents:
            continue
        try:
            begins = _timestamp(policy["effective_at"])
            ends = _timestamp(policy["expires_at"]) if policy.get("expires_at") else None
            if ends is not None and ends <= begins:
                raise ValueError("Invalid policy interval")
        except (KeyError, ValueError, TypeError):
            blockers.append(f"UNKNOWN_POLICY_TIME:{policy.get('id', '?')}")
            continue
        if begins <= effective_time and (ends is None or effective_time < ends):
            active[policy["id"]].append(policy)

    selected = []
    for policy_id, candidates in sorted(active.items()):
        latest = max(_timestamp(policy["effective_at"]) for policy in candidates)
        unique = {document_key(p): p for p in candidates if _timestamp(p["effective_at"]) == latest}
        if len(unique) != 1:
            blockers.append(f"CONFLICTING_POLICY:{policy_id}")
            continue
        selected.append(next(iter(unique.values())))
    return selected, blockers


@lru_cache(maxsize=4096)
def _tokens(text: str) -> tuple[str, ...]:
    return tuple(word for word in jieba.cut_for_search(text.lower()) if re.search(r"\w", word))


def _body(policy: dict) -> str:
    return f"{policy.get('title', '')} {policy['text']}"


def bm25(query: str, documents: dict[str, str]) -> list[str]:
    if not documents:
        return []
    terms = sorted(set(_tokens(query)))
    frequencies = {key: Counter(_tokens(value)) for key, value in documents.items()}
    lengths = {key: sum(words.values()) for key, words in frequencies.items()}
    average = sum(lengths.values()) / len(lengths) or 1
    scores = defaultdict(float)
    for term in terms:
        document_frequency = sum(term in words for words in frequencies.values())
        inverse_frequency = math.log(
            1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        for key, words in frequencies.items():
            occurrences = words[term]
            if occurrences:
                normalizer = BM25_K1 * (1 - BM25_B + BM25_B * lengths[key] / average)
                scores[key] += (
                    inverse_frequency * occurrences * (BM25_K1 + 1) / (occurrences + normalizer)
                )
    return sorted(scores, key=lambda key: (-scores[key], key))


def rrf(*rankings: list[str], limit: int = RETRIEVAL_LIMIT) -> list[str]:
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), start=1):
            scores[key] += 1 / (RRF_K + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]


@lru_cache(maxsize=2)
def _load_bge(model_path: str, revision: str | None):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_path, revision=revision, local_files_only=True)


def bge_encoder(model_path: str, revision: str | None) -> Callable:
    def encode(texts: list[str]) -> list[list[float]]:
        vectors = (
            _load_bge(model_path, revision)
            .encode(texts, normalize_embeddings=True, show_progress_bar=False)
            .tolist()
        )
        if any(
            len(v) != DIMENSIONS or not all(math.isfinite(x) for x in v) or not any(v)
            for v in vectors
        ):
            raise ValueError("Invalid BGE embedding")
        return vectors

    return encode


class PolicySearch:
    def __init__(
        self,
        engine,
        *,
        encode: Callable | None = None,
        encoder: str = MODEL_ID,
        evaluation_catalogue: Callable | None = None,
    ):
        self.engine = engine
        self.encode = encode
        self.encoder = encoder
        self.evaluation_catalogue = evaluation_catalogue
        self.index_state = "NOT_APPLICABLE" if encode is None else "NOT_USED"

    def _prepare_evaluation_index(self):
        if self.evaluation_catalogue is not None and self.index_state == "NOT_USED":
            try:
                self.index(self.evaluation_catalogue())
            except (ValueError, OSError, RuntimeError, SQLAlchemyError):
                self.index_state = "FAILED"
            else:
                self.index_state = "BUILT"

    def index(self, policies: list[dict]) -> int:
        if self.encode is None:
            raise ValueError("BGE encoder is not configured")

        from sqlalchemy.dialects.postgresql import insert

        documents = {document_key(policy): _body(policy) for policy in policies}
        with Session(self.engine) as session:
            present = set(
                session.scalars(
                    select(PolicyVector.key).where(
                        PolicyVector.key.in_(documents),
                        PolicyVector.encoder == self.encoder,
                    )
                )
            )
        missing = sorted(documents.keys() - present)
        if not missing:
            return 0
        vectors = self.encode([documents[key] for key in missing])
        rows = [
            {"key": key, "encoder": self.encoder, "embedding": vector}
            for key, vector in zip(missing, vectors, strict=True)
        ]
        with Session(self.engine) as session, session.begin():
            result = session.scalars(
                insert(PolicyVector)
                .values(rows)
                .on_conflict_do_nothing()
                .returning(PolicyVector.key)
            )
            return len(result.all())

    def search(
        self,
        query: str,
        policies: list[dict],
        *,
        intent: str,
        as_of,
        limit: int = RETRIEVAL_LIMIT,
    ) -> SearchResult:
        valid, blockers = applicable(policies, intent=intent, as_of=as_of)
        return self.search_selected(query, valid, blockers=blockers, limit=limit)

    def search_selected(
        self, query: str, valid: list[dict], *, blockers=(), limit=RETRIEVAL_LIMIT
    ) -> SearchResult:
        blockers = list(blockers)
        documents = {document_key(policy): policy for policy in valid}
        lexical = bm25(query, {key: _body(p) for key, p in documents.items()})
        semantic = []
        mode = "BM25"
        vector_evidence = None
        query_vector_sha256 = None
        if documents and self.encode is not None:
            self._prepare_evaluation_index()
            try:
                if self.index_state != "FAILED":
                    with Session(self.engine) as session:
                        present = set(
                            session.scalars(
                                select(PolicyVector.key).where(
                                    PolicyVector.key.in_(documents),
                                    PolicyVector.encoder == self.encoder,
                                )
                            )
                        )
                    rows = []
                    if present == documents.keys():
                        (embedding,) = self.encode([QUERY_PREFIX + query])
                        if self.evaluation_catalogue is not None:
                            query_vector_sha256 = vector_digest(embedding)
                        with Session(self.engine) as session:
                            rows = session.execute(
                                select(
                                    PolicyVector.key, PolicyVector.encoder, PolicyVector.embedding
                                )
                                .where(
                                    PolicyVector.key.in_(documents),
                                    PolicyVector.encoder == self.encoder,
                                )
                                .order_by(
                                    PolicyVector.embedding.cosine_distance(embedding),
                                    PolicyVector.key,
                                )
                            ).all()
                    if {row.key for row in rows} == documents.keys():
                        semantic = [row.key for row in rows]
                        if self.evaluation_catalogue is not None:
                            vector_evidence = vector_index_evidence(rows)
                        mode = "HYBRID"
            except (ValueError, OSError, RuntimeError, SQLAlchemyError):
                if self.evaluation_catalogue is None:
                    raise
                semantic = []
            if mode != "HYBRID":
                blockers.append("INCOMPLETE_VECTOR_INDEX")
        fused = rrf(lexical, semantic, limit=limit)
        return SearchResult(
            [documents[key] for key in fused],
            [documents[key]["id"] for key in lexical],
            [documents[key]["id"] for key in semantic],
            mode,
            blockers,
            [{k: documents[key][k] for k in ("id", "version")} for key in lexical],
            [{k: documents[key][k] for k in ("id", "version")} for key in semantic],
            {
                "schema_version": QUERY_EVIDENCE_VERSION,
                "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                "candidate_keys": sorted(documents),
                "query_vector_sha256": query_vector_sha256,
                "vector_index": vector_evidence,
            }
            if self.evaluation_catalogue is not None
            else {},
        )
