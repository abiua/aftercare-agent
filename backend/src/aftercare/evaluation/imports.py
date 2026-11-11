"""Validate external JSON without promoting its assertions to trusted evidence."""

import hmac
import json
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from aftercare.business.errors import BusinessError

MAX_REPORT_BYTES = 8 * 1024 * 1024
EXPERIMENT_STATES = {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}
RESULT_STATES = {
    "QUEUED",
    "RUNNING",
    "PASSED",
    "FAILED",
    "TIMEOUT",
    "ERROR",
    "NEEDS_REVIEW",
    "NOT_RUN",
}


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, Field(min_length=1, max_length=MAX_REPORT_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    origin: Annotated[str, Field(min_length=1, max_length=200)]


def _reject_nonfinite(value):
    raise ValueError(f"Non-finite value: {value}")


def _invalid():
    return BusinessError("INVALID_REPORT", "报告格式、状态或执行计数不一致。", 422)


def parse_report(request: ImportInput):
    try:
        raw = request.content.encode()
    except UnicodeError:
        raise _invalid() from None
    if len(raw) > MAX_REPORT_BYTES:
        raise BusinessError("REPORT_TOO_LARGE", "报告最多为 8 MiB。", 413)
    if not hmac.compare_digest(sha256(raw).hexdigest(), request.sha256):
        raise BusinessError("REPORT_HASH_MISMATCH", "报告摘要与上传内容不一致。", 422)
    try:
        report = json.loads(raw, parse_constant=_reject_nonfinite)
        json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise _invalid() from None
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("name"), str)
        or not 1 <= len(report["name"]) <= 120
        or not isinstance(report.get("status"), str)
        or report["status"] not in EXPERIMENT_STATES
        or not isinstance(report.get("config"), dict)
        or not isinstance(report.get("counts"), dict)
        or not isinstance(report.get("metrics"), list)
        or not isinstance(report.get("results"), list)
        or not isinstance(report.get("gate"), dict)
    ):
        raise _invalid()
    results, counts = report["results"], report["counts"]
    if len(results) > 432 or any(
        type(counts.get(key)) is not int or counts[key] < 0
        for key in ("planned", "started", "finished", "not_run")
    ):
        raise _invalid()
    if (
        counts["planned"] != len(results)
        or counts["started"] + counts["not_run"] != counts["planned"]
        or counts["finished"] > counts["started"]
    ):
        raise _invalid()
    seen = set()
    for row in results:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("case_id"), str)
            or not 1 <= len(row["case_id"]) <= 64
            or type(row.get("repetition")) is not int
            or row["repetition"] not in range(1, 4)
            or not isinstance(row.get("status"), str)
            or row["status"] not in RESULT_STATES
            or not isinstance(row.get("checks"), list)
            or not isinstance(row.get("reviews"), list)
        ):
            raise _invalid()
        key = row["case_id"], row["repetition"]
        if key in seen:
            raise _invalid()
        seen.add(key)
    observed_started = sum(row.get("started_at") is not None for row in results)
    observed_finished = sum(
        row.get("started_at") is not None and row.get("finished_at") is not None for row in results
    )
    if (observed_started, observed_finished) != (counts["started"], counts["finished"]):
        raise _invalid()
    return report
