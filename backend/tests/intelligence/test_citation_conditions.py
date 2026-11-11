import json

import pytest

from aftercare.config import ROOT
from aftercare.intelligence.citation_conditions import (
    CITATION_REGISTRY_VERSION,
    CONDITIONS,
    CitationCondition,
    assess_citations,
)
from aftercare.intelligence.policies import document_key


@pytest.fixture
def documents():
    data = json.loads((ROOT / "data/v0.1/agent/business.json").read_text())
    return {
        (row["id"], row["version"]): row
        for row in data["policies"]
        if row["id"] in {"POL08", "POL12"}
    }


def cite(document):
    return {"id": document["id"], "version": document["version"], "quote": document["text"]}


@pytest.mark.parametrize(
    ("ledger", "status", "reason", "ids"),
    [
        ([], "UNMET", "NO_REFUND_EXECUTION_RECORD", []),
        (None, "UNKNOWN", "REFUND_LEDGER_EVIDENCE_UNAVAILABLE", []),
        (["malformed"], "UNKNOWN", "REFUND_LEDGER_EVIDENCE_UNAVAILABLE", []),
        (
            [{"action": "REFUND", "status": "SUBMITTING", "id": "pending"}],
            "UNKNOWN",
            "REFUND_STILL_SUBMITTING_TIMEOUT_NOT_VERIFIED",
            [],
        ),
        (
            [{"action": "REFUND", "status": "FAILED", "id": "failed"}],
            "UNMET",
            "NO_CONFIRMED_UNKNOWN_REFUND_RESULT",
            [],
        ),
        (
            [{"action": "REFUND", "status": "UNKNOWN"}],
            "UNKNOWN",
            "REFUND_EXECUTION_EVIDENCE_INCOMPLETE",
            [],
        ),
        (
            [
                {"action": "REFUND", "status": "UNKNOWN", "id": "pending-b"},
                {"action": "REPLACE", "status": "UNKNOWN", "id": "unrelated"},
                {"action": "REFUND", "status": "UNKNOWN", "id": "pending-a"},
                {"action": "REFUND", "status": "SUCCEEDED", "id": "completed"},
            ],
            "MET",
            "UNKNOWN_REFUND_EXECUTION_FOUND",
            ["pending-a", "pending-b"],
        ),
    ],
)
def test_unresolved_refund_condition_requires_trusted_execution_record(
    documents, ledger, status, reason, ids
):
    citation = cite(documents["POL12", "1.0"])
    (result,) = assess_citations(
        [citation],
        documents,
        {"case": {"intent": "refund_status"}, "ledger": ledger, "order": {"refunded_cents": 3000}},
    )
    assert result == {
        **citation,
        "use": "RECONCILE_UNKNOWN_REFUND",
        "condition_status": status,
        "reason_code": reason,
        "registry_version": CITATION_REGISTRY_VERSION,
        "scope": {"kind": "ORDER_REFUND_EXECUTION_LEDGER", "execution_ids": ids},
    }


def test_same_request_is_unknown_with_only_same_order_records_and_keeps_duplicate_quote_order(
    documents,
):
    first = cite(documents["POL08", "1.0"])
    second = cite(documents["POL12", "1.0"])
    results = assess_citations(
        [first, second, first],
        documents,
        {
            "case": {"intent": "refund_status"},
            "ledger": [{"action": "REFUND", "status": "SUCCEEDED", "id": "another-request"}],
        },
    )
    assert [row["quote"] for row in results] == [first["quote"], second["quote"], first["quote"]]
    assert results[0] == results[2]
    assert results[0]["condition_status"] == "UNKNOWN"
    assert results[0]["reason_code"] == "SAME_REFUND_REQUEST_NOT_VERIFIED"
    assert results[0]["scope"]["execution_ids"] == []


def test_registered_condition_reuses_handler_and_binds_full_document_digest(documents, monkeypatch):
    original = documents["POL12", "1.0"]
    other = {**original, "id": "ANOTHER"}
    changed = {**original, "parameters": {"modified": True}}
    unregistered_version = {**original, "version": "2.0"}
    monkeypatch.setitem(
        CONDITIONS,
        ("ANOTHER", "1.0", document_key(other)),
        CitationCondition("RECONCILE_UNKNOWN_REFUND", "REFUND_EXECUTION_RESULT_UNRESOLVED"),
    )
    evidence = {(row["id"], row["version"]): row for row in (other, changed, unregistered_version)}
    results = assess_citations(
        [cite(other), cite(changed), cite(unregistered_version)],
        evidence,
        {
            "case": {"intent": "damaged"},
            "ledger": [{"action": "REFUND", "status": "UNKNOWN", "id": "in-this-case"}],
        },
    )
    assert [row["condition_status"] for row in results] == ["MET", "NOT_ASSESSED", "NOT_ASSESSED"]
    assert results[0]["scope"] == {
        "kind": "CURRENT_CASE_EXECUTION_LEDGER",
        "execution_ids": ["in-this-case"],
    }
    assert all(row["scope"] == {"kind": None, "execution_ids": []} for row in results[1:])


@pytest.mark.parametrize(
    ("target_status", "timeout_status"),
    [
        ("SUCCEEDED", "UNMET"),
        ("UNKNOWN", "MET"),
        ("SUBMITTING", "UNKNOWN"),
    ],
)
def test_target_citation_conditions_only_use_the_matched_refund(
    documents, target_status, timeout_status
):
    citations = [cite(documents[key, "1.0"]) for key in ("POL08", "POL12")]
    result = assess_citations(
        citations,
        documents,
        {
            "case": {"intent": "refund_status"},
            "ledger": [
                {"id": "target", "action": "REFUND", "status": target_status},
                {"id": "unrelated", "action": "REFUND", "status": "UNKNOWN"},
            ],
            "refund_lookup": {"status": "FOUND", "execution": {"id": "target"}},
        },
    )
    assert [row["condition_status"] for row in result] == ["MET", timeout_status]
    assert result[0]["scope"] == {"kind": "MATCHED_REFUND_EXECUTION", "execution_ids": ["target"]}
    assert result[1]["scope"] == {
        "kind": "MATCHED_REFUND_EXECUTION",
        "execution_ids": ["target"] if timeout_status == "MET" else [],
    }
