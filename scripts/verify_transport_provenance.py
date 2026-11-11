"""Preflight, or explicitly run, four one-shot default Responses transport checks."""

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import HumanMessage
from sqlalchemy import URL, create_engine, select
from sqlalchemy.orm import Session

from aftercare.db import Base, User
from aftercare.intelligence import models
from aftercare.intelligence.service import RunAudit
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from verify_streaming_compatibility import (
    API_URL,
    MODELS,
    ROOT,
    ProbeError,
    _configuration,
    _private_json,
    _usage,
    provider_httpx,
)

OUTPUT_CAP = 900
BODY_CAP = 2048
TOTAL_CAP = Decimal("0.05")
TIMEOUT = 30
DEADLINE = 35
MAX_POSTS = len(MODELS) * 2
PROMPT = 'Synthetic transport test. Reply with exactly {"ok":true} as JSON.'
LOGICAL_ID = "synthetic-provenance"
ACTOR_ID = "transport-probe-support"
EVIDENCE_VERSION = "ac-attempt-transport-v1"


@dataclass(frozen=True)
class Probe:
    spec: models.ModelSpec
    stream: bool
    reserve: Decimal
    wire_reserve: Decimal

    @property
    def path(self):
        return "DEFAULT_STREAM" if self.stream else "DEFAULT_SYNC"

    def registry(self):
        return models.Registry(
            version="synthetic-provenance-v1",
            models=[self.spec],
            allowed_providers=["openai"],
            max_attempts=1,
            max_run_attempts=1,
            max_usd=self.reserve,
            max_run_usd=self.reserve,
            timeout_seconds=TIMEOUT,
            deadline_seconds=DEADLINE,
            native_streaming=self.stream,
        )


class RedactedRunAudit(RunAudit):
    """Use real admission and persistence. Remove only streamed draft text from events."""

    def event(self, kind, payload):
        if kind == "model_draft_delta":
            payload = {key: value for key, value in payload.items() if key != "text"}
            payload["text_omitted"] = True
        return super().event(kind, payload)


def _messages():
    return [HumanMessage(content=PROMPT)]


def _plan():
    specs = _configuration()
    if getattr(models, "TRANSPORT_EVIDENCE_VERSION", None) != EVIDENCE_VERSION:
        raise ProbeError("TRANSPORT_EVIDENCE_VERSION_REQUIRED")
    serialized = json.dumps([message.model_dump(mode="json") for message in _messages()], ensure_ascii=False)
    prompt_count = len(serialized.encode()) + 256
    if prompt_count > BODY_CAP:
        raise ProbeError("PROMPT_ALLOWANCE_REJECTED")
    probes = []
    for name in MODELS:
        spec = specs[name]
        reserve = models._reserve_price(spec, prompt_count, OUTPUT_CAP)
        wire_reserve = models._reserve_price(spec, BODY_CAP, OUTPUT_CAP)
        if reserve is None or wire_reserve is None or not Decimal(0) < reserve <= wire_reserve:
            raise ProbeError("FROZEN_PRICE_REQUIRED")
        for stream in (False, True):
            if stream and not spec.streaming_usage:
                raise ProbeError("VERIFIED_STREAM_USAGE_REQUIRED")
            probe = Probe(spec, stream, reserve, wire_reserve)
            task = models.TaskSpec(spec.alias, prompt_count, tools=False, streaming=stream)
            if task.output_reserve != OUTPUT_CAP:
                raise ProbeError("DEFAULT_RUNNER_OUTPUT_CAP_CHANGED")
            candidates, _ = models.eligible_models(probe.registry(), task)
            if candidates != [spec]:
                raise ProbeError("EXACT_MODEL_PATH_NOT_ELIGIBLE")
            probes.append(probe)
    if sum(probe.wire_reserve for probe in probes) > TOTAL_CAP:
        raise ProbeError("TOTAL_ESTIMATED_RESERVATION_EXCEEDS_CAP")
    return probes, prompt_count


def _engine(path):
    engine = create_engine(URL.create("sqlite", database=str(path)), hide_parameters=True)
    Base.metadata.create_all(engine, tables=[User.__table__, AgentRun.__table__, RunEvent.__table__, ModelAttempt.__table__])
    with Session(engine) as db, db.begin():
        db.add(User(id=ACTOR_ID, token_hash="0" * 64, role="support", display_name="Synthetic probe", enabled=True))
    return engine


def _new_run(engine, probe, run_id):
    stamp = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as db, db.begin():
        run = AgentRun(
            id=run_id, thread_id=f"synthetic:{run_id}", case_id=f"synthetic:{run_id}",
            owner_id=ACTOR_ID, actor={"user_id": ACTOR_ID, "role": "support"},
            status="RUNNING", mode="single", strategy=probe.spec.alias, message="synthetic",
            case_version=1, cycle=0, deadline=stamp + timedelta(seconds=DEADLINE),
            created_at=stamp, updated_at=stamp,
        )
        db.add(run)
    return RedactedRunAudit(engine, run)


def _attempts(engine, run_id):
    with Session(engine) as db:
        return list(db.scalars(select(ModelAttempt).where(ModelAttempt.run_id == run_id)))


def _transport_evidence(probe):
    return {"version": EVIDENCE_VERSION, "path": probe.path, "response": "UNOBSERVED"}


def _check_started(engine, probe, run_id):
    attempts = _attempts(engine, run_id)
    if len(attempts) != 1:
        raise ProbeError("SINGLE_PERSISTED_ATTEMPT_REQUIRED")
    attempt = attempts[0]
    metadata = attempt.metadata_json
    if (
        (attempt.cycle, attempt.logical_id, attempt.ordinal, attempt.status) != (0, LOGICAL_ID, 1, "STARTED")
        or metadata.get("run_id") != run_id
        or metadata.get("model") != probe.spec.model
        or metadata.get("reserved_cost_usd") != str(probe.reserve)
        or metadata.get("transport_evidence") != _transport_evidence(probe)
    ):
        raise ProbeError("PERSISTED_PRE_SEND_EVIDENCE_MISMATCH")


def _preflight_storage(probes):
    with TemporaryDirectory(prefix="aftercare-transport-preflight-") as directory:
        path = Path(directory)
        report = path / "offline.json"
        with os.fdopen(_exclusive(report), "w") as handle:
            _durable_write(handle, {"wire_requests": 0})
        _save(report, {"wire_requests": 0})
        engine = _engine(path / "audit.sqlite")
        try:
            for probe in probes:
                run_id = str(uuid4())
                audit = _new_run(engine, probe, run_id)
                registry = probe.registry()
                audit.begin_attempt(LOGICAL_ID, 1, probe.spec, probe.reserve, registry,
                                    transport_evidence=_transport_evidence(probe))
                _check_started(engine, probe, run_id)
                try:
                    audit.begin_attempt(LOGICAL_ID, 2, probe.spec, probe.reserve, registry)
                except models.ModelFailure as error:
                    if error.code != "MODEL_BUDGET_EXHAUSTED":
                        raise
                else:
                    raise ProbeError("SECOND_ATTEMPT_WAS_NOT_REJECTED")
        finally:
            engine.dispose()


def _durable_write(handle, value):
    json.dump(value, handle, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


def _exclusive(path):
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path, report):
    fd, temporary = mkstemp(prefix=".transport-provenance-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            _durable_write(handle, report)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _validate(calls, content):
    value = json.loads(content)
    if calls or not isinstance(value, dict) or set(value) != {"ok"} or value["ok"] is not True:
        raise models.InvalidOutput("PROBE_JSON_MISMATCH", "unknown_field")
    # Persist a marker only. The raw response and draft text stay out of the ledger.
    return {"result": {"probe_json_valid": True}}


def _guarded_wire(engine, probe, run_id, row, report, output):
    original_send = provider_httpx.Client.send
    original_handle = provider_httpx.HTTPTransport.handle_request
    expected_payload = {
        "model": probe.spec.model, "stream": probe.stream, "store": False, "max_output_tokens": OUTPUT_CAP,
        "input": [{"content": PROMPT, "role": "user", "type": "message"}],
    }

    def reject(code):
        row["wire_guard_code"] = code
        _save(output, report)
        raise ProbeError(code)

    def send(client, request, *args, **kwargs):
        transport = client._transport_for_url(request.url)
        if type(transport) is not provider_httpx.HTTPTransport:
            reject("UNOBSERVED_HTTP_TRANSPORT_REJECTED")
        if getattr(transport._pool, "_retries", None) != 0:
            reject("HTTP_TRANSPORT_RETRIES_NOT_ZERO")
        kwargs["follow_redirects"] = False
        return original_send(client, request, *args, **kwargs)

    def handle(transport, request):
        if row["wire_requests"] or report["wire_requests"] >= MAX_POSTS:
            reject("WIRE_REQUEST_LIMIT_REJECTED")
        if (
            request.method != "POST"
            or str(request.url) != API_URL + "/responses"
            or request.headers.get("x-stainless-retry-count") != "0"
            or len(request.content) > BODY_CAP
            or json.loads(request.content) != expected_payload
        ):
            reject("OFFICIAL_REQUEST_SCOPE_REJECTED")
        try:
            _check_started(engine, probe, run_id)
        except ProbeError as error:
            reject(str(error))
        row.update(wire_requests=1, wire_status="DELIVERY_UNKNOWN", started_audit_observed=True,
                   sent_max_output_tokens=OUTPUT_CAP, sdk_retry_index=0, serialized_body_bytes=len(request.content))
        report["wire_requests"] += 1
        _save(output, report)
        response = original_handle(transport, request)
        row.update(http_status=response.status_code, wire_status="HTTP_RESPONSE_OBSERVED")
        _save(output, report)
        return response

    return patch.object(provider_httpx.Client, "send", send), patch.object(provider_httpx.HTTPTransport, "handle_request", handle)


def _run_probe(engine, probe, row, report, output):
    run_id = str(uuid4())
    audit = _new_run(engine, probe, run_id)
    row.update(run_id=run_id, status="STARTED")
    _save(output, report)
    try:
        send, handle = _guarded_wire(engine, probe, run_id, row, report, output)
        with send, handle:
            models.ModelRunner(probe.registry()).call(
                audit, LOGICAL_ID, preferred=probe.spec.alias, messages=_messages(),
                tools=[], validate=_validate, fixed=True,
            )
    except models.ModelFailure as error:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error.code):
            row["runner_code"] = error.code

    attempts = _attempts(engine, run_id)
    if len(attempts) != 1:
        row["status"] = "FAILED"
        _save(output, report)
        return False
    attempt = attempts[0]
    evidence = attempt.metadata_json.get("transport_evidence") or {}
    usage = _usage(attempt.usage)
    cost = Decimal(attempt.estimated_cost_usd) if attempt.estimated_cost_usd is not None else None
    row.update(
        audit_status=attempt.status, transport_evidence={
            key: evidence[key] for key in ("version", "path", "response", "http_status") if key in evidence
        }, usage=usage, estimated_cost_usd=str(cost) if cost is not None else None,
    )
    passed = (
        row["wire_requests"] == 1 and row.get("http_status") == 200
        and attempt.status == "SUCCEEDED"
        and evidence == {**_transport_evidence(probe), "response": "SDK_RETURNED"}
        and usage is not None and {"input_tokens", "output_tokens", "total_tokens"} <= usage.keys()
        and usage["input_tokens"] <= BODY_CAP and usage["output_tokens"] <= OUTPUT_CAP
        and cost is not None and Decimal(0) <= cost <= probe.wire_reserve
    )
    unknown = attempt.status in {"STARTED", "UNKNOWN"} or (attempt.status == "SUCCEEDED" and cost is None)
    row["status"] = "PASSED" if passed else "UNKNOWN" if unknown else "FAILED"
    with Session(engine) as db, db.begin():
        run = db.get(AgentRun, run_id)
        run.status = "COMPLETED" if passed else "NEEDS_REVIEW"
        run.updated_at = datetime.now(UTC)
    _save(output, report)
    return passed


def _execute(output, probes, report):
    audit_path = output.with_name(output.name + ".sqlite")
    if os.path.lexists(audit_path):
        raise FileExistsError
    # Reserve both durable names before reading the secret. Never resume this batch.
    with os.fdopen(_exclusive(output), "w") as handle:
        _durable_write(handle, report)
    os.close(_exclusive(audit_path))
    _sync_directory(output.parent)
    secret = _private_json(ROOT / ".runtime" / "model-secret.json")
    key = secret.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ProbeError("MISSING_PROJECT_MODEL_KEY")
    engine = _engine(audit_path)
    logging.disable(logging.CRITICAL)
    try:
        with patch.dict(
            os.environ,
            {
                "AFTERCARE_OPENAI_KEY": key,
                "OPENAI_LOG": "",
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
                "LANGSMITH_OTEL_ENABLED": "false",
                "OTEL_SDK_DISABLED": "true",
            },
        ):
            for probe, row in zip(probes, report["attempts"], strict=True):
                if not _run_probe(engine, probe, row, report, output):
                    break
    finally:
        engine.dispose()
    rows = report["attempts"]
    known_cost = sum((Decimal(row["estimated_cost_usd"]) for row in rows if row["estimated_cost_usd"] is not None), Decimal(0))
    report.update(
        status="PASSED" if all(row["status"] == "PASSED" for row in rows) and known_cost <= TOTAL_CAP
        else "UNKNOWN" if any(row["status"] == "UNKNOWN" for row in rows) else "FAILED",
        estimated_known_cost_usd=str(known_cost),
        unknown_cost_wire_attempts=sum(row["wire_requests"] and row["estimated_cost_usd"] is None for row in rows),
    )
    _save(output, report)
    print(json.dumps({"status": report["status"], "wire_requests": report["wire_requests"],
                      "estimated_known_cost_usd": report["estimated_known_cost_usd"],
                      "unknown_cost_wire_attempts": report["unknown_cost_wire_attempts"]}))
    return 0 if report["status"] == "PASSED" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Validate configuration and local audit only (default)")
    mode.add_argument("--execute", action="store_true", help="Allow real provider POSTs after separate authorization")
    parser.add_argument("--output", type=Path, help="New private report path required only for execution")
    args = parser.parse_args()
    if args.execute != (args.output is not None):
        parser.error("--execute and --output must be used together")
    output = args.output.expanduser().absolute() if args.output is not None else None
    if output is not None and (os.path.lexists(output) or os.path.lexists(output.with_name(output.name + ".sqlite"))):
        raise FileExistsError
    os.umask(0o077)
    probes, prompt_count = _plan()
    _preflight_storage(probes)
    report = {
        "status": "IN_PROGRESS" if args.execute else "PREFLIGHT_ONLY",
        "scope": "synthetic transport provenance; no business or quality evaluation",
        "at": datetime.now(UTC).isoformat(), "wire_requests": 0, "maximum_wire_requests": MAX_POSTS,
        "automatic_sdk_retries": 0, "maximum_attempts_per_run": 1, "output_token_cap": OUTPUT_CAP,
        "transport_evidence_version": EVIDENCE_VERSION, "streamed_draft_text_retained": False,
        "actual_serialized_prompt_allowance": prompt_count, "wire_body_cap_bytes": BODY_CAP,
        "total_reservation_limit_usd": str(TOTAL_CAP),
        "estimated_runner_reservation_total_usd": str(sum(probe.reserve for probe in probes)),
        "estimated_wire_reservation_total_usd": str(sum(probe.wire_reserve for probe in probes)),
        "private_sqlite_preflight": "PASSED",
        "attempts": [{"model": probe.spec.model, "path": probe.path, "status": "NOT_RUN", "wire_requests": 0,
                      "wire_status": "NOT_SENT", "estimated_cost_usd": None,
                      "runner_reservation_usd": str(probe.reserve), "wire_reservation_usd": str(probe.wire_reserve)}
                     for probe in probes],
    }
    if not args.execute:
        print(json.dumps(report, indent=2))
        return 0
    return _execute(output, probes, report)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileExistsError:
        raise SystemExit("TRANSPORT_PROVENANCE_OUTPUT_ALREADY_EXISTS") from None
    except ProbeError as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit("TRANSPORT_PROVENANCE_INTERRUPTED_DO_NOT_REPLAY") from None
    except Exception:  # noqa: BLE001 - never print SDK, filesystem or private config exception text
        raise SystemExit("TRANSPORT_PROVENANCE_STOPPED_REVIEW_PRIVATE_STATE") from None
