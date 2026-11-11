import importlib
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def takeover(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("verify_chat_live_business_takeover")


def test_takeover_resolves_only_frozen_technical_blockers_on_a_new_review(takeover):
    snapshot = {
        "id": takeover.SNAPSHOT,
        "version": 1,
        "sha256": "a" * 64,
        "candidate": {
            "unresolved": list(takeover.TECHNICAL),
            "options": [
                {"id": "graph-disposition", "action": "HUMAN_HANDOFF", "target_line_ids": ["L01"]}
            ],
        },
        "checks": {"graph-disposition": {"status": "NON_EXECUTING"}},
    }
    original = deepcopy(snapshot)
    review = takeover._review_body(snapshot, ["material-item", "material-parcel"])["review"]
    assert review["action"] == "MODIFY"
    assert review["resolved_items"] == list(takeover.TECHNICAL)
    assert review["replacement"]["lines"] == [{"line_id": "L01", "amount_cents": 4900}]
    assert "CONTROL" in review["reason"]
    assert snapshot == original
    snapshot["candidate"]["unresolved"][1] = "尚未确认商品归属"
    with pytest.raises(takeover.VerificationError, match="LIVE_TAKEOVER_NONTECHNICAL_UNRESOLVED"):
        takeover._review_body(snapshot, ["material-item", "material-parcel"])
