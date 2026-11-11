"""Private tester-only P5 review endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from aftercare.business.api import Actor, BusinessRoute, Key

from .contracts import InitialJudgment, PostLabels, Versioned
from .service import BlindReviewService


class BlindReviewRoute(BusinessRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request: Request):
            try:
                response = await handler(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


router = APIRouter(prefix="/chat-blind-reviews", route_class=BlindReviewRoute)


def _service(request: Request):
    return BlindReviewService(request.app.state.conversations.business)


Service = Annotated[BlindReviewService, Depends(_service)]


@router.get("")
def tasks(
    service: Service,
    actor: Actor,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
):
    return service.list(actor, after=str(after) if after else None, limit=limit)


@router.get("/{task_id}")
def detail(task_id: UUID, service: Service, actor: Actor):
    return service.get(actor, str(task_id))


@router.get("/{task_id}/images/{image_id}")
def original(task_id: UUID, image_id: UUID, service: Service, actor: Actor):
    content, content_type = service.image(actor, str(task_id), str(image_id))
    return Response(content, media_type=content_type, headers={"X-Content-Type-Options": "nosniff"})


@router.post("/{task_id}/initial")
def initial(task_id: UUID, body: InitialJudgment, service: Service, actor: Actor, key: Key):
    return service.write(actor, str(task_id), "initial", body, key)


@router.post("/{task_id}/reveal")
def reveal(task_id: UUID, body: Versioned, service: Service, actor: Actor, key: Key):
    return service.write(actor, str(task_id), "reveal", body, key)


@router.post("/{task_id}/labels")
def labels(task_id: UUID, body: PostLabels, service: Service, actor: Actor, key: Key):
    return service.write(actor, str(task_id), "labels", body, key)
