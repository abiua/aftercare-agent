"""Exercise the P3 adapters against isolated databases, including a service restart."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from tau_rag_store import MILVUS_URI, PG_DSN, MilvusStore, PgVectorStore, load_models

ROOT = Path(__file__).resolve().parents[1]


def restart_databases():
    environment = dict(os.environ, DOCKER_CONTEXT="colima-tau-rag")
    environment.pop("DOCKER_HOST", None)
    environment["TOKENIZERS_PARALLELISM"] = "false"
    compose = ["docker-compose", "-f", str(ROOT / "infra/compose.tau-rag.yml")]

    def inspect_containers():
        ids = subprocess.check_output([*compose, "ps", "-q"], env=environment, text=True).split()
        details = json.loads(subprocess.check_output(
            ["docker", "inspect", *ids], env=environment, text=True,
        ))
        return {
            row["Config"]["Labels"]["com.docker.compose.service"]: {
                "id": row["Id"], "started_at": row["State"]["StartedAt"],
                "health": row["State"]["Health"]["Status"],
                "image": row["Config"]["Image"], "image_id": row["Image"],
            }
            for row in details
        }

    before = inspect_containers()
    subprocess.run([*compose, "stop"], check=True, env=environment)
    subprocess.run([*compose, "up", "-d", "--wait"], check=True, env=environment)
    after = inspect_containers()
    assert before.keys() == after.keys() == {"postgres", "milvus"}
    for name, old in before.items():
        current = after[name]
        assert old["id"] == current["id"] and old["started_at"] != current["started_at"]
        assert current["health"] == "healthy"
    return {"before": before, "after": after}


def verify_lifecycle(embed, restart=restart_databases):
    prefix = "tau_p3_" + uuid4().hex
    chunks = [
        {"id": "returns", "text": "退货政策允许在收货后的十四日内申请。", "version": "v1"},
        {"id": "shipping", "text": "标准快递在发货后五天内送达。", "version": "v1"},
        {"id": "refund", "text": "Refunds can be issued to the original payment method.", "version": "v1"},
    ]
    vectors = {chunk["id"]: embed(chunk["text"]) for chunk in chunks}
    query = embed("如何申请退货？")
    ids = set(vectors)
    updated = dict(chunks[0], text="退货期限已修改为收货后七日。", version="v2")
    update_vector = embed(updated["text"])
    config = {
        "pgvector": (PgVectorStore, PG_DSN),
        "milvus": (MilvusStore, MILVUS_URI),
    }
    report = {"dimension": len(query), "backends": {}}
    active = {}

    def open_store(backend):
        store_class, address = config[backend]
        return store_class(address, prefix, len(query))

    try:
        for backend in config:
            store = active[backend] = open_store(backend)
            store.create()
            store.upsert(chunks, vectors)
            before = store.rank(query, ids, 3)
            assert len(before) == 3 and {item[0] for item in before} == ids, before
            assert {item[0] for item in store.rank(query, {"shipping"}, 3)} == {"shipping"}
            assert not store.rank(query, set(), 3)

            store.upsert([updated], {"returns": update_vector})
            updated_payload = store.get("returns")
            updated_cosine = dict(store.rank(update_vector, {"returns"}, 1))["returns"]
            assert updated_payload == updated and abs(updated_cosine - 1) < 1e-6
            store.delete(["shipping"])
            deleted_payload = store.get("shipping")
            assert deleted_payload is None
            expected = store.rank(query, ids, 3)
            assert len(expected) == 2 and "shipping" not in dict(expected)
            report["backends"][backend] = {
                "version": store.version(), "before": before, "after_update_delete": expected,
                "updated_payload": updated_payload, "updated_vector_self_cosine": updated_cosine,
                "deleted_payload": deleted_payload,
            }

        started = time.perf_counter()
        for store in active.values():
            store.close()
        active.clear()
        report["containers"] = restart()
        report["restart_seconds"] = round(time.perf_counter() - started, 3)

        for backend in config:
            store = active[backend] = open_store(backend)
            evidence = report["backends"][backend]
            read_started = time.perf_counter()
            restored_payload = store.get("returns")
            restored_deleted_payload = store.get("shipping")
            assert restored_payload == updated and restored_deleted_payload is None
            restored = store.rank(query, ids, 3)
            evidence.update({
                "post_restart_read_seconds": round(time.perf_counter() - read_started, 3),
                "post_restart_readiness_retries": getattr(store, "readiness_retries", 0),
                "post_restart_backoff_seconds": round(
                    getattr(store, "readiness_backoff_seconds", 0), 3,
                ),
            })
            assert restored == evidence["after_update_delete"], (backend, restored)
            store.drop()
            store.create()
            store.upsert(chunks, vectors)
            rebuilt = store.rank(query, ids, 3)
            assert rebuilt == evidence["before"], (backend, rebuilt)
            rebuilt_payloads = {chunk["id"]: store.get(chunk["id"]) for chunk in chunks}
            assert rebuilt_payloads == {chunk["id"]: chunk for chunk in chunks}
            evidence.update({
                "after_restart": restored, "after_rebuild": rebuilt,
                "restored_updated_payload": restored_payload,
                "restored_deleted_payload": restored_deleted_payload,
                "rebuilt_payloads": rebuilt_payloads,
            })

        comparisons = {}
        for phase in ("before", "after_update_delete", "after_restart", "after_rebuild"):
            pg = report["backends"]["pgvector"][phase]
            milvus = report["backends"]["milvus"][phase]
            comparisons[phase] = {
                "same_ranking": [key for key, _ in pg] == [key for key, _ in milvus],
                "max_score_delta": max(abs(score - dict(milvus)[key]) for key, score in pg),
            }
            assert comparisons[phase]["same_ranking"]
            assert comparisons[phase]["max_score_delta"] < 1e-6
        report["comparison"] = comparisons
        return report
    finally:
        for store in active.values():
            try:
                store.drop()
            finally:
                store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-cache", type=Path, default=ROOT / ".runtime/tau-rag-p3/models")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    embed, rerank, provenance = load_models(args.model_cache)
    report = verify_lifecycle(embed)
    report["models"] = provenance
    report["reranker_probe_scores"] = rerank(
        "如何申请退货？", ["退货政策允许在十四日内申请。", "快递在五天内送达。"],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
