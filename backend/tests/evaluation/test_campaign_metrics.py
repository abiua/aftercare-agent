from aftercare.evaluation.campaign_metrics import summarize


def test_unknown_cost_and_unreviewed_success_are_not_invented():
    rows=[{'complexity': 'ordinary','arm': 'multi','strategy': 'routed','cost_usd': '0.12','calls': 3,'elapsed_seconds': 4,'candidate_present': True},
          {'complexity': 'ordinary','arm': 'multi','strategy': 'routed','cost_usd': None,'unknown_cost_calls': 1,'calls': 1,'elapsed_seconds': 2,'candidate_present': False}]
    group=summarize(rows)[0]
    assert group['candidate_rate']==0.5
    assert group['cost_per_case_usd'] is None
    assert group['independent_success_rate'] is None
    assert group['cost_per_independent_success_usd'] is None
    assert group['mean_calls']==2
    assert group['development_acceptance_rate'] is None


def test_normal_and_complex_are_never_pooled():
    rows=[{'complexity': c,'arm': 'multi','strategy': 'analysis','cost_usd': '0.1'} for c in ['ordinary','complex']]
    assert len(summarize(rows))==2


def test_success_cost_includes_failed_cases_and_keeps_label_source_separate():
    rows=[{'complexity': 'complex','arm': 'multi','strategy': 'analysis','cost_usd': '0.10','development_accepted': True},
          {'complexity': 'complex','arm': 'multi','strategy': 'analysis','cost_usd': '0.20','development_accepted': False}]
    metric=summarize(rows)[0]
    assert metric['cost_per_development_success_usd']=='0.30'
    assert metric['cost_per_independent_success_usd'] is None
