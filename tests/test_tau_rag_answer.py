import json

from scripts.tau_rag_answer import answer


def evidence():
    return {"results": [{"id": "chunk-1", "text": "Refunds require approval.",
                        "citations": [{"block_id": "block-1", "location": {"line_start": 8}}],
                        "document_version": "v1", "policy_key": "shop-a",
                        "date_applicability": "unknown"}]}


def generator(chunk="chunk-1", quote="Refunds require approval."):
    return lambda *_: {"text": json.dumps({"answer": "退款需要审批。", "citations": [
        {"chunk_id": chunk, "quote": quote},
    ]})}


def test_no_evidence_does_not_call_model():
    def unexpected(*_):
        raise AssertionError("must not be called")
    assert answer("退款？", {"results": []}, unexpected)["generation_called"] is False


def test_valid_quote_resolves_to_source_but_does_not_claim_semantic_verification():
    result = answer("退款？", evidence(), generator())
    assert result["status"] == "citation_validated"
    assert result["citations"][0]["chunk_source_locations"][0]["location"]["line_start"] == 8
    assert result["semantic_correctness"] == "requires_independent_review"


def test_invented_source_or_quote_rejected_and_raw_output_preserved():
    for generate in [generator(chunk="secret-shop-b"), generator(quote="No approval required")]:
        result = answer("退款？", evidence(), generate)
        assert result["status"] == "rejected"
        assert "raw_output" in result
        assert "answer" not in result


def test_failed_model_attempt_is_preserved_without_exception_secrets():
    def fail(*_):
        raise TimeoutError("credential should not enter output")
    result = answer("退款？", evidence(), fail)
    assert result["status"] == "generation_failed"
    assert result["error_class"] == "TimeoutError"
    assert "credential" not in json.dumps(result)


def test_malformed_model_envelopes_are_recorded_as_failed():
    for response in [None, {}, {"text": None}]:
        result = answer("退款？", evidence(), lambda *_, value=response: value)
        assert result["status"] == "generation_failed"
