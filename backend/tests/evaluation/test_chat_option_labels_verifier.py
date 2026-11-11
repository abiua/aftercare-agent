"""Offline contracts for the dedicated PG option-label acceptance harness."""

import importlib.util
from pathlib import Path

from aftercare.evaluation.chat.contracts import PostLabels, PostOptionLabelsV2


def test_verifier_uses_opaque_option_labels_only_on_v2(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "option_labels_verifier", scripts / "verify_chat_p5_option_labels.py"
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    candidates = [
        {"id": "candidate", "status": "AVAILABLE", "options": [{"id": "opaque"}]},
        {"id": "missing", "status": "UNAVAILABLE", "options": []},
    ]
    body = PostOptionLabelsV2.model_validate(harness._label_body(candidates, v2=True))
    assert len(body.items) == 2
    assert [item.model_dump() for item in body.option_labels] == [
        {"candidate_id": "candidate", "option_id": "opaque", "decision": "UNDETERMINED"}
    ]
    old = PostLabels.model_validate(harness._label_body(candidates, v2=False))
    assert len(old.items) == 2 and "option_labels" not in old.model_dump()
