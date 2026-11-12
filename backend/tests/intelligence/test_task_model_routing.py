from types import SimpleNamespace

import pytest

from aftercare.investigation.model_routing import select_model

REGISTRY = SimpleNamespace(models=[SimpleNamespace(alias='economy'), SimpleNamespace(alias='analysis')])


def test_fact_extraction_prefers_economy_with_upgrade_available():
    selected = select_model('routed','fact','fact',REGISTRY)
    assert selected.preferred == 'economy' and not selected.fixed


@pytest.mark.parametrize('role,phase',[('policy','policy'),('verifier','verify-0'),('fact','recheck'),('strong_single','single')])
def test_decision_and_recheck_never_silently_downgrade(role,phase):
    selected = select_model('routed',role,phase,REGISTRY)
    assert selected.preferred == 'analysis' and selected.fixed


def test_fixed_baselines_are_fixed_and_missing_economy_is_explained():
    assert select_model('economy','verifier','verify-0',REGISTRY).preferred == 'economy'
    selected = select_model('routed','fact','fact',SimpleNamespace(models=[]))
    assert selected.reason == 'ECONOMY_MODEL_NOT_CONFIGURED'
