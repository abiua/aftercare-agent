"""Customer-safe progress from an actual offline automatic investigation."""

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session
from test_business import OTHER, SUPPORT
from test_chat_investigation_delivery import _published
from test_chat_investigation_delivery import enabled as _enabled
from test_conversations import client as _client
from test_conversations import headers, send
from test_conversations import system as _system

from aftercare.conversations import investigation_delivery as delivery
from aftercare.conversations.decision_delivery import project
from aftercare.conversations.tables import InvestigationRequest
from aftercare.intelligence.service import AgentService

client = _client
system = _system
enabled = _enabled


def test_progress_reads_bound_request_run_and_current_published_head(enabled):
    url, business, turn_id = _published(enabled)

    def progress():
        response = enabled.client.get(url, headers=headers())
        assert response.status_code == 200
        return response.json()["investigation_progress"]

    assert progress() is None
    assert delivery._stage(business, turn_id) is not None
    assert progress() == {"state": "QUEUED"}
    enabled.settings.model_enabled = False
    agent = AgentService(
        enabled.engine, enabled.settings, business, InMemorySaver(), dispatch=lambda _run: None
    )
    delivery.process(business, agent, turn_id)
    with Session(enabled.engine) as db:
        run_id = db.get_one(InvestigationRequest, turn_id).run_id
    assert run_id
    assert progress() == {"state": "QUEUED"}
    agent.process(run_id)
    assert progress() == {"state": "CHECKING"}
    project(business, run_id)
    assert progress() == {"state": "PENDING_SUPPORT"}
    assert "investigation_progress" not in enabled.client.get(url, headers=headers(SUPPORT)).json()
    assert enabled.client.get(url, headers=headers(OTHER)).status_code == 404
    assert send(enabled.client, url, "补充：位置在边缘").status_code == 201
    assert progress() is None


def test_customer_progress_ignores_unbound_request_and_flag_off(enabled):
    url, business, turn_id = _published(enabled)
    assert delivery._stage(business, turn_id) is not None
    with Session(enabled.engine) as db, db.begin():
        request = db.get_one(InvestigationRequest, turn_id)
        scope = request.scope
        request.scope = {**scope, "line_ids": ["unrelated-line"]}
    assert enabled.client.get(url, headers=headers()).json()["investigation_progress"] is None
    enabled.settings.chat_investigation_enabled = False
    assert "investigation_progress" not in enabled.client.get(url, headers=headers()).json()
