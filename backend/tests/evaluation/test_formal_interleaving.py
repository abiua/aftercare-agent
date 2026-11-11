"""Frozen interleaving preserves case identity, budgets and recovery fences."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_formal_execution import NEXT, ORDER, outcome, run
from test_formal_execution import system as system  # noqa: PLC0414 - pytest fixture export

from aftercare.evaluation.formal.contracts import FormalFreezeDraftV3, parse_freeze_draft
from aftercare.evaluation.tables import EvaluationResult, Experiment


def interleaved(system):
    old = system.args["draft"]
    system.args["draft"] = parse_freeze_draft({
        **old.model_dump(mode="json"),
        "schema_version": "ac-formal-freeze-draft-v3",
        "schedule": "REPETITION_CASE_CONDITION",
    })
    assert isinstance(system.args["draft"], FormalFreezeDraftV3)
    return old


def test_interleaved_order_and_idempotent_resume(system):
    old = interleaved(system)
    assert "schedule" not in old.model_dump()
    first = run(system)
    assert first["status"] == "COMPLETED"
    expected = [(key, case) for _ in range(2)
                for case in sorted(item["id"] for item in system.cases) for key in ORDER]
    assert [(key, case) for _, key, case in system.service.runner.calls] == expected
    ids = [ident for ident, _, _ in system.service.runner.calls]
    assert len(set(ids)) == 24
    assert run(system, NEXT) == first
    assert len(system.service.runner.calls) == 24
    with Session(system.parent) as db:
        assert all(row.config["formal_schedule"]["conditions"] == ORDER
                   for row in db.scalars(select(Experiment)))


@pytest.mark.parametrize("crash", [False, True])
def test_unknown_in_later_condition_fences_entire_campaign_and_resume(system, crash):
    interleaved(system)

    def controlled(_result, _admit):
        if len(system.service.runner.calls) == 2:
            if crash:
                raise KeyboardInterrupt
            return outcome([{"status": "UNKNOWN", "usage": None, "estimated_cost_usd": None}])
        return outcome([])

    system.service.runner.control = controlled
    if crash:
        with pytest.raises(KeyboardInterrupt):
            run(system)
    else:
        assert run(system)["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    assert len(system.service.runner.calls) == 2
    resumed = run(system, NEXT)
    assert resumed["reason"] == "FORMAL_ATTEMPT_ACCOUNTING_INCOMPLETE"
    assert len(system.service.runner.calls) == 2
    with Session(system.parent) as db:
        assert len(list(db.scalars(select(EvaluationResult).where(
            EvaluationResult.status == "QUEUED")))) == 22
