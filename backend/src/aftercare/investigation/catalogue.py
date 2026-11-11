"""Private frozen raw policy material for independent investigation."""

import json
from datetime import datetime

from aftercare.business.rules import digest

CATALOGUE_VERSION = "ac-investigation-policy-catalogue-v1"
CATALOGUE_REF = "investigation-policy-catalogue"
MAX_POLICY_DOCUMENTS = 64
MAX_POLICY_BYTES = 96_000


def valid_documents(documents):
    if (
        not isinstance(documents, list)
        or not 0 < len(documents) <= MAX_POLICY_DOCUMENTS
        or len(json.dumps(documents, ensure_ascii=False).encode()) > MAX_POLICY_BYTES
        or any(
            not isinstance(row, dict)
            or not all(
                isinstance(row.get(key), str) and row[key] for key in ("id", "version", "text")
            )
            for row in documents
        )
    ):
        return False
    keys = [(row["id"], row["version"]) for row in documents]
    return keys == sorted(set(keys))


def catalogue_capsule(documents, decision_at, products):
    if not valid_documents(documents):
        raise ValueError("INVESTIGATION_POLICY_CATALOGUE_LIMIT")
    return {
        "version": CATALOGUE_VERSION,
        "documents": documents,
        "documents_sha256": digest(documents),
        "decision_at": decision_at.isoformat(),
        "products": products,
    }


def valid_capsule(value):
    if not isinstance(value, dict) or value.get("version") != CATALOGUE_VERSION:
        return False
    documents, products = value.get("documents"), value.get("products")
    try:
        date = datetime.fromisoformat(value["decision_at"])
    except (KeyError, ValueError, TypeError):
        return False
    return (
        date.utcoffset() is not None
        and valid_documents(documents)
        and value.get("documents_sha256") == digest(documents)
        and isinstance(products, list)
        and all(
            isinstance(row, dict)
            and set(row) == {"id", "category"}
            and isinstance(row["id"], str)
            and bool(row["id"])
            and (row["category"] is None or isinstance(row["category"], str))
            for row in products
        )
        and len(products) == len({row["id"] for row in products})
    )


def exact_policy_refs_match(frozen, option):
    """Check only publication authority. Raw investigation tools never receive this map."""
    catalogue = frozen.get("policy_catalogue")
    if not catalogue:
        return True
    known = {f"{doc['id']}:{doc['version']}": doc["id"] for doc in catalogue["documents"]}
    cited = set(option.evidence_ids).intersection(known)
    if not cited:
        return False
    targets = {line.line_id for line in option.lines}
    products = {
        item["product_id"] for item in frozen["order"]["items"] if item["line_id"] in targets
    }
    by_product = frozen["policies"]["by_product"]
    selected = {
        product: {f"{row['id']}:{row['version']}" for row in by_product.get(product, [])}
        for product in products
    }
    if not cited <= set().union(*selected.values()):
        return False
    for policy in option.policy_ids:
        relevant = [{key for key in keys if known.get(key) == policy} for keys in selected.values()]
        applicable = [keys for keys in relevant if keys]
        if not applicable or any(not keys.intersection(cited) for keys in applicable):
            return False
    return True
