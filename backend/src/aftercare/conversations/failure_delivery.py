"""Customer-facing failure handoff in the same transaction as candidate delivery."""
from sqlalchemy import select

from aftercare.conversation_analysis.tables import ChatAnalysisTurn
from aftercare.db import User
from aftercare.intelligence.tables import AgentRun

from .service import ConversationService
from .tables import Conversation, InvestigationRequest

FAILURES = {
    'MODEL_TIMEOUT', 'RUN_DEADLINE_EXCEEDED', 'MODEL_UNAVAILABLE',
    'INVESTIGATION_MODEL_UNAVAILABLE', 'MODEL_INCOMPLETE', 'INVALID_MODEL_OUTPUT',
    'MODEL_BUDGET_EXHAUSTED', 'MODEL_COST_BUDGET_EXHAUSTED',
    'MODEL_RUN_BUDGET_EXHAUSTED', 'MODEL_RUN_COST_BUDGET_EXHAUSTED', 'MODEL_COST_UNKNOWN',
    'INVESTIGATION_RECHECK_TOOL_DENIED', 'INVESTIGATION_MATERIAL_READ_FAILED',
    'TOOL_ARGUMENTS_INVALID', 'TOOL_CALL_FORBIDDEN', 'TOOL_BATCH_LIMIT',
    'MODEL_CONNECTION_FAILURE', 'MODEL_RATE_LIMIT', 'MODEL_AUTHORIZATION',
    'MODEL_CONFIGURATION', 'MODEL_ADAPTER_ERROR', 'NO_COMPATIBLE_MODEL',
    'INTERRUPTED_MODEL_CALL', 'MODEL_STREAM_INTERRUPTED',
    'RUN_INTERNAL_ERROR', 'MODEL_FAILED', 'MODEL_CONTEXT_INSUFFICIENT',
    'INVESTIGATION_PROMPT_LIMIT', 'INVESTIGATION_ISSUE_COST_UNKNOWN',
    'INVESTIGATION_ISSUE_CALL_LIMIT', 'INVESTIGATION_ISSUE_COST_LIMIT',
    'INVESTIGATION_ISSUE_BUDGET_VIOLATION',
    'PROVIDER_LIMIT_CONFIG_MISMATCH', 'PROVIDER_COOLDOWN',
    'PROVIDER_IN_FLIGHT_LIMIT', 'PROVIDER_REQUEST_RATE_LIMIT',
}


def message_for(reason):
    if reason in {'MODEL_TIMEOUT', 'RUN_DEADLINE_EXCEEDED'}:
        return '这次查询超时了，已转客服继续处理。之前的说明和照片都已保留，不用重新提交。'
    if 'BUDGET' in reason or reason == 'MODEL_COST_UNKNOWN' or reason.startswith('INVESTIGATION_ISSUE_'):
        return '自动调查已暂停，已转客服继续核对。之前提交的资料都在。'
    return '这次自动查询没有完成，已转客服处理。之前的说明和照片都已保留。'


def notify(business, db, run: AgentRun):
    if run.reason not in FAILURES or run.status not in {'FAILED', 'NEEDS_REVIEW'}:
        return False
    request = db.scalar(select(InvestigationRequest).where(InvestigationRequest.run_id == run.id))
    if not request:
        return False
    actor = db.get(User, request.actor_id)
    row = db.get(Conversation, request.conversation_id, with_for_update=True)
    turn = db.get(ChatAnalysisTurn, request.turn_id)
    if not actor or not actor.enabled or actor.role not in {'customer', 'support'} or not row or not turn:
        return False
    if actor.role == 'customer' and actor.customer_id != row.customer_id:
        return False
    if (row.active_case_id, row.analysis_issue_id, row.revision) != (request.case_id, request.issue_id, turn.revision):
        return False
    if row.status != 'OPEN' or turn.state != 'PUBLISHED' or run.owner_id != request.actor_id:
        return False
    chat = ConversationService(business)
    row.status = 'HUMAN_HANDOFF'
    chat._touch(row)
    chat._reply(db, row, message_for(run.reason), [
        {'type':'status','data':{'code':'HUMAN_HANDOFF','run_id':run.id,'reason':run.reason}}
    ])
    return True
