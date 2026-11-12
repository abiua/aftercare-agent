from datetime import datetime, timezone
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from aftercare.business.decision_summary import agent_advice
from aftercare.decisions.tables import DecisionSnapshot


def test_summary_uses_original_agent_and_rejects_other_case():
    engine = create_engine('sqlite://')
    DecisionSnapshot.__table__.create(engine)
    case = SimpleNamespace(id='case1', decision_conversation_id='chat1', decision_issue_id='issue1')
    now = datetime.now(timezone.utc)
    with Session(engine) as db, db.begin():
        for version, origin in [(1, 'AGENT'), (2, 'SUPPORT_MODIFICATION')]:
            db.add(DecisionSnapshot(id=str(version), conversation_id='chat1', issue_id='issue1',
                version=version, origin=origin, actor_id='test', sha256='hash',
                input={'case_id':'case1','frozen_context':{'policies':{'documents':[
                    {'id':'P1','text':'original clause','version':'1'}, {'id':'P2','text':'unrelated'}]}}},
                candidate={'options':[{'policy_ids':['P1'],'rationale':origin}], 'unresolved':['not verified']},
                checks={}, expires_at=now, created_at=now))
        db.flush()
        summary=agent_advice(db, case)
        assert summary['candidate']['options'][0]['rationale']=='AGENT'
        assert [p['id'] for p in summary['policies']]==['P1']
        assert summary['historical'] is True
        case.id='case2'
        assert agent_advice(db, case) is None
    engine.dispose()
