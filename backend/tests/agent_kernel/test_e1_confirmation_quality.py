import json
from pathlib import Path
from review_confirmation_audit import audit_sample, prior_authorization_candidates


def test_last_confirmation_removal_does_not_remove_earlier_partial_authority():
    messages=[{'role':'assistant','content':'Change watch and laptop; refund to gift card?'},
        {'role':'user','content':'For the watch, yes, refund to the existing gift card is fine. I am unsure about the laptop.'},
        {'role':'assistant','content':'Please confirm all three changes.'}]
    finding=prior_authorization_candidates(messages)
    assert len(finding)==1 and finding[0]['customer_message_index']==1
    result=audit_sample({'id':'s','task_id':'111','mutation':'remove_confirmation','messages':messages})
    assert result['status']=='needs_semantic_review' and not result['label_changed']


def test_non_confirmation_mutations_not_relabelled():
    assert audit_sample({'id':'s','mutation':'foreign_payment'})=={'id':'s','applicable':False}


def test_actual_frozen_disputed_samples_are_flagged_without_mutation():
    root=Path(__file__).resolve().parents[3]
    path=root/'.runtime/e1-review/e1-candidates-v3.json'
    if not path.exists():
        import pytest
        pytest.skip('Local immutable E1 fixture not available')
    original=path.read_bytes();data=json.loads(original)
    ids={'26d1b48237ba690a2228','8231178bc8a4c782d23f'}
    selected=[s for s in data['samples'] if s['id'] in ids]
    assert len(selected)==2
    assert all(audit_sample(s)['status']=='needs_semantic_review' for s in selected)
    assert path.read_bytes()==original
