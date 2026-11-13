"""Product smoke acceptance on new synthetic orders; not a benchmark or quality score."""
from contextlib import contextmanager
from decimal import Decimal
import argparse
import json
import os
from pathlib import Path
from time import sleep
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tau_p5_runtime import configure
from verify_chat_p1_http import message

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/step5-basic-20261113'
IDS = {'ordinary': 'DEMO-STEP5-NORMAL-1113', 'complex': 'DEMO-STEP5-COMPLEX-1113', 'failure': 'DEMO-STEP5-FAILURE-1113'}


def save(r):
    (OUT / (r['scenario'] + '.json')).write_text(json.dumps(r, ensure_ascii=False, indent=2, default=str))


@contextmanager
def client(role):
    c = httpx.Client(base_url='http://127.0.0.1:18225/api/', timeout=60, trust_env=False)
    res = c.post('auth/demo-role', json={'role': role}); res.raise_for_status()
    c.headers['Authorization'] = 'Bearer ' + res.json()['token']
    try:
        yield c
    finally:
        c.close()


def post(c, url, data):
    response = c.post(url, json=data, headers={'Idempotency-Key': str(uuid4())})
    if response.status_code >= 400:
        raise RuntimeError(f'{url}: {response.status_code}: {response.text[:500]}')
    return response.json()


def setup(live=False):
    configure()
    from aftercare import config
    from tau_p9_runtime import task_registry
    previous = config.load_settings
    def settings(profile='test'):
        return config.Settings.model_validate({**previous(profile).model_dump(),
            'model_enabled': live, 'model_config_path': ROOT / '.runtime/p9-task-models.json',
            'chat_investigation_kernel_review': True,
            'chat_investigation_run_max_calls': 12,
            'chat_investigation_issue_max_calls': 32,
            'chat_investigation_issue_max_microusd': 1500000,
            'chat_investigation_direct_recheck_request': True})
    config.load_settings = settings
    if live:
        assert task_registry().max_run_usd <= Decimal('0.70')
        os.environ['AFTERCARE_OPENAI_KEY'] = json.loads((ROOT / '.runtime/model-secret.json').read_text())['api_key']
        os.environ['AFTERCARE_DEEPSEEK_KEY'] = json.loads((ROOT / '.runtime/deepseek-secret.json').read_text())['api_key']


def stage(name):
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.business.models import Order, OrderLine
    from aftercare.business.seed import initialize
    path = OUT / (name + '.json')
    assert not path.exists() or 'conversation_id' not in json.loads(path.read_text()), 'Existing scenario must be inspected, not replayed'
    engine = guarded_engine(load_settings('test'))
    with Session(engine) as db:
        source = db.get_one(Order, 'O9962')
        lines = [dict(x.data) for x in db.scalars(select(OrderLine).where(OrderLine.order_id == source.id).order_by(OrderLine.line_id))]
        if name != 'complex': lines = lines[:1]
        total = sum(x['paid_cents'] for x in lines)
        order = {**source.data, 'id': IDS[name], 'items': lines, 'refunded_cents': 0,
                 'subtotal_cents': total, 'payable_cents': total, 'paid_cents': total}
    initialize(engine, {'customers': [], 'products': [], 'orders': [order], 'policies': [], 'shipments': []})
    engine.dispose()
    r = {'scenario': name, 'order_id': order['id'], 'evidence_mode': 'SYNTHETIC_PRODUCT_DEMO', 'stage': 'created', 'mutations': []}
    save(r)
    with client('customer') as c, client('support') as s:
        created = post(c, 'conversations', {})
        r['conversation_id'] = created['id']; save(r)
        base = 'conversations/' + created['id']
        post(c, base + '/context', {'order_id': order['id'], 'line_ids': [x['line_id'] for x in lines],
            'expected_revision': created['revision'], 'new_issue': False})
        text = f"订单 {order['id']} 的杯子收到时有破损，我想退货退款。"
        if name == 'complex': text += '这单有两件商品，我觉得两件都坏了，希望都退款；照片只拍到一个杯子。'
        ids = {'client_message_id': str(uuid4()), 'idempotency_key': str(uuid4())}
        files = [(f, (ROOT / 'data/vision-v1' / f).read_bytes()) for f in ('synthetic-cracked-mug.png', 'synthetic-torn-parcel.png')]
        sent = message(c, created['id'], text, ids, files)
        r['case_id'] = sent['conversation']['active_case_id']; save(r)
        assert r['case_id']
        attachments = next(m['attachments'] for m in sent['messages'] if m['role'] == 'customer')
        material_ids = []
        for attachment, kind in zip(attachments, ('ITEM', 'PARCEL')):
            current = s.get(base + '/decision-materials').json()
            linked = post(s, base + '/decision-materials', {'attachment_id': attachment['id'], 'kind': kind,
                'expected_issue_id': current['issue_id'], 'expected_case_id': r['case_id'],
                'expected_materials_version': current['materials_version']})
            material_ids.append(linked['material']['id'])
        detail = s.get('cases/' + r['case_id']).json()
        post(s, 'cases/' + r['case_id'] + '/material-reviews', {'expected_version': detail['case']['version'],
            'decision': 'APPROVED', 'notes': '自动化模拟客服登记：合成图片只对应L01；不据此证明图片真实性。',
            'observations': [{'material_id': material_ids[0], 'finding': 'VISIBLE_DAMAGE', 'line_id': 'L01', 'affected_quantity': 1}]})
        r.update(stage='materials_reviewed', attachments=attachments, material_ids=material_ids)
        save(r)
    print(json.dumps({k:r[k] for k in ('scenario','conversation_id','case_id','stage')}))


def investigate(name, fault=False):
    from aftercare.runtime import open_runtime
    from aftercare.conversations.service import ConversationService
    from aftercare.conversations.analysis import process as analyze
    from aftercare.conversations.investigation_delivery import process as dispatch
    from aftercare.conversations.decision_delivery import project
    from aftercare.conversation_analysis.tables import ChatAnalysisTurn
    from aftercare.conversations.tables import InvestigationRequest
    from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
    r = json.loads((OUT / (name + '.json')).read_text())
    assert 'run_id' not in r, 'Never replay a paid run'
    # Analysis uses reviewed synthetic materials, no image-model call. Investigation is live.
    with open_runtime('test', dispatch=lambda _: None) as (settings, engine, business, agent):
        chat = ConversationService(business)
        for _ in range(30):
            with Session(engine) as db:
                turn = db.scalar(select(ChatAnalysisTurn).where(ChatAnalysisTurn.conversation_id == r['conversation_id']).order_by(ChatAnalysisTurn.generation.desc()))
                ident, state = turn.id, turn.state
            if state == 'PUBLISHED': break
            if ident in chat.analysis.due(): analyze(chat, ident, settings, None)
            sleep(.5)
        assert state == 'PUBLISHED', state
        dispatch(business, agent, ident)
        with Session(engine) as db:
            r['run_id'] = db.get_one(InvestigationRequest, ident).run_id
        r['stage'] = 'run_reserved'; save(r)
        if fault:
            with Session(engine) as db, db.begin():
                run = db.get_one(AgentRun, r['run_id'])
                assert run.status == 'QUEUED'
                run.status, run.reason = 'FAILED', 'MODEL_TIMEOUT'
                run.event_seq += 1
                db.add(RunEvent(run_id=run.id, seq=run.event_seq, kind='evaluation_fault',
                    payload={'scenario':'controlled_timeout','provider_calls':0}, created_at=business.clock()))
            r['fault'] = 'CONTROLLED_TIMEOUT_NO_PROVIDER_CALL'
        else:
            agent.process(r['run_id'])
        project(business, r['run_id']); project(business, r['run_id'])
        with Session(engine) as db:
            run = db.get_one(AgentRun, r['run_id'])
            events = [{'kind': e.kind, 'payload': e.payload} for e in db.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))]
            attempts = [{'status': a.status, 'error': a.error_code, 'cost_usd': a.estimated_cost_usd} for a in db.scalars(select(ModelAttempt).where(ModelAttempt.run_id == run.id))]
            r.update(stage='investigated', run_status=run.status, run_reason=run.reason, events=events, attempts=attempts, run_output=run.output)
            save(r)
    with client('support') as s:
        r['decisions'] = s.get('conversations/' + r['conversation_id'] + '/decisions').json(); save(r)
    print(json.dumps({k:r[k] for k in ('scenario','run_id','run_status','run_reason','attempts')},default=str))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('action', choices=['stage','live','fault']);p.add_argument('scenario',choices=list(IDS));args=p.parse_args()
    OUT.mkdir(exist_ok=True,parents=True)
    setup(args.action == 'live')
    if args.action == 'stage': stage(args.scenario)
    else: investigate(args.scenario, fault=args.action == 'fault')
