"""Research routes: estimate, admission, runs, cancel, report, usage, activity and SSE
(Design sections 14, 16; R018-R027, R066). Handlers only translate HTTP into service calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from alloyiq.api.deps import Auth, CsrfProtected, Svc, session_token
from alloyiq.api.envelope import Envelope, ok
from alloyiq.api.routes.billing import IdempotencyKey
from alloyiq.api.schemas.common import Page, PageLimit, RunStatus
from alloyiq.api.schemas.research import (
    Activity,
    CancelRequest,
    Estimate,
    EstimateRequest,
    Report,
    RunAccepted,
    RunSnapshot,
    RunSummary,
    RunUsage,
    StartRunRequest,
)
from alloyiq.application.research import ResearchService
from alloyiq.application.streams import StreamService
from alloyiq.domain.identity import AccountContext

router = APIRouter(tags=["research"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
SSE_DOC: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "text/event-stream of persisted events; replay via Last-Event-ID or ?after=",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


def _research(svc: Svc) -> ResearchService:
    r: ResearchService = svc.extra["research"]
    return r


def _streams(svc: Svc) -> StreamService:
    s: StreamService = svc.extra["streams"]
    return s


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@router.post(
    "/research/estimates",
    status_code=201,
    response_model=Envelope[Estimate],
    dependencies=[CsrfProtected],
)
async def create_estimate(
    request: Request, body: EstimateRequest, ctx: Auth, svc: Svc
) -> Envelope[Estimate]:
    est = await _research(svc).estimate(
        ctx,
        instrument_id=body.instrument_id,
        horizon=body.horizon,
        model_profile_id=body.model_profile_id,
        question=body.question,
        conversation_id=body.conversation_id,
    )
    return ok(request, est)


@router.post(
    "/research/runs",
    status_code=202,
    response_model=Envelope[RunAccepted],
    dependencies=[CsrfProtected],
)
async def start_run(
    request: Request,
    response: Response,
    body: StartRunRequest,
    key: IdempotencyKey,
    ctx: Auth,
    svc: Svc,
) -> Envelope[RunAccepted]:
    status, accepted = await _research(svc).admit(
        ctx,
        estimate_id=body.estimate_id,
        max_charge=body.max_charge.amount,
        key=key,
        request_id=_rid(request),
    )
    response.status_code = status
    return ok(request, accepted)


@router.get("/research/runs", response_model=Envelope[Page[RunSummary]])
async def list_runs(
    request: Request,
    ctx: Auth,
    svc: Svc,
    status: RunStatus | None = None,
    instrument_id: UUID | None = None,
    model_profile_id: UUID | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: PageLimit = 20,
) -> Envelope[Page[RunSummary]]:
    items, nxt = await _research(svc).list_runs(
        ctx,
        status=status,
        instrument_id=instrument_id,
        model_profile_id=model_profile_id,
        start=from_,
        end=to,
        cursor=cursor,
        limit=limit,
    )
    return ok(request, Page[RunSummary](items=items, next_cursor=nxt))


@router.get("/research/runs/{run_id}", response_model=Envelope[RunSnapshot])
async def get_run(request: Request, run_id: UUID, ctx: Auth, svc: Svc) -> Envelope[RunSnapshot]:
    return ok(request, await _research(svc).snapshot(ctx, run_id))


@router.post(
    "/research/runs/{run_id}/cancel",
    status_code=202,
    response_model=Envelope[RunSnapshot],
    responses={200: {"model": Envelope[RunSnapshot], "description": "Already terminal"}},
    dependencies=[CsrfProtected],
)
async def cancel_run(
    request: Request,
    response: Response,
    run_id: UUID,
    ctx: Auth,
    svc: Svc,
    body: CancelRequest | None = None,
) -> Envelope[RunSnapshot]:
    status, snap = await _research(svc).cancel(
        ctx, run_id, body.reason if body else None, _rid(request)
    )
    response.status_code = status
    return ok(request, snap)


@router.get("/research/runs/{run_id}/report", response_model=Envelope[Report])
async def get_report(request: Request, run_id: UUID, ctx: Auth, svc: Svc) -> Envelope[Report]:
    return ok(request, await _research(svc).report(ctx, run_id))


@router.get("/research/runs/{run_id}/usage", response_model=Envelope[RunUsage])
async def get_usage(request: Request, run_id: UUID, ctx: Auth, svc: Svc) -> Envelope[RunUsage]:
    return ok(request, await _research(svc).usage(ctx, run_id))


@router.get("/research/runs/{run_id}/activity", response_model=Envelope[Page[Activity]])
async def get_activity(
    request: Request,
    run_id: UUID,
    ctx: Auth,
    svc: Svc,
    cursor: Annotated[str | None, Query(pattern=r"^\d{1,18}$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Envelope[Page[Activity]]:
    items, nxt = await _research(svc).activity(ctx, run_id, after=int(cursor or 0), limit=limit)
    return ok(
        request, Page[Activity](items=items, next_cursor=str(nxt) if nxt is not None else None)
    )


async def _sse(
    request: Request,
    svc: Svc,
    ctx: AccountContext,
    scope: str,
    scope_id: UUID,
    last_event_id: str | None,
    after: str | None,
) -> StreamingResponse:
    streams = _streams(svc)
    plan = await streams.prepare(
        scope=scope,  # type: ignore[arg-type]
        scope_id=scope_id,
        account_id=ctx.account_id,
        cursor=last_event_id if last_event_id is not None else after,  # header wins (D088)
    )
    token = session_token(request, svc)

    async def active() -> bool:
        return await svc.auth.session_active(ctx, token)

    return StreamingResponse(
        streams.stream(plan, active), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/research/runs/{run_id}/events", response_class=StreamingResponse, responses=SSE_DOC)
async def run_events(
    request: Request,
    run_id: UUID,
    ctx: Auth,
    svc: Svc,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=100)] = None,
    after: Annotated[str | None, Query(max_length=100)] = None,
) -> StreamingResponse:
    return await _sse(request, svc, ctx, "run", run_id, last_event_id, after)


@router.get("/events", response_class=StreamingResponse, responses=SSE_DOC)
async def account_events(
    request: Request,
    ctx: Auth,
    svc: Svc,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=100)] = None,
    after: Annotated[str | None, Query(max_length=100)] = None,
) -> StreamingResponse:
    return await _sse(request, svc, ctx, "account", ctx.account_id, last_event_id, after)
