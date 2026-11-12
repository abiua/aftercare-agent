import pytest
from pydantic import ValidationError
from aftercare.agent_kernel.contracts import ReviewResult, ExpertClaim, architectures


def test_review_cannot_veto_and_revision_is_actionable():
    for data in ({'verdict':'veto'}, {'verdict':'revise'}, {'verdict':'revise','issue':'wrong'}):
        with pytest.raises(ValidationError): ReviewResult(**data)
    assert ReviewResult(verdict='allow',fallback_reason='timeout').verdict=='allow'


def test_claim_cannot_promote_opinion_to_verified_fact():
    with pytest.raises(ValidationError):
        ExpertClaim(id='1',subject='order',predicate='paid',value='yes',state='SUPPORTED')


def test_configurations_separate_architecture_and_model():
    groups=architectures()
    assert len(groups)==6 and groups['S0'].historical_only
    assert groups['S1c'].executor.model==groups['M2'].executor.model=='deepseek-flash'
    assert groups['M2'].reviewer.model==groups['S2'].reviewer.model=='gpt-6.1-sol'
    assert groups['M1'].executor==groups['S1'].executor
    assert all(g.max_agent_calls==48 for g in groups.values())
    assert groups['S2'].review=='self' and groups['M1'].review=='independent'
