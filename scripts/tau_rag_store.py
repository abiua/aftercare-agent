"""Isolated P3 model inference and exact vector search adapters."""

from __future__ import annotations

import hashlib
import json
import platform
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

PG_DSN = "postgresql://tau_rag:tau_rag_local@127.0.0.1:15438/tau_rag"
MILVUS_URI = "http://127.0.0.1:19538"
EMBEDDING = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
)
RERANKER = (
    "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    "1427fd652930e4ba29e8149678df786c240d8825",
)


def load_models(cache_dir: Path, *, local_files_only=False):
    """Load pinned model weights locally using ONNX Runtime's CPU provider."""
    import numpy as np
    import onnxruntime as ort
    from huggingface_hub import snapshot_download
    from tokenizers import Tokenizer

    graph = {
        "arm64": "onnx/model_qint8_arm64.onnx",
        "aarch64": "onnx/model_qint8_arm64.onnx",
        "x86_64": "onnx/model_quint8_avx2.onnx",
        "AMD64": "onnx/model_quint8_avx2.onnx",
    }[platform.machine()]
    provenance = {"runtime": ort.__version__, "provider": "CPUExecutionProvider"}

    def load(role, model, max_length):
        repo, revision = model
        path = Path(snapshot_download(
            repo, revision=revision, cache_dir=cache_dir,
            allow_patterns=[graph, "tokenizer.json"], local_files_only=local_files_only,
        ))
        tokenizer_path = path / "tokenizer.json"
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
        tokenizer.no_truncation()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        model_path = path / graph
        session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"],
        )
        with model_path.open("rb") as handle:
            sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        with tokenizer_path.open("rb") as handle:
            tokenizer_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
        provenance[role] = {
            "repo": repo, "revision": revision, "artifact": graph,
            "artifact_sha256": sha256, "tokenizer_sha256": tokenizer_sha256,
            "max_length": max_length, "overflow": "reject",
        }

        def infer(text, pair=None):
            encoded = tokenizer.encode(text, pair)
            token_count = len(encoded.ids)
            if token_count > max_length:
                raise ValueError(
                    f"{role} input has {token_count} tokens, exceeding {max_length}"
                )
            inputs = {
                "input_ids": encoded.ids,
                "attention_mask": encoded.attention_mask,
                "token_type_ids": encoded.type_ids,
            }
            outputs = session.run(None, {
                node.name: np.array([inputs[node.name]], dtype=np.int64)
                for node in session.get_inputs()
            })
            return outputs[0], np.array(encoded.attention_mask, dtype=np.float32)

        return infer

    embed_model = load("embedding", EMBEDDING, 128)
    rerank_model = load("reranker", RERANKER, 512)
    provenance["embedding"]["pooling"] = "attention-mask mean, L2-normalized"
    provenance["reranker"]["score"] = "raw single-logit relevance"

    def embed(text):
        tokens, mask = embed_model(text)
        pooled = (tokens[0] * mask[:, None]).sum(axis=0) / mask.sum()
        return (pooled / np.linalg.norm(pooled)).tolist()

    def rerank(query, passages):
        return [float(rerank_model(query, passage)[0].item()) for passage in passages]

    return embed, rerank, provenance


class PgVectorStore:
    def __init__(self, dsn, name, dimension):
        import psycopg
        from psycopg import sql

        self.conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        self.sql = sql
        self.table = sql.Identifier(name)
        self.dimension = dimension

    def create(self):
        sql = self.sql
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.conn.execute(sql.SQL(
            "CREATE TABLE {} (id text PRIMARY KEY, embedding vector({}), payload jsonb NOT NULL)"
        ).format(self.table, sql.Literal(self.dimension)))

    def version(self):
        postgres = self.conn.execute("SHOW server_version").fetchone()[0]
        vector = self.conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        return {"postgres": postgres, "pgvector": vector.fetchone()[0]}

    def upsert(self, chunks, vectors):
        sql = self.sql
        statement = sql.SQL(
            "INSERT INTO {} (id, embedding, payload) VALUES (%s, %s::vector, %s::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET embedding=excluded.embedding, payload=excluded.payload"
        ).format(self.table)
        with self.conn.cursor() as cursor:
            cursor.executemany(statement, [
                (row["id"], json.dumps(vectors[row["id"]]), json.dumps(row)) for row in chunks
            ])

    def delete(self, ids):
        self.conn.execute(
            self.sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(self.table), (list(ids),),
        )

    def get(self, item_id):
        record = self.conn.execute(
            self.sql.SQL("SELECT payload FROM {} WHERE id = %s").format(self.table), (item_id,),
        ).fetchone()
        return record[0] if record else None

    def rank(self, vector, allowed_ids, limit):
        if not allowed_ids or limit < 1:
            return []
        return self.conn.execute(self.sql.SQL(
            "SELECT id, 1 - (embedding <=> %s::vector) AS score FROM {}"
            " WHERE id = ANY(%s) ORDER BY score DESC, id LIMIT %s"
        ).format(self.table), (json.dumps(vector), sorted(allowed_ids), limit)).fetchall()

    def drop(self):
        self.conn.execute(self.sql.SQL("DROP TABLE IF EXISTS {}").format(self.table))

    def close(self):
        self.conn.close()


class MilvusStore:
    def __init__(self, uri, name, dimension):
        from pymilvus import MilvusClient

        # Closing this adapter must not disconnect another collection on the same server.
        self.client = MilvusClient(uri=uri, timeout=20, alias="tau_p3_" + uuid4().hex)
        self.name = name
        self.dimension = dimension
        self.readiness_retries = 0
        self.readiness_backoff_seconds = 0.0

    def _read(self, operation, *args, **kwargs):
        from pymilvus.exceptions import MilvusException

        deadline = monotonic() + 30
        while True:
            try:
                timeout = min(10, deadline - monotonic())
                return operation(*args, timeout=timeout, **kwargs)
            except MilvusException as error:
                remaining = deadline - monotonic()
                if error.code != 503 or remaining <= 0.5:
                    raise
                # The standalone health endpoint can recover before persisted channels load.
                self.readiness_retries += 1
                started = monotonic()
                sleep(0.5)
                self.readiness_backoff_seconds += monotonic() - started

    def create(self):
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
        schema.add_field("payload", DataType.JSON)
        indexes = self.client.prepare_index_params()
        indexes.add_index("vector", index_type="FLAT", metric_type="COSINE")
        self.client.create_collection(
            self.name, schema=schema, index_params=indexes, consistency_level="Strong",
        )

    def version(self):
        return {"milvus": self.client.get_server_version()}

    def upsert(self, chunks, vectors):
        self.client.upsert(self.name, [
            {"id": row["id"], "vector": vectors[row["id"]], "payload": row} for row in chunks
        ])
        self.client.flush(self.name)

    def delete(self, ids):
        self.client.delete(self.name, ids=list(ids))
        self.client.flush(self.name)

    def get(self, item_id):
        records = self._read(
            self.client.get, self.name, ids=[item_id], output_fields=["payload"],
        )
        return records[0]["payload"] if records else None

    def rank(self, vector, allowed_ids, limit):
        if not allowed_ids or limit < 1:
            return []
        hits = self._read(
            self.client.search, self.name, data=[vector], limit=limit, filter="id in {allowed_ids}",
            filter_params={"allowed_ids": sorted(allowed_ids)},
            search_params={"metric_type": "COSINE"}, consistency_level="Strong",
        )[0]
        return sorted(
            [(hit["id"], hit["distance"]) for hit in hits], key=lambda hit: (-hit[1], hit[0]),
        )

    def drop(self):
        self.client.drop_collection(self.name)

    def close(self):
        self.client.close()


def index_factories(pg_dsn=PG_DSN, milvus_uri=MILVUS_URI):
    """Create independent temporary stores from the evaluator's shared vector map."""
    def factory(store_class, address):
        @contextmanager
        def index(chunks, vectors):
            dimension = len(next(iter(vectors.values())))
            store = store_class(address, "tau_p3_" + uuid4().hex, dimension)
            try:
                store.create()
                store.upsert(chunks, vectors)
                yield store.rank
            finally:
                try:
                    store.drop()
                finally:
                    store.close()

        return index

    return {
        "pgvector": factory(PgVectorStore, pg_dsn),
        "milvus": factory(MilvusStore, milvus_uri),
    }
