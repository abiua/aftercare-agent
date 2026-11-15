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
    assert len(groups)==10 and groups['S0'].historical_only
    assert groups['S1c'].executor.model==groups['M2'].executor.model=='deepseek-flash'
    assert groups['M2'].reviewer.model==groups['S2'].reviewer.model=='gpt-6.1-sol'
    assert groups['M1'].executor==groups['S1'].executor
    assert all(g.max_agent_calls==48 for g in groups.values())
    assert groups['S2'].review=='self' and groups['M1'].review=='independent'


def test_original_six_and_four_ablation_configurations():
    groups = architectures()
    for name in ('S0','S1','S2','S1c','M1','M2'):
        assert groups[name].model_dump() == {
            'name':name, 'guards':name!='S0',
            'review':'self' if name=='S2' else 'independent' if name in ('M1','M2') else 'none',
            'experts':name in ('M1','M2'),
            'executor':{'model':'deepseek-flash','reasoning':'disabled'} if name in ('S1c','M2')
                       else {'model':'gpt-6.1-sol','reasoning':'low'},
            'reviewer':{'model':'gpt-6.1-sol','reasoning':'low'},
            'expert':{'model':'gpt-6.1-sol','reasoning':'low'},
            'max_agent_calls':48, 'historical_only':name=='S0',
        }
    for base in ('M1','M2'):
        for suffix, change in [('x', {'experts':False}), ('r', {'review':'none'})]:
            assert groups[base+suffix].model_dump() == {
                **groups[base].model_dump(), 'name':base+suffix, **change}
