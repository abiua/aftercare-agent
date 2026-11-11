from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from aftercare.auth import UserContext, current_user

from .contracts import MaterialReviewObservations
from .errors import BusinessError
from .service import BusinessService
from .storage import MAX_BYTES


def _bounded_upload_request(request: Request) -> Request:
    maximum = MAX_BYTES + 65536
    length = request.headers.get("content-length")
    if length and length.isdecimal() and (len(length) > 12 or int(length) > maximum):
        raise BusinessError("MATERIAL_TOO_LARGE", "照片最大为 8 MiB。", 413)

    received = 0

    async def receive():
        nonlocal received
        message = await request.receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > maximum:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "MATERIAL_TOO_LARGE", "message": "照片最大为 8 MiB。"},
                )
        return message

    return Request(request.scope, receive)


class BusinessRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        headers = {"Cache-Control": "no-store"} if self.path.endswith("/refund-lookup") else {}

        async def dispatch(request: Request):
            try:
                if request.method == "POST" and self.path.endswith("/materials"):
                    request = _bounded_upload_request(request)
                response = await handler(request)
                response.headers.update(headers)
                return response
            except RequestValidationError as error:
                failure = error.errors()[0]
                field = ".".join(str(part) for part in failure["loc"][1:])
                raise HTTPException(
                    status_code=422,
                    detail={"code": "VALIDATION_ERROR", "message": f"请求参数无效：{field}"},
                    headers=headers,
                ) from None
            except BusinessError as error:
                raise HTTPException(
                    status_code=error.status,
                    detail={"code": error.code, "message": error.message},
                    headers=headers,
                ) from None
            except HTTPException as error:
                if headers:
                    error.headers = {**(error.headers or {}), **headers}
                raise

        return dispatch


router = APIRouter(route_class=BusinessRoute, tags=["business"])


def _business(request: Request) -> BusinessService:
    return request.app.state.business


def _idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if value is None:
        raise BusinessError("INVALID_IDEMPOTENCY_KEY", "操作必须带 Idempotency-Key。", 422)
    return value


Actor = Annotated[UserContext, Depends(current_user)]
Business = Annotated[BusinessService, Depends(_business)]
Key = Annotated[str, Depends(_idempotency_key)]
Version = Annotated[StrictInt, Field(ge=1)]
ID = Annotated[str, Field(min_length=1, max_length=64)]
Notes = Annotated[str, Field(min_length=1, max_length=2000)]
Limit = Annotated[int, Query(ge=1, le=300)]
Offset = Annotated[int, Query(ge=0)]


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Version


class CreateCase(Mutation):
    order_id: ID
    intent: Literal["damaged", "wrong_item", "refund_status", "cancel", "return", "warranty"]
    description: Annotated[str, Field(min_length=5, max_length=2000)]
    line_ids: Annotated[list[ID], Field(min_length=1, max_length=30)] | None = None


class ReviewMaterials(Mutation):
    decision: Literal["APPROVED", "REJECTED", "NEEDS_MORE"]
    notes: Notes
    observations: MaterialReviewObservations | None = None


class ProposedLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: ID
    amount_cents: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)] | None = None
    quantity: Annotated[StrictInt, Field(gt=0, le=2_147_483_647)] | None = None


class CreateProposal(Mutation):
    action: Literal["REFUND", "REPLACE"]
    lines: Annotated[list[ProposedLine], Field(min_length=1, max_length=30)]
    notes: Notes
    policy_ids: Annotated[list[ID], Field(max_length=30)] = Field(default_factory=list)


class ExecuteProposal(Mutation):
    proposal_id: ID


class DecideProposal(ExecuteProposal):
    decision: Literal["APPROVED", "REJECTED"]
    notes: Notes


class RecordFacts(Mutation):
    line_ids: Annotated[list[ID], Field(min_length=1, max_length=30)] | None = None
    facts: Annotated[dict[ID, ID], Field(min_length=1, max_length=5)]
    notes: Notes


@router.get("/orders")
def orders(service: Business, user: Actor, limit: Limit = 100, offset: Offset = 0):
    return {"items": service.orders(user, limit=limit, offset=offset)}


@router.get("/orders/{order_id}")
def order(order_id: str, service: Business, user: Actor):
    return service.order(order_id, user)


@router.get("/cases")
def cases(service: Business, user: Actor, limit: Limit = 100, offset: Offset = 0):
    return {"items": service.cases(user, limit=limit, offset=offset)}


@router.post("/cases", status_code=201)
def create_case(body: CreateCase, service: Business, user: Actor, key: Key):
    return service.create_case(user, idempotency_key=key, **body.model_dump())


@router.get("/cases/{case_id}")
def case_detail(case_id: str, service: Business, user: Actor):
    return service.detail(case_id, user)


@router.get("/cases/{case_id}/materials")
def materials(case_id: str, service: Business, user: Actor):
    return {"items": service.context(case_id, user)["materials"]}


@router.post("/cases/{case_id}/materials", status_code=201)
def upload_material(
    case_id: str,
    service: Business,
    user: Actor,
    key: Key,
    kind: Annotated[Literal["ITEM", "PARCEL", "WRONG_ITEM", "LABEL"], Form()],
    expected_version: Annotated[int, Form(ge=1)],
    file: Annotated[UploadFile, File()],
):
    try:
        return service.upload(
            case_id,
            user,
            kind=kind,
            content=file.file.read(MAX_BYTES + 1),
            expected_version=expected_version,
            idempotency_key=key,
        )
    finally:
        file.file.close()


@router.get("/cases/{case_id}/materials/{material_id}/content")
def material_content(case_id: str, material_id: str, service: Business, user: Actor):
    content, content_type = service.material_content(case_id, material_id, user)
    return Response(
        content,
        media_type=content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/cases/{case_id}/materials/{material_id}/link")
def material_link(
    case_id: str, material_id: str, response: Response, service: Business, user: Actor
):
    response.headers["Cache-Control"] = "private, no-store"
    return service.material_link(case_id, material_id, user)


@router.get("/cases/{case_id}/material-reviews")
def material_reviews(case_id: str, service: Business, user: Actor):
    return {"items": service.context(case_id, user, include_review_observations=True)["reviews"]}


@router.post("/cases/{case_id}/material-reviews", status_code=201)
def review_materials(case_id: str, body: ReviewMaterials, service: Business, user: Actor, key: Key):
    return service.review_materials(case_id, user, idempotency_key=key, **body.model_dump())


@router.post("/cases/{case_id}/facts")
def record_facts(case_id: str, body: RecordFacts, service: Business, user: Actor, key: Key):
    return service.record_facts(case_id, user, idempotency_key=key, **body.model_dump())


@router.get("/cases/{case_id}/proposals")
def proposals(case_id: str, service: Business, user: Actor):
    return {"items": service.detail(case_id, user)["proposals"]}


@router.post("/cases/{case_id}/proposals", status_code=201)
def propose(case_id: str, body: CreateProposal, service: Business, user: Actor, key: Key):
    return service.propose(
        case_id,
        user,
        idempotency_key=key,
        **body.model_dump(exclude_none=True),
    )


@router.get("/cases/{case_id}/approvals")
def approvals(case_id: str, service: Business, user: Actor):
    return {"items": service.detail(case_id, user)["approvals"]}


@router.post("/cases/{case_id}/approvals", status_code=201)
def approve(case_id: str, body: DecideProposal, service: Business, user: Actor, key: Key):
    return service.approve(case_id, user, idempotency_key=key, **body.model_dump())


@router.get("/cases/{case_id}/executions")
def executions(case_id: str, service: Business, user: Actor):
    return {"items": service.context(case_id, user)["ledger"]}


@router.get("/cases/{case_id}/refund-lookup")
def refund_lookup(
    case_id: str, service: Business, user: Actor, execution_id: Annotated[list[str], Query()]
):
    return service.refund_lookup(case_id, user, execution_id)


@router.post("/cases/{case_id}/execute")
def execute(case_id: str, body: ExecuteProposal, service: Business, user: Actor, key: Key):
    return service.execute(case_id, user, idempotency_key=key, **body.model_dump())


@router.post("/cases/{case_id}/executions/{execution_id}/reconcile")
def reconcile(
    case_id: str,
    execution_id: str,
    body: Mutation,
    service: Business,
    user: Actor,
    key: Key,
):
    return service.reconcile(
        case_id,
        execution_id,
        user,
        idempotency_key=key,
        **body.model_dump(),
    )


@router.get("/cases/{case_id}/events")
async def events(
    case_id: str,
    request: Request,
    service: Business,
    user: Actor,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    follow: bool = True,
):
    if last_id is not None and (
        not last_id.isascii() or not last_id.isdecimal() or len(last_id) > 12
    ):
        raise BusinessError("INVALID_EVENT_CURSOR", "事件游标必须为非负整数。", 422)
    cursor = max(after or 0, int(last_id or 0))
    first = await run_in_threadpool(service.events, case_id, user, cursor)

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        rows = first
        if follow:
            yield "retry: 1500\n\n"
        for tick in range(25 if follow else 1):
            if await request.is_disconnected():
                return
            while rows:
                for row in rows:
                    cursor = row["sequence"]
                    data = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: business\ndata: {data}\n\n"
                if len(rows) < 100:
                    break
                rows = await run_in_threadpool(service.events, case_id, user, cursor)
            if not follow or tick == 24:
                return
            if tick % 8 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)
            rows = await run_in_threadpool(service.events, case_id, user, cursor)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
