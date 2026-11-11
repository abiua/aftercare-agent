"""Verify the P3 same-chat execution projection on a separate offline localhost stack."""

from unittest.mock import patch

import verify_chat_p3_offline as p3
from verify_chat_p1_http import read
from verify_fresh_bootstrap import VerificationError, require

STATE = p3.ROOT / ".runtime" / "offline-browser-demo-p3-progress.json"
PORT, OBJECT_PORT = 5194, 58356
BACKEND = "aftercare-backend:chat-p3-progress-20261111"
FRONTEND = "aftercare-frontend:chat-p3-progress-20261111"
IMAGES = {
    BACKEND: "667eccc4b7edb5142d02f4254ca646655ba7723ebb12f370721a4eb8b7ce4a6f",
    FRONTEND: "243fd1c1617d49bbb0dd13e68c202d24c2cf9266b74165a9f60ed0f8a973d92b",
}

_preflight = p3._preflight
_scenario = p3._scenario


def preflight():
    plan = _preflight()
    require(
        plan["images"] == {name: f"sha256:{value}" for name, value in IMAGES.items()},
        "P3_PROGRESS_IMAGE_MISMATCH",
    )
    return {
        **plan,
        "scope": "P3_ISOLATED_REAL_HTTP_PG_S3_CELERY_OFFLINE_GRAPH_AND_CHAT_EXECUTION_PROGRESS",
        "checks": ["COMPLETED_RECEIPT_VISIBLE", "WITHDRAWAL_NO_EXECUTION_PROGRESS"],
    }


def scenario(clients, order, report, name, *, withdraw):
    _scenario(clients, order, report, name, withdraw=withdraw)
    result = report["scenarios"][name]
    result["stage"] = "chat_execution_progress"
    detail = read(clients["customer"], f"conversations/{result['conversation_id']}")
    require(detail["conversation"]["active_case_id"] == result["case_id"], "P3_CHAT_CASE_MISMATCH")
    require("execution_progress" in detail, "P3_CHAT_PROGRESS_FIELD_MISSING")
    progress = detail["execution_progress"]
    if withdraw:
        require(progress is None, "P3_WITHDRAWAL_PROGRESS_UNEXPECTED")
    else:
        expected = {
            "id": result["execution_id"],
            "case_id": result["case_id"],
            "action": "REFUND",
            "status": "SUCCEEDED",
            "lines": [
                {
                    "line_id": order["items"][0]["line_id"],
                    "amount_cents": result["simulated_amount_cents"],
                }
            ],
            "amount_cents": result["simulated_amount_cents"],
            "simulated": True,
            "receipt_confirmed": True,
        }
        require(
            isinstance(progress, dict)
            and set(progress) == set(expected) | {"updated_at"}
            and all(progress[key] == value for key, value in expected.items())
            and progress["simulated"] is True
            and progress["receipt_confirmed"] is True
            and isinstance(progress["updated_at"], str)
            and bool(progress["updated_at"]),
            "P3_CHAT_PROGRESS_RECEIPT_MISMATCH",
        )
    result["execution_progress"] = progress
    result["stage"] = "verified"


def main():
    # Reuse the existing acceptance script's safety gates, report admission and
    # finance checks. Keep its 5193 constants and original file untouched.
    with patch.multiple(
        p3,
        STATE=STATE,
        PORT=PORT,
        OBJECT_PORT=OBJECT_PORT,
        BACKEND=BACKEND,
        FRONTEND=FRONTEND,
        IMAGES=IMAGES,
        _preflight=preflight,
        _scenario=scenario,
    ):
        return p3.main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, OSError, ValueError):
        print("P3_PROGRESS_ACCEPTANCE_BLOCKED")
        raise SystemExit(1) from None
