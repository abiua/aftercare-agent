import json
from decimal import Decimal
import pytest
from run_review_benchmark import restore_budget,claim_sample
from tau_official_budget import BudgetExhausted


def journal(path,events):
    path.write_text(''.join(json.dumps(e)+'\n' for e in events))


def test_restart_retains_unknown_reservation_and_call_sequence(tmp_path):
    p=tmp_path/'calls.jsonl'
    journal(p,[{'call_id':1,'reserve_usd':'2'}, {'call_id':2,'reserve_usd':'3'}, {'call_id':1,'actual_usd':'1'}])
    b=restore_budget(p)
    assert b.calls==2 and b.committed==Decimal('4')
    assert b.reserve('sample','agent','review',Decimal('1'))==3
    assert restore_budget(p).committed==Decimal('5')


def test_started_sample_is_never_reissued(tmp_path):
    p=tmp_path/'sample.json'
    assert claim_sample(p,'sample','M1')
    before=p.read_bytes()
    assert not claim_sample(p,'sample','M1')
    assert p.read_bytes()==before


def test_partial_journal_fails_closed(tmp_path):
    p=tmp_path/'calls.jsonl';p.write_text('{"call_id":1')
    with pytest.raises(ValueError,match='Incomplete'):restore_budget(p)
    assert p.read_text()=='{"call_id":1'


def test_exceeded_reservation_halt_survives_restart(tmp_path):
    p=tmp_path/'calls.jsonl'
    journal(p,[{'call_id':1,'reserve_usd':'1'},{'call_id':1,'actual_usd':'2'}])
    with pytest.raises(BudgetExhausted):restore_budget(p).reserve('s','agent','review',Decimal('.01'))


def test_shared_ten_dollar_ceiling(tmp_path):
    p=tmp_path/'calls.jsonl';journal(p,[{'call_id':1,'reserve_usd':'9.9'}])
    with pytest.raises(BudgetExhausted):restore_budget(p).reserve('s','agent','review',Decimal('.11'))
