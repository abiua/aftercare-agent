"""Authorized conversation and original-attachment HTTP endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from aftercare.business.api import ID, Actor, BusinessRoute, Key, Version
from aftercare.business.errors import BusinessError
from aftercare.business.storage import MAX_BYTES

from .service import MAX_ATTACHMENTS, ConversationService

BODY_MAX = MAX_BYTES + 64 * 1024


class ConversationRoute(BusinessRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
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
