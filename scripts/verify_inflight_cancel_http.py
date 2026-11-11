"""One real test Celery request with controlled response delivery during cancellation."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from aftercare.auth import token_hash
from aftercare.business.models import Approval, Execution, Proposal
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine
from aftercare.dispatch import DispatchEvent, DispatchJob
from aftercare.evaluation.tables import EvaluationMutation
from aftercare.intelligence.models import Registry, _reserve_price
from aftercare.intelligence.tables import AgentRun, ModelAttempt, RunEvent
from aftercare.queue import make_queue
from langchain_openai._compat import httpx as sdk_http
from langchain_openai.chat_models import base as langchain_base
from openai import _base_client as sdk_client
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from verify_evaluation_http import CheckFailed, client_for, request, require
from verify_response_clarity_http import reserve, save
from verify_worker_crash import require_quiet

BASE = "http://127.0.0.1:8022"
STATE = ROOT / ".runtime/inflight-cancel-http"
EXPERIMENTS = "/api/evaluation/experiments"
BUDGET = Decimal("0.10")
PROVIDER_URL = "https://api.openai.com/v1/responses"
GATE_SECONDS = 8
WAIT_SECONDS = 155
LOGICAL_ID = "evaluation:intent_probe"
CASE_ID = "AC02-C081"
MAX_REQUEST_BYTES = 4096
MAX_OUTPUT_TOKENS = 900


def stamp():
    return datetime.now(UTC).isoformat()


def read(path):
    return json.loads(path.read_text())


def plan():
    return {
        "name": "One-shot in-flight cancellation acceptance",
        "dataset_id": "v0.2",
        "split": "dev",
        "case_ids": [CASE_ID],
        "mode": "single",
        "strategy": "analysis",
        "source": "live",
        "repetitions": 1,
        "concurrency": 1,
        "max_calls": 2,
        "budget_usd": float(BUDGET),
        "probe_intent": True,
    }


class WireGate:
    """Wrap the real SDK transport and hold its response before SDK parsing."""

    def __init__(self, directory, model):
        self.record = directory / "wire.json"
        self.release = directory / "release.json"
        self.outbound_lock = directory / "outbound.lock"
        self.model = model
        self.evidence = {"outbound_count": 0, "guard_rejections": 0}

    def persist(self, **fields):
        self.evidence.update(fields)
        save(self.record, self.evidence)

    def observed(self, original, transport, outgoing):
        try:
            payload = json.loads(outgoing.content)
            cap = payload.get("max_output_tokens")
            valid = (
                outgoing.method == "POST"
                and str(outgoing.url) == PROVIDER_URL
                and payload.get("model") == self.model
                and type(payload.get("stream", False)) is bool
                and outgoing.headers.get("x-stainless-retry-count") == "0"
                and type(cap) is int
                and 0 < cap <= MAX_OUTPUT_TOKENS
                and len(outgoing.content) <= MAX_REQUEST_BYTES
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid or self.evidence["outbound_count"]:
            self.persist(guard_rejections=self.evidence["guard_rejections"] + 1)
            raise CheckFailed("PROBE_OUTBOUND_SCOPE_REJECTED")
        reserve(self.outbound_lock)
        self.persist(
            outbound_count=1,
            transport_entered_at=stamp(),
            method="POST",
            provider="openai",
            interface="responses",
            model=self.model,
            sent_max_output_tokens=cap,
            sdk_retry_index=0,
            request_body_sha256=hashlib.sha256(outgoing.content).hexdigest(),
            native_streaming=payload.get("stream") is True,
        )
        response = original(transport, outgoing)
        if response.status_code != 200:
            self.persist(headers_at=stamp(), http_status=response.status_code, gate="HTTP_ERROR")
            return response
        self.persist(headers_at=stamp(), http_status=200)
        if self.evidence["native_streaming"]:
            self.persist(gate="AWAITING_PROVIDER_TERMINAL")
        else:
            self.hold("HTTP_HEADERS_BEFORE_RESPONSE_BODY_DELIVERY")
        return response

    def hold(self, boundary):
        self.persist(gate="HOLDING", gate_boundary=boundary, gate_entered_at=stamp())
        deadline = time.monotonic() + GATE_SECONDS
        while time.monotonic() < deadline:
            if self.release.exists():
                self.persist(gate="RELEASED", release_observed_at=stamp())
                return
            time.sleep(0.025)
        self.persist(gate="TIMED_OUT", release_observed_at=stamp())

    def run(self, operation):
        require(getattr(sdk_client, "httpx2", None) is sdk_http, "ACTIVE_TRANSPORT_NOT_HTTPX2")
        original_send = sdk_http.Client.send
        original_handle = sdk_http.HTTPTransport.handle_request
        original_converter = langchain_base._convert_responses_chunk_to_generation_chunk

        def guarded_send(client, outgoing, *args, **kwargs):
            if type(client._transport_for_url(outgoing.url)) is not sdk_http.HTTPTransport:
                self.persist(guard_rejections=self.evidence["guard_rejections"] + 1)
                raise CheckFailed("UNOBSERVED_TRANSPORT_REJECTED")
            kwargs["follow_redirects"] = False
            return original_send(client, outgoing, *args, **kwargs)

        def handle(transport, outgoing):
            return self.observed(original_handle, transport, outgoing)

        def convert(chunk, *args, **kwargs):
            if (
                chunk.type == "response.completed"
                and self.evidence.get("gate") == "AWAITING_PROVIDER_TERMINAL"
            ):
                self.persist(provider_terminal_received_at=stamp())
                self.hold("PROVIDER_COMPLETED_BEFORE_ADAPTER_CONVERSION")
            return original_converter(chunk, *args, **kwargs)

        with (
            patch.object(sdk_http.Client, "send", guarded_send),
            patch.object(sdk_http.HTTPTransport, "handle_request", handle),
            patch.object(langchain_base, "_convert_responses_chunk_to_generation_chunk", convert),
        ):
            return operation()


def fixture_check():
    """Use the actual SDK HTTP client with a replaced transport response, no network."""
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        gate = WireGate(directory, "fixture-model")
        network_entry = []

        def response(_, __):
            network_entry.append(1)
            save(gate.release, {"requested_at": stamp(), "reason": "CONTROLLED_FIXTURE"})
            return sdk_http.Response(200, json={"fixture": True})

        def send():
            with sdk_http.Client(trust_env=False) as client:
                args = {
                    "json": {"model": "fixture-model", "max_output_tokens": 256},
                    "headers": {"x-stainless-retry-count": "0"},
                }
                assert client.post(PROVIDER_URL, **args).status_code == 200
                try:
                    client.post(PROVIDER_URL, **args)
                except CheckFailed:
                    pass
                else:
                    raise AssertionError("second outbound request was not blocked")

        with patch.object(sdk_http.HTTPTransport, "handle_request", response):
            gate.run(send)
        observed = read(gate.record)
        assert network_entry == [1]
        assert observed["sent_max_output_tokens"] == 256 and observed["gate"] == "RELEASED"
        assert observed["outbound_count"] == observed["guard_rejections"] == 1


def fixture_stream_check():
    with TemporaryDirectory() as temporary:
        gate = WireGate(Path(temporary), "fixture-model")

        def send():
            with sdk_http.Client(trust_env=False) as client:
                response = client.post(
                    PROVIDER_URL,
                    json={"model": "fixture-model", "max_output_tokens": 256, "stream": True},
                    headers={"x-stainless-retry-count": "0"},
                )
                assert response.status_code == 200
            assert read(gate.record)["gate"] == "AWAITING_PROVIDER_TERMINAL"
            save(gate.release, {"requested_at": stamp(), "reason": "CONTROLLED_FIXTURE"})
            terminal = SimpleNamespace(type="response.completed")
            assert langchain_base._convert_responses_chunk_to_generation_chunk(terminal) is terminal

        with (
            patch.object(
                sdk_http.HTTPTransport, "handle_request", return_value=sdk_http.Response(200)
            ),
            patch.object(
                langchain_base, "_convert_responses_chunk_to_generation_chunk", lambda chunk: chunk
            ),
        ):
            gate.run(send)
        observed = read(gate.record)
        assert observed["gate"] == "RELEASED"
        assert observed["gate_boundary"] == "PROVIDER_COMPLETED_BEFORE_ADAPTER_CONVERSION"
        assert (
            observed["headers_at"]
            <= observed["provider_terminal_received_at"]
            <= observed["release_observed_at"]
        )


def private_worker():
    from aftercare import queue_delivery
    from aftercare.worker import app

    require(os.environ.get("AFTERCARE_PROFILE") == "test", "PRIVATE_WORKER_REQUIRES_TEST")
    state = read(STATE / "state.json")
    require(state.get("experiment_id") and state.get("worker_name"), "PRIVATE_TARGET_MISSING")
    reserve(STATE / "worker.lock")
    gate = WireGate(STATE, state["model"])
    original = queue_delivery.process_target

    def process(profile, kind, target_id, job_id=None):
        require(
            (profile, kind, target_id) == ("test", "evaluation", state["experiment_id"]),
            "PRIVATE_WORKER_TARGET_REJECTED",
        )
        return original(profile, kind, target_id, job_id)

    with patch.object(queue_delivery, "process_target", process):
        gate.run(
            lambda: app.worker_main(
                [
                    "worker",
                    "--pool=solo",
                    "--concurrency=1",
                    "--queues=aftercare-test",
                    f"--hostname={state['worker_name']}@%h",
                    "--loglevel=WARNING",
                    "--without-mingle",
                    "--without-gossip",
                ]
            )
        )


def preflight(engine, settings, client, tester):
    require(
        not settings.container
        and settings.model_enabled
        and settings.redis_url == "redis://127.0.0.1:56380/0"
        and settings.object_endpoint == "http://127.0.0.1:58351",
        "LIVE_HOST_TEST_TARGET_REQUIRED",
    )
    me = request(client, "/api/me")
    require(me["role"] == "tester" and me["user_id"] == tester["user_id"], "TESTER_HTTP_MISMATCH")
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.token_hash == token_hash(tester["token"])))
        require(
            user and user.enabled and user.role == "tester" and user.id == me["user_id"],
            "TESTER_DB_MISMATCH",
        )
    runtime = request(client, "/api/runtime")
    require(runtime["mode"] == "live", "TEST_API_MODE_NOT_LIVE")
    analysis = next((m for m in runtime["models"] if m["alias"] == "analysis"), {})
    require(
        analysis.get("available") and analysis.get("compatibility") == "VERIFIED",
        "ANALYSIS_MODEL_UNAVAILABLE",
    )
    registry = Registry.read(settings.model_config_path)
    spec = next((model for model in registry.models if model.alias == "analysis"), None)
    require(
        spec is not None
        and spec.model == analysis["model"]
        and spec.interface == "responses"
        and spec.provider == "openai"
        and spec.endpoint.rstrip("/") + "/responses" == PROVIDER_URL
        and spec.output_tokens >= MAX_OUTPUT_TOKENS,
        "LOCAL_ANALYSIS_REGISTRY_MISMATCH",
    )
    estimate = _reserve_price(spec, MAX_REQUEST_BYTES, MAX_OUTPUT_TOKENS)
    require(estimate is not None and 0 < estimate <= BUDGET, "ONE_REQUEST_RESERVATION_REJECTED")
    case_list = request(client, "/api/evaluation/cases?dataset=v0.2&split=dev")
    require(any(row["id"] == CASE_ID for row in case_list["items"]), "DEV_CASE_UNAVAILABLE")
    return analysis["model"]


def snapshot(engine, experiment_id, result_id):
    namespace = "eval_" + UUID(result_id).hex
    with Session(engine) as db:
        job = db.scalar(
            select(DispatchJob).where(
                DispatchJob.kind == "evaluation", DispatchJob.target_id == experiment_id
            )
        )
        dispatch = (
            None
            if job is None
            else {
                "id": job.id,
                "generation": job.generation,
                "owner": job.owner,
                "claims": job.claims,
                "lease_until": job.lease_until.isoformat() if job.lease_until else None,
                "state": job.state,
                "failures": job.failures,
                "events": [
                    {"state": event.state, "owner": event.owner, "at": event.created_at.isoformat()}
                    for event in db.scalars(
                        select(DispatchEvent)
                        .where(DispatchEvent.job_id == job.id)
                        .order_by(DispatchEvent.created_at)
                    )
                ],
            }
        )
    result = {
        "observed_at": stamp(),
        "dispatch": dispatch,
        "namespace": namespace,
        "attempts": [],
        "run": None,
        "business": None,
        "run_event_kinds": [],
    }
    if not all(
        inspect(engine).has_table(name, schema=namespace)
        for name in ("agent_runs", "agent_model_attempts")
    ):
        return result
    scoped = engine.execution_options(schema_translate_map={None: namespace})
    with Session(scoped) as db:
        runs = list(db.scalars(select(AgentRun)))
        require(len(runs) <= 1, "MORE_THAN_ONE_AGENT_RUN")
        if runs:
            run = runs[0]
            result["run"] = {
                key: getattr(run, key)
                for key in ("id", "thread_id", "cycle", "status", "reason", "output")
            }
        result["attempts"] = [
            {
                "run_id": row.run_id,
                "cycle": row.cycle,
                "logical_id": row.logical_id,
                "ordinal": row.ordinal,
                "status": row.status,
                "error": row.error_code,
                "started_at": row.started_at.isoformat(),
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "usage": row.usage,
                "estimated_cost_usd": row.estimated_cost_usd,
                "reserved_cost_usd": row.metadata_json.get("reserved_cost_usd"),
            }
            for row in db.scalars(select(ModelAttempt).order_by(ModelAttempt.started_at))
        ]
        result["business"] = {
            table.__tablename__: len(db.scalars(select(table.id)).all())
            for table in (Approval, Execution, Proposal)
        }
        result["run_event_kinds"] = [
            {"seq": row.seq, "kind": row.kind}
            for row in db.scalars(select(RunEvent).order_by(RunEvent.seq))
        ]
    return result


def launch_worker(state):
    name = "aftercare-inflight-" + uuid4().hex[:12]
    state["worker_name"] = name
    save(STATE / "state.json", state)
    descriptor = os.open(STATE / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        env = {**os.environ, "AFTERCARE_PROFILE": "test", "PYTHONPATH": str(ROOT / "backend/src")}
        env.update(LANGCHAIN_TRACING_V2="false", LANGSMITH_TRACING="false")
        env.pop("AFTERCARE_OPENAI_KEY", None)
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--private-worker"],
            cwd=ROOT / "backend",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def wire_readback():
    path = STATE / "wire.json"
    return read(path) if path.exists() else None


def release(reason):
    path = STATE / "release.json"
    if not path.exists():
        save(path, {"requested_at": stamp(), "reason": reason})
    return read(path)


def observe(client, engine, state):
    experiment = request(client, f"{EXPERIMENTS}/{state['experiment_id']}")
    require(len(experiment["results"]) == 1, "EXPECTED_ONE_EVALUATION_RESULT")
    row = experiment["results"][0]
    require(row["case_id"] == CASE_ID, "UNEXPECTED_EVALUATION_CASE")
    state["result_id"] = row["id"]
    return experiment, row, snapshot(engine, state["experiment_id"], state["result_id"])


def settled(row, raw):
    return row["status"] not in {"QUEUED", "RUNNING"} and (
        raw["dispatch"] is not None and raw["dispatch"]["state"] in {"DONE", "FAILED", "CANCELLED"}
    )


def wait_settled(client, engine, state, report, persist):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        experiment, row, raw = observe(client, engine, state)
        report.update(
            experiment_status=experiment["status"],
            raw_final=raw,
            result_snapshot=row,
            wire=wire_readback(),
        )
        persist()
        if settled(row, raw):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def assess(report):
    before, final = report["before_cancel"], report["raw_final"]
    attempts = final["attempts"]
    wire = report["wire"] or {}
    row = report["result_snapshot"]
    run = final["run"] or {}
    returned = (row.get("actual") or {}).get("model_attempts", [])
    attempt = attempts[0] if len(attempts) == 1 else {}
    saved = returned[0] if len(returned) == 1 else {}
    cost = attempt.get("estimated_cost_usd")
    complete_usage = isinstance(attempt.get("usage"), dict) and all(
        type(attempt["usage"].get(field)) is int
        for field in ("input_tokens", "output_tokens", "total_tokens")
    )
    report["usage_status"] = "PROVIDER_REPORTED" if complete_usage else "UNKNOWN"
    report["estimated_known_cost_usd"] = cost
    report["unknown_cost_attempts"] = sum(a["estimated_cost_usd"] is None for a in attempts)
    cancel = report["cancel_response"]
    timing = wire.get("release_observed_at") and attempt.get("finished_at")
    checks = {
        "one_outbound_http_response": wire.get("outbound_count") == 1
        and wire.get("http_status") == 200
        and wire.get("guard_rejections") == 0,
        "response_late_relative_to_cancel": bool(
            timing
            and wire["gate"] == "RELEASED"
            and wire["headers_at"]
            <= cancel["requested_at"]
            <= cancel["observed_at"]
            <= report["release"]["requested_at"]
            <= wire["release_observed_at"]
            <= attempt["finished_at"]
        ),
        "same_run_and_attempt": bool(
            len(attempts) == 1
            and before["run"]["id"] == run.get("id")
            and before["run"]["thread_id"] == run.get("thread_id")
            and all(
                before["attempts"][0][k] == attempt[k]
                for k in ("run_id", "cycle", "logical_id", "ordinal")
            )
        ),
        "cancelled_without_publication": report["experiment_status"] == "CANCELLED"
        and row["status"] == "ERROR"
        and row["failure_reason"] == "EXPERIMENT_CANCELLED"
        and run.get("status") == "CANCELED"
        and run.get("reason") == "EXPERIMENT_CANCELLED"
        and run.get("output") is None
        and not any(e["kind"] == "tool" for e in final["run_event_kinds"]),
        "business_unchanged": before["business"] == final["business"]
        and not any(final["business"].values()),
        "valid_lease_one_claim": before["dispatch"]["owner"]
        and report["after_cancel"]["dispatch"]["owner"] == before["dispatch"]["owner"]
        and final["dispatch"]["id"] == before["dispatch"]["id"]
        and final["dispatch"]["generation"] == before["dispatch"]["generation"]
        and final["dispatch"]["state"] == "DONE"
        and final["dispatch"]["claims"] == 1
        and not any(e["state"] == "LEASE_EXPIRED" for e in final["dispatch"]["events"]),
        "late_usage_matches_snapshot": complete_usage
        and attempt.get("status") in {"SUCCEEDED", "UNKNOWN"}
        and cost is not None
        and 0 <= Decimal(cost) <= BUDGET
        and all(
            saved.get(field) == attempt[field]
            for field in ("logical_id", "ordinal", "status", "usage", "estimated_cost_usd")
        ),
    }
    report["checks"] = {key: bool(value) for key, value in checks.items()}
    report["status"] = (
        "PASSED"
        if all(report["checks"].values())
        else ("UNKNOWN" if report["usage_status"] == "UNKNOWN" else "FAILED")
    )


def run_paid(client, engine, queue, state, report, persist):
    require_quiet(engine, queue)
    state["phase"] = "CREATE_PENDING"
    persist()
    try:
        experiment = request(
            client, EXPERIMENTS, expected=201, body=plan(), key=state["create_key"]
        )
        state["experiment_id"] = str(UUID(experiment["id"]))
    except Exception:
        with Session(engine) as db:
            mutation = db.get(
                EvaluationMutation, (state["tester_id"], "create", state["create_key"])
            )
            if mutation:
                state["experiment_id"] = str(UUID(mutation.object_id))
        raise
    require(
        len(experiment["results"]) == 1
        and experiment["config"]["execution_driver"] == "queued"
        and experiment["config"]["case_ids"] == [CASE_ID]
        and experiment["results"][0]["quota"]["calls"] == 2
        and Decimal(experiment["results"][0]["quota"]["budget_usd"]) <= BUDGET,
        "EVALUATION_QUOTA_OR_DRIVER_MISMATCH",
    )
    state["result_id"] = str(UUID(experiment["results"][0]["id"]))
    state["phase"] = "EXPERIMENT_CREATED"
    persist()

    worker = None
    observed_terminal = False
    try:
        worker = launch_worker(state)
        state["worker_pid"] = worker.pid
        persist()
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            wire = wire_readback()
            if wire is not None:
                report["wire"] = wire
                persist()
                break
            require(worker.poll() is None, "PRIVATE_WORKER_EXITED")
            require(time.monotonic() < deadline, "NO_ACTUAL_TRANSPORT_OBSERVED")
            time.sleep(0.1)
        while wire.get("gate") in {None, "AWAITING_PROVIDER_TERMINAL"}:
            require(
                worker.poll() is None and time.monotonic() < deadline, "NO_HTTP_HEADERS_OBSERVED"
            )
            time.sleep(0.05)
            wire = wire_readback()
        require(
            wire.get("gate") == "HOLDING" and wire["http_status"] == 200, "NO_OPEN_RESPONSE_GATE"
        )
        experiment, row, raw = observe(client, engine, state)
        attempts, job = raw["attempts"], raw["dispatch"]
        require(
            experiment["status"] == row["status"] == "RUNNING"
            and job
            and job["state"] == "RUNNING"
            and job["owner"]
            and job["claims"] == 1
            and job["lease_until"]
            and job["lease_until"] > stamp()
            and raw["run"]
            and len(attempts) == 1
            and attempts[0]["logical_id"] == LOGICAL_ID
            and attempts[0]["status"] == "STARTED"
            and attempts[0]["usage"] is None
            and attempts[0]["reserved_cost_usd"] is not None
            and 0 < Decimal(attempts[0]["reserved_cost_usd"]) <= BUDGET
            and wire_readback()["gate"] == "HOLDING",
            "INFLIGHT_BEFORE_CANCEL_NOT_OBSERVED",
        )
        report["before_cancel"] = raw
        report["wire"] = wire
        state.update(phase="CANCEL_POST_PENDING", cancel_version=experiment["version"])
        report["cancel_requested_at"] = stamp()
        persist()
        cancelled = request(
            client,
            f"{EXPERIMENTS}/{state['experiment_id']}/cancel",
            body={"expected_version": experiment["version"]},
            key=state["cancel_key"],
        )
        report["cancel_response"] = {
            "requested_at": report["cancel_requested_at"],
            "observed_at": stamp(),
            "status": cancelled["status"],
            "updated_at": cancelled["updated_at"],
        }
        require(cancelled["status"] == "CANCELLED", "CANCEL_NOT_COMMITTED")
        state["phase"] = "CANCEL_COMMITTED"
        report["after_cancel"] = snapshot(engine, state["experiment_id"], state["result_id"])
        persist()
        after = report["after_cancel"]
        require(
            after["dispatch"]
            and after["dispatch"]["owner"] == job["owner"]
            and after["dispatch"]["lease_until"] > stamp()
            and len(after["attempts"]) == 1
            and after["attempts"][0]["status"] == "STARTED"
            and after["attempts"][0]["usage"] is None
            and wire_readback()["gate"] == "HOLDING",
            "LATE_USAGE_WINDOW_NOT_OBSERVED",
        )
        report["release"] = release("CANCEL_COMMITTED")
        persist()
        observed_terminal = wait_settled(client, engine, state, report, persist)
        require(observed_terminal, "SAME_HANDLE_TERMINAL_NOT_OBSERVED")
        assess(report)
    finally:
        report["release"] = release("FINALLY_UNBLOCK")
        report["wire"] = wire_readback()
        if worker is not None:
            if observed_terminal and worker.poll() is None:
                worker.terminate()  # only this script's private, now terminal Worker
                try:
                    worker.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    report["private_worker_disposition"] = "LEFT_RUNNING"
                else:
                    report["private_worker_disposition"] = "STOPPED_AFTER_TERMINAL"
            else:
                report["private_worker_disposition"] = (
                    "LEFT_RUNNING_READ_SAME_HANDLE" if worker.poll() is None else "EXITED"
                )
        persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute-paid", action="store_true")
    mode.add_argument("--fixture-check", action="store_true")
    mode.add_argument(
        "--readback", action="store_true", help="Read the same existing handle without writes"
    )
    mode.add_argument("--private-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.execute_paid != (args.output is not None):
        parser.error("--execute-paid requires a new --output; other modes produce no report file")
    if args.fixture_check:
        fixture_check()
        fixture_stream_check()
        print(
            json.dumps(
                {
                    "status": "FIXTURE_PASSED",
                    "network_calls": 0,
                    "boundaries": ["http_headers", "native_terminal_conversion"],
                }
            )
        )
        return 0
    if args.private_worker:
        private_worker()
        return 0
    if args.output is not None and STATE.resolve() in (
        args.output.resolve(),
        *args.output.resolve().parents,
    ):
        parser.error("--output must be outside the private state directory")

    state = {"create_key": str(uuid4()), "cancel_key": str(uuid4())}
    report = {
        "status": "NOT_RUN",
        "scope": "real_test_http_celery_with_controlled_provider_response_delivery",
        "maximum_outbound_requests": 1,
        "evaluation_max_calls": 2,
        "estimated_budget_usd": str(BUDGET),
        "forced_lease_loss": "NOT_RUN",
    }
    reserved = False

    def persist():
        if reserved:
            save(STATE / "state.json", state)
            save(args.output, report)

    try:
        if args.execute_paid:
            require(not os.path.lexists(STATE), "PRIVATE_STATE_EXISTS_USE_READBACK")
            reserve(args.output)
            STATE.mkdir(mode=0o700)
            reserved = True
            persist()
        settings = load_settings("test")
        engine = guarded_engine(settings, test_only=True)
        try:
            tester = next(
                row for row in read(ROOT / ".runtime/users-test.json") if row["role"] == "tester"
            )
            with client_for(BASE, tester["token"]) as client:
                state["tester_id"] = tester["user_id"]
                state["model"] = preflight(engine, settings, client, tester)
                if args.readback:
                    state = read(STATE / "state.json")
                    experiment, row, raw = observe(client, engine, state)
                    print(
                        json.dumps(
                            {
                                "experiment_id": state["experiment_id"],
                                "experiment_status": experiment["status"],
                                "result_status": row["status"],
                                "result_snapshot": row,
                                "raw_audit": raw,
                                "wire": wire_readback(),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0 if settled(row, raw) else 1
                if args.execute_paid:
                    report["status"] = "IN_PROGRESS"
                    run_paid(client, engine, make_queue("test"), state, report, persist)
                else:
                    report["status"] = "PREFLIGHT_PASSED"
                    report["regular_worker_must_be_stopped_before_paid_run"] = True
        finally:
            engine.dispose()
    except Exception as error:  # noqa: BLE001 - report safe codes only
        report.update(
            status="UNKNOWN" if state.get("phase") else "FAILED",
            failure=str(error) if isinstance(error, CheckFailed) else type(error).__name__,
        )
    finally:
        persist()
    print(
        json.dumps(
            {
                "status": report["status"],
                "failure": report.get("failure"),
                "experiment_id": state.get("experiment_id"),
                "worker_pid": state.get("worker_pid"),
                "maximum_outbound_requests": 1,
                "estimated_budget_usd": str(BUDGET),
            },
            ensure_ascii=False,
        )
    )
    return int(report["status"] not in {"PASSED", "PREFLIGHT_PASSED"})


if __name__ == "__main__":
    raise SystemExit(main())
