"""Tester-only evaluation endpoints for the workbench."""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from aftercare.auth import bearer, current_user
from aftercare.business.api import Actor, BusinessRoute, Key, Version
from aftercare.business.errors import BusinessError

from .chat.api import router as blind_review_router
from .imports import ImportInput
from .progress import PAGE_SIZE
from .service import EvaluationService, ExperimentInput


class EvaluationRoute(BusinessRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        if "/campaigns" in self.path or "/bad-cases" in self.path or "/investigations" in self.path or "/scenario-metrics" in self.path or self.path.endswith(
            ("/progress", "/progress/stream")
        ):

            async def read_no_store(request: Request):
                try:
                    response = await handler(request)
                except HTTPException as error:
                    error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
                    raise
                response.headers["Cache-Control"] = "no-store"
                return response

            return read_no_store
        if not self.path.endswith("/imports") or "POST" not in self.methods:
            return handler

        async def bounded(request: Request):
            maximum = 17 * 1024 * 1024
            total = 0
            length = request.headers.get("content-length")

            def reject():
                raise HTTPException(
                    413,
                    detail={
                        "code": "REPORT_TOO_LARGE",
                        "message": "导入请求大小超过限制。",
                    },
                )

            if length and length.isdecimal() and (len(length) > 10 or int(length) > maximum):
                reject()

            async def receive():
                nonlocal total
                event = await request.receive()
                if event["type"] == "http.request":
                    total += len(event.get("body", b""))
                    if total > maximum:
                        reject()
                return event

            return await handler(Request(request.scope, receive=receive))

        return bounded


router = APIRouter(prefix="/evaluation", tags=["evaluation"], route_class=EvaluationRoute)
router.include_router(blind_review_router)


def _service(request: Request) -> EvaluationService:
    return request.app.state.evaluation


Evaluation = Annotated[EvaluationService, Depends(_service)]

def _legacy_service(service: Evaluation, user: Actor) -> EvaluationService:
    from .investigations import authorize

    authorize(user)
    if service.catalog is None:
        raise BusinessError("EVALUATION_RETIRED", "项目用例评测已退役，请查看官方评测。", 410)
    return service


LegacyEvaluation = Annotated[EvaluationService, Depends(_legacy_service)]



class CancelInput(BaseModel):
    expected_version: Version


AnnotationMap = Annotated[dict[str, Literal["PASS", "FAIL", "UNKNOWN"]], Field(max_length=100)]


class ReviewAnnotations(BaseModel):
    model_config = {"extra": "forbid"}

    tools: AnnotationMap = Field(default_factory=dict)
    citations: AnnotationMap = Field(default_factory=dict)
    facts: AnnotationMap = Field(default_factory=dict)


class ReviewInput(CancelInput):
    decision: Literal["PASS", "FAIL", "UNKNOWN"]
    notes: Annotated[str, Field(min_length=1, max_length=1500)]
    annotations: ReviewAnnotations | None = None


class RetryInput(CancelInput):
    result_ids: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]],
        Field(min_length=1, max_length=432),
    ]
    name: Annotated[str, Field(min_length=1, max_length=120)] | None = None


@router.get("/capabilities")
def capabilities(service: Evaluation, user: Actor):
    return service.capabilities(user)


@router.get("/gates")
def gates(service: LegacyEvaluation, user: Actor):
    return {"items": service.gates(user)}


@router.get("/imports")
def imports(service: LegacyEvaluation, user: Actor, limit: Annotated[int, Query(ge=1, le=100)] = 100):
    return {"items": service.imports(user, limit=limit)}


@router.post("/imports", status_code=201)
def import_report(body: ImportInput, service: LegacyEvaluation, user: Actor, key: Key):
    return service.import_report(user, body, key)


@router.get("/imports/{import_id}")
def imported_report(import_id: str, service: LegacyEvaluation, user: Actor):
    return service.imported_report(user, import_id)


@router.get("/datasets")
def datasets(service: LegacyEvaluation, user: Actor):
    return {"items": service.datasets(user)}


@router.get("/cases")
def cases(
    service: LegacyEvaluation,
    user: Actor,
    dataset: str,
    split: Literal["dev", "validation", "holdout", "regression", "test"] | None = None,
):
    return {"items": service.cases(user, dataset, split=split)}


@router.get("/experiments")
def experiments(
    service: LegacyEvaluation,
    user: Actor,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return {"items": service.experiments(user, limit=limit, offset=offset)}


@router.post("/experiments", status_code=201)
def create_experiment(body: ExperimentInput, service: LegacyEvaluation, user: Actor, key: Key):
    return service.create(user, body, key)


@router.get("/experiments/{experiment_id}")
def experiment(experiment_id: str, service: LegacyEvaluation, user: Actor):
    return service.experiment(user, experiment_id)


ProgressCursor = Annotated[int, Query(ge=0, le=2_147_483_647)]


@router.get("/experiments/{experiment_id}/progress")
def experiment_progress(
    experiment_id: str,
    service: LegacyEvaluation,
    user: Actor,
    after: ProgressCursor = 0,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE)] = PAGE_SIZE,
):
    return service.progress(user, experiment_id, after=after, limit=limit)


def _progress_frame(kind, data, sequence=None):
    event_id = f"id: {sequence}\n" if sequence is not None else ""
    return f"{event_id}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/experiments/{experiment_id}/progress/stream")
async def stream_experiment_progress(
    request: Request,
    experiment_id: str,
    service: LegacyEvaluation,
    user: Actor,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    after: ProgressCursor = 0,
    follow: bool = True,
):
    header = request.headers.get("last-event-id")
    if header is not None:
        if (
            not header.isascii()
            or not header.isdecimal()
            or len(header) > 10
            or int(header) > 2_147_483_647
        ):
            raise BusinessError("INVALID_PROGRESS_CURSOR", "进度游标无效。", 400)
        after = int(header)

    # Validate the requested cursor and permission before sending SSE headers.
    first = await run_in_threadpool(
        service.progress, user, experiment_id, after=after, limit=PAGE_SIZE
    )

    async def authorized():
        return await run_in_threadpool(current_user, request, credentials) == user

    async def stream():
        cursor = after
        page = first
        previous_state = None
        while not await request.is_disconnected():
            try:
                if not await authorized():
                    return
                for event in page["events"]:
                    # Recheck even buffered frames because the consumer may be slow.
                    if not await authorized():
                        return
                    cursor = event["sequence"]
                    yield _progress_frame("evaluation-progress", event, cursor)
                if cursor >= page["latest_sequence"]:
                    state = {key: value for key, value in page.items() if key != "events"}
                    if state != previous_state:
                        if not await authorized():
                            return
                        yield _progress_frame("evaluation-state", state)
                        previous_state = state
                    if not follow or state["terminal"] or state["history"] == "HISTORICAL_SNAPSHOT":
                        return
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
                if not await authorized():
                    return
                page = await run_in_threadpool(
                    service.progress, user, experiment_id, after=cursor, limit=PAGE_SIZE
                )
            except (HTTPException, BusinessError):
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


ScenarioFilter = Annotated[str | None, Query(min_length=1, max_length=120)]


@router.get("/experiments/{experiment_id}/scenario-metrics")
def scenario_metrics(
    experiment_id: str,
    service: LegacyEvaluation,
    user: Actor,
    scenario: ScenarioFilter = None,
    intent: ScenarioFilter = None,
):
    return service.scenario_metrics(user, experiment_id, scenario=scenario, intent=intent)


@router.get("/experiments/{experiment_id}/scenario-metrics/{metric_key}/contributors")
def scenario_contributors(
    experiment_id: str,
    metric_key: str,
    service: LegacyEvaluation,
    user: Actor,
    basis_revision: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    scenario: ScenarioFilter = None,
    intent: ScenarioFilter = None,
    bucket: Literal["included", "attention", "unknown", "excluded"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return service.scenario_metrics(
        user,
        experiment_id,
        scenario=scenario,
        intent=intent,
        metric_key=metric_key,
        basis_revision=basis_revision,
        bucket=bucket,
        limit=limit,
        offset=offset,
    )


@router.post("/experiments/{experiment_id}/cancel")
def cancel(experiment_id: str, body: CancelInput, service: LegacyEvaluation, user: Actor, key: Key):
    return service.cancel(user, experiment_id, expected_version=body.expected_version, key=key)


@router.post("/experiments/{experiment_id}/retry", status_code=201)
def retry(experiment_id: str, body: RetryInput, service: LegacyEvaluation, user: Actor, key: Key):
    return service.retry(user, experiment_id, key=key, **body.model_dump())


@router.post("/results/{result_id}/reviews", status_code=201)
def review(result_id: str, body: ReviewInput, service: LegacyEvaluation, user: Actor, key: Key):
    return service.review(user, result_id, key=key, **body.model_dump())


@router.get("/results/{result_id}/supplemental-usage")
def supplemental_usage(result_id: str, service: LegacyEvaluation, user: Actor):
    return JSONResponse(
        service.supplemental_usage(user, result_id), headers={"Cache-Control": "no-store"}
    )


@router.get("/compare")
def compare(service: LegacyEvaluation, user: Actor, left: str, right: str):
    return service.compare(user, left, right)


@router.get("/experiments/{experiment_id}/export")
def export(
    experiment_id: str,
    service: LegacyEvaluation,
    user: Actor,
    format: Literal["json", "csv"] = "json",
):
    # Use a fixed filename to avoid reflecting request input in the attachment header.
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="evaluation.{format}"',
    }
    if format == "json":
        return JSONResponse(service.experiment(user, experiment_id), headers=headers)
    return Response(service.export_csv(user, experiment_id), media_type="text/csv", headers=headers)


@router.get('/investigations')
def investigations(service: Evaluation, user: Actor,
                   order: Annotated[str, Query(max_length=80)] = '',
                   offset: Annotated[int, Query(ge=0, le=100000)] = 0,
                   limit: Annotated[int, Query(ge=1, le=50)] = 30):
    from .investigations import investigations as read
    return read(service.engine, user, order=order, offset=offset, limit=limit)


@router.get('/investigations/{run_id}')
def investigation(run_id: str, service: Evaluation, user: Actor):
    from .investigations import investigation as read
    return read(service.engine, user, run_id)


@router.get('/bad-cases')
def bad_cases(service: Evaluation, user: Actor):
    from aftercare.config import ROOT
    from .bad_cases import catalogue
    return catalogue(ROOT, user)


@router.get('/campaigns')
def campaign_list(user: Actor):
    from aftercare.config import ROOT
    from .campaigns import campaigns
    return campaigns(ROOT,user)


@router.get('/campaigns/{campaign_id}')
def campaign_detail(user: Actor, campaign_id: str):
    from aftercare.config import ROOT
    from .campaigns import read_campaign
    return read_campaign(ROOT,user,campaign_id)
