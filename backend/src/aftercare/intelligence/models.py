"""Model registry, budgeted LangChain adapter and replayable model attempts."""

import json
import os
import re
from collections.abc import Callable
from contextvars import Context
from copy import copy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)

from aftercare.business.rules import digest
from aftercare.intelligence.model_limits import ProviderLimitError
from aftercare.intelligence.native_stream import StreamInterrupted, collect_stream

MAX_ATTEMPTS = 12
MAX_LOGICAL_ATTEMPTS = 3
TRANSPORT_EVIDENCE_VERSION = "ac-attempt-transport-v1"
CACHE_WRITE_MODELS = {"gpt-6.1-sol", "gpt-6.1-terra"}
VISION_INPUT_PREFIX = "data:image/jpeg;base64,"
MAX_VISION_IMAGES = 6
MAX_VISION_DATA_URL = 256 * 1024 + len(VISION_INPUT_PREFIX)
SAFE_VALIDATION_FIELDS = {
    "intent",
    "options",
    "REFUND",
    "REPLACE",
    "REQUEST_EVIDENCE",
    "NOT_SUPPORTED",
    "HUMAN_HANDOFF",
    "notes",
    "action",
    "lines",
    "line_id",
    "amount_cents",
    "quantity",
    "citations",
    "id",
    "version",
    "quote",
    "conflicts",
    "photos",
    "image_index",
    "candidate_kind",
    "uncertainty",
    "findings",
    "recheck",
    "read_order_lines",
    "read_material_evidence",
    "read_refund_execution",
    "search_applicable_policy",
    "read_policy_clause",
    "arguments",
    "target_role",
    "claim_ids",
    "tool_name",
    "line_ids",
    "evidence_ids",
    "execution_id",
    "query",
    "condition_keys",
    "policy_id",
    "product_id",
}


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: Literal["economy", "analysis"]
    provider: str
    model: str
    endpoint: str
    api_key_env: str
    interface: Literal["chat", "responses"] = "chat"
    tool_calls: bool = True
    structured_output: bool = True
    streaming: bool = False
    streaming_tools: bool = False
    streaming_usage: bool = False
    streaming_checked_at: date | None = None
    streaming_compatibility_version: str | None = None
    fallback_aliases: list[Literal["economy", "analysis"]] | None = None
    compatibility_group: str | None = None
    context_tokens: int = Field(default=16000, gt=0)
    output_tokens: int = Field(default=900, gt=0)
    data_scopes: list[str] = Field(default_factory=lambda: ["simulated_case"])
    healthy: bool = True
    compatibility: Literal["UNVERIFIED", "VERIFIED"] = "UNVERIFIED"
    compatibility_version: str | None = None
    source: str | None = None
    checked_at: date | None = None
    price_date: date | None = None
    input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_read_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_ttl: Literal["30m", "12h"] | None = None

    vision: bool = False
    vision_checked_at: date | None = None
    vision_compatibility_version: str | None = None
    vision_input_tokens_per_image: int | None = Field(default=None, strict=True, gt=0)

    @model_serializer(mode="wrap")
    def preserve_legacy_digest(self, handler):
        data = handler(self)
        if not self.vision:
            for key in (
                "vision",
                "vision_checked_at",
                "vision_compatibility_version",
                "vision_input_tokens_per_image",
            ):
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def validate_metadata(self):
        if self.compatibility == "VERIFIED" and not (
            self.compatibility_version and self.source and self.checked_at
        ):
            raise ValueError("A verified model requires dated compatibility evidence")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("Use an environment variable name for api_key_env")
        if (
            self.input_usd_per_million is not None or self.output_usd_per_million is not None
        ) and not (self.price_date and self.source):
            raise ValueError("Price estimates require date and source")
        cache_prices = (
            self.cache_read_usd_per_million,
            self.cache_write_usd_per_million,
            self.cache_write_ttl,
        )
        if any(value is not None for value in cache_prices) and not (
            all(value is not None for value in cache_prices)
            and self.input_usd_per_million is not None
            and self.output_usd_per_million is not None
        ):
            raise ValueError("Cache prices require input/output prices, both cache rates and TTL")
        if (self.streaming_tools or self.streaming_usage) and not self.streaming:
            raise ValueError("Streaming sub-capabilities require streaming")
        if self.streaming and not (
            self.streaming_checked_at and self.streaming_compatibility_version
        ):
            raise ValueError("Streaming requires dated wire compatibility evidence")
        if self.alias in (self.fallback_aliases or []):
            raise ValueError("A model cannot be its own fallback")
        if self.cache_write_ttl == "12h" and self.interface != "responses":
            raise ValueError("12-hour caching requires Responses API")
        vision_fields = (
            self.vision_checked_at,
            self.vision_compatibility_version,
            self.vision_input_tokens_per_image,
        )
        if self.vision and not (all(vision_fields) and self.compatibility == "VERIFIED"):
            raise ValueError(
                "Vision requires dated transport evidence and a per-image token upper bound"
            )
        if not self.vision and any(value is not None for value in vision_fields):
            raise ValueError("Vision metadata requires vision to be enabled")
        return self


class ProviderLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    max_in_flight: int = Field(ge=1, le=64)
    requests_per_minute: int = Field(ge=1, le=6000)


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    models: list[ModelSpec]
    allow_unverified: bool = False
    allowed_providers: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=MAX_ATTEMPTS, ge=1, le=MAX_ATTEMPTS)
    deadline_seconds: int = Field(default=120, ge=1, le=240)
    timeout_seconds: float = Field(default=30, gt=0, le=90)
    cooldown_seconds: float = Field(default=5, ge=0, le=120)
    max_usd: Decimal | None = Field(default=None, gt=0)
    max_run_attempts: int | None = Field(default=None, ge=1, le=144)
    max_run_usd: Decimal | None = Field(default=None, gt=0)
    native_streaming: bool = False
    provider_limits: dict[str, ProviderLimit] = Field(default_factory=dict)

    @model_validator(mode="after")
    def distinct_models(self):
        if len({spec.alias for spec in self.models}) != len(self.models):
            raise ValueError("Duplicate model alias")
        if len({(spec.provider, spec.model) for spec in self.models}) != len(self.models):
            raise ValueError("Aliases must map to distinct actual models")
        return self

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text())


P4_OUTPUT_RESERVE = 1800
P4_EXTENDED_OUTPUT_RESERVE = 4096
P4_INPUT_LIMIT = 24_576


@dataclass(frozen=True)
class TaskSpec:
    preferred: str
    prompt_tokens: int
    output_reserve: int = 900
    tools: bool = True
    structured: bool = True
    data_scope: str = "simulated_case"
    streaming: bool = False
    image_count: int = 0

    def input_tokens(self, model: ModelSpec) -> int:
        return self.prompt_tokens + self.image_count * (model.vision_input_tokens_per_image or 0)


class ModelFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidOutput(ValueError):
    def __init__(self, code: str, field: str = "output", *, tool_counts=None):
        self.code, self.field, self.tool_counts = code, field, tool_counts
        super().__init__(code)


def _validation_fields(error):
    if isinstance(error, ValidationError):
        return [
            {
                "field": ".".join(
                    str(part)
                    if (
                        (type(part) is int or part in SAFE_VALIDATION_FIELDS)
                        and not (
                            issue["type"] == "extra_forbidden" and index == len(issue["loc"]) - 1
                        )
                    )
                    else "unknown_field"
                    for index, part in enumerate(issue["loc"])
                )
                or "output",
                "code": issue["type"],
            }
            for issue in error.errors(
                include_input=False, include_context=False, include_url=False
            )[:8]
        ]
    if isinstance(error, InvalidOutput):
        issue = {"field": error.field, "code": error.code}
        if error.code == "TOOL_BATCH_LIMIT" and error.tool_counts is not None:
            attempted, allowed = error.tool_counts
            issue.update(attempted=attempted, allowed=allowed)
        return [issue]
    return [{"field": "output", "code": "INVALID_JSON"}]


def eligible_models(registry: Registry, task: TaskSpec) -> tuple[list[ModelSpec], dict[str, str]]:
    candidates, excluded = [], {}
    for spec in registry.models:
        if not spec.healthy:
            reason = "UNHEALTHY"
        elif spec.compatibility != "VERIFIED" and not registry.allow_unverified:
            reason = "UNVERIFIED"
        elif registry.allowed_providers and spec.provider not in registry.allowed_providers:
            reason = "PROVIDER_RESTRICTED"
        elif registry.provider_limits and spec.provider not in registry.provider_limits:
            reason = "PROVIDER_LIMIT_UNCONFIGURED"
        elif task.data_scope not in spec.data_scopes:
            reason = "DATA_SCOPE_RESTRICTED"
        elif task.data_scope == "customer_conversation" and spec.cache_write_ttl == "12h":
            reason = "CONVERSATION_RETENTION_RESTRICTED"
        elif task.image_count and not spec.vision:
            reason = "VISION_UNSUPPORTED"
        elif (
            task.image_count
            and task.data_scope == "customer_attachment"
            and spec.cache_write_ttl == "12h"
        ):
            reason = "VISION_RETENTION_RESTRICTED"
        elif task.tools and not spec.tool_calls:
            reason = "TOOLS_UNSUPPORTED"
        elif task.streaming and (not spec.streaming or (task.tools and not spec.streaming_tools)):
            reason = "STREAM_UNSUPPORTED"
        elif task.structured and not spec.structured_output:
            reason = "OUTPUT_UNSUPPORTED"
        elif spec.output_tokens < task.output_reserve:
            reason = "OUTPUT_LIMIT"
        elif spec.context_tokens < task.input_tokens(spec) + task.output_reserve:
            reason = "CONTEXT_LIMIT"
        elif (registry.max_usd is not None or registry.max_run_usd is not None) and (
            spec.input_usd_per_million is None or spec.output_usd_per_million is None
        ):
            reason = "UNPRICED_MODEL"
        elif (
            (registry.max_usd is not None or registry.max_run_usd is not None)
            and spec.provider == "openai"
            and spec.model in CACHE_WRITE_MODELS
            and spec.cache_write_usd_per_million is None
        ):
            reason = "UNPRICED_PROMPT_CACHE"
        else:
            candidates.append(spec)
            continue
        excluded[spec.alias] = reason
    return sorted(candidates, key=lambda candidate: candidate.alias != task.preferred), excluded


def _client(spec: ModelSpec, tools: list[dict], timeout: float, output: int):
    from langchain_openai import ChatOpenAI

    api_key = os.getenv(spec.api_key_env)
    if not api_key:
        raise ModelFailure("MISSING_API_KEY")
    client = ChatOpenAI(
        model=spec.model,
        base_url=spec.endpoint,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
        max_tokens=output,
        use_responses_api=spec.interface == "responses",
        # Economy tasks use tool calls without a provider-specific reasoning transcript.
        **({"extra_body": {"thinking": {"type": "disabled"}}} if spec.provider == "deepseek" else {}),
        **({"store": False} if spec.interface == "responses" else {}),
        **(
            {"prompt_cache_options": {"mode": "implicit", "ttl": "12h"}}
            if spec.cache_write_ttl == "12h"
            else {}
        ),
    )
    return client.bind_tools(tools) if tools else client


def _private_transport(spec, messages, tools, timeout, output):
    # The low-level provider generation path does not install a callback manager.
    from langsmith import tracing_context

    def generate_private():
        with tracing_context(enabled=False, parent=False):
            client = _client(spec, [], timeout, output)
            kwargs = client.bind_tools(tools).kwargs if tools else {}
            generation = client._generate(messages, run_manager=None, **kwargs).generations[0]
            message = generation.message
            for key in ("finish_reason", "status", "incomplete_details"):
                if key in (generation.generation_info or {}):
                    message.response_metadata.setdefault(key, generation.generation_info[key])
            return message

    return Context().run(generate_private)


def _transport(
    spec: ModelSpec,
    messages: list[BaseMessage],
    tools: list[dict],
    timeout: float,
    output: int,
) -> AIMessage:
    if any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
    ):
        return _private_transport(spec, messages, tools, timeout, output)
    return _client(spec, tools, timeout, output).invoke(messages)


def _safe_termination(metadata):
    fields = {
        "status": {"completed", "incomplete", "failed", "cancelled", "in_progress", "queued"},
        "finish_reason": {"stop", "length", "content_filter", "tool_calls", "function_call"},
        "incomplete_reason": {"max_output_tokens", "content_filter"},
    }
    details = metadata.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    values = {**metadata, "incomplete_reason": reason}
    return {
        key: values[key] if values[key] in allowed else "OTHER"
        for key, allowed in fields.items()
        if isinstance(values.get(key), str)
    }


def _stream_transport(spec, messages, tools, timeout, output, on_delta):
    client = _client(spec, tools, timeout, output)
    opts = {"stream_usage": spec.streaming_usage} if spec.interface == "chat" else {}
    return collect_stream(
        client.stream(messages, **opts),
        interface=spec.interface,
        deadline=monotonic() + timeout,
        on_delta=on_delta,
    )


_DEFAULT_SYNC_TRANSPORT = _transport
_DEFAULT_STREAM_TRANSPORT = _stream_transport


def finish_transport_evidence(metadata, evidence):
    started = metadata.get("transport_evidence")
    if (
        evidence is not None
        and isinstance(started, dict)
        and all(
            started.get(key) == evidence.get(key)
            for key in ("version", "path", "max_output_tokens")
        )
    ):
        return {**metadata, "transport_evidence": evidence}
    return metadata


def _http_error_status(error):
    from openai import APIStatusError

    if isinstance(error, APIStatusError) and error.response is not None:
        status = error.response.status_code
        if type(status) is int and 400 <= status <= 599 and status == error.status_code:
            return status
    return None


def _failure(error: Exception) -> tuple[str, bool, bool]:
    import openai

    if isinstance(error, StreamInterrupted):
        return error.code, error.code == "MODEL_STREAM_INTERRUPTED", True
    if isinstance(error, ModelFailure):
        return error.code, False, False
    if isinstance(error, (openai.APITimeoutError, TimeoutError)):
        return "MODEL_TIMEOUT", True, True
    if isinstance(error, openai.APIConnectionError):
        return "MODEL_CONNECTION_FAILURE", True, True
    status = getattr(error, "status_code", None)
    if status == 429:
        return "MODEL_RATE_LIMIT", True, False
    if isinstance(status, int) and status >= 500:
        return "MODEL_UNAVAILABLE", True, False
    if status in {401, 403}:
        return "MODEL_AUTHORIZATION", False, False
    if status in {400, 404, 422}:
        return "MODEL_CONFIGURATION", False, False
    return "MODEL_ADAPTER_ERROR", False, False


def _reserve_price(spec: ModelSpec, input_tokens: int, output_tokens: int) -> Decimal | None:
    base, output = spec.input_usd_per_million, spec.output_usd_per_million
    if base is None or output is None:
        return None
    input_upper = max(
        rate
        for rate in (base, spec.cache_read_usd_per_million, spec.cache_write_usd_per_million)
        if rate is not None
    )
    return (input_tokens * input_upper + output_tokens * output) / 1_000_000


def _estimated_price(spec: ModelSpec, usage: dict) -> Decimal | None:
    base, output = spec.input_usd_per_million, spec.output_usd_per_million
    inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    details = usage.get("input_token_details") or {}
    reads, writes = details.get("cache_read", 0), details.get("cache_creation", 0)
    if (
        base is None
        or output is None
        or any(type(count) is not int or count < 0 for count in (inputs, outputs, reads, writes))
    ):
        return None
    if reads + writes > inputs or any(
        count
        for key, count in details.items()
        if key == "audio" or key.startswith(("priority", "flex"))
    ):
        return None
    read_rate, write_rate = spec.cache_read_usd_per_million, spec.cache_write_usd_per_million
    if spec.provider == "deepseek":
        # Conservative peak-price estimate: do not invent cache TTLs or discounts.
        read_rate = base if read_rate is None else read_rate
        write_rate = base if write_rate is None else write_rate
    if (reads and read_rate is None) or (writes and write_rate is None):
        return None
    ordinary = inputs - reads - writes
    return (
        ordinary * base + reads * (read_rate or 0) + writes * (write_rate or 0) + outputs * output
    ) / 1_000_000


def _content(response: AIMessage) -> str:
    if isinstance(response.content, str):
        return response.content
    return "".join(
        block.get("text", "")
        for block in response.content
        if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
    )


def _estimate_input(messages, tools, *, vision):
    payload = []
    images = 0
    for message in messages:
        row = message.model_dump(mode="json")
        if isinstance(row["content"], list):
            for block in row["content"]:
                if not isinstance(block, dict) or block.get("type") not in {
                    "image_url",
                    "image",
                    "input_image",
                }:
                    continue
                value = block.get("image_url")
                url = value.get("url") if isinstance(value, dict) else None
                if (
                    not vision
                    or block["type"] != "image_url"
                    or not isinstance(url, str)
                    or not url.startswith(VISION_INPUT_PREFIX)
                    or len(url) > MAX_VISION_DATA_URL
                    or value.get("detail") != "low"
                ):
                    raise ModelFailure("VISION_INPUT_UNSUPPORTED")
                images += 1
                value["url"] = VISION_INPUT_PREFIX
        payload.append(row)
    if vision and not 1 <= images <= MAX_VISION_IMAGES:
        raise ModelFailure("VISION_INPUT_LIMIT")
    serialized = json.dumps(payload + tools, ensure_ascii=False)
    return len(serialized.encode()) + 256, images


class ModelRunner:
    _logical_attempt_limit = MAX_LOGICAL_ATTEMPTS

    def __init__(
        self,
        registry: Registry,
        *,
        transport: Callable = _transport,
        stream_transport: Callable = _stream_transport,
    ):
        self._registry = registry.model_copy(deep=True)
        self._base_registry_digest = digest(self._registry.model_dump(mode="json"))
        self.transport = transport
        self.stream_transport = stream_transport

    @property
    def registry(self) -> Registry:
        # A caller can inspect or derive another registry without mutating this runner.
        return self._registry.model_copy(deep=True)

    @property
    def base_registry_digest(self) -> str:
        return self._base_registry_digest

    def with_evaluation_limits(self, *, calls: int, budget: Decimal) -> "ModelRunner":
        # The evaluation scheduler skips unallocated live rows before creating a runner.
        if type(calls) is not int or not 1 <= calls <= 144:
            raise ValueError("Evaluation call quota must be between 1 and 144")
        if not isinstance(budget, Decimal) or not budget.is_finite() or budget <= 0:
            raise ValueError("Evaluation cost quota must be a positive finite Decimal")
        registry = self._registry
        clone = copy(self)
        clone._registry = registry.model_copy(
            deep=True,
            update={
                "max_attempts": min(calls, registry.max_attempts),
                "max_usd": min(budget, registry.max_usd)
                if registry.max_usd is not None
                else budget,
                "max_run_attempts": min(calls, registry.max_run_attempts or calls),
                "max_run_usd": min(budget, registry.max_run_usd)
                if registry.max_run_usd is not None
                else budget,
            },
        )
        return clone

    def call(
        self,
        audit,
        logical_id: str,
        *,
        preferred: str,
        messages: list[BaseMessage],
        tools: list[dict],
        validate: Callable,
        fixed: bool,
        vision: bool = False,
        data_scope: str = "simulated_case",
        output_reserve: int = 900,
    ) -> dict:
        registry = self.registry
        if type(output_reserve) is not int or (
            output_reserve != 900
            and (
                data_scope != "customer_conversation"
                or vision
                or output_reserve not in {P4_OUTPUT_RESERVE, P4_EXTENDED_OUTPUT_RESERVE}
            )
        ):
            raise ModelFailure("MODEL_OUTPUT_BUDGET_UNSUPPORTED")
        if vision and not any(
            value is not None and value > 0 for value in (registry.max_usd, registry.max_run_usd)
        ):
            raise ModelFailure("VISION_BUDGET_REQUIRED")
        if vision and (
            not fixed or tools or any(isinstance(message, ToolMessage) for message in messages)
        ):
            raise ModelFailure("VISION_INPUT_UNSUPPORTED")
        prompt_tokens, images = _estimate_input(messages, tools, vision=vision)
        task = TaskSpec(
            preferred,
            prompt_tokens,
            output_reserve=output_reserve,
            tools=bool(tools) or any(isinstance(message, ToolMessage) for message in messages),
            streaming=registry.native_streaming
            and not vision
            and data_scope != "customer_conversation",
            data_scope=data_scope,
            image_count=images,
        )
        candidates, excluded = eligible_models(registry, task)
        cooldown = audit.cooldown_aliases(registry.cooldown_seconds)
        excluded.update(
            {spec.alias: "RATE_LIMIT_COOLDOWN" for spec in candidates if spec.alias in cooldown}
        )
        candidates = [spec for spec in candidates if spec.alias not in cooldown]
        if fixed:
            excluded.update(
                {spec.alias: "FIXED_STRATEGY" for spec in candidates if spec.alias != preferred}
            )
            candidates = [spec for spec in candidates if spec.alias == preferred]
        existing = (
            audit.replay(logical_id, expected_output_reserve=task.output_reserve)
            if data_scope == "customer_conversation"
            else audit.replay(logical_id)
        )
        if existing is not None:
            audit.confirm_admission()
            try:
                if set(existing) == {"calls"} and isinstance(existing["calls"], list):
                    return validate(existing["calls"], "")
                if set(existing) == {"result"} and isinstance(existing["result"], dict):
                    return validate([], json.dumps(existing["result"], ensure_ascii=False))
            except (KeyError, TypeError, ValueError):
                raise ModelFailure("REPLAY_VALIDATION_FAILED") from None
            raise ModelFailure("REPLAY_VALIDATION_FAILED")
        audit.event(
            "route",
            {
                "logical_id": logical_id,
                "preferred": preferred,
                "excluded": excluded,
                "selected": candidates[0].alias if candidates else None,
                "reason": "FIXED_STRATEGY" if fixed else "TASK_PREFERENCE",
                "registry_version": registry.version,
                "budget": audit.budget(registry),
            },
        )
        if not candidates:
            raise ModelFailure("NO_COMPATIBLE_MODEL")

        selected = candidates[0]
        fallback = next(
            (
                other
                for other in candidates[1:]
                if (selected.fallback_aliases is None or other.alias in selected.fallback_aliases)
                and (
                    not task.tools
                    or (
                        (selected.compatibility_group or selected.interface)
                        == (other.compatibility_group or other.interface)
                    )
                )
            ),
            None,
        )
        repaired = technical_retry = switched = False
        transcript = list(messages)
        code = "MODEL_FAILED"
        ordinal = 0
        attempt_limit = 1 if vision else min(MAX_LOGICAL_ATTEMPTS, self._logical_attempt_limit)
        while ordinal < attempt_limit:
            attempt_input = task.input_tokens(selected) + (256 if transcript != messages else 0)
            if data_scope == "customer_conversation":
                attempt_input = _estimate_input(transcript, tools, vision=False)[0]
                if attempt_input > P4_INPUT_LIMIT:
                    raise ModelFailure("INVESTIGATION_PROMPT_LIMIT")
                if attempt_input + task.output_reserve > selected.context_tokens:
                    raise ModelFailure("MODEL_CONTEXT_INSUFFICIENT")
            reserve = _reserve_price(
                selected,
                attempt_input,
                task.output_reserve,
            )
            try:
                slot = audit.claim_provider(selected, registry)
            except ProviderLimitError as error:
                code = error.code
                if fallback is None or switched or code == "PROVIDER_LIMIT_CONFIG_MISMATCH":
                    break
                audit.event(
                    "model_switch",
                    {
                        "from": selected.alias,
                        "to": fallback.alias,
                        "reason": code,
                        "logical_id": logical_id,
                    },
                )
                selected, transcript, switched = fallback, list(messages), True
                continue

            ordinal += 1
            native_streaming = task.streaming
            transport = self.stream_transport if native_streaming else self.transport
            default = transport is (
                _DEFAULT_STREAM_TRANSPORT if native_streaming else _DEFAULT_SYNC_TRANSPORT
            )
            transport_evidence = {
                "version": TRANSPORT_EVIDENCE_VERSION,
                "path": f"{'DEFAULT' if default else 'INJECTED'}_{'STREAM' if native_streaming else 'SYNC'}",
                "response": "UNOBSERVED",
            }
            if data_scope == "customer_conversation":
                transport_evidence["max_output_tokens"] = task.output_reserve
            try:
                remaining = audit.begin_attempt(
                    logical_id,
                    ordinal,
                    selected,
                    reserve,
                    registry,
                    transport_evidence=transport_evidence,
                    base_registry_digest=self.base_registry_digest,
                )
            except BaseException:
                audit.release_provider(slot, attempted=False)
                raise
            code = "MODEL_FAILED"
            usage = price = None
            uncertain = True
            draft = {"logical_id": logical_id, "ordinal": ordinal, "alias": selected.alias}

            def emit(delta, draft=draft):
                audit.confirm_inflight_admission()
                audit.event("model_draft_delta", {**draft, "text": delta})

            try:
                args = (
                    selected,
                    transcript,
                    tools,
                    min(registry.timeout_seconds, remaining),
                    task.output_reserve,
                )
                if default and data_scope == "customer_conversation":
                    response = _private_transport(*args)
                else:
                    response = transport(*args, emit) if native_streaming else transport(*args)
                if default and isinstance(response, AIMessage):
                    transport_evidence = {**transport_evidence, "response": "SDK_RETURNED"}
                if data_scope == "customer_conversation" and isinstance(response, AIMessage):
                    transport_evidence["termination"] = _safe_termination(
                        response.response_metadata
                    )
                usage = response.usage_metadata
                if usage:
                    price = _estimated_price(selected, usage)
                if price is not None and reserve is not None and price > reserve:
                    if vision:
                        raise ModelFailure("VISION_ESTIMATE_EXCEEDED")
                    if data_scope == "customer_conversation":
                        raise ModelFailure("INVESTIGATION_ESTIMATE_EXCEEDED")
                if response.response_metadata.get("status") in {
                    "incomplete",
                    "failed",
                    "cancelled",
                } or response.response_metadata.get("finish_reason") in {
                    "length",
                    "content_filter",
                }:
                    raise ModelFailure("MODEL_INCOMPLETE")
                if response.invalid_tool_calls:
                    raise InvalidOutput("MALFORMED_TOOL_ARGUMENTS", "tool_calls.arguments")
                if response.tool_calls and not tools:
                    raise InvalidOutput("UNAVAILABLE_TOOL", "tool_calls")
                value = validate(response.tool_calls, _content(response))
            except (ValidationError, InvalidOutput, json.JSONDecodeError) as error:
                code, uncertain = "INVALID_MODEL_OUTPUT", False
                fields = _validation_fields(error)
                audit.finish_attempt(
                    logical_id,
                    ordinal,
                    "FAILED",
                    code,
                    usage,
                    price,
                    None,
                    transport_evidence=transport_evidence,
                )
                audit.event("model_validation", {**draft, "errors": fields})
                if native_streaming:
                    audit.close_draft("model_draft_discarded", {**draft, "reason": code})
                if not repaired and ordinal < attempt_limit:
                    repaired = True
                    from langchain_core.messages import HumanMessage

                    transcript = [
                        *messages,
                        HumanMessage(
                            content=(
                                "上一响应未满足字段约束，请按系统定义修复。"
                                + json.dumps(fields, ensure_ascii=False)
                            )
                        ),
                    ]
                    continue
            except Exception as error:  # noqa: BLE001 - provider failure is audited and stops
                code, retryable, uncertain = _failure(error)
                if default and transport_evidence["response"] == "UNOBSERVED":
                    http_status = _http_error_status(error)
                    if http_status is not None:
                        transport_evidence = {
                            **transport_evidence,
                            "response": "HTTP_ERROR_RESPONSE",
                            "http_status": http_status,
                        }
                if isinstance(error, StreamInterrupted) and error.usage is not None:
                    usage = error.usage
                    price = _estimated_price(selected, usage)
                audit.finish_attempt(
                    logical_id,
                    ordinal,
                    "UNKNOWN" if uncertain else "FAILED",
                    code,
                    usage,
                    price,
                    None,
                    transport_evidence=transport_evidence,
                )
                if native_streaming:
                    audit.close_draft("model_draft_discarded", {**draft, "reason": code})
                if not retryable:
                    break
                if (
                    code != "MODEL_RATE_LIMIT"
                    and not technical_retry
                    and not uncertain
                    and ordinal < attempt_limit
                ):
                    technical_retry = True
                    continue
            else:
                uncertain = False
                audit.finish_attempt(
                    logical_id,
                    ordinal,
                    "SUCCEEDED",
                    None,
                    usage,
                    price,
                    value,
                    transport_evidence=transport_evidence,
                )
                try:
                    audit.confirm_admission()
                except ModelFailure as error:
                    if native_streaming:
                        audit.close_draft("model_draft_discarded", {**draft, "reason": error.code})
                    raise
                if native_streaming:
                    if "calls" in value:
                        audit.close_draft(
                            "model_draft_discarded", {**draft, "reason": "TOOL_REQUEST"}
                        )
                    else:
                        audit.close_draft("model_draft_committed", draft)
                return value
            finally:
                audit.release_provider(
                    slot,
                    attempted=True,
                    uncertain=uncertain,
                    cooldown_seconds=registry.cooldown_seconds if code == "MODEL_RATE_LIMIT" else 0,
                )
            if not switched and fallback is not None and ordinal < attempt_limit:
                switched = True
                audit.event(
                    "model_switch",
                    {
                        "from": selected.alias,
                        "to": fallback.alias,
                        "reason": code,
                        "logical_id": logical_id,
                    },
                )
                selected, transcript = fallback, list(messages)
                continue
            break
        raise ModelFailure(code)


def tool_reply(name: str, call_id: str, result: dict) -> ToolMessage:
    return ToolMessage(
        name=name,
        tool_call_id=call_id,
        content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    )
