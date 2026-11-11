"""Authenticated run API with replayable SSE cursors."""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from aftercare.auth import UserContext, bearer, current_user
from aftercare.business.errors import BusinessError
from aftercare.intelligence.service import EVENT_PAGE_SIZE, AgentService, RunError

router = APIRouter(prefix="/cases/{case_id}/runs", tags=["agent"])
Actor = Annotated[UserContext, Depends(current_user)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
Cursor = Annotated[int, Query(ge=0)]


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0, strict=True)


class Start(Transition):
    mode: Literal["multi", "single"] = "multi"
    strategy: Literal["routed", "economy", "analysis"] = "routed"
    message: str = Field(default="", max_length=2000)


def _service(request: Request) -> AgentService:
    return request.app.state.agent


Service = Annotated[AgentService, Depends(_service)]


def _invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (RunError, BusinessError) as error:
        raise HTTPException(error.status, {"code": error.code, "message": error.message}) from None


@router.post("", status_code=202)
def start(case_id: str, data: Start, user: Actor, key: IdempotencyKey, service: Service):
    return _invoke(service.start, case_id, user, idempotency_key=key, **data.model_dump())


@router.get("")
def list_runs(case_id: str, user: Actor, service: Service):
    return _invoke(service.list, case_id, user)


@router.get("/{run_id}")
def get_run(case_id: str, run_id: str, user: Actor, service: Service):
    return _invoke(service.get, case_id, run_id, user)


@router.post("/{run_id}/resume", status_code=202)
def resume(
    case_id: str,
    run_id: str,
    data: Transition,
    user: Actor,
    key: IdempotencyKey,
    service: Service,
):
    return _invoke(
        service.transition,
        case_id,
        run_id,
        user,
        operation="resume",
        expected_version=data.expected_version,
        idempotency_key=key,
    )


@router.post("/{run_id}/cancel")
def cancel(
    case_id: str,
    run_id: str,
    data: Transition,
    user: Actor,
    key: IdempotencyKey,
    service: Service,
):
    return _invoke(
        service.transition,
        case_id,
        run_id,
        user,
        operation="cancel",
        expected_version=data.expected_version,
        idempotency_key=key,
    )


@router.get("/{run_id}/events")
def events(case_id: str, run_id: str, user: Actor, service: Service, after: Cursor = 0):
    return _invoke(service.events, case_id, run_id, user, after)


@router.get("/{run_id}/stream")
async def stream(
    request: Request,
    case_id: str,
    run_id: str,
    user: Actor,
    service: Service,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    after: Cursor = 0,
):
    header = request.headers.get("last-event-id")
    try:
        if header is not None and (not header.isascii() or not header.isdecimal()):
            raise ValueError
        cursor = int(header) if header is not None else after
    except ValueError:
        raise HTTPException(400, {"code": "INVALID_CURSOR", "message": "事件游标无效。"}) from None
    await run_in_threadpool(_invoke, service.get, case_id, run_id, user, after=cursor)

    def authenticate():
        return current_user(request, credentials)

    def poll(position):
        refreshed = authenticate()
        batch, status = _invoke(service.event_page, case_id, run_id, refreshed, position)
        return refreshed, batch, status

    async def produce():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                refreshed, batch, status = await run_in_threadpool(poll, cursor)
                for event in batch:
                    # A page can outlive the access used to fetch it during a slow connection.
                    if await run_in_threadpool(authenticate) != refreshed:
                        return
                    cursor = event["seq"]
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\ndata: {payload}\n\n"
                if len(batch) == EVENT_PAGE_SIZE:
                    continue
                if status not in {"QUEUED", "RUNNING"}:
                    if await run_in_threadpool(authenticate) == refreshed:
                        yield f"event: idle\ndata: {json.dumps({'status': status})}\n\n"
                    return
            except HTTPException:
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        produce(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
