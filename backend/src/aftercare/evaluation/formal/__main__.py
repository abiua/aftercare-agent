"""Register and inspect holdout campaigns, or explicitly run an authorized freeze."""

import argparse
import json
import re
from pathlib import Path

from aftercare.business.errors import BusinessError

from .contracts import (
    Candidate,
    FormalFreezeDraftV2,
    FreezeConfirmation,
    FreezeDraft,
    HumanRecord,
    NativeValidation,
    Unlock,
    Validation,
    ValidationPlan,
    parse_freeze_draft,
)
from .files import json_data, read_path
from .register import DEFAULT_ROOT, CampaignRegister


def _input(path, model):
    value = json_data(read_path(path.absolute()))
    return parse_freeze_draft(value) if model is FreezeDraft else model.model_validate(value)


def _native_snapshot(plan, request):
    from sqlalchemy.exc import SQLAlchemyError

    from aftercare.config import load_settings
    from aftercare.db import guarded_engine

    from ..cli import _actor
    from ..service import EvaluationService

    try:
        settings = load_settings("test")
        engine = guarded_engine(settings, test_only=True)
        try:
            return EvaluationService(engine, settings).native_validation_snapshot(
                _actor(engine), plan, request
            )
        finally:
            engine.dispose()
    except SQLAlchemyError:
        raise BusinessError(
            "FORMAL_NATIVE_AUDIT_UNAVAILABLE", "无法读取测试库原生验证快照。", 503
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Local ledger root. Other roots and external disclosures are outside the exposure check.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    models = {
        "register": Candidate,
        "plan": ValidationPlan,
        "validation": Validation,
        "validation-native": NativeValidation,
        "record-exposure": HumanRecord,
        "check-draft": FreezeDraft,
        "freeze": FreezeDraft,
        "unlock": Unlock,
    }
    for command in ["preflight", "run", *models]:
        sub = commands.add_parser(command)
        sub.add_argument("--campaign", required=True)
        if command in models:
            sub.add_argument("--record", type=Path, required=True)
        if command in {"freeze", "check-draft"}:
            sub.add_argument("--confirmation", type=Path, required=command == "freeze")
        if command == "run":
            sub.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    register = CampaignRegister(args.root.absolute())
    executing = args.command == "run" and args.execute
    try:
        if args.command == "run":
            if not executing:
                result = register.preflight(args.campaign)
            else:
                from .orchestrator import run_campaign

                result = run_campaign(register, args.campaign, execute=True)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return int(executing and result["status"] != "COMPLETED")
        method = getattr(register, args.command.replace("-", "_"))
        arguments = [args.campaign]
        if args.command in models:
            arguments.append(_input(args.record, models[args.command]))
        if args.command in {"freeze", "check-draft"} and args.confirmation is not None:
            arguments.append(_input(args.confirmation, FreezeConfirmation))
        if args.command == "check-draft" and args.confirmation is not None:
            method = register.preview_freeze
        if args.command == "freeze" and isinstance(arguments[1], FormalFreezeDraftV2):
            from .orchestrator import freeze_campaign

            method = freeze_campaign
            arguments.insert(0, register)
        if args.command == "unlock":
            from .orchestrator import unlock_campaign

            method = unlock_campaign
            arguments.insert(0, register)
        if args.command == "validation-native":
            arguments.append(_native_snapshot)
        print(json.dumps(method(*arguments), ensure_ascii=False, sort_keys=True))
    except BusinessError as error:
        if executing:
            code = (
                error.code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error.code)
                else "FORMAL_EXECUTION_FAILED"
            )
            parser.exit(1, f"{code}\n")
        parser.exit(1, f"{error.code} {error.message}\n")
    except KeyboardInterrupt:
        if not executing:
            raise
        parser.exit(130, "FORMAL_EXECUTION_INTERRUPTED\n")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        parser.exit(1, "FORMAL_LOCAL_INPUT_INVALID 登记文件、格式或权限不可用。\n")
    except Exception:
        # Explicit execution failures may contain provider or local data.
        if not executing:
            raise
        parser.exit(1, "FORMAL_EXECUTION_FAILED\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
