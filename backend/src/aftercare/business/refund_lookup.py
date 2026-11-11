"""Read-only lookup of one refund execution in an already authorized order ledger."""

import re

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_EXECUTION_ID = re.compile(_UUID, re.IGNORECASE | re.ASCII)
_REFERENCE = re.compile(r"退款执行(?:记录)?编号\s*[:：]?\s*")
_LABELLED_ID = re.compile(rf"{_UUID}(?![a-z0-9_-])", re.IGNORECASE | re.ASCII)
_SEPARATOR = re.compile(r"\s*(?:[,，、/;；]|以及|或|和)\s*|\s+")


def canonical_execution_id(value: str) -> str | None:
    value = value.strip()
    return value.lower() if _EXECUTION_ID.fullmatch(value) else None


def _resolution(status: str, execution: dict | None = None) -> dict:
    return {"status": status, "execution": execution}


def locate_refund(ledger: list[dict], execution_id: str) -> dict:
    matched = [
        row for row in ledger if row.get("action") == "REFUND" and row.get("id") == execution_id
    ]
    if len(matched) > 1:
        return _resolution("AMBIGUOUS_REFERENCE")
    if not matched:
        return _resolution("NOT_FOUND")
    row = matched[0]
    receipt = row.get("receipt")
    receipt_id = receipt.get("id") if isinstance(receipt, dict) else None
    return _resolution(
        "FOUND",
        {
            "id": row["id"],
            "status": row["status"],
            "amount_cents": row["amount_cents"],
            "created_at": row["created_at"],
            "receipt_confirmed": (
                row["status"] in {"SUCCEEDED", "FAILED"}
                and isinstance(receipt_id, str)
                and bool(receipt_id.strip())
            ),
        },
    )


def resolve_refund_text(ledger: list[dict], *texts: str) -> dict:
    identifiers = set()
    invalid = False
    for text in texts:
        for label in _REFERENCE.finditer(text):
            match = _LABELLED_ID.match(text, label.end())
            if match is None:
                invalid = True
            while match is not None:
                identifiers.add(match[0].lower())
                separator = _SEPARATOR.match(text, match.end())
                match = _LABELLED_ID.match(text, separator.end()) if separator else None
    if len(identifiers) > 1:
        return _resolution("AMBIGUOUS_REFERENCE")
    if invalid:
        return _resolution("INVALID_REFERENCE")
    if not identifiers:
        return _resolution("REFERENCE_REQUIRED")
    return locate_refund(ledger, identifiers.pop())


def targeted_refund(lookup: dict | None) -> bool:
    return lookup is not None and lookup["status"] != "REFERENCE_REQUIRED"
