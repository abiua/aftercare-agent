"""Offline replay regressions, including explicit visibility of missing raw review entries."""
import json
from types import SimpleNamespace

import pytest

from aftercare.agent_kernel.contracts import StepProposal, architectures
from aftercare.agent_kernel.experts import ExpertService
from replay_amount_guard import public
from replay_expert_staleness import ORDER_TOOLS, statistics
from replay_kernel_v3_1 import ROOT, replay


def test_frozen_dev_and_saved_review_replay_without_network(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('Replay must be offline')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    report = replay()
    rows = {row['task']: row for row in report['development']}
    assert [(rows[t]['v3_replay_consults'], rows[t]['v3_1_replay_consults']) for t in ('41', '96')] == [(4, 1), (3, 1)]
    for task in ('41', '16'):
        assert rows[task]['historical_dropped_entries'] == 3
        assert rows[task]['saved_entries_dropped_after'] == 0
        assert rows[task]['full_inventory_dropped_after'] is None  # raw bodies were never saved
    assert rows['54']['interrupted_consult_boundary_appended']
    mini = report['review_minitest']
    assert mini['samples'] == 34 and mini['success_path_samples'] == 33
    assert mini['success_path_conflicts_after_on_saved_entries'] == 0
    assert mini['task_97_control_blocked_after_on_saved_entries']
    assert mini['missing_raw_entries'] == 29 and mini['saved_entries_dropped_after'] == 0


@pytest.mark.parametrize('task', ['41', '96'])
def test_scheduler_replay_matches_actual_expert_service_on_frozen_messages(task):
    trajectory = json.loads((ROOT / f'.runtime/kernel-v3-dev/run-v1/official/{task}-M2.json').read_text())
    service = ExpertService(SimpleNamespace(generate=lambda **kw: StepProposal(text='{}')))
    context = []
    for message in trajectory['messages']:
        if message['role'] == 'assistant':
            service.advise(architectures()['M2'], context, ORDER_TOOLS, '')
        context.append(public(message))
    result = statistics(trajectory['messages'])
    assert service.consults == result['new_consults'] == 1
    assert sum(e.stage == 'expert_self_write_rebased' for e in service.trace) == result['self_write_rebases']
    assert not any('refresh_of' in e.output for e in service.trace)
