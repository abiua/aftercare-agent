import json
from hashlib import sha256
import pytest
import audit_review_run as auditor


def fixture(tmp_path,monkeypatch):
    data=tmp_path/'data.json';data.write_text('{}');monkeypatch.setattr(auditor,'DATA',data)
    (tmp_path/'development-manifest.json').write_text(json.dumps({'dataset_sha256':sha256(data.read_bytes()).hexdigest(),'code_sha256':{},'schedule':[['x','M1']]}))
    events=[{'call_id':1,'row':'x-M1','role':'agent','actor':'reviewer','model':'gpt-6.1-sol','reserve_usd':'.10'},
        {'call_id':1,'actual_usd':'.02'}]
    (tmp_path/'calls.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    (tmp_path/'x-M1.json').write_text(json.dumps({'id':'x','mode':'M1','split':'development','status':'complete',
        'result':{'verdict':'allow'},'metrics':{'agent_cost_usd':.02}}))
    return events


def test_receipt_reconciles_and_detects_duplicates(tmp_path,monkeypatch):
    events=fixture(tmp_path,monkeypatch)
    assert auditor.audit(tmp_path)['complete']
    with (tmp_path/'calls.jsonl').open('a') as f:f.write(json.dumps({**events[0],'call_id':2})+'\n')
    with pytest.raises(AssertionError,match='Repeated'):auditor.audit(tmp_path)


def test_unknown_result_retains_reserved_cost(tmp_path,monkeypatch):
    events=fixture(tmp_path,monkeypatch)
    (tmp_path/'calls.jsonl').write_text(json.dumps(events[0])+'\n')
    (tmp_path/'x-M1.json').write_text(json.dumps({'id':'x','mode':'M1','status':'started'}))
    result=auditor.audit(tmp_path)
    assert not result['complete'] and result['held_usd']=='0.10' and result['known_usd']=='0'
