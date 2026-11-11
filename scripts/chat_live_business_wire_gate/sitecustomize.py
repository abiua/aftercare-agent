"""Pin the owned native worker's OpenAI SDK transport to a private wire plan."""

import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import local

import policy
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


@contextmanager
def attempt(binding):
    policy.validate_binding(PLAN, binding)
    if getattr(_CONTEXT, "binding", None) is not None:
        raise policy.WireDenied("LIVE_BUSINESS_WIRE_NESTED_ATTEMPT")
    _CONTEXT.binding = deepcopy(binding)
    try:
        yield
    finally:
        del _CONTEXT.binding


def _send(client, request, *args, **kwargs):
    if (
        getattr(_CONTEXT, "binding", None) is None
        or str(request.url) != policy.URL
        or type(client._transport_for_url(request.url)) is not httpx.HTTPTransport
    ):
        raise policy.WireDenied("LIVE_BUSINESS_WIRE_TRANSPORT_TARGET")
    kwargs["follow_redirects"] = False
    return SEND(client, request, *args, **kwargs)


def _handle(transport, request):
    binding = getattr(_CONTEXT, "binding", None)
    return policy.send(
        STATE,
        PLAN,
        binding,
        request,
        os.environ.get("AFTERCARE_OPENAI_KEY"),
        lambda native: HANDLE(transport, native),
    )


httpx.Client.send = _send
httpx.HTTPTransport.handle_request = _handle
READY = True
