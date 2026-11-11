"""Policy and private journal for one owned live-business wire phase."""

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import contextmanager

PROTOCOL = "p5-live-business-wire-v1"
SCOPED_PROTOCOL = "p5-live-business-wire-v2"
EXPANDED_PROTOCOL = "p5-live-business-wire-v3"
SCOPED_PROTOCOLS = {SCOPED_PROTOCOL, EXPANDED_PROTOCOL}
SCOPES = {
    "SINGLE": ("C001", "O9902", ["L01"]),
    "MULTI": ("C001", "DEV-ORDER-04", ["L01", "L02"]),
}
_ROUTE_PHASES = {
    "SINGLE": {"single"},
    "MULTI": {"fact", "policy", "verify-0", "verify-1", "recheck"},
}
URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-6.1-terra"
CAPS = {"VISION": (1, 50_000, 44_500, 900), "TEXT": (8, 400_000, 39_720, 1800)}
TOOLS = {
    "strong_single": {
        "read_order_lines",
        "read_material_evidence",
        "read_refund_execution",
        "search_applicable_policy",
        "read_policy_clause",
    },
    "fact": {"read_order_lines", "read_material_evidence", "read_refund_execution"},
    "policy": {"search_applicable_policy", "read_policy_clause"},
    "verifier": {"request_targeted_check"},
}
_BINDING_IDS = ("conversation_id", "case_id", "issue_id", "turn_id")
_TEXT_DIGESTS = ("scope_sha256", "relevant_input_sha256", "policy_catalogue_sha256")
_TEXT_PIN = ("run_id", *_TEXT_DIGESTS, "role_evidence")


class WireDenied(RuntimeError):
    pass


def _require(condition, code):
    if not condition:
        raise WireDenied(f"LIVE_BUSINESS_WIRE_{code}")


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is not None


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _hashes(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(map(_sha, value))
        and len(set(value)) == 2
    )


def validate_plan(plan):
    _require(isinstance(plan, dict), "PLAN")
    kind = plan.get("kind")
    _require(kind in CAPS, "PLAN_KIND")
    protocol = plan.get("protocol")
    posts, total, reserve, output = (
        (8, 600_000, 51_200, 4096)
        if protocol == EXPANDED_PROTOCOL and kind == "TEXT"
        else CAPS[kind]
    )
    _require(
        protocol in {PROTOCOL, *SCOPED_PROTOCOLS}
        and plan.get("model") == MODEL
        and all(_identity(plan.get(key)) for key in (*_BINDING_IDS, "campaign_id"))
        and all(
            type(plan.get(field)) is int and plan[field] == expected
            for field, expected in (
                ("maximum_provider_posts", posts),
                ("maximum_reservation_microusd", total),
                ("per_post_reserved_microusd", reserve),
                ("output_tokens", output),
            )
        ),
        "PLAN_LIMITS",
    )
    if plan["protocol"] in SCOPED_PROTOCOLS:
        route = plan.get("expected_route")
        scope = plan.get("case_scope")
        _require(
            isinstance(route, str)
            and route in SCOPES
            and isinstance(scope, dict)
            and scope.keys() == {"customer_id", "order_id", "line_ids", "order_version"}
            and (scope["customer_id"], scope["order_id"], scope["line_ids"]) == SCOPES[route]
            and type(scope["order_version"]) is int
            and scope["order_version"] == 1,
            "PLAN_NATIVE_CASE_SCOPE",
        )
    else:
        _require(
            "case_scope" not in plan and "expected_route" not in plan, "PLAN_NATIVE_CASE_SCOPE"
        )
    if kind == "VISION":
        _require(
            all(_hashes(plan.get(key)) for key in ("original_sha256s", "sent_sha256s")),
            "PLAN_IMAGES",
        )
    else:
        present = {key for key in _TEXT_PIN if key in plan}
        _require(not present or (present == set(_TEXT_PIN) and _valid_text_pin(plan)), "PLAN_SCOPE")


def _valid_ref(ref):
    return (
        isinstance(ref, list)
        and len(ref) >= 3
        and isinstance(ref[0], str)
        and bool(ref[0])
        and isinstance(ref[1], str)
        and bool(ref[1])
        and _sha(ref[2])
    )


def _valid_text_pin(binding):
    refs = binding.get("role_evidence")
    return (
        _identity(binding.get("run_id"))
        and all(_sha(binding.get(key)) for key in _TEXT_DIGESTS)
        and isinstance(refs, dict)
        and refs.keys() == TOOLS.keys()
        and all(
            isinstance(rows, list)
            and all(_valid_ref(ref) and len(ref) == 3 for ref in rows)
            and len({tuple(ref[:2]) for ref in rows}) == len(rows)
            for rows in refs.values()
        )
    )


def admitted_logical_phase(plan, logical_id):
    """Test the expected v2 text phase before starting a native reservation."""
    if plan["protocol"] not in SCOPED_PROTOCOLS or plan["kind"] != "TEXT":
        return True
    match = re.fullmatch(r"p4:compact-v7:([a-z0-9-]+):[0-7]", logical_id)
    return bool(match and match[1] in _ROUTE_PHASES[plan["expected_route"]])


def validate_binding(plan, binding):
    _require(isinstance(binding, dict), "ATTEMPT_REQUIRED")
    logical = binding.get("native_logical_id")
    ordinal = binding.get("native_ordinal")
    reserve = binding.get("reserved_microusd")
    _require(
        all(binding.get(key) == plan[key] for key in ("kind", *_BINDING_IDS))
        and isinstance(logical, str)
        and type(ordinal) is int
        and ordinal == 1
        and type(reserve) is int
        and 0 < reserve <= plan["per_post_reserved_microusd"],
        "NATIVE_ATTEMPT_BINDING",
    )
    if plan["protocol"] in SCOPED_PROTOCOLS:
        _require(
            binding.get("case_scope") == plan["case_scope"]
            and binding.get("expected_route") == plan["expected_route"],
            "NATIVE_CASE_SCOPE_CHANGED",
        )
    if plan["kind"] == "VISION":
        _require(
            re.fullmatch(r"vision:[a-f0-9]{64}", logical)
            and all(binding.get(key) == plan[key] for key in ("original_sha256s", "sent_sha256s")),
            "NATIVE_IMAGE_BINDING",
        )
        return
    _require(
        _valid_text_pin(binding)
        and all(binding[key] == plan[key] for key in _TEXT_PIN if key in plan)
        and re.fullmatch(r"p4:compact-v7:(single|fact|policy|verify-[01]|recheck):[0-7]", logical),
        "NATIVE_TEXT_SCOPE",
    )

    if plan["protocol"] == EXPANDED_PROTOCOL:
        _require(
            type(binding.get("native_output_tokens")) is int
            and binding["native_output_tokens"] == plan["output_tokens"],
            "NATIVE_OUTPUT_RESERVE",
        )

    if plan["protocol"] in SCOPED_PROTOCOLS:
        _require(
            admitted_logical_phase(plan, logical),
            "NATIVE_ROUTE_PHASE",
        )


def _text(content):
    if isinstance(content, str):
        return content
    _require(
        isinstance(content, list)
        and all(
            isinstance(item, dict)
            and item.get("type") == "input_text"
            and isinstance(item.get("text"), str)
            for item in content
        ),
        "TEXT_CONTENT",
    )
    return "".join(item["text"] for item in content)


def _messages(payload):
    inputs = payload.get("input")
    _require(isinstance(inputs, list) and all(isinstance(item, dict) for item in inputs), "INPUT")
    system = [row for row in inputs if row.get("role") in {"system", "developer"}]
    user = [row for row in inputs if row.get("role") == "user"]
    _require(
        len(system) == len(user) == 1 and inputs[:2] == [system[0], user[0]], "MESSAGE_BOUNDARY"
    )
    return _text(system[0].get("content")), user[0].get("content"), inputs[2:]


def _vision_payload(plan, payload):
    system, content, remainder = _messages(payload)
    _require(
        system.startswith("仅观察当前编号照片，返回一个 JSON 对象。")
        and not remainder
        and not payload.get("tools")
        and isinstance(content, list)
        and len(content) == 4
        and all(isinstance(item, dict) for item in content),
        "VISION_INPUT",
    )
    hashes = []
    for index in range(2):
        label, image = content[2 * index : 2 * index + 2]
        _require(label == {"type": "input_text", "text": f"照片 {index + 1}"}, "VISION_LABEL")
        url = image.get("image_url")
        prefix = "data:image/jpeg;base64,"
        _require(
            image.get("type") == "input_image"
            and image.get("detail") == "low"
            and isinstance(url, str)
            and url.startswith(prefix),
            "VISION_IMAGE",
        )
        try:
            decoded = base64.b64decode(url[len(prefix) :], validate=True)
        except (ValueError, binascii.Error):
            raise WireDenied("LIVE_BUSINESS_WIRE_VISION_IMAGE") from None
        hashes.append(_hash(decoded))
    _require(hashes == plan["sent_sha256s"], "VISION_IMAGE_DIGEST")
    return {"sent_sha256s": hashes, "tool_names": []}


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _phase_role(phase, role):
    if phase == "recheck":
        return role in {"fact", "policy"}
    expected = {
        "single": "strong_single",
        "fact": "fact",
        "policy": "policy",
        "verify-0": "verifier",
        "verify-1": "verifier",
    }
    return expected.get(phase) == role


def _check_transcript(items, role):
    calls, outputs = set(), set()
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            ident = item.get("call_id")
            _require(
                isinstance(ident, str)
                and bool(ident)
                and ident not in calls
                and item.get("name") in TOOLS[role],
                "TEXT_TRANSCRIPT_TOOL",
            )
            calls.add(ident)
        elif kind == "function_call_output":
            ident = item.get("call_id")
            _require(
                isinstance(ident, str) and ident in calls and ident not in outputs,
                "TEXT_TRANSCRIPT_TOOL",
            )
            outputs.add(ident)
        elif kind == "reasoning":
            _require(
                isinstance(item.get("encrypted_content"), str) and bool(item["encrypted_content"]),
                "TEXT_TRANSCRIPT",
            )
        else:
            _require(
                item.get("role") == "assistant" and kind in (None, "message"), "TEXT_TRANSCRIPT"
            )
    _require(calls == outputs, "TEXT_TRANSCRIPT_TOOL")


def _text_payload(plan, binding, payload, raw):
    system, content, remainder = _messages(payload)
    role_match = re.match(r"^调查角色 (strong_single|fact|policy|verifier)。", system)
    _require(bool(role_match), "TEXT_ROLE")
    role = role_match[1]
    phase = binding["native_logical_id"].split(":")[2]
    _require(_phase_role(phase, role), "TEXT_PHASE")
    tools = payload.get("tools", [])
    _require(
        isinstance(tools, list)
        and all(
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and tool.get("name") in TOOLS[role]
            for tool in tools
        ),
        "TEXT_TOOLS",
    )
    names = [tool["name"] for tool in tools]
    _require(len(set(names)) == len(names), "TEXT_TOOLS")
    _require(
        not any(
            row.get("type")
            in {"input_image", "image_url", "input_file", "input_audio", "computer_call"}
            for row in _objects(payload["input"])
        )
        and b"data:image/" not in raw
        and b"base64," not in raw,
        "TEXT_ONLY",
    )
    _check_transcript(remainder, role)
    try:
        user = json.loads(_text(content))
    except (ValueError, TypeError):
        raise WireDenied("LIVE_BUSINESS_WIRE_TEXT_INPUT") from None
    _require(
        isinstance(user, dict) and set(user) == {"frozen_input", "upstream_claims"}, "TEXT_INPUT"
    )
    view = user["frozen_input"]
    scope = [plan[key] for key in ("conversation_id", "issue_id", "case_id")]
    _require(
        isinstance(view, dict)
        and view.get("evidence_encoding") == "compact-v7"
        and view.get("scope") == scope
        and view.get("input_digest") == binding["relevant_input_sha256"],
        "TEXT_SCOPE",
    )
    context = view.get("policy_context")
    _require(
        context is None
        if role == "fact"
        else isinstance(context, dict)
        and context.get("catalogue_sha256") == binding["policy_catalogue_sha256"],
        "TEXT_CATALOGUE",
    )
    refs = view.get("available_evidence")
    _require(
        isinstance(refs, list)
        and all(map(_valid_ref, refs))
        and [ref[:3] for ref in refs] == binding["role_evidence"][role],
        "TEXT_EVIDENCE",
    )
    for row in _objects(view):
        if isinstance(row.get("scope"), list):
            _require(row["scope"] == scope, "TEXT_EVIDENCE_SCOPE")
    return {"role": role, "phase": phase, "tool_names": names}


def verify_request(plan, binding, request, api_key):
    validate_binding(plan, binding)
    headers = request.headers
    _require(request.method == "POST", "REQUEST_METHOD")
    _require(str(request.url) == URL, "REQUEST_URL")
    authorization = headers.get("authorization", "")
    _require(
        isinstance(api_key, str)
        and bool(api_key)
        and api_key.isascii()
        and isinstance(authorization, str)
        and authorization.isascii()
        and hmac.compare_digest(authorization, "Bearer " + api_key),
        "REQUEST_AUTHORIZATION",
    )
    _require(headers.get("x-stainless-retry-count") == "0", "REQUEST_RETRY_HEADER")
    _require(headers.get("content-type") == "application/json", "REQUEST_CONTENT_TYPE")
    for name, code in (
        ("cookie", "REQUEST_COOKIE"),
        ("proxy-authorization", "REQUEST_PROXY_AUTHORIZATION"),
        ("openai-project", "REQUEST_PROJECT"),
        ("openai-organization", "REQUEST_ORGANIZATION"),
    ):
        _require(name not in headers, code)
    raw = request.content
    _require(len(raw) <= (300_000 if plan["kind"] == "VISION" else 200_000), "REQUEST_SIZE")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise WireDenied("LIVE_BUSINESS_WIRE_JSON") from None
    _require(
        isinstance(payload, dict)
        and payload.get("model") == MODEL
        and type(payload.get("max_output_tokens")) is int
        and payload["max_output_tokens"] == plan["output_tokens"]
        and payload.get("store") is False
        and payload.get("stream") in (None, False)
        and not any(
            key in payload
            for key in (
                "previous_response_id",
                "conversation",
                "background",
                "prompt_cache_options",
                "metadata",
            )
        ),
        "REQUEST_OPTIONS",
    )
    checked = (
        _vision_payload(plan, payload)
        if plan["kind"] == "VISION"
        else _text_payload(plan, binding, payload, raw)
    )
    return {"request_sha256": _hash(raw), "serialized_body_bytes": len(raw), **checked}


def _private(path):
    info = path.lstat()
    _require(
        stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "PRIVATE_FILE",
    )
    return path.read_bytes()


def read_plan(directory, expected):
    raw = _private(directory / "plan.json")
    _require(_sha(expected) and _hash(raw) == expected, "PLAN_DIGEST")
    plan = json.loads(raw)
    validate_plan(plan)
    _private(directory / "lock")
    return plan


def _write(directory, name, payload):
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _lock(directory):
    with (directory / "lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def send(directory, plan, binding, request, api_key, transport, *, observe_stage=None):
    """Serialize delivery so an unclosed request blocks every later admission."""
    if observe_stage is not None:
        observe_stage("validate")
    checked = verify_request(plan, binding, request, api_key)
    with _lock(directory):
        admissions = sorted(directory.glob("admission-*.json"))
        responses = sorted(directory.glob("response-*.json"))
        _require(
            len(admissions) == len(responses)
            and all(
                path.name == f"admission-{index:02}.json"
                for index, path in enumerate(admissions, 1)
            )
            and all(
                path.name == f"response-{index:02}.json" for index, path in enumerate(responses, 1)
            ),
            "UNKNOWN_DELIVERY",
        )
        previous = []
        for index, (admitted, response) in enumerate(zip(admissions, responses, strict=True), 1):
            row, settled = json.loads(_private(admitted)), json.loads(_private(response))
            _require(
                row["index"] == settled["index"] == index
                and row["kind"] == plan["kind"]
                and row["campaign_id"] == plan["campaign_id"]
                and row["wire_reserved_microusd"] == plan["per_post_reserved_microusd"]
                and row["output_tokens"] == plan["output_tokens"]
                and (
                    plan["protocol"] != EXPANDED_PROTOCOL
                    or plan["kind"] != "TEXT"
                    or row.get("native_output_tokens") == plan["output_tokens"]
                )
                and all(row[key] == plan[key] for key in _BINDING_IDS)
                and (
                    plan["protocol"] not in SCOPED_PROTOCOLS
                    or (
                        row.get("case_scope") == plan["case_scope"]
                        and row.get("expected_route") == plan["expected_route"]
                    )
                )
                and row["request_sha256"] == settled["request_sha256"]
                and type(settled.get("http_status")) is int
                and 100 <= settled["http_status"] <= 599,
                "LEDGER_INTEGRITY",
            )
            previous.append(row)
        index = len(previous) + 1
        _require(index <= plan["maximum_provider_posts"], "POST_CAP")
        _require(
            sum(row["wire_reserved_microusd"] for row in previous)
            + plan["per_post_reserved_microusd"]
            <= plan["maximum_reservation_microusd"],
            "RESERVATION_CAP",
        )
        _require(
            all(
                row["request_sha256"] != checked["request_sha256"]
                and row["native_logical_id"] != binding["native_logical_id"]
                for row in previous
            ),
            "REPLAY",
        )
        public_keys = (
            ("run_id", *_TEXT_DIGESTS) if plan["kind"] == "TEXT" else ("original_sha256s",)
        )
        if plan["kind"] == "TEXT":
            pin = _hash(
                json.dumps(
                    {key: binding[key] for key in _TEXT_PIN},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            _require(
                all(row["native_scope_pin"] == pin for row in previous), "NATIVE_SCOPE_CHANGED"
            )
        else:
            pin = None
        admitted = {
            "index": index,
            "status": "DELIVERY_UNKNOWN",
            "kind": plan["kind"],
            "campaign_id": plan["campaign_id"],
            "model": plan["model"],
            "output_tokens": plan["output_tokens"],
            "native_scope_pin": pin,
            **(
                {"native_output_tokens": binding["native_output_tokens"]}
                if plan["protocol"] == EXPANDED_PROTOCOL and plan["kind"] == "TEXT"
                else {}
            ),
            **(
                {"case_scope": plan["case_scope"], "expected_route": plan["expected_route"]}
                if plan["protocol"] in SCOPED_PROTOCOLS
                else {}
            ),
            "wire_reserved_microusd": plan["per_post_reserved_microusd"],
            **{
                key: binding[key]
                for key in (
                    *_BINDING_IDS,
                    "native_logical_id",
                    "native_ordinal",
                    "reserved_microusd",
                    *public_keys,
                )
            },
            **checked,
        }
        if observe_stage is not None:
            observe_stage("validate", index=index)
        _write(directory, f"admission-{index:02}.json", admitted)
        if observe_stage is not None:
            observe_stage("send", index=index, admitted=True)
        response = transport(request)
        if observe_stage is not None:
            observe_stage("read", index=index, admitted=True)
        response.read()
        cookie_receipt = {}
        if plan["protocol"] in SCOPED_PROTOCOLS:
            discarded = "set-cookie" in response.headers
            if discarded:
                del response.headers["set-cookie"]
            cookie_receipt["set_cookie_discarded"] = discarded
        _write(
            directory,
            f"response-{index:02}.json",
            {
                "index": index,
                "request_sha256": checked["request_sha256"],
                "http_status": response.status_code,
                **cookie_receipt,
            },
        )
        return response
