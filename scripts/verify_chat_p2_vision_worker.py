"""One approved real-provider request through an isolated P2 chat worker on port 5192."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from shutil import copyfile
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID

import httpx
import offline_demo
from aftercare.intelligence import models, vision
from sqlalchemy.engine import make_url
from verify_chat_p1_http import call, message, read, request_ids
from verify_chat_p2_offline import P2Stack, _begin, _cards, _stats, _wait, owned_container
from verify_fresh_bootstrap import LABEL, VerificationError, require
from verify_streaming_compatibility import ROOT, _configuration, _private_json, _save
from verify_synthetic_vision_v1 import _read

PRIOR_STATE = ROOT / ".runtime" / "offline-browser-demo-p2-vision-worker.json"
PRIOR_ADMISSION = ROOT / ".runtime" / "chat-p2-vision-worker-20261111-admission"
PRIOR_REPORT = ROOT / "reports" / "chat-p2-vision-worker-live-20261111.json"
PRIOR_REPORT_SHA = "9f6938728208ccc021c68e60f6902ad6333a38678e063ee5add058128d931b2a"
PRIOR_RUN_ID = "58dd11b17ad74a22a7e217717b14e5ca"
STATE = ROOT / ".runtime" / "offline-browser-demo-p2-vision-worker-resume.json"
ADMISSION = ROOT / ".runtime" / "chat-p2-vision-worker-20261111-resume-admission"
BACKEND = "aftercare-backend:chat-p2-20261111"
FRONTEND = "aftercare-frontend:chat-p2-20261111"
IMAGES = {BACKEND: "c784887997f0", FRONTEND: "c779df98295f"}
PORT = 5192
OBJECT_PORT = 58354
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
EMBEDDING = Path.home() / ".cache" / "aftercare-agent" / "models" / REVISION
IMAGE = ROOT / "data" / "vision-v1" / "synthetic-cracked-mug.png"
IMAGE_SHA = "6e4390e0e3afcec897eb0969e43d9666b64ac4f4d8238c0654219d3fe92a6939"
WIRE_GATE = Path(__file__).parent / "chat_vision_wire_gate" / "sitecustomize.py"
CAP = Decimal("0.05")
OUTPUT = 900
TASK = "aftercare.chat_analysis"
WORKER = (
    "celery",
    "-A",
    "aftercare.worker",
    "worker",
    "--pool=solo",
    "--concurrency=1",
    "--loglevel=WARNING",
)


def _plan():
    spec = _configuration()["gpt-6.1-terra"]
    require(spec.alias == "economy" and spec.output_tokens >= OUTPUT, "ECONOMY_TERRA_REQUIRED")
    require(spec.cache_write_usd_per_million is not None, "REGISTERED_CACHE_PRICE_REQUIRED")
    require(spec.cache_write_ttl != "12h", "VISION_RETENTION_RESTRICTED")
    raw = _read(IMAGE, 8 * 1024 * 1024)
    require(hashlib.sha256(raw).hexdigest() == IMAGE_SHA, "FROZEN_ORIGINAL_IMAGE_MISMATCH")
    (photo,) = vision.prepare_photos(
        [vision.PhotoSource("VIS-01", "fixture", IMAGE_SHA, "image/png", len(raw))],
        lambda _: raw,
    )
    prompt, _ = models._estimate_input(vision._messages([photo]), [], vision=True)
    allowance = spec.context_tokens - OUTPUT - prompt
    reserve = models._reserve_price(spec, spec.context_tokens, OUTPUT)
    require(
        allowance > 0 and reserve is not None and reserve <= CAP, "ESTIMATED_RESERVATION_REJECTED"
    )
    require(EMBEDDING.is_dir(), "LOCAL_BGE_SNAPSHOT_REQUIRED")
    payload = {
        "scope": "P2_ISOLATED_SINGLE_SYNTHETIC_VISION_WORKER",
        "images": IMAGES,
        "port": PORT,
        "object_port": OBJECT_PORT,
        "model": spec.model,
        "source_registry_sha256": models.digest(spec.model_dump(mode="json")),
        "original_sha256": IMAGE_SHA,
        "sent_sha256": photo.sent_sha256,
        "transform_version": vision.TRANSFORM_VERSION,
        "prompt_version": vision.PROMPT_VERSION,
        "output_tokens": OUTPUT,
        "maximum_provider_posts": 1,
        "prior_zero_provider_failure_report_sha256": PRIOR_REPORT_SHA,
        "automatic_retries": 0,
        "registered_context_tokens": spec.context_tokens,
        "image_token_operational_allowance": allowance,
        "estimated_reservation_usd": str(reserve),
        "estimated_reservation_cap_usd": str(CAP),
        "estimation_basis": "Registered context and prices, plus output cap. Estimates are not provider guarantees.",
        "wire_guard_sha256": hashlib.sha256(WIRE_GATE.read_bytes()).hexdigest(),
        "embedding_revision": REVISION,
    }
    return payload, spec, raw


def _verify_zero_provider_failure():
    raw = _read(PRIOR_REPORT, 1024 * 1024)
    require(hashlib.sha256(raw).hexdigest() == PRIOR_REPORT_SHA, "PRIOR_FAILURE_REPORT_CHANGED")
    report = json.loads(raw)
    state = _private_json(PRIOR_STATE)
    require(
        PRIOR_ADMISSION.is_file()
        and report["status"] == "FAILED"
        and report["failure_code"] == "START_OBJECTS_FAILED"
        and not report["request_ids"]
        and report.get("wire") is None
        and state["run_id"] == PRIOR_RUN_ID
        and state["status"] == "FAILED"
        and state.get("cleanup_leftovers") == [],
        "PRIOR_ZERO_PROVIDER_SCOPE_UNPROVEN",
    )
    stack = offline_demo.restore(state)
    require(
        not (stack.directory / "vision-paid-runtime").exists()
        and not (stack.directory / "vision-wire").exists()
        and all(
            kind != "container" or name.endswith(("-pg", "-objects"))
            for kind, name in stack.resources
        ),
        "PRIOR_PAID_ACTIVITY_NOT_EXCLUDED",
    )
    for kind in ("container", "network", "volume"):
        arguments = ("ls", "--all") if kind == "container" else ("ls",)
        result = subprocess.run(
            ["docker", kind, *arguments, "--quiet", "--filter", f"label={LABEL}={PRIOR_RUN_ID}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        require(
            result.returncode == 0 and not result.stdout.strip(), "PRIOR_OWNED_RESOURCES_REMAIN"
        )


def _check_ports():
    for port in (PORT, OBJECT_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise VerificationError(f"ISOLATED_PORT_{port}_UNAVAILABLE") from None


def _one_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _worker_options(runtime, mode):
    return (
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "AFTERCARE_CONTAINER=1",
        "--env",
        "AFTERCARE_PROFILE=app",
        "--env",
        f"AFTERCARE_CONTAINER_MODEL_MODE={mode}",
        "--mount",
        f"type=bind,source={runtime},target=/workspace/.runtime,readonly",
    )


def _configure_paid(stack, plan, spec):
    runtime = stack.directory / "vision-paid-runtime"
    runtime.mkdir(mode=0o700)
    config = _private_json(stack.runtime / "local.json")
    maximum = int(
        (Decimal(plan["estimated_reservation_usd"]) * 1_000_000).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    config["app"].update(
        chat_vision_enabled=True,
        chat_vision_alias="economy",
        chat_analysis_max_calls=1,
        chat_analysis_max_microusd=maximum,
    )
    _one_file(runtime / "local.json", json.dumps(config))
    model = spec.model_dump(mode="json")
    model.update(
        vision=True,
        vision_checked_at="2026-11-11",
        vision_compatibility_version="synthetic-vision-v1-live-20261111-isolated-only",
        vision_input_tokens_per_image=plan["image_token_operational_allowance"],
        data_scopes=["customer_attachment"],
    )
    registry = models.Registry.model_validate(
        {
            "version": "ac-p2-isolated-vision-20261111",
            "max_attempts": 1,
            "max_run_attempts": 1,
            "max_usd": plan["estimated_reservation_usd"],
            "max_run_usd": plan["estimated_reservation_usd"],
            "native_streaming": False,
            "models": [model],
        }
    )
    _one_file(runtime / "models.json", registry.model_dump_json())
    key = _private_json(ROOT / ".runtime" / "model-secret.json").get("api_key")
    require(isinstance(key, str) and bool(key.strip()), "PROJECT_MODEL_KEY_REQUIRED")
    stack.secrets.append(key)
    _one_file(runtime / "model-secret.json", json.dumps({"api_key": key}))
    return runtime


def _audit(stack, api, conversation):
    ident = str(UUID(conversation))
    return stack.json_exec(
        "CHAT_VISION_AUDIT",
        api,
        f"""
import json
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import guarded_engine
engine = guarded_engine(load_settings('app'))
with engine.connect() as conn, conn.begin():
    conn.execute(text('SET TRANSACTION READ ONLY'))
    rows = conn.execute(text('''
      SELECT t.id AS turn_id,t.state AS turn_state,a.id AS attempt_id,a.state,a.reserved_microusd,
             a.estimated_cost_microusd,a.budget_violation,a.usage,a.model_evidence,a.response_evidence
      FROM chat_model_attempts a JOIN chat_analysis_turns t ON t.id=a.turn_id
      WHERE a.conversation_id=:id'''), {{'id': {ident!r}}}).mappings()
    attempts = [dict(row) for row in rows]
    observations = [dict(row) for row in conn.execute(text('''
      SELECT o.attachment_id,o.attempt_id,o.original_sha256,o.sent_sha256,o.findings,o.certainty
      FROM chat_observations o JOIN chat_analysis_turns t ON t.id=o.turn_id
      WHERE t.conversation_id=:id'''), {{'id': {ident!r}}}).mappings()]
    count = conn.scalar(text('SELECT count(*) FROM chat_model_attempts'))
    unrelated = conn.scalar(text('SELECT (SELECT count(*) FROM agent_model_attempts) + (SELECT count(*) FROM intake_model_audits)'))
print(json.dumps({{'attempts': attempts, 'observations': observations, 'all_chat_attempts': count, 'unrelated_attempts': unrelated}}))
engine.dispose()
""",
    )


def _exercise(stack, api, beat, report, raw, wire):
    token = next(
        item["token"]
        for item in _private_json(stack.credentials_path)
        if item["role"] == "customer"
    )
    with httpx.Client(
        base_url=f"http://127.0.0.1:{PORT}/api/",
        trust_env=False,
        timeout=10,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        require(read(client, "runtime")["mode"] == "offline", "API_MUST_REMAIN_OFFLINE")
        order = next(
            (
                row
                for row in read(client, "orders")["items"]
                if row["status"] == "DELIVERED" and row["items"]
            ),
            None,
        )
        require(order is not None, "OWN_DELIVERED_ORDER_REQUIRED")
        stack.docker("PAUSE_OWN_BEAT", "pause", beat)
        try:
            ident, case_id, _ = _begin(
                client, report, "conversation_id", order, f"订单 {order['id']} 的商品有破损"
            )
            report["case_id"] = case_id
            upload = message(
                client,
                ident,
                "",
                request_ids(report, "original_upload"),
                [(IMAGE.name, raw)],
            )
            (attachment,) = next(
                row["attachments"] for row in upload["messages"] if row["role"] == "customer"
            )
            report["attachment_id"] = attachment["id"]
            require(attachment["sha256"] == IMAGE_SHA, "S3_ATTACHMENT_DIGEST_MISMATCH")
            fetched = call(client, "GET", attachment["content_path"].removeprefix("/api/"), 200)
            require(fetched.content == raw, "S3_ORIGINAL_READBACK_MISMATCH")
        finally:
            stack.docker("RESUME_OWN_BEAT", "unpause", beat)

        path = f"conversations/{ident}"
        detail = _wait(
            client, path, lambda data: bool(_cards(data, "photo_observation")), seconds=65
        )
        cards = _cards(detail, "photo_observation")
        require(
            len(cards) == 1 and cards[0]["data"]["attachment_refs"] == [attachment["id"]],
            "PUBLIC_OBSERVATION_SCOPE_MISMATCH",
        )
        audit = _audit(stack, api, ident)
        report["audit"] = audit
        require(
            audit["all_chat_attempts"] == 1 and audit["unrelated_attempts"] == 0,
            "ONE_ATTEMPT_REQUIRED",
        )
        (attempt,) = audit["attempts"]
        (observed,) = audit["observations"]
        require(
            attempt["state"] == "SUCCEEDED"
            and attempt["turn_state"] == "PUBLISHED"
            and attempt["estimated_cost_microusd"] is not None
            and not attempt["budget_violation"]
            and attempt["response_evidence"]["path"] == "DEFAULT_SYNC"
            and attempt["response_evidence"]["response"] == "SDK_RETURNED"
            and attempt["response_evidence"].get("validated_result_sha256")
            and observed["attempt_id"] == attempt["attempt_id"]
            and observed["attachment_id"] == attachment["id"]
            and observed["original_sha256"] == IMAGE_SHA
            and observed["sent_sha256"] == report["plan"]["sent_sha256"],
            "PERSISTED_VISION_BINDING_MISMATCH",
        )
        report["wire"] = _private_json(wire / "admission.json")
        report["wire_response"] = _private_json(wire / "response.json")
        require(report["wire_response"]["http_status"] == 200, "EXPECTED_PROVIDER_RESPONSE")
        require(
            report["wire"]["sent_sha256"] == observed["sent_sha256"],
            "WIRE_OBSERVATION_DIGEST_MISMATCH",
        )
        before = _stats(stack, api)
        stack.json_exec(
            "DUPLICATE_OWN_TURN",
            api,
            f"""
import json
from aftercare.queue import make_queue
make_queue('app').send_task({TASK!r}, args=[{str(UUID(attempt["turn_id"]))!r}])
print(json.dumps({{'submitted': True}}))
""",
        )
        deadline = monotonic() + 15
        while monotonic() < deadline and _stats(stack, api) <= before:
            sleep(0.25)
        require(_stats(stack, api) > before, "DUPLICATE_TASK_NOT_OBSERVED")
        require(_audit(stack, api, ident) == audit, "DUPLICATE_ALTERED_AUDIT")
        require(read(client, path)["messages"] == detail["messages"], "DUPLICATE_PUBLICATION")
        report["public_cards"] = cards
        report["duplicate_delivery_verified"] = True
        report["original_s3_readback_verified"] = True


def _execute(report, spec, raw):
    require(not STATE.exists(), "DEDICATED_STACK_ALREADY_EXISTS")
    for image, expected in IMAGES.items():
        # Inspect configured local Docker before any test resources are created.
        response = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        require(
            response.returncode == 0 and response.stdout.strip().startswith("sha256:" + expected),
            "PINNED_IMAGE_MISMATCH",
        )
    offline_demo.STATE = STATE
    offline_demo.FreshStack = P2Stack
    offline_demo.start(
        SimpleNamespace(
            port=PORT,
            object_port=OBJECT_PORT,
            backend_image=BACKEND,
            frontend_image=FRONTEND,
        )
    )
    state = _private_json(STATE)
    stack = offline_demo.restore(state)
    report["preserved_isolated_url"] = state["url"]
    stack.prefix = f"aftercare-fresh-{stack.run_id}"
    stack.label = f"{LABEL}={stack.run_id}"
    config = _private_json(stack.runtime / "local.json")["app"]
    stack.secrets = [
        make_url(config["database_url"]).password,
        config["object_access_key"],
        config["object_secret_key"],
    ]
    require(
        all(isinstance(value, str) and value for value in stack.secrets),
        "LOCAL_SECRET_METADATA_REQUIRED",
    )
    state["resources"] = stack.resources
    api, beat = (owned_container(stack, name) for name in ("api", "beat"))
    worker = owned_container(stack, "worker")
    net, egress = (f"{stack.prefix}-{name}" for name in ("net", "ingress"))
    require(
        all(stack.owned("network", name) for name in (net, egress)), "NETWORK_OWNERSHIP_MISMATCH"
    )
    wire = stack.directory / "vision-wire"
    wire.mkdir(mode=0o700)
    _one_file(wire / "plan.json", json.dumps(report["plan"]))
    gate = stack.directory / "vision-wire-python"
    gate.mkdir(mode=0o700)
    copyfile(WIRE_GATE, gate / "sitecustomize.py")
    require(
        hashlib.sha256((gate / "sitecustomize.py").read_bytes()).hexdigest()
        == report["plan"]["wire_guard_sha256"],
        "WIRE_GUARD_CHANGED",
    )
    paid = f"{stack.prefix}-vision-worker"
    secret = stack.directory / "vision-paid-runtime" / "model-secret.json"
    try:
        paid_runtime = _configure_paid(stack, report["plan"], spec)
        stack.docker("STOP_OFFLINE_WORKER", "stop", worker)
        stack.resources.append(("container", paid))
        offline_demo.save(state)
        stack.run(
            BACKEND,
            "vision-worker",
            net,
            (
                *_worker_options(paid_runtime, "live-hybrid"),
                "--env",
                f"AFTERCARE_EMBEDDING_REVISION={REVISION}",
                "--env",
                "HF_HUB_OFFLINE=1",
                "--env",
                "TRANSFORMERS_OFFLINE=1",
                "--env",
                "LANGSMITH_TRACING=false",
                "--env",
                "LANGCHAIN_TRACING_V2=false",
                "--env",
                "PYTHONPATH=/vision-gate",
                "--env",
                "HTTP_PROXY=",
                "--env",
                "HTTPS_PROXY=",
                "--env",
                "ALL_PROXY=",
                "--env",
                "http_proxy=",
                "--env",
                "https_proxy=",
                "--env",
                "all_proxy=",
                "--mount",
                f"type=bind,source={EMBEDDING},target=/workspace/models/{REVISION},readonly",
                "--mount",
                f"type=bind,source={gate},target=/vision-gate,readonly",
                "--mount",
                f"type=bind,source={wire},target=/vision-wire",
                "--entrypoint",
                "python",
            ),
            (
                "-c",
                "import sitecustomize; assert sitecustomize.READY; from celery.__main__ import main; main()",
                *WORKER[1:],
            ),
        )
        # FreshStack.run records its own resource too; keep a single durable entry.
        stack.resources[:] = list(dict.fromkeys(stack.resources))
        offline_demo.save(state)
        stack.docker("CONNECT_PAID_WORKER_EGRESS", "network", "connect", egress, paid)
        stack.wait(
            "PAID_WORKER_READY",
            "exec",
            api,
            "celery",
            "-A",
            "aftercare.worker",
            "inspect",
            "--timeout",
            "2",
            "ping",
        )
        require(not (wire / "admission.json").exists(), "PREEXISTING_PROVIDER_ADMISSION")
        _exercise(stack, api, beat, report, raw, wire)
    finally:
        try:
            if stack.owned("container", paid):
                stack.docker("REMOVE_OWN_PAID_WORKER", "rm", "--force", paid)
            require(stack.owned("container", paid) is None, "PAID_WORKER_STILL_EXISTS")
            report["paid_worker_removed"] = True
        finally:
            secret.unlink(missing_ok=True)
            report["private_worker_key_removed"] = not secret.exists()
        stack.docker("RESTART_OFFLINE_WORKER", "start", worker)
        state["resources"] = [entry for entry in stack.resources if entry != ("container", paid)]
        offline_demo.save(state)
        report["preserved_isolated_url"] = state["url"]
        if (wire / "admission.json").exists():
            report["wire"] = _private_json(wire / "admission.json")
        if (wire / "response.json").exists():
            report["wire_response"] = _private_json(wire / "response.json")
        if report.get("conversation_id") and "audit" not in report:
            report["audit"] = _audit(stack, api, report["conversation_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--resume-zero-provider", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.execute_paid and (
        not args.expected_plan_sha256 or args.report is None or not args.resume_zero_provider
    ):
        parser.error("paid execution requires the frozen plan digest and a new report path")
    try:
        require(
            not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://"),
            "LOCAL_DOCKER_REQUIRED",
        )
        context = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        require(
            context.returncode == 0 and context.stdout.strip().startswith("unix://"),
            "LOCAL_DOCKER_REQUIRED",
        )
        _check_ports()
        _verify_zero_provider_failure()
        require(not STATE.exists() and not ADMISSION.exists(), "RESUMED_RUN_ALREADY_ATTEMPTED")
        plan, spec, raw = _plan()
        checksum = models.digest(plan)
        if not args.execute_paid:
            print(
                json.dumps(
                    {"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": plan}, indent=2
                )
            )
            return 0
        require(checksum == args.expected_plan_sha256, "PLAN_DIGEST_MISMATCH")
        report = {"status": "IN_PROGRESS", "plan_sha256": checksum, "plan": plan, "request_ids": {}}
        _one_file(args.report, json.dumps(report))
        try:
            _one_file(ADMISSION, checksum + "\n")
            _execute(report, spec, raw)
            report["status"] = "PASSED"
        except VerificationError as error:
            report.update(status="FAILED", failure_code=str(error))
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
            report.update(status="FAILED", failure_code="ISOLATED_EXECUTION_FAILED")
        finally:
            _save(args.report, report)
        print(report["status"])
        return int(report["status"] != "PASSED")
    except (VerificationError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("P2_VISION_WORKER_BLOCKED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
