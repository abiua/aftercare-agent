from types import SimpleNamespace

import pytest

from aftercare.business.errors import BusinessError
from aftercare.evaluation.investigations import authorize, usage


def attempt(cost, status='SUCCEEDED', reserved=None):
    return SimpleNamespace(estimated_cost_usd=cost, status=status,
                           metadata_json={} if reserved is None else {'reserved_cost_usd':reserved})


def test_unknown_cost_is_not_zero_or_complete():
    result=usage([attempt('0.001'),attempt(None,'UNKNOWN','0.03'),attempt(None,'FAILED')])
    assert result['estimated_usd'] is None
    assert result['known_usd']=='0.001'
    assert result['reserved_usd']=='0.03'
    assert result['unpriced_calls']==2
    assert result['reserve_complete'] is False
    assert result['failed_calls']==2


def test_known_costs_use_decimal_and_zero_calls_are_zero():
    assert usage([attempt('0.1'),attempt('0.2')])['estimated_usd']=='0.3'
    assert usage([])['estimated_usd']=='0'


@pytest.mark.parametrize('role',['customer','support','approver'])
def test_business_roles_cannot_read_test_workbench(role):
    with pytest.raises(BusinessError):
        authorize(SimpleNamespace(role=role))
    authorize(SimpleNamespace(role='tester'))
