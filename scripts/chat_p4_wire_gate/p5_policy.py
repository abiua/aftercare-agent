"""Row-bound admission extension for the one-case P5 paid text pilot."""

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager

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


def _indexed_v7_refs(view, role, approved, valid_ref):
    """Resolve verifier-only indices without trusting evidence outside the pinned scope."""
    encoding = view.get("verifier_encoding")
    if (
        role != "verifier"
        or view.get("evidence_encoding") != "compact-v7"
        or encoding not in {"verifier-record-index-v1", "verifier-record-index-v2"}
    ):
        return None
    table = view.get("original_frozen_evidence")
    if (
        not isinstance(table, dict)
        or table.keys() != {"columns", "rows"}
        or table["columns"] != ["ref", "trust", "data"]
        or not isinstance(table["rows"], list)
    ):
        return None

    def complete(ref):
        return (
            valid_ref(ref)
            and len(ref) in (3, 4)
            and (len(ref) == 3 or isinstance(ref[3], str) and bool(ref[3]))
            and tuple(ref[:3]) in approved
        )

    original = []
    for row in table["rows"]:
        if (
            not isinstance(row, list)
            or len(row) != 3
            or not complete(row[0])
            or row[1]
            not in ("VERIFIED_RECORD", "CUSTOMER_ATTACHMENT", "UNVERIFIED_OBSERVATION", "POLICY")
            or not isinstance(row[2], dict)
        ):
            return None
        original.append(row[0])
    if len({tuple(ref[:3]) for ref in original}) != len(original):
        return None

    def expand(refs, *, available=False):
        if not isinstance(refs, list):
            return None
        resolved = []
        for ref in refs:
            if type(ref) is int:
                if not 0 <= ref < len(original):
                    return None
                ref = original[ref]
            if available and encoding == "verifier-record-index-v2" and isinstance(ref, str):
                candidates = [row for row in approved if row[:2] == ("POLICY", ref)]
                if len(candidates) != 1:
                    return None
                ref = list(candidates[0])
            if not complete(ref):
                return None
            resolved.append(ref[:3])
        return resolved

    receipts = view.get("original_read_receipts")
    if not isinstance(receipts, list) or any(
        not isinstance(receipt, dict) or expand(receipt.get("refs")) is None for receipt in receipts
    ):
        return None
    corpus_rows = view.get("policy_corpus_rows", [])
    if (
        not isinstance(corpus_rows, list)
        or not all(
            type(index) is int and 0 <= index < len(original) and original[index][0] == "POLICY"
            for index in corpus_rows
        )
        or len(set(corpus_rows)) != len(corpus_rows)
    ):
        return None
    return expand(view.get("available_evidence"), available=True)


def validate_v7_references(plan, role, view):
    expected = plan["role_evidence"][role]
    refs = view.get("available_evidence")
    if "verifier_encoding" in view:
        approved = {tuple(ref) for role_refs in plan["role_evidence"].values() for ref in role_refs}

        def valid_ref(ref):
            return (
                isinstance(ref, list)
                and len(ref) >= 3
                and all(isinstance(value, str) and bool(value) for value in ref[:3])
                and re.fullmatch(r"[a-f0-9]{64}", ref[2]) is not None
            )

        actual = _indexed_v7_refs(view, role, approved, valid_ref)
    else:
        actual = (
            [ref[:3] for ref in refs]
            if isinstance(refs, list)
            and all(isinstance(ref, list) and len(ref) >= 3 for ref in refs)
            else None
        )
    if actual != expected:
        raise RuntimeError("P4_WIRE_V7_EVIDENCE_BINDING_REJECTED")
    if (
        role in {"fact", "strong_single"}
        and [row.get("ref", [])[:3] for row in view.get("unverified_observations", [])]
        != plan["observation_commitments"]
    ):
        raise RuntimeError("P4_WIRE_V7_OBSERVATION_BINDING_REJECTED")
