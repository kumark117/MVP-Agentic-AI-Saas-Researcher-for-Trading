"""Conversations, feedback, reports and exports (Design sections 14-15, 19; R033-R035,
R044-R048). Handlers only translate HTTP into service calls."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from alloyiq.api.deps import Auth, CsrfProtected, Svc
from alloyiq.api.envelope import Envelope, ok
from alloyiq.api.routes.billing import IdempotencyKey
from alloyiq.api.schemas.common import Page, PageLimit
from alloyiq.api.schemas.history import (
    Conversation,
    CreateConversationRequest,
    CreateMessageRequest,
    ExportJob,
    ExportRequest,
    Message,
)
from alloyiq.api.schemas.reports import ModelsReport, Summary
from alloyiq.api.schemas.research import Feedback, FeedbackRequest
from alloyiq.application.exports import ExportService
from alloyiq.application.history import HistoryService
from alloyiq.application.reports import ReportService

router = APIRouter()
Cursor = Annotated[str | None, Query(max_length=200)]


def _history(svc: Svc) -> HistoryService:
    h: HistoryService = svc.extra["history"]
    return h


def _reports(svc: Svc) -> ReportService:
    r: ReportService = svc.extra["reports"]
    return r


def _exports(svc: Svc) -> ExportService:
    e: ExportService = svc.extra["exports"]
    return e


# ------------------------------------------------------------------ conversations
@router.post(
    "/conversations",
    status_code=201,
    response_model=Envelope[Conversation],
    dependencies=[CsrfProtected],
    tags=["history"],
)
async def create_conversation(
    request: Request, body: CreateConversationRequest, ctx: Auth, svc: Svc
) -> Envelope[Conversation]:
    return ok(request, await _history(svc).create_conversation(ctx, body.title))


@router.get("/conversations", response_model=Envelope[Page[Conversation]], tags=["history"])
async def list_conversations(
    request: Request, ctx: Auth, svc: Svc, cursor: Cursor = None, limit: PageLimit = 20
) -> Envelope[Page[Conversation]]:
    items, nxt = await _history(svc).list_conversations(ctx, cursor=cursor, limit=limit)
    return ok(request, Page[Conversation](items=items, next_cursor=nxt))


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=Envelope[Page[Message]],
    tags=["history"],
)
async def list_messages(
    request: Request,
    conversation_id: UUID,
    ctx: Auth,
    svc: Svc,
    cursor: Cursor = None,
    limit: PageLimit = 50,
) -> Envelope[Page[Message]]:
    items, nxt = await _history(svc).messages(ctx, conversation_id, cursor=cursor, limit=limit)
    return ok(request, Page[Message](items=items, next_cursor=nxt))


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=201,
    response_model=Envelope[Message],
    dependencies=[CsrfProtected],
    tags=["history"],
)
async def post_message(
    request: Request,
    response: Response,
    conversation_id: UUID,
    body: CreateMessageRequest,
    ctx: Auth,
    svc: Svc,
) -> Envelope[Message]:
    created, msg = await _history(svc).post_message(
        ctx, conversation_id, body.content, body.client_message_id
    )
    response.status_code = 201 if created else 200
    return ok(request, msg)


# ------------------------------------------------------------------ feedback
@router.post(
    "/research/runs/{run_id}/feedback",
    status_code=201,
    response_model=Envelope[Feedback],
    dependencies=[CsrfProtected],
    tags=["research"],
)
async def create_feedback(
    request: Request, response: Response, run_id: UUID, body: FeedbackRequest, ctx: Auth, svc: Svc
) -> Envelope[Feedback]:
    created, fb = await _history(svc).set_feedback(ctx, run_id, body.rating, body.comment)
    response.status_code = 201 if created else 200
    return ok(request, fb)


@router.put(
    "/research/runs/{run_id}/feedback",
    response_model=Envelope[Feedback],
    dependencies=[CsrfProtected],
    tags=["research"],
)
async def update_feedback(
    request: Request, run_id: UUID, body: FeedbackRequest, ctx: Auth, svc: Svc
) -> Envelope[Feedback]:
    _, fb = await _history(svc).set_feedback(ctx, run_id, body.rating, body.comment)
    return ok(request, fb)


# ------------------------------------------------------------------ reports
@router.get("/reports/summary", response_model=Envelope[Summary], tags=["reports"])
async def summary(
    request: Request,
    ctx: Auth,
    svc: Svc,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    instrument_id: UUID | None = None,
    model_profile_id: UUID | None = None,
) -> Envelope[Summary]:
    return ok(
        request,
        await _reports(svc).summary(
            ctx, start=from_, end=to, instrument_id=instrument_id, model_profile_id=model_profile_id
        ),
    )


@router.get("/reports/models", response_model=Envelope[ModelsReport], tags=["reports"])
async def models_report(
    request: Request,
    ctx: Auth,
    svc: Svc,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
) -> Envelope[ModelsReport]:
    return ok(request, await _reports(svc).models(ctx, start=from_, end=to))


# ------------------------------------------------------------------ exports
@router.post(
    "/exports",
    status_code=202,
    response_model=Envelope[ExportJob],
    dependencies=[CsrfProtected],
    tags=["exports"],
)
async def create_export(
    request: Request,
    response: Response,
    body: ExportRequest,
    key: IdempotencyKey,
    ctx: Auth,
    svc: Svc,
) -> Envelope[ExportJob]:
    status, job = await _exports(svc).create(
        ctx,
        kind=body.kind,
        fmt=body.format,
        start=body.from_,
        end=body.to,
        key=key,
        request_id=getattr(request.state, "request_id", None),
    )
    response.status_code = status
    return ok(request, job)


@router.get("/exports/{export_id}", response_model=Envelope[ExportJob], tags=["exports"])
async def get_export(request: Request, export_id: UUID, ctx: Auth, svc: Svc) -> Envelope[ExportJob]:
    return ok(request, await _exports(svc).get(ctx, export_id))


@router.get(
    "/exports/{export_id}/download",
    response_class=Response,
    tags=["exports"],
    responses={
        200: {
            "description": "The export file (CSV or JSON)",
            "content": {
                "text/csv": {"schema": {"type": "string"}},
                "application/json": {"schema": {"title": "ExportFile", "type": "object"}},
            },
        }
    },
)
async def download_export(
    export_id: UUID,
    ctx: Auth,
    svc: Svc,
    expires: Annotated[int, Query()],
    sig: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
) -> Response:
    data, ctype, name = await _exports(svc).download(ctx, export_id, expires, sig)
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
