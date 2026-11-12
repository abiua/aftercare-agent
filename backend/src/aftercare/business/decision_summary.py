"""Read-only Agent advice for the already-authorized internal case view."""
from sqlalchemy import select
from aftercare.decisions.tables import DecisionSnapshot


def agent_advice(db, case):
    if not case.decision_conversation_id or not case.decision_issue_id:
        return None
    snapshot = db.scalar(select(DecisionSnapshot).where(
        DecisionSnapshot.conversation_id == case.decision_conversation_id,
        DecisionSnapshot.issue_id == case.decision_issue_id,
        DecisionSnapshot.origin == 'AGENT',
    ).order_by(DecisionSnapshot.version.desc()).limit(1))
    if snapshot is None or snapshot.input.get('case_id') != case.id:
        return None
    ids = {p for option in snapshot.candidate.get('options', []) for p in option.get('policy_ids', [])}
    policies = snapshot.input.get('frozen_context', {}).get('policies', {}).get('documents', [])
    return {'id': snapshot.id, 'version': snapshot.version, 'origin': 'AGENT',
            'created_at': snapshot.created_at.isoformat(), 'historical': True,
            'candidate': snapshot.candidate, 'checks': snapshot.checks,
            'policies': [{key: p.get(key) for key in ('id', 'version', 'title', 'text')}
                         for p in policies if p.get('id') in ids]}
