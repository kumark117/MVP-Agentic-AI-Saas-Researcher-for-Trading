"""Identity routes (Design section 13, 19). Handlers translate HTTP to AuthService calls."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from alloyiq.api.deps import (
    Auth,
    Client,
    CsrfProtected,
    Svc,
    clear_session_cookie,
    optional_auth,
    session_token,
    set_csrf_cookie,
    set_session_cookies,
)
from alloyiq.api.envelope import Envelope, ok
from alloyiq.api.schemas.auth import (
    CsrfToken,
    EmailRequest,
    Fresh,
    GenericAccepted,
    LoginRequest,
    LoginResult,
    ReauthenticateRequest,
    RegisterAccepted,
    RegisterRequest,
    ResetDone,
    ResetPasswordRequest,
    Revoked,
    RevokedCount,
    TokenRequest,
    UserSummary,
    Verified,
)
from alloyiq.domain.identity import AccountContext

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/csrf", response_model=Envelope[CsrfToken])
async def csrf(
    request: Request,
    response: Response,
    svc: Svc,
    ctx: Annotated[AccountContext | None, Depends(optional_auth)],
) -> Envelope[CsrfToken]:
    token = svc.csrf.issue(str(ctx.session_id) if ctx else "anon")
    set_csrf_cookie(response, svc.settings, token)
    return ok(request, CsrfToken(csrf_token=token))


@router.post(
    "/register",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[RegisterAccepted],
    dependencies=[CsrfProtected],
)
async def register(
    request: Request, body: RegisterRequest, svc: Svc, client: Client
) -> Envelope[RegisterAccepted]:
    await svc.auth.register(
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        terms_version=body.terms_version,
        privacy_version=body.privacy_version,
        client=client,
    )
    return ok(request, RegisterAccepted())


@router.post("/verify-email", response_model=Envelope[Verified], dependencies=[CsrfProtected])
async def verify_email(
    request: Request, body: TokenRequest, svc: Svc, client: Client
) -> Envelope[Verified]:
    await svc.auth.verify_email(body.token, client)
    return ok(request, Verified())


@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[GenericAccepted],
    dependencies=[CsrfProtected],
)
async def resend_verification(
    request: Request, body: EmailRequest, svc: Svc, client: Client
) -> Envelope[GenericAccepted]:
    await svc.auth.resend_verification(body.email, client)
    return ok(request, GenericAccepted())


@router.post("/login", response_model=Envelope[LoginResult], dependencies=[CsrfProtected])
async def login(
    request: Request, response: Response, body: LoginRequest, svc: Svc, client: Client
) -> Envelope[LoginResult]:
    outcome = await svc.auth.login(
        email=body.email,
        password=body.password,
        client=client,
        previous_token=session_token(request, svc),
    )
    s = outcome.session
    csrf_token = svc.csrf.issue(str(s.session_id))
    set_session_cookies(response, svc.settings, session_token=s.token, csrf_token=csrf_token)
    user = UserSummary(
        user_id=s.user_id,
        email=outcome.email,
        display_name=outcome.display_name,
        email_verified=True,
    )
    return ok(
        request, LoginResult(user=user, session_expires_at=s.expires_at, csrf_token=csrf_token)
    )


@router.post("/logout", response_model=Envelope[Revoked], dependencies=[CsrfProtected])
async def logout(
    request: Request, response: Response, ctx: Auth, svc: Svc, client: Client
) -> Envelope[Revoked]:
    await svc.auth.logout(ctx, client)
    clear_session_cookie(response, svc.settings)
    set_csrf_cookie(response, svc.settings, svc.csrf.issue("anon"))
    return ok(request, Revoked())


@router.post("/logout-all", response_model=Envelope[RevokedCount], dependencies=[CsrfProtected])
async def logout_all(
    request: Request, response: Response, ctx: Auth, svc: Svc, client: Client
) -> Envelope[RevokedCount]:
    count = await svc.auth.logout_all(ctx, client)
    clear_session_cookie(response, svc.settings)
    set_csrf_cookie(response, svc.settings, svc.csrf.issue("anon"))
    return ok(request, RevokedCount(revoked_count=count))


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[GenericAccepted],
    dependencies=[CsrfProtected],
)
async def forgot_password(
    request: Request, body: EmailRequest, svc: Svc, client: Client
) -> Envelope[GenericAccepted]:
    await svc.auth.forgot_password(body.email, client)
    return ok(request, GenericAccepted())


@router.post("/reset-password", response_model=Envelope[ResetDone], dependencies=[CsrfProtected])
async def reset_password(
    request: Request, body: ResetPasswordRequest, svc: Svc, client: Client
) -> Envelope[ResetDone]:
    await svc.auth.reset_password(body.token, body.new_password, client)
    return ok(request, ResetDone())


@router.post("/reauthenticate", response_model=Envelope[Fresh], dependencies=[CsrfProtected])
async def reauthenticate(
    request: Request, body: ReauthenticateRequest, ctx: Auth, svc: Svc, client: Client
) -> Envelope[Fresh]:
    until = await svc.auth.reauthenticate(ctx, body.password, client)
    return ok(request, Fresh(fresh_until=until))
