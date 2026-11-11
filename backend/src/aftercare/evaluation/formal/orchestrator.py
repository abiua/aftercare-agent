"""Explicit, bounded formal campaign orchestration and private report output."""

import os
import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine

from ..cli import _runtime
from ..service import _lease
from .contracts import FormalExecutionDeployment, HumanRecord, SingleConditions
from .files import directory, fail, json_data, read_private, write_once

_CONDITIONS = SingleConditions | {"multi/economy", "multi/analysis", "multi/routed"}
_STATUS = {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "INCOMPLETE", "CANCELLED", "NOT_RUN"}
_COUNTS = {"planned", "started", "finished", "not_run", "failed", "timed_out", "cancelled"}
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_SHA256 = re.compile(r"[a-f0-9]{64}")


def _read_private_runtime(name):
    with directory(ROOT / ".runtime") as fd:
        return json_data(read_private(fd, name))


def _deployment(campaign_id, freeze_sha256):
    try:
        config = FormalExecutionDeployment.model_validate(
            _read_private_runtime("formal-execution.json")
        )
    except (BusinessError, OSError, ValueError, RecursionError):
        fail("FORMAL_EXECUTION_CAP_UNAVAILABLE", "正式执行的私有部署上限不可用。")
    if (config.campaign_id, config.freeze_sha256) != (campaign_id, freeze_sha256):
        fail("FORMAL_EXECUTION_CAP_MISMATCH", "部署上限未授权当前冻结批次。")
    return config.execution_cap


def _tester(engine):
    selected_id = os.environ.get("AFTERCARE_FORMAL_TESTER_ID")
    try:
        credentials = _read_private_runtime("users-test.json")
        testers = [
            item for item in credentials
            if item.get("role") == "tester"
            and (selected_id is None or item.get("user_id") == selected_id)
        ]
        if len(testers) != 1:
            raise ValueError("Ambiguous tester credential")
        tester = testers[0]
        raw_token = tester["token"]
        user_id = tester["user_id"]
        if not isinstance(user_id, str) or not isinstance(raw_token, str) or not 32 <= len(raw_token) <= 256:
            raise ValueError("Invalid credential")
        credential_digest = token_hash(raw_token)
    except (AttributeError, BusinessError, KeyError, OSError, TypeError, ValueError, RecursionError):
        fail("FORMAL_TESTER_UNAVAILABLE", "无法取得当前测试员凭据。")
    with Session(engine) as db:
        row = db.scalar(select(User).where(
            User.id == user_id,
            User.token_hash == credential_digest,
            User.enabled.is_(True),
            User.role == "tester",
        ))
        if row is None:
            fail("FORMAL_TESTER_UNAVAILABLE", "当前测试员凭据无效或已撤销。")
        return UserContext(user_id=row.id, role="tester"), credential_digest


def _require_tester():
    """Authorize register operations through the test database without a model runtime."""
    try:
        settings = load_settings("test")
        engine = guarded_engine(settings, test_only=True)
        try:
            _tester(engine)
        finally:
            engine.dispose()
    except (OSError, SQLAlchemyError, ValueError):
        fail("FORMAL_TESTER_UNAVAILABLE", "受保护的测试员身份不可用。")


def freeze_campaign(register, campaign_id, draft, confirmation):
    preview = register.preview_freeze(campaign_id, draft, confirmation)
    freeze_sha256 = preview["prospective_freeze_sha256"]
    cap = _deployment(campaign_id, freeze_sha256)
    _require_tester()
    return register.freeze(
        campaign_id, draft, confirmation,
        execution_cap=cap, expected_freeze_sha256=freeze_sha256,
    )


def unlock_campaign(register, campaign_id, request):
    state = register.preflight(campaign_id)
    if state["purpose"] != "formal":
        return register.unlock(campaign_id, request)
    if state["status"] != "FROZEN" or state["freeze_sha256"] != request.freeze_sha256:
        fail("FORMAL_FREEZE_MISMATCH", "解锁请求与当前冻结记录不一致。")
    cap = _deployment(campaign_id, request.freeze_sha256)
    _require_tester()
    return register.unlock(
        campaign_id, request, execution_cap=cap,
        expected_freeze_sha256=request.freeze_sha256,
    )


def _execution_api():
    from .execution import FormalEvaluationService

    return FormalEvaluationService


def _private_report(register, campaign_id, freeze_sha256, summary, details):
    # Keep one atomic artifact for each observed execution, including incomplete ones.
    conditions = []
    for block, detail in zip(summary["conditions"], details["conditions"], strict=True):
        if (
            detail["id"] != block["experiment_id"]
            or detail["formal_condition_key"] != block["condition_key"]
            or detail["formal_freeze_sha256"] != freeze_sha256
        ):
            fail("FORMAL_EXECUTION_REPORT_INVALID", "正式执行条件报告绑定不一致。")
        conditions.append({**block, "report": detail})
    value = {
        **summary,
        "schema_version": "ac-formal-execution-report-v1",
        "campaign_id": campaign_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "formal": True,
        "formal_freeze_sha256": freeze_sha256,
        "conditions": conditions,
    }
    with register._campaign(campaign_id, writing=True) as (_, fd):
        write_once(fd, f"execution-{uuid4().hex}.json", value)


def _public_summary(campaign_id, freeze_sha256, report):
    conditions = []
    for block in report["conditions"]:
        condition = block["condition_key"]
        if condition not in _CONDITIONS:
            fail("FORMAL_EXECUTION_REPORT_INVALID", "正式执行条件报告格式无效。")
        experiment_id = block.get("experiment_id")
        counts = {
            key: value for key, value in block.get("counts", {}).items()
            if key in _COUNTS and type(value) is int and value >= 0
        }
        conditions.append({
            "condition_key": condition,
            "experiment_id": str(UUID(str(experiment_id))) if experiment_id is not None else None,
            "status": block["status"] if block["status"] in _STATUS else "INCOMPLETE",
            "counts": counts,
        })
    reason = report.get("reason")
    return {
        "campaign_id": campaign_id,
        "freeze_sha256": freeze_sha256,
        "status": report["status"] if report["status"] in _STATUS else "INCOMPLETE",
        "reason": reason if isinstance(reason, str) and _CODE.fullmatch(reason) else None,
        "conditions": conditions,
    }


def run_campaign(register, campaign_id, *, execute=False):
    state = register.preflight(campaign_id)
    if not execute:
        return state
    freeze_sha256 = state.get("freeze_sha256")
    if (
        state.get("execution_available") is not True
        or state.get("status") != "UNLOCKED"
        or state.get("purpose") != "formal"
        or state.get("source_verification") != "NATIVE_VALIDATION_VERIFIED"
        or not isinstance(freeze_sha256, str)
        or not _SHA256.fullmatch(freeze_sha256)
    ):
        fail("FORMAL_EXECUTION_UNAVAILABLE", "当前批次尚不能正式执行。")
    cap = _deployment(campaign_id, freeze_sha256)
    service_class = _execution_api()
    with _runtime("live") as (settings, engine, checkpointer, model):
        if settings.profile != "test" or engine.dialect.name != "postgresql" or model is None:
            fail("FORMAL_EXECUTION_TEST_REQUIRED", "正式执行需要受保护的独立测试环境。")
        # The service acquires its distinct internal lock in this order only.
        with _lease(engine, f"formal-cli:{freeze_sha256}") as held:
            if not held:
                fail("FORMAL_EXECUTION_BUSY", "已有测试员处理这一冻结批次。")
            actor, credential_digest = _tester(engine)
            handoff = register.open_for_execution(
                campaign_id,
                expected_freeze_sha256=freeze_sha256,
                execution_cap=cap,
                operator=HumanRecord(
                    actor=actor.user_id, at=datetime.now(UTC),
                    source="GUARDED_TEST_TESTER_CREDENTIAL",
                ),
            )
            service = service_class(engine, settings, checkpointer, model=model)
            summary = service.run(
                actor,
                freeze_sha256=handoff.freeze_sha256,
                draft=handoff.draft,
                controls=handoff.controls,
                package=handoff.package,
                credential_token_hash=credential_digest,
                execution_cap=cap,
            )
            details = service.private_report(
                actor, freeze_sha256=handoff.freeze_sha256, draft=handoff.draft,
                credential_token_hash=credential_digest,
            )
            _private_report(register, campaign_id, freeze_sha256, summary, details)
            return _public_summary(campaign_id, freeze_sha256, summary)
