"""Profile, password, session inventory and deletion routes (Design sections 13, 19)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response, status

from alloyiq.api.deps import (
    Auth,
    Client,
    CsrfProtected,
    Svc,
    clear_session_cookie,
    set_session_cookies,
)
from alloyiq.api.envelope import Envelope, ok
from alloyiq.api.schemas.auth import (
    AccountSummary,
    ChangePasswordRequest,
    DeletionRequest,
    DeletionScheduled,
    Me,
    PasswordChanged,
    Preferences,
    Revoked,
    SessionInfo,
    UpdateMeRequest,
    UserSummary,
)
from alloyiq.api.schemas.common import Page
from alloyiq.domain.identity import AccountContext

router = APIRouter(prefix="/me", tags=["me"])

_PERMISSIONS = {
    "member": ["research", "billing.simulated", "exports"],
    "platform_admin": ["research", "billing.simulated", "exports"],  # admin is CLI-only (DV-02)
}


def _me(ctx: AccountContext, row: dict[str, Any]) -> Me:
    return Me(
        user=UserSummary(
            user_id=row["user_id"],
            email=row["email"],
            display_name=row["display_name"],
            email_verified=row["email_verified_at"] is not None,
        ),
        account=AccountSummary(
            account_id=row["account_id"], status=row["account_status"], plan=row["plan"]
        ),
        permissions=_PERMISSIONS.get(ctx.role, []),
        preferences=Preferences(
            locale=row["locale"],
            timezone=row["timezone"],
            default_model_profile_id=row["default_model_profile_id"],
        ),
        current_session_id=ctx.session_id,
    )


@router.get("", response_model=Envelope[Me])
async def get_me(request: Request, ctx: Auth, svc: Svc) -> Envelope[Me]:
    return ok(request, _me(ctx, await svc.auth.me(ctx)))


@router.patch("", response_model=Envelope[Me], dependencies=[CsrfProtected])
async def patch_me(request: Request, body: UpdateMeRequest, ctx: Auth, svc: Svc) -> Envelope[Me]:
    changes = body.model_dump(exclude_unset=True)
    return ok(request, _me(ctx, await svc.auth.update_me(ctx, changes)))


@router.post("/password", response_model=Envelope[PasswordChanged], dependencies=[CsrfProtected])
async def change_password(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    ctx: Auth,
    svc: Svc,
    client: Client,
) -> Envelope[PasswordChanged]:
    session = await svc.auth.change_password(ctx, body.new_password, client)
    csrf_token = svc.csrf.issue(str(session.session_id))
    set_session_cookies(response, svc.settings, session_token=session.token, csrf_token=csrf_token)
    return ok(request, PasswordChanged(csrf_token=csrf_token))


@router.get("/sessions", response_model=Envelope[Page[SessionInfo]])
async def list_sessions(request: Request, ctx: Auth, svc: Svc) -> Envelope[Page[SessionInfo]]:
    rows = await svc.auth.sessions(ctx)
    items = [
        SessionInfo(
            session_id=r["id"],
            device=r["user_agent_summary"] or "Unknown device",
            ip_prefix=r["ip_prefix"],
            created_at=r["created_at"],
            last_seen_at=r["last_seen_at"],
            expires_at=r["expires_at"],
            current=r["id"] == ctx.session_id,
        )
        for r in rows
    ]
    return ok(request, Page[SessionInfo](items=items))


@router.delete(
    "/sessions/{session_id}", response_model=Envelope[Revoked], dependencies=[CsrfProtected]
)
async def revoke_session(
    request: Request, response: Response, session_id: UUID, ctx: Auth, svc: Svc, client: Client
) -> Envelope[Revoked]:
    await svc.auth.revoke_session(ctx, session_id, client)
    if session_id == ctx.session_id:
        clear_session_cookie(response, svc.settings)
    return ok(request, Revoked())


@router.post(
    "/deletion-requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[DeletionScheduled],
    dependencies=[CsrfProtected],
)
async def request_deletion(
    request: Request, body: DeletionRequest, ctx: Auth, svc: Svc, client: Client
) -> Envelope[DeletionScheduled]:
    rid, when = await svc.auth.request_deletion(ctx, client)
    return ok(request, DeletionScheduled(request_id=rid, scheduled_at=when))
