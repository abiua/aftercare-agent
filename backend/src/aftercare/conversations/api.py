"""Authorized conversation and original-attachment HTTP endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from aftercare.business.api import ID, Actor, BusinessRoute, Key, Version
from aftercare.business.errors import BusinessError
from aftercare.business.storage import MAX_BYTES
from aftercare.decisions.contracts import CustomerResponse, DecisionRef, Review

from .decisions import ConversationDecisions
from .service import MAX_ATTACHMENTS, ConversationService

BODY_MAX = MAX_BYTES + 64 * 1024


class ConversationRoute(BusinessRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        if "/decisions" in self.path or "/decision-materials" in self.path:

            async def private(request: Request):
                try:
                    response = await handler(request)
                except HTTPException as error:
                    error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                    raise
                response.headers["Cache-Control"] = "no-store"
                return response

            return private
        if not self.path.endswith("/messages") or "POST" not in self.methods:
            return handler

        async def bounded(request: Request):
            total = 0

            def rejected():
                return HTTPException(
                    413,
                    detail={
                        "code": "ATTACHMENTS_TOO_LARGE",
                        "message": "每条消息的照片总量不能超过 8 MiB。",
                    },
                )

            size = request.headers.get("content-length")
            if size and size.isdecimal() and (len(size) > 10 or int(size) > BODY_MAX):
                raise rejected()

            async def receive():
                nonlocal total
                event = await request.receive()
                if event["type"] == "http.request":
                    total += len(event.get("body", b""))
                    if total > BODY_MAX:
                        raise rejected()
                return event

            return await handler(Request(request.scope, receive=receive))

        return bounded


router = APIRouter(prefix="/conversations", route_class=ConversationRoute, tags=["conversations"])


def _service(request: Request):
    return request.app.state.conversations


Service = Annotated[ConversationService, Depends(_service)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Handoff(Empty):
    expected_revision: Version


class Context(Handoff):
    order_id: ID
    line_ids: Annotated[list[ID], Field(max_length=100)] = Field(default_factory=list)
    new_issue: bool = False


@router.post("", status_code=201)
def create(_body: Empty, service: Service, user: Actor, key: Key):
    return service.create(user, key)


@router.get("")
def list_conversations(service: Service, user: Actor, limit: Limit = 100, offset: Offset = 0):
    return {"items": service.list(user, limit=limit, offset=offset)}


@router.get("/{conversation_id}")
def detail(
    conversation_id: UUID, service: Service, user: Actor, after: Offset = 0, limit: Limit = 100
):
    return service.detail(str(conversation_id), user, after=after, limit=limit)


@router.post("/{conversation_id}/messages", status_code=201)
def message(
    conversation_id: UUID,
    service: Service,
    user: Actor,
    key: Key,
    client_message_id: Annotated[UUID, Form()],
    text: Annotated[str, Form(max_length=2000)] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    files = files or []
    try:
        if len(files) > MAX_ATTACHMENTS:
            raise BusinessError("ATTACHMENTS_TOO_LARGE", "每条消息最多可上传 6 张照片。", 413)
        payloads = []
        available = MAX_BYTES
        for file in files:
            content = file.file.read(available + 1)
            available -= len(content)
            if available < 0:
                raise BusinessError("ATTACHMENTS_TOO_LARGE", "照片总大小不能超过 8 MiB。", 413)
            payloads.append((file.filename or "photo", content))
        return service.message(
            str(conversation_id), user, str(client_message_id), text, payloads, key
        )
    finally:
        for file in files:
            file.file.close()


@router.get("/{conversation_id}/attachments/{attachment_id}/content")
def content(conversation_id: UUID, attachment_id: UUID, service: Service, user: Actor):
    data, content_type = service.content(str(conversation_id), str(attachment_id), user)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/{conversation_id}/context")
def context(conversation_id: UUID, body: Context, service: Service, user: Actor, key: Key):
    return service.context(str(conversation_id), user, **body.model_dump(), key=key)


@router.post("/{conversation_id}/handoff")
def handoff(conversation_id: UUID, body: Handoff, service: Service, user: Actor, key: Key):
    return service.handoff(str(conversation_id), user, **body.model_dump(), key=key)


class ReviewDecision(Empty):
    ref: DecisionRef
    review: Review
    expected_review_id: UUID | None = None
    evidence_gaps: Annotated[
        list[Literal["ITEM", "PARCEL", "WRONG_ITEM", "LABEL", "ISSUE_DESCRIPTION"]],
        Field(max_length=5),
    ] = Field(default_factory=list)


class RespondDecision(Empty):
    ref: DecisionRef
    response: CustomerResponse


class ConfirmDecisionMaterial(Empty):
    attachment_id: UUID
    expected_issue_id: UUID
    expected_case_id: UUID
    expected_materials_version: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
    kind: Literal["ITEM", "PARCEL", "WRONG_ITEM", "LABEL"]


@router.get("/{conversation_id}/decision-materials")
def decision_material_candidates(
    conversation_id: UUID,
    service: Service,
    user: Actor,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
):
    from .materials import candidates

    return candidates(service.business, user, str(conversation_id), after=after, limit=limit)


@router.post("/{conversation_id}/decision-materials")
def confirm_decision_material(
    conversation_id: UUID,
    body: ConfirmDecisionMaterial,
    service: Service,
    user: Actor,
    key: Key,
):
    from .materials import confirm

    return confirm(
        service.business,
        user,
        str(conversation_id),
        attachment_id=str(body.attachment_id),
        kind=body.kind,
        case_id=str(body.expected_case_id),
        issue_id=str(body.expected_issue_id),
        version=body.expected_materials_version,
        key=key,
    )


@router.get("/{conversation_id}/decisions")
def current_decision(conversation_id: UUID, service: Service, user: Actor):
    return ConversationDecisions(service.business).current(user, str(conversation_id))


@router.get("/{conversation_id}/decisions/history/{snapshot_id}")
def decision_history(conversation_id: UUID, snapshot_id: UUID, service: Service, user: Actor):
    return ConversationDecisions(service.business).history(
        user, str(conversation_id), str(snapshot_id)
    )


@router.post("/{conversation_id}/decisions/run", status_code=202)
def run_decision(
    conversation_id: UUID, _body: Empty, request: Request, service: Service, user: Actor, key: Key
):
    from aftercare.intelligence.service import RunError

    try:
        run = ConversationDecisions(service.business).run(
            user, str(conversation_id), key, request.app.state.agent
        )
    except RunError as error:
        raise BusinessError(error.code, error.message, error.status) from None
    return {"run_id": run["id"], "status": run["status"]}


@router.post("/{conversation_id}/decisions/review")
def review_decision(
    conversation_id: UUID, body: ReviewDecision, service: Service, user: Actor, key: Key
):
    return ConversationDecisions(service.business).review(
        user,
        str(conversation_id),
        body.ref,
        body.review,
        key=key,
        expected_review_id=str(body.expected_review_id) if body.expected_review_id else None,
        evidence_gaps=body.evidence_gaps,
        chat=service,
    )


@router.get("/{conversation_id}/decisions/offer")
def current_offer(conversation_id: UUID, service: Service, user: Actor):
    return ConversationDecisions(service.business).offer(user, str(conversation_id))


@router.post("/{conversation_id}/decisions/offer/respond")
def respond_to_offer(
    conversation_id: UUID, body: RespondDecision, service: Service, user: Actor, key: Key
):
    return ConversationDecisions(service.business).respond(
        user,
        str(conversation_id),
        body.ref,
        body.response,
        key=key,
    )
