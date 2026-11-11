"""Row-bound admission extension for the one-case P5 paid text pilot."""

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager

PROTOCOL = "p5-paid-text-v1"
MAX_ROW_POSTS = 8
MAX_CAMPAIGN_POSTS = 24
MAX_ROW_MICROUSD = 400_000
MAX_CAMPAIGN_MICROUSD = 1_200_000


def _locked(directory):
    return (directory / "lock").open("r+")


def _binding(plan, row):
    expected = next((item for item in plan["wire_rows"] if item["id"] == row["id"]), None)
    if (
        expected is None
        or row["arm"] != expected["arm"]
        or row["input_sha256"] != plan["scope_sha256"]
    ):
        raise RuntimeError("P5_WIRE_ROW_BINDING_REJECTED")
    return {"id": row["id"], "arm": row["arm"], "input_sha256": row["input_sha256"]}


@contextmanager
def active_row(directory, plan, write, row):
    binding = _binding(plan, row)
    path = directory / "active-row.json"
    with _locked(directory) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        write(path, binding)
    try:
        yield
    finally:
        with _locked(directory) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if json.loads(path.read_bytes()) != binding:
                raise RuntimeError("P5_WIRE_ROW_BINDING_CHANGED")
            path.unlink()


def request_role(messages, view, catalogue_sha256):
    system = [item["content"] for item in messages if item.get("role") in {"developer", "system"}]
    if len(system) != 1:
        raise RuntimeError("P5_WIRE_SYSTEM_ROLE_REQUIRED")
    content = system[0]
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") for block in content if block.get("type") == "input_text"
        )
    match = re.match(r"^调查角色 (strong_single|fact|policy|verifier)。", content)
    if not match:
        raise RuntimeError("P5_WIRE_ROLE_REJECTED")
    role = match[1]
    context = view.get("policy_context")
    if role == "fact":
        if context is not None:
            raise RuntimeError("P5_WIRE_FACT_POLICY_CONTEXT_REJECTED")
    elif not isinstance(context, dict) or context.get("catalogue_sha256") != catalogue_sha256:
        raise RuntimeError("P5_WIRE_CATALOGUE_BINDING_REQUIRED")
    return role, "check_receipt" in view


def _phase(arm, role, recheck):
    phases = {
        "strong_single": {"strong_single": "single"},
        "single_staged": {"fact": "staged-fact", "policy": "staged-policy", "verifier": "verify-0"},
        "multi": {"fact": "fact", "policy": "policy", "verifier": "verify"},
    }
    if arm == "multi" and role in {"fact", "policy"} and recheck:
        return "recheck"
    phase = phases.get(arm, {}).get(role)
    if not phase or recheck:
        raise RuntimeError("P5_WIRE_ARM_ROLE_REJECTED")
    return phase


def admit(directory, plan, write, request_bytes, tool_names, *, role, recheck=False):
    with _locked(directory) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            row = _binding(plan, json.loads((directory / "active-row.json").read_bytes()))
        except (FileNotFoundError, KeyError, ValueError):
            raise RuntimeError("P5_WIRE_ACTIVE_ROW_REQUIRED") from None
        phase = _phase(row["arm"], role, recheck)
        records = sorted(directory.glob("admission-*.json"))
        admissions = [json.loads(path.read_bytes()) for path in records]
        spent = sum(item["reserved_microusd"] for item in admissions)
        same_row = [item for item in admissions if item["row_id"] == row["id"]]
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        for item in same_row:
            if item["request_sha256"] != request_sha256:
                continue
            response_path = directory / f"response-{item['call_index']:02}.json"
            try:
                response = json.loads(response_path.read_bytes())
            except (OSError, ValueError):
                raise RuntimeError("P5_WIRE_UNKNOWN_REQUEST_REPLAY") from None
            if (
                response.get("row_id") != row["id"]
                or response.get("call_index") != item["call_index"]
                or type(response.get("http_status")) is not int
            ):
                raise RuntimeError("P5_WIRE_UNKNOWN_REQUEST_REPLAY")
            status = response["http_status"]
            if status != 429 and not 500 <= status <= 599:
                raise RuntimeError("P5_WIRE_NONRETRYABLE_REQUEST_REPLAY")
        row_spent = sum(item["reserved_microusd"] for item in same_row)
        reserve = plan["per_post_reserved_microusd"]
        if (
            type(reserve) is not int
            or not 1 <= reserve <= MAX_ROW_MICROUSD
            or len(admissions) >= MAX_CAMPAIGN_POSTS
            or len(same_row) >= MAX_ROW_POSTS
            or spent + reserve > MAX_CAMPAIGN_MICROUSD
            or row_spent + reserve > MAX_ROW_MICROUSD
        ):
            raise RuntimeError("P5_WIRE_ADMISSION_LIMIT")
        index = len(admissions) + 1
        write(
            directory / f"admission-{index:02}.json",
            {
                "call_index": index,
                "status": "DELIVERY_UNKNOWN",
                "row_id": row["id"],
                "arm": row["arm"],
                "role": role,
                "phase_family": phase,
                "model": plan["model"],
                "output_tokens": plan["output_tokens"],
                "reserved_microusd": reserve,
                "request_sha256": request_sha256,
                "serialized_body_bytes": len(request_bytes),
                "tool_names": tool_names,
                "retry_index": 0,
                "attempt_binding": "ROW_ONLY_NOT_MATCHED_TO_LOGICAL_ATTEMPT",
            },
        )
        return index, row["id"]
