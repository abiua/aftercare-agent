"""Build and open persistent P3 snapshots for the offline retrieval CLI."""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

if __package__:
    from .tau_rag_store import MILVUS_URI, PG_DSN, MilvusStore, PgVectorStore
else:
    from tau_rag_store import MILVUS_URI, PG_DSN, MilvusStore, PgVectorStore


BACKENDS = ("milvus", "pgvector")
INDEX_NAME = re.compile(r"tau_p3_[0-9a-f]{32}")


class CatalogError(ValueError):
    pass


def _store(backend, name, dimension):
    if backend == "milvus":
        return MilvusStore(os.environ.get("TAU_RAG_MILVUS_URI", MILVUS_URI), name, dimension)
    if backend == "pgvector":
        return PgVectorStore(os.environ.get("TAU_RAG_PG_DSN", PG_DSN), name, dimension)
    raise CatalogError("unsupported catalog backend")


def _binding(chunks, models):
    payload = json.dumps(chunks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "chunks_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "count": len(chunks),
        "embedding": models["embedding"],
    }


def _verify_payloads(store, chunks):
    for chunk in chunks:
        if store.get(chunk["id"]) != chunk:
            raise CatalogError("stored snapshot differs from the verified P2 chunks")


def build_catalog(path, backend, chunks, embed, models):
    """Publish a new immutable snapshot after verifying stored payloads."""
    path = Path(path)
    if path.exists():
        raise CatalogError("catalog already exists; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    name = "tau_p3_" + uuid4().hex
    binding = _binding(chunks, models)
    vectors = {chunk["id"]: embed(chunk["text"]) for chunk in chunks}
    dimension = len(next(iter(vectors.values())))
    catalog = {"schema_version": 1, "backend": backend, "name": name,
               "dimension": dimension, **binding}
    store = _store(backend, name, dimension)
    created = False
    try:
        store.create()
        created = True
        store.upsert(chunks, vectors)
        _verify_payloads(store, chunks)
        with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(catalog, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
                os.link(temporary, path)
            finally:
                temporary.unlink()
    except Exception:
        if created:
            store.drop()
        raise
    finally:
        store.close()
    return catalog


@contextmanager
def open_catalog(path, backend, chunks, models):
    """Open a prebuilt snapshot after comparing catalog, model, and source payloads."""
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(catalog, dict) or catalog.get("schema_version") != 1
            or catalog.get("backend") != backend
            or not INDEX_NAME.fullmatch(str(catalog.get("name", "")))
            or type(catalog.get("dimension")) is not int or catalog["dimension"] <= 0
            or any(catalog.get(key) != value for key, value in _binding(chunks, models).items())):
        raise CatalogError("catalog does not match the verified source, configuration, or model")
    store = _store(backend, catalog["name"], catalog["dimension"])
    try:
        _verify_payloads(store, chunks)
        yield store.rank
    finally:
        store.close()
