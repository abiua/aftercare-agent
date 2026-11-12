"""One bounded live model evaluation of a disclosed, controlled upstream claim error."""

import argparse
import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from tau_p5_controlled_recheck import EVIDENCE_MODE, controlled_seed, invoke_controlled_recheck
from tau_p5_runtime import (
    CONTINUATION_MAX_TOTAL_CALLS,
    CONTINUATION_MAX_TOTAL_MICROUSD,
    CONTINUATION_TIMEOUT,
    RUNTIME,
    configure,
)
from verify_fresh_bootstrap import require
from verify_tau_p5_live import _checkpoint, audit, transport_timing

MAX_CALLS = 5
MAX_USD = Decimal("0.70")
LEDGER_NAME = "controlled-recheck-20261112.sqlite"


def load_report(path):
    raw = path.read_bytes()
    return json.loads(raw), sha256(raw).hexdigest()


def open_ledger(path, *, deadline):
    from aftercare.db import User
    from aftercare.intelligence.service import RunAudit
    from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    for model in (User, AgentRun, RunEvent, ModelAttempt):
        model.__table__.create(engine)
    now = datetime.now(UTC)
    run_id = str(uuid4())
    with Session(engine, expire_on_commit=False) as db, db.begin():
        db.add(
            User(
                id="tester",
                role="tester",
                token_hash="offline-evaluation",
                display_name="P5 evaluation fixture",
                enabled=True,
            )
        )
        run = AgentRun(
            id=run_id,
            thread_id=f"p5-evaluation:{run_id}",
            case_id="p5-controlled-offline-ledger",
            owner_id="p5-synthetic",
            actor={"user_id": "tester", "role": "tester"},
            status="RUNNING",
            mode="single",
            strategy="routed",
            message="P5 controlled upstream error validation",
            case_version=1,
            cycle=0,
            event_seq=0,
            deadline=now + timedelta(seconds=deadline),
            created_at=now,
            updated_at=now,
        )
        db.add(run)
    return engine, RunAudit(engine, run)


def read_ledger(engine, run_id, outcome):
    from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent

    with Session(engine) as db, db.begin():
        run = db.get_one(AgentRun, run_id)
        run.status = outcome
        attempts = [
            {
                "logical_id": row.logical_id,
                "ordinal": row.ordinal,
                "status": row.status,
                "error_code": row.error_code,
                "usage": row.usage,
                "estimated_cost_usd": row.estimated_cost_usd,
                "metadata_json": row.metadata_json,
                "result": row.result,
            }
            for row in db.scalars(
                select(ModelAttempt)
                .where(ModelAttempt.run_id == run_id)
                .order_by(ModelAttempt.started_at)
            )
        ]
        events = [
            {"kind": row.kind, "payload": row.payload, "seq": row.seq}
            for row in db.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            )
        ]
    return attempts, events


def run_controlled(output, source_path, cumulative_path):
    from aftercare.decisions.contracts import DecisionScope
    from aftercare.investigation.protocol import HUMAN_PAYMENT_V7
    from aftercare.runtime import open_runtime

    source, source_sha = load_report(source_path)
    cumulative, cumulative_sha = load_report(cumulative_path)
    archived = next(
        row["payload"] for row in source["paid_usage"]["events"] if row["kind"] == "decision_input"
    )
    scope = DecisionScope.model_validate(archived["scope"])
    require(not output.exists(), "P5_CONTROLLED_REQUIRES_NEW_REPORT")
    ledger_path = RUNTIME / LEDGER_NAME
    with open_runtime("test", dispatch=lambda _: None) as (_, engine, _, agent):
        before = audit(engine, cumulative)
        require(
            len(before["attempts"]) + MAX_CALLS <= CONTINUATION_MAX_TOTAL_CALLS
            and before["executions"] == before["vision_attempts"] == 0
            and before["run"]["status"] == "NEEDS_REVIEW"
            and all(row["status"] != "STARTED" for row in before["attempts"]),
            "P5_CONTROLLED_GLOBAL_CALL_ADMISSION",
        )
        cap = Decimal(CONTINUATION_MAX_TOTAL_MICROUSD) / 1_000_000
        booked = Decimal(before["conservative_booked_usd"])
        require(booked + MAX_USD <= cap, "P5_CONTROLLED_GLOBAL_COST_ADMISSION")
        with engine.begin() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            recorded = db.scalar(
                text(
                    "SELECT payload FROM agent_run_events WHERE run_id=:id AND kind='decision_input'"
                ),
                {"id": source["paid_usage"]["run"]["id"]},
            )
        require(recorded == archived, "P5_CONTROLLED_ARCHIVED_SCOPE_CHANGED")

        model = agent.model.with_evaluation_limits(calls=MAX_CALLS, budget=MAX_USD)
        require(
            model.registry.timeout_seconds == CONTINUATION_TIMEOUT
            and model.registry.max_run_attempts == MAX_CALLS,
            "P5_CONTROLLED_REGISTRY_SCOPE_CHANGED",
        )
        legacy = SimpleNamespace(
            model=model, search=agent.search, audit=None, validate_draft=None, strategy="routed"
        )
        context = agent._investigation_context(
            legacy,
            scope,
            "auto",
            protocol=HUMAN_PAYMENT_V7,
            output_reserve=archived["investigation_output_reserve"],
        )
        _, injection = controlled_seed(context, source)
        require(context.direct_recheck_request, "P5_CONTROLLED_DIRECT_RECHECK_REQUIRED")

        ledger, run_audit = open_ledger(ledger_path, deadline=model.registry.deadline_seconds)
        context.audit = run_audit
        report = {
            "status": "EVALUATION_PENDING",
            "mode": EVIDENCE_MODE,
            "source_report": source_path.name,
            "source_report_sha256": source_sha,
            "cumulative_report": cumulative_path.name,
            "cumulative_report_sha256": cumulative_sha,
            "injection": injection,
            "ledger": ledger_path.name,
            "ledger_run_id": run_audit.run_id,
            "provider_invoice_verified": False,
            "case_projection": False,
            "timeout_seconds": model.registry.timeout_seconds,
            "deadline_seconds": model.registry.deadline_seconds,
            "max_attempts": MAX_CALLS,
            "max_usd": str(MAX_USD),
            "global_ledger_before": len(before["attempts"]),
            "global_max_attempts": CONTINUATION_MAX_TOTAL_CALLS,
            "global_max_usd": str(cap),
            "global_booked_usd_before": str(booked),
            "global_unreconciled_usd_before": before["unreconciled_reserved_usd"],
        }
        _checkpoint(report, output, "controlled_dispatch_pending")
        outcome = "FAILED"
        try:
            with transport_timing(report, MAX_CALLS):
                state = invoke_controlled_recheck(context, source)
            report["result"] = state.get("output")
            outcome = "SUCCEEDED"
        except BaseException as error:
            report["failure"] = type(error).__name__
            raise
        finally:
            attempts, events = read_ledger(ledger, run_audit.run_id, outcome)
            ledger.dispose()
            report["attempts"], report["events"] = attempts, events
            known = sum((Decimal(row["estimated_cost_usd"] or 0) for row in attempts), Decimal(0))
            held = sum(
                (
                    Decimal(row["metadata_json"]["reserved_cost_usd"])
                    for row in attempts
                    if row["estimated_cost_usd"] is None
                ),
                Decimal(0),
            )
            report["global_ledger_after"] = len(before["attempts"]) + len(attempts)
            report["global_booked_usd_after"] = str(booked + known + held)
            report["controlled_known_usd"] = str(known)
            report["controlled_unreconciled_usd"] = str(held)
            report["status"] = (
                "EVALUATION_CAPTURED" if outcome == "SUCCEEDED" else "EVALUATION_FAILED"
            )
            _checkpoint(report, output, "controlled_readback")
        require(
            len(attempts) == len(report["transport_intervals"]) <= MAX_CALLS
            and len(before["attempts"]) + len(attempts) <= CONTINUATION_MAX_TOTAL_CALLS
            and booked + known + held <= cap
            and audit(engine, cumulative)["attempts"] == before["attempts"],
            "P5_CONTROLLED_READBACK_UNVERIFIED",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--cumulative", required=True, type=Path)
    args = parser.parse_args()
    configure(live=True, timeout_seconds=CONTINUATION_TIMEOUT)
    with (RUNTIME / "acceptance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_controlled(args.output.resolve(), args.source.resolve(), args.cumulative.resolve())


if __name__ == "__main__":
    main()
