"""Actual CLI/register/core/PG chain, synthetic source evidence and zero-model runner."""

import json
import os
import stat
import sys
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_formal_execution import CREATOR
from test_formal_execution_pg import pg_system as pg_system  # noqa: PLC0414
from test_formal_handoff import CAP, _prepared
from test_formal_source_verification import (
    CATALOGUE_SHA,
    _fixtures,
    _sha,
    _snapshot,
    _with_controls,
)

from aftercare.business.rules import digest
from aftercare.db import User
from aftercare.evaluation.formal import __main__ as cli
from aftercare.evaluation.formal import orchestrator
from aftercare.evaluation.formal.execution import FormalEvaluationService
from aftercare.evaluation.tables import EvaluationResult, Experiment

pytestmark = [pytest.mark.pg, pytest.mark.skipif(
    os.environ.get("AFTERCARE_FORMAL_CLI_PG") != "1",
    reason="explicit isolated PostgreSQL CLI acceptance required",
)]


def _source(service):
    runtime = service._runtime({key: CATALOGUE_SHA for key in ("V1", "V2")})
    fixture = _with_controls(_fixtures(), lambda config: config.update(runtime))
    fixture.plan = fixture.plan.model_copy(update={
        "policies_digest": digest(runtime["policy_snapshot"]),
        "retrieval_digest": digest(runtime["retrieval_snapshot"]),
    })
    fixture.request = fixture.request.model_copy(update={"plan_sha256": _sha(fixture.plan)})
    spec = runtime["model_snapshot"]["models"][0]
    for report in fixture.reports.values():
        for result in report["results"]:
            result["actual"]["retrieval_evidence"]["selector_sha256"] = runtime["policy_snapshot"]["selector_sha256"]
            raw = fixture.evidence[result["id"]][0][1]
            for attempt in [*result["actual"]["model_attempts"], *(item["metadata_json"] for item in raw)]:
                attempt.update({key: spec[key] for key in ("alias", "provider", "model")})
                attempt["base_registry_digest"] = runtime["model_snapshot"]["registry_digest"]
                attempt["registry_version"] = runtime["model_snapshot"]["version"]
    snapshot = _snapshot(fixture)
    assert snapshot.source_verification == "NATIVE_VALIDATION_VERIFIED", snapshot
    return fixture


def test_cli_preview_freeze_unlock_execute_report_and_repeat(pg_system, tmp_path, monkeypatch, capsys):
    fixture = _prepared(
        tmp_path.resolve(), frozen_record=False, interleaved=True,
        source_fixture=_source(pg_system.service),
    )
    campaign, register = fixture.campaign_id, fixture.register
    runtime_root = tmp_path / "runtime-root"
    runtime_dir = runtime_root / ".runtime"
    runtime_dir.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(orchestrator, "ROOT", runtime_root)
    monkeypatch.delenv("AFTERCARE_FORMAL_TESTER_ID", raising=False)

    def private(name, value, directory=runtime_dir):
        path = directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return path

    private("users-test.json", [{
        "user_id": CREATOR.user_id, "role": "tester", "token": CREATOR.user_id * 10,
    }])
    draft = private("draft.json", fixture.frozen["draft"], tmp_path)
    confirmation = private("confirmation.json", fixture.frozen["confirmation"], tmp_path)
    unlock = private("unlock.json", fixture.request.model_dump(mode="json"), tmp_path)

    @contextmanager
    def runtime(source):
        assert source == "live"
        yield pg_system.service.settings, pg_system.parent, None, pg_system.service.model

    class Service(FormalEvaluationService):
        def _runner(self, package):
            return pg_system.runner

    monkeypatch.setattr(orchestrator, "_runtime", runtime)
    monkeypatch.setattr(orchestrator, "_execution_api", lambda: Service)
    monkeypatch.setattr(orchestrator, "guarded_engine", lambda *_args, **_kwargs: pg_system.parent)

    def command(name, *arguments):
        monkeypatch.setattr(sys, "argv", [
            "formal", "--root", str(register.root), name, "--campaign", campaign, *arguments,
        ])
        assert cli.main() == 0
        return json.loads(capsys.readouterr().out)

    preview = command("check-draft", "--record", str(draft), "--confirmation", str(confirmation))
    assert not fixture.journal.exists()
    private("formal-execution.json", {
        "schema_version": "ac-formal-execution-deployment-v1", "campaign_id": campaign,
        "freeze_sha256": preview["prospective_freeze_sha256"], "execution_cap": CAP.model_dump(),
    })
    assert command("freeze", "--record", str(draft), "--confirmation", str(confirmation))["status"] == "FROZEN"
    assert not fixture.journal.exists()
    assert command("unlock", "--record", str(unlock))["status"] == "UNLOCKED"
    before = fixture.journal.read_bytes()
    assert command("run")["execution_available"] is True
    assert fixture.journal.read_bytes() == before and not pg_system.runner.calls
    first = command("run", "--execute")
    assert first["status"] == "COMPLETED"
    assert len(pg_system.runner.calls) == 12
    artifacts = list((register.root / campaign).glob("execution-*.json"))
    assert len(artifacts) == 1
    original = artifacts[0].read_bytes()
    saved = json.loads(original)
    assert len(saved["conditions"]) == 4
    assert all(len(block["report"]["results"]) == 3 for block in saved["conditions"])
    assert stat.S_IMODE(artifacts[0].stat().st_mode) == 0o600
    # Revoking the real temporary tester must stop before reopening the package.
    before_reopen = fixture.journal.read_bytes()
    with Session(pg_system.parent) as db, db.begin():
        db.get_one(User, CREATOR.user_id).enabled = False
    with pytest.raises(SystemExit) as denied:
        command("run", "--execute")
    assert denied.value.code == 1
    assert capsys.readouterr().err == "FORMAL_TESTER_UNAVAILABLE\n"
    assert fixture.journal.read_bytes() == before_reopen
    assert len(pg_system.runner.calls) == 12
    assert len(list((register.root / campaign).glob("execution-*.json"))) == 1
    with Session(pg_system.parent) as db, db.begin():
        db.get_one(User, CREATOR.user_id).enabled = True
    assert command("run", "--execute") == first
    assert len(pg_system.runner.calls) == 12 and artifacts[0].read_bytes() == original
    assert len(list((register.root / campaign).glob("execution-*.json"))) == 2
    with Session(pg_system.parent) as db:
        rows = db.execute(select(
            EvaluationResult.id, EvaluationResult.case_id, Experiment.formal_condition_key,
        ).join(Experiment, Experiment.id == EvaluationResult.experiment_id)).all()
    ordering = {row.id: (row.case_id, row.formal_condition_key) for row in rows}
    assert [ordering[ident] for ident in pg_system.runner.calls] == [
        (case, key) for case in sorted({row.case_id for row in rows})
        for key in fixture.frozen["draft"]["block_order"]
    ]
    assert str(fixture.package_path) not in json.dumps(first)
