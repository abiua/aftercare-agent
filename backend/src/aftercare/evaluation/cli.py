"""Run a frozen evaluation sequentially in the guarded test environment."""

from __future__ import annotations

import argparse
import json
import os
import stat
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from aftercare.auth import UserContext, token_hash
from aftercare.business.errors import BusinessError
from aftercare.config import ROOT, load_settings
from aftercare.db import User, guarded_engine
from aftercare.intelligence.models import ModelRunner, Registry

from .runner import EvaluationRunner
from .service import EvaluationService, ExperimentInput


def _parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--name", default="顺序评测")
    cli.add_argument("--split", choices=("dev", "validation", "holdout", "regression"))
    cli.add_argument("--case", action="append", dest="case_ids")
    cli.add_argument("--mode", choices=("single", "multi"), default="single")
    cli.add_argument("--strategy", choices=("economy", "analysis", "routed"), default="routed")
    cli.add_argument("--source", choices=("fixture", "fault", "live"), default="fixture")
    cli.add_argument("--repeat", type=int, default=1)
    cli.add_argument("--max-calls", type=int)
    cli.add_argument("--budget-usd", type=Decimal)
    cli.add_argument("--probe-intent", action="store_true")
    cli.add_argument("--gate-policy-id")
    cli.add_argument("--output", type=Path, required=True)
    return cli


def _request(args):
    if args.source == "live" and (
        not args.case_ids or args.max_calls is None or not args.budget_usd
    ):
        raise BusinessError(
            "LIVE_SCOPE_REQUIRED", "真实顺序评测需要明确案例、调用上限和正数费用上限。", 422
        )
    return ExperimentInput(
        name=args.name,
        dataset_id="v0.2",
        split=args.split,
        case_ids=args.case_ids,
        mode=args.mode,
        strategy=args.strategy,
        repetitions=args.repeat,
        concurrency=1,
        source=args.source,
        max_calls=args.max_calls if args.max_calls is not None else 144,
        budget_usd=args.budget_usd if args.budget_usd is not None else Decimal(0),
        gate_policy_id=args.gate_policy_id,
        probe_intent=args.probe_intent,
    )


def _actor(engine):
    path = ROOT / ".runtime/users-test.json"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise BusinessError("TEST_CREDENTIALS_UNAVAILABLE", "测试身份文件权限不符合要求。", 403)
    rows = json.loads(path.read_text())
    token = next((row.get("token") for row in rows if row.get("role") == "tester"), None)
    if not isinstance(token, str):
        raise BusinessError("TEST_CREDENTIALS_UNAVAILABLE", "测试员凭据不可用。", 403)
    with Session(engine) as db:
        user = db.scalar(
            select(User).where(
                User.token_hash == token_hash(token),
                User.enabled.is_(True),
                User.role == "tester",
            )
        )
        if user is None:
            raise BusinessError("TEST_CREDENTIALS_UNAVAILABLE", "测试员凭据无效。", 403)
        return UserContext(user_id=user.id, role="tester", display_name=user.display_name)


@contextmanager
def _runtime(source):
    settings = load_settings("test")
    model = None
    if source == "live":
        if not settings.model_enabled:
            raise BusinessError("LIVE_EVALUATION_UNAVAILABLE", "测试环境未启用真实模型。", 422)
        model = ModelRunner(Registry.read(settings.model_config_path))
        if not any(os.getenv(spec.api_key_env) for spec in model.registry.models):
            raise BusinessError(
                "LIVE_EVALUATION_UNAVAILABLE", "当前进程未加载已配置的模型凭据。", 422
            )
    else:
        settings = settings.model_copy(update={"model_enabled": False})

    engine = guarded_engine(settings, test_only=True)
    conninfo = (
        make_url(settings.database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    try:
        with ConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=3,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        ) as pool:
            yield settings, engine, PostgresSaver(pool), model
    finally:
        engine.dispose()


def main():
    parser = _parser()
    args = parser.parse_args()
    try:
        request = _request(args)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with (
            os.fdopen(fd, "w", encoding="utf-8") as output,
            _runtime(request.source) as (settings, engine, checkpointer, model),
        ):
            actor = _actor(engine)
            service = EvaluationService(engine, settings, model=model, execution_driver="inline")
            runner = EvaluationRunner(engine, settings, checkpointer, model=model)
            planned = service.create(actor, request, str(uuid4()))
            try:
                service.process(planned["id"], runner)
            except KeyboardInterrupt:
                current = service.experiment(actor, planned["id"])
                if current["status"] in {"QUEUED", "RUNNING"}:
                    service.cancel(
                        actor,
                        current["id"],
                        expected_version=current["version"],
                        key=str(uuid4()),
                    )
                service.process(planned["id"], runner)
            report = service.experiment(actor, planned["id"])
            json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        print(
            json.dumps(
                {"id": report["id"], "status": report["status"], "counts": report["counts"]},
                ensure_ascii=False,
            )
        )
        complete = report["status"] == "COMPLETED" and not any(
            report["counts"][key] for key in ("not_run", "failed", "timed_out")
        )
        return 0 if complete else 1
    except BusinessError as error:
        parser.exit(1, f"{error.code}\n")
    except Exception:  # noqa: BLE001 - avoid printing credentials from runtime failures
        parser.exit(1, "EVALUATION_CLI_FAILED\n")


if __name__ == "__main__":
    raise SystemExit(main())
