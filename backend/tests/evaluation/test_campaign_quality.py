import pytest

from aftercare.evaluation.campaign_quality import quality_metrics


def row(**changes):
    label={'source_sha256': 'a'*64,'reviewer': 'reviewer-1','blind_review_record': 'audit-1','judged_before_exposure': True,'new_information': False,
               'acceptable': True,'proposal_quality': 'CORRECT','rule_intercepted': False,'upstream_error': False,'corrected': False,
               'key_fact_omissions': 0,'policy_errors': 0,'citation_errors': 0}
    label.update(changes)
    return {'source_sha256': 'a'*64,'cost_usd': '0.1','independent_judgment': label}


def test_denominators_separate_errors_false_blocks_and_corrections():
    result=quality_metrics([row(rule_intercepted=True),row(acceptable=False,proposal_quality='INCORRECT',upstream_error=True)])
    assert result['independent_success_rate']==0.5
    assert result['missed_error_rate']==1
    assert result['false_block_rate']==1
    assert result['correction_rate']==0
    assert result['cost_per_independent_success_usd']=='0.2'


def test_new_information_and_exposed_judgments_do_not_become_gold():
    result=quality_metrics([row(),row(new_information=True),row(judged_before_exposure=False)])
    assert result['independent_review_count']==1
    assert result['new_information_reviews']==result['unblinded_reviews']==1
    assert result['cost_per_independent_success_usd'] is None
    assert result['missed_error_rate'] is None


def test_judgment_for_other_input_is_rejected():
    with pytest.raises(ValueError,match='REVIEW_INPUT_CHANGED'):
        quality_metrics([row(source_sha256='b'*64)])
