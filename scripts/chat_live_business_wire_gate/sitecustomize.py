"""Pin the owned native worker's OpenAI SDK transport to a private wire plan."""

import os
import re
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import local

import policy
from langchain_openai import ChatOpenAI
from langchain_openai._compat import httpx
from openai import _base_client

STATE = Path("/live-business-wire")
try:
    PLAN = policy.read_plan(STATE, os.environ.get("P5_LIVE_BUSINESS_PLAN_SHA256"))
    if getattr(_base_client, "httpx2", None) is not httpx:
        raise policy.WireDenied("LIVE_BUSINESS_WIRE_SDK_TRANSPORT")
except Exception as error:  # noqa: BLE001 - fatal startup, never expose native input or credentials
    code = str(error) if isinstance(error, policy.WireDenied) else "LIVE_BUSINESS_WIRE_STARTUP"
    raise SystemExit(code) from None

_CONTEXT = local()
SEND = httpx.Client.send
HANDLE = httpx.HTTPTransport.handle_request
PAYLOAD = ChatOpenAI._get_request_payload
GENERATE = ChatOpenAI._generate
_STAGES = {"serialize", "validate", "send", "read", "adapter"}
_EXCEPTION_TYPES = {
    "APIConnectionError",
    "APITimeoutError",
    "AttributeError",
    "ConnectError",
    "ConnectTimeout",
    "FileNotFoundError",
    "KeyError",
    "OSError",
    "PermissionError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "RuntimeError",
    "TimeoutError",
    "TypeError",
    "ValidationError",
    "ValueError",
    "WireDenied",
    "WriteError",
}


@contextmanager
def attempt(binding):
    policy.validate_binding(PLAN, binding)
    if getattr(_CONTEXT, "binding", None) is not None:
        raise policy.WireDenied("LIVE_BUSINESS_WIRE_NESTED_ATTEMPT")
    _CONTEXT.binding = deepcopy(binding)
    _CONTEXT.trace = {
        "stage": "serialize",
        "index": None,
        "admitted": False,
        "fault_logged": False,
    }
    try:
        yield
    finally:
        del _CONTEXT.binding
        del _CONTEXT.trace


def _observe(stage, *, index=None, admitted=False):
    trace = getattr(_CONTEXT, "trace", None)
    if trace is not None:
        trace.update(stage=stage, index=index, admitted=admitted)


def _fault(error, *, stage=None):
    binding = getattr(_CONTEXT, "binding", None)
    trace = getattr(_CONTEXT, "trace", None)
    if binding is None or trace is None or trace["fault_logged"]:
        return
    trace["fault_logged"] = True
    stage = stage or trace["stage"]
    if stage not in _STAGES:
        stage = "adapter"
    exception = type(error).__name__
    code = str(error) if isinstance(error, policy.WireDenied) else ""
    if not re.fullmatch(r"LIVE_BUSINESS_WIRE_[A-Z0-9_]{1,80}", code):
        code = None
    logical = binding["native_logical_id"]
    ordinal = binding["native_ordinal"]
    index = trace["index"]
    try:
        policy._write(
            STATE,
            f"fault-{sha256(logical.encode()).hexdigest()}-{ordinal}-{stage}.json",
            {
                "kind": PLAN["kind"],
                "native_logical_id": logical,
                "native_ordinal": ordinal,
                "stage": stage,
                "exception_type": exception if exception in _EXCEPTION_TYPES else "OTHER",
                "wire_denial_code": code,
                "admitted": trace["admitted"],
                "admission_index": index,
                "admission_file_present": (
                    index is not None and (STATE / f"admission-{index:02}.json").is_file()
                ),
            },
        )
    except Exception:  # noqa: BLE001 - diagnostics cannot replace the original failure
        return


def _payload(client, *args, **kwargs):
    try:
        return PAYLOAD(client, *args, **kwargs)
    except Exception as error:
        _fault(error, stage="serialize")
        raise


def _generate(client, *args, **kwargs):
    try:
        return GENERATE(client, *args, **kwargs)
    except Exception as error:
        # HTTP statuses already have their own known-response record.
        if not isinstance(getattr(error, "status_code", None), int):
            _fault(error, stage="adapter")
        raise


def _send(client, request, *args, **kwargs):
    try:
        if (
            getattr(_CONTEXT, "binding", None) is None
            or str(request.url) != policy.URL
            or type(client._transport_for_url(request.url)) is not httpx.HTTPTransport
        ):
            raise policy.WireDenied("LIVE_BUSINESS_WIRE_TRANSPORT_TARGET")
    except Exception as error:
        _fault(error, stage="validate")
        raise
    kwargs["follow_redirects"] = False
    try:
        return SEND(client, request, *args, **kwargs)
    except Exception as error:
        _fault(error, stage="send")
        raise


def _handle(transport, request):
    binding = getattr(_CONTEXT, "binding", None)
    try:
        return policy.send(
            STATE,
            PLAN,
            binding,
            request,
            os.environ.get("AFTERCARE_OPENAI_KEY"),
            lambda native: HANDLE(transport, native),
            observe_stage=_observe,
        )
    except Exception as error:
        _fault(error)
        raise


ChatOpenAI._get_request_payload = _payload
ChatOpenAI._generate = _generate
httpx.Client.send = _send
httpx.HTTPTransport.handle_request = _handle
READY = True
