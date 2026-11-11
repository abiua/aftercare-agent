"""Run the controlled MULTI recheck ablation on the existing owned P5 test stack."""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import offline_demo
import verify_chat_p5_pg as pg
from verify_chat_p5_paid import _private
from verify_chat_p5_paid import _safe as _legacy_safe
from verify_streaming_compatibility import _save

CAMPAIGN = "chat-p5-pg-recheck-ablation-v1"
SCRIPT = Path(__file__).resolve()
AUDITED = (
    "chat_conversations",
    "chat_messages",
    "chat_attachments",
    "chat_observations",
    "chat_model_attempts",
    "chat_decision_snapshots",
    "agent_runs",
    "agent_model_attempts",
    "business_executions",
)


def _safe(error):
    value = str(error)
    if re.fullmatch(r"P5_ABLATION_[A-Z0-9_]{1,80}", value):
        return value
    return _legacy_safe(error)


def _plan(report_path, image):
    from aftercare.business.rules import digest

    baseline = _private(report_path)
    state = _private(pg.STATE)
    evidence = baseline["acceptance"]
    stack = offline_demo.restore(state)
    old = stack.directory / "p5-pg-recovery-output/manifest"
    pg._check(
        baseline["status"] == "PASSED"
        and baseline["plan_sha256"] == digest(baseline["plan"])
        and baseline["plan"]["campaign"] == pg.CAMPAIGN
        and state["profile"] == "test"
        and state["status"] == "READY"
        and baseline["owned_stack"] == state["run_id"]
        and baseline["owned_url"] == state["url"] == "http://127.0.0.1:5200"
        and Path(baseline["private_manifest_directory"]) == old
        and not (stack.directory / CAMPAIGN).exists(),
        "P5_ABLATION_PRIOR_SOURCE_REQUIRED",
    )
    old_plan = _private(old / "plan.json")
    pg._check(
        digest(old_plan) == evidence["receipt"]["plan_sha256"]
        and old_plan["campaign"] == pg.CAMPAIGN
        and {row["case_id"] for row in old_plan["cases"]} == set(pg.CASES)
        and len(old_plan["cases"]) == 2,
        "P5_ABLATION_PRIOR_PLAN_MISMATCH",
    )
    endpoint = stack.docker(
        "P5_ABLATION_DOCKER_CONTEXT",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
        log_output=False,
    ).stdout.strip()
    pg._check(
        endpoint.startswith("unix://")
        and (not os.environ.get("DOCKER_HOST") or os.environ["DOCKER_HOST"].startswith("unix://")),
        "P5_ABLATION_LOCAL_DOCKER_REQUIRED",
    )
    images = baseline["plan"]["images"]
    network, system_id = pg._topology(stack, images)
    image_id = stack.docker(
        "P5_ABLATION_IMAGE",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image,
        log_output=False,
    ).stdout.strip()
    pg._check(
        image_id == image and system_id == evidence["system_identifier"],
        "P5_ABLATION_PIN_MISMATCH",
    )
    return {
        "campaign": CAMPAIGN,
        "source_campaign": pg.CAMPAIGN,
        "scope": "CONTROLLED_PG_MULTI_RECHECK_ABLATION",
        "calls_per_arm": 8,
        "budget_usd_per_arm": "0.10",
        "provider_calls_allowed": 0,
        "stack_id": stack.run_id,
        "system_id": system_id,
        "network": network,
        "sentinel_sha256": evidence["object_sentinel_sha256"],
        "verifier_image": image,
        "images": images,
        "source_report_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "source_plan_sha256": sha256((old / "plan.json").read_bytes()).hexdigest(),
        "script_sha256": sha256(SCRIPT.read_bytes()).hexdigest(),
        "transport_sha256": sha256(Path(pg.__file__).read_bytes()).hexdigest(),
        "sources": old_plan["cases"],
    }


def _check_results(rows):
    for case_id in pg.CASES:
        full, disabled = [row for row in rows if row["case_id"] == case_id]
        pg._check(
            (full["arm"], disabled["arm"]) == ("multi", "multi_no_recheck")
            and full["input_sha256"] == disabled["input_sha256"]
            and full["controls_sha256"] == disabled["controls_sha256"]
            and full["route"] == disabled["route"] == "MULTI"
            and all(
                row["input_comparable"]
                and row["checkpoint_persisted"]
                and row["candidate_status"] == "AVAILABLE"
                for row in (full, disabled)
            ),
            "P5_ABLATION_PAIR_MISMATCH",
        )
        if case_id == "DAMAGE-ORDINARY":
            pg._check(
                full["status"] == disabled["status"] == "COMPLETED"
                and full["program_checks"] == disabled["program_checks"]
                and len(full["attempts"]) == len(disabled["attempts"]) == 5
                and not (
                    full["rechecks"] or disabled["rechecks"] or disabled["suppressed_rechecks"]
                ),
                "P5_ABLATION_NO_REQUEST_PAIR_MISMATCH",
            )
        else:
            pg._check(
                len(full["rechecks"]) == len(disabled["suppressed_rechecks"]) == 1
                and full["rechecks"][0]["claim_ids"] == ["fact/package"]
                and disabled["suppressed_rechecks"][0]["claim_ids"] == ["fact/package"]
                and {"recheck", "verify-1"} <= set(full["phases"])
                and set(disabled["phases"]) == {"fact", "policy", "verify-0"}
                and not disabled["rechecks"]
                and disabled["status"] == "NEEDS_REVIEW"
                and disabled["reason"] == "INVESTIGATION_RECHECK_SUPPRESSED",
                "P5_ABLATION_CONFLICT_PAIR_MISMATCH",
            )


def _worker(plan, output, expected_digest):
    from aftercare.auth import UserContext
    from aftercare.business.rules import digest
    from aftercare.business.service import BusinessService
    from aftercare.business.storage import S3ObjectStore
    from aftercare.config import load_settings
    from aftercare.db import guarded_engine
    from aftercare.evaluation.chat.manifest import Manifest
    from aftercare.evaluation.chat.pg_runtime import PgAttestation, open_pg_runtime, sentinel_key
    from aftercare.evaluation.chat.runner import ChatEvaluationRunner, FrozenCase, _populate
    from aftercare.intelligence.models import ModelRunner, Registry
    from sqlalchemy import text

    pg._check(
        digest(plan) == expected_digest
        and plan["campaign"] == CAMPAIGN
        and plan["provider_calls_allowed"] == 0
        and sha256(SCRIPT.read_bytes()).hexdigest() == plan["script_sha256"]
        and sha256(Path(pg.__file__).read_bytes()).hexdigest() == plan["transport_sha256"],
        "P5_ABLATION_WORKER_PLAN_MISMATCH",
    )
    settings = load_settings("test")
    store = S3ObjectStore(settings)
    marker = store.get(sentinel_key(plan["stack_id"]), 64)
    pg._check(sha256(marker).hexdigest() == plan["sentinel_sha256"], "P5_ABLATION_SENTINEL_CHANGED")
    stamp = PgAttestation(
        plan["stack_id"], plan["system_id"], store.bucket, plan["sentinel_sha256"], 64
    )
    public = guarded_engine(settings, test_only=True)

    def public_state():
        with public.connect() as db, db.begin():
            db.execute(text("SET TRANSACTION READ ONLY"))
            pg._check(
                db.scalar(text("SELECT system_identifier::text FROM pg_control_system()"))
                == plan["system_id"],
                "P5_ABLATION_SYSTEM_ID_CHANGED",
            )
            return {
                table: db.scalar(
                    text(
                        f"SELECT md5(coalesce(string_agg(datum, ',' ORDER BY datum), '')) "
                        f"FROM (SELECT to_jsonb(t)::text AS datum FROM public.{table} t) rows"
                    )
                )
                for table in AUDITED
            }

    def case_for(runtime, engine, storage, row):
        scope = row["scope"]
        decision_at = datetime.fromisoformat(row["decision_at"])
        return FrozenCase(
            row["case_id"],
            row["repetition"],
            BusinessService(engine, runtime.settings, storage=storage, clock=lambda: decision_at),
            UserContext(user_id="support", role="support"),
            scope["conversation_id"],
            scope["issue_id"],
            decision_at,
            row["provenance"],
        )

    registry = Registry.model_validate(
        {
            "version": "chat-p5-pg-controlled",
            "allow_unverified": True,
            "models": [
                {
                    "alias": "analysis",
                    "provider": "fixture",
                    "model": "controlled",
                    "endpoint": "http://127.0.0.1:1/v1",
                    "api_key_env": "UNUSED_FIXTURE",
                    "input_usd_per_million": "1",
                    "output_usd_per_million": "1",
                    "price_date": "2026-11-11",
                    "source": "synthetic",
                    "context_tokens": 32000,
                    "data_scopes": ["customer_conversation"],
                    "output_tokens": 1800,
                }
            ],
        }
    )
    calls = []
    runner = ChatEvaluationRunner(
        ModelRunner(
            registry, transport=pg._controlled_transport(calls, paired_recheck_fixture=True)
        ),
        None,
        calls=8,
        budget=Decimal("0.10"),
        recheck_ablation=True,
    )
    manifest = Manifest(output / "manifest")
    try:
        baseline = public_state()
        with open_pg_runtime(settings, stamp, pg.CAMPAIGN) as original:
            frozen = []
            for row in plan["sources"]:
                with original.source(
                    row["case_id"],
                    row["repetition"],
                    row["source_sha256"],
                    require_existing=True,
                ) as (engine, storage, fresh):
                    pg._check(not fresh, "P5_ABLATION_SOURCE_MISSING")
                    case = case_for(original, engine, storage, row)
                source = original.freeze(case)
                pg._check(
                    all(
                        source.record[key] == row[key]
                        for key in (
                            "source_namespace",
                            "scope_sha256",
                            "relevant_input_sha256",
                            "paired_sha256",
                            "images",
                            "material_objects",
                        )
                    ),
                    "P5_ABLATION_SOURCE_CHANGED",
                )
                frozen.append(source)

            def run(*, fresh_expected):
                with open_pg_runtime(settings, stamp, CAMPAIGN) as runtime:
                    cases = []
                    for source in frozen:
                        row = source.record
                        with runtime.source(
                            row["case_id"],
                            row["repetition"],
                            digest([CAMPAIGN, row["source_sha256"]]),
                            require_existing=not fresh_expected,
                        ) as (engine, storage, fresh):
                            pg._check(fresh == fresh_expected, "P5_ABLATION_SOURCE_REPLAY_MISMATCH")
                            if fresh:
                                _populate(source, engine, storage)
                            case = case_for(runtime, engine, storage, row)
                        clone = runtime.freeze(case)
                        pg._check(
                            clone.scope == source.scope
                            and clone.images == source.images
                            and clone.materials == source.materials,
                            "P5_ABLATION_CLONE_MISMATCH",
                        )
                        cases.append(case)
                    return runtime.run(manifest, runner, cases)

            result = run(fresh_expected=True)
            _check_results(result["rows"])
            count = len(calls)
            pg._check(
                count == sum(len(row["attempts"]) for row in result["rows"])
                and run(fresh_expected=False) == result
                and len(calls) == count
                and public_state() == baseline
                and all(original.freeze(source.definition) == source for source in frozen),
                "P5_ABLATION_READBACK_MISMATCH",
            )
        return {
            "receipt": result,
            "controlled_transport_calls": count,
            "real_provider_calls": 0,
            "public_tables_verified_unchanged": list(AUDITED),
            "source_readback_verified": True,
            "replay_no_new_attempts": True,
        }
    finally:
        public.dispose()


def _execute(plan, report, expected_digest):
    from verify_chat_p2_vision_worker import _one_file

    state = _private(pg.STATE)
    stack = offline_demo.restore(state)
    pg._check(
        pg._topology(stack, plan["images"]) == (plan["network"], plan["system_id"]),
        "P5_ABLATION_TOPOLOGY_CHANGED",
    )
    output = stack.directory / CAMPAIGN
    output.mkdir(mode=0o700)
    _one_file(output / "plan.json", json.dumps(plan))
    name = f"aftercare-fresh-{stack.run_id}-p5-ablation"
    state["resources"].append(["container", name])
    with patch.object(offline_demo, "STATE", pg.STATE):
        offline_demo.save(state)
    report["private_output_directory"] = str(output)
    try:
        completed = stack.docker(
            "P5_ABLATION_CONTROLLED_RUN",
            "run",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--label",
            f"com.aftercare.fresh-bootstrap={stack.run_id}",
            "--network",
            plan["network"],
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "AFTERCARE_CONTAINER=1",
            "--env",
            "AFTERCARE_PROFILE=test",
            "--env",
            "AFTERCARE_CONTAINER_MODEL_MODE=offline",
            "--mount",
            f"type=bind,source={stack.runtime},target=/workspace/.runtime,readonly",
            "--mount",
            f"type=bind,source={SCRIPT.parent},target=/p5-scripts,readonly",
            "--mount",
            f"type=bind,source={output},target=/p5-output",
            "--entrypoint",
            "python",
            plan["verifier_image"],
            "/p5-scripts/verify_chat_p5_pg_ablation.py",
            "_worker",
            expected_digest,
            timeout=300,
            required=False,
            log_output=False,
        )
        if completed is not None and completed.returncode != 0:
            for line in reversed(completed.stderr.splitlines()):
                line = line.strip()
                if re.fullmatch(r"P5_ABLATION_[A-Z0-9_]{1,80}", line) or pg._safe_worker_code(line):
                    report["worker_error_code"] = line
                    break
        pg._check(completed is not None and completed.returncode == 0, "P5_ABLATION_WORKER_FAILED")
        report["acceptance"] = json.loads(completed.stdout.strip().splitlines()[-1])
    finally:
        owned = stack.owned("container", name)
        pg._check(owned is not False, "P5_ABLATION_VERIFIER_OWNER_MISMATCH")
        if owned:
            stack.docker("P5_ABLATION_REMOVE_VERIFIER", "rm", "-f", name)
        pg._check(stack.owned("container", name) is None, "P5_ABLATION_VERIFIER_STILL_EXISTS")
        state["resources"].remove(["container", name])
        with patch.object(offline_demo, "STATE", pg.STATE):
            offline_demo.save(state)


def main():
    from aftercare.business.rules import digest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovered-report", type=Path, required=True)
    parser.add_argument("--verifier-image-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.verifier_image_id):
        parser.error("supply the complete immutable verifier image ID")
    if args.execute and (not args.expected_plan_sha256 or not args.report):
        parser.error("--execute requires the reviewed plan digest and a new report")
    plan = _plan(args.recovered_report, args.verifier_image_id)
    checksum = digest(plan)
    visible = {key: value for key, value in plan.items() if key != "sources"}
    if not args.execute:
        print(json.dumps({"status": "PREFLIGHT_ONLY", "plan_sha256": checksum, "plan": visible}))
        return 0
    pg._check(checksum == args.expected_plan_sha256, "P5_ABLATION_PLAN_CHANGED")
    report = {"status": "IN_PROGRESS", "plan": visible, "plan_sha256": checksum}
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        with offline_demo.lifecycle_lock(pg.STATE):
            pg._check(
                _plan(args.recovered_report, args.verifier_image_id) == plan,
                "P5_ABLATION_PLAN_CHANGED",
            )
            _execute(plan, report, checksum)
        report["status"] = "PASSED"
    except Exception as error:  # noqa: BLE001
        report.update(status="FAILED", failure_code=_safe(error))
    finally:
        _save(args.report, report)
    print(report["status"])
    return int(report["status"] != "PASSED")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        try:
            print(
                json.dumps(
                    _worker(_private(Path("/p5-output/plan.json")), Path("/p5-output"), sys.argv[2])
                )
            )
        except Exception as error:  # noqa: BLE001
            print(_safe(error), file=sys.stderr)
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())
