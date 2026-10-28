"""Service container and request dependencies (authentication, CSRF, client info).

AccountContext is built here and only here, from the session cookie. Nothing in a request
body, query or path can choose the account (D017).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import urlsplit

import structlog
from fastapi import Depends, Request, Response

from alloyiq.application.auth import AuthService, ClientInfo
from alloyiq.application.catalog import CatalogService
from alloyiq.config import Settings
from alloyiq.domain.errors import DomainError, ErrorCode
from alloyiq.domain.identity import SESSION_ABSOLUTE, AccountContext
from alloyiq.repositories.db import Database
from alloyiq.security.passwords import Argon2Params, PasswordService
from alloyiq.security.ratelimit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter
from alloyiq.security.tokens import CsrfTokens, pseudonym

CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PLACEHOLDER_DB = "postgresql+asyncpg://unconfigured@127.0.0.1:1/unconfigured"


@dataclass
class Services:
    settings: Settings
    db: Database
    limiter: RateLimiter
    csrf: CsrfTokens
    auth: AuthService
    extra: dict[str, Any] = field(default_factory=dict)  # later phases register services here


def build_services(
    settings: Settings, *, db: Database | None = None, limiter: RateLimiter | None = None
) -> Services:
    url = settings.database_url.get_secret_value() if settings.database_url else _PLACEHOLDER_DB
    db = db or Database(url)
    if limiter is None:
        limiter = (
            RedisRateLimiter(settings.redis_url) if settings.redis_url else InMemoryRateLimiter()
        )
    passwords = PasswordService(
        Argon2Params(
            settings.argon2_time_cost, settings.argon2_memory_kib, settings.argon2_parallelism
        )
    )
    auth = AuthService(db, passwords, limiter, public_origin=settings.public_origin)
    svc = Services(
        settings=settings,
        db=db,
        limiter=limiter,
        csrf=CsrfTokens(settings.session_secret.get_secret_value()),
        auth=auth,
    )
    svc.extra["catalog"] = CatalogService(db)
    return svc


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(services)]


def client_info(request: Request) -> ClientInfo:
    # request.client is the real peer: uvicorn trusts X-Forwarded-For only from Caddy.
    return ClientInfo(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


Client = Annotated[ClientInfo, Depends(client_info)]


def session_token(request: Request, svc: Svc) -> str | None:
    return request.cookies.get(svc.settings.session_cookie)


async def optional_auth(request: Request, svc: Svc) -> AccountContext | None:
    ctx = await svc.auth.authenticate(session_token(request, svc))
    if ctx:
        request.state.account = ctx
        structlog.contextvars.bind_contextvars(
            account_pseudonym=pseudonym(
                svc.settings.session_secret.get_secret_value(), str(ctx.account_id)
            ),
            session_ref=str(ctx.session_id)[:8],
        )
    return ctx


async def require_auth(
    ctx: Annotated[AccountContext | None, Depends(optional_auth)],
) -> AccountContext:
    if ctx is None:
        raise DomainError(
            401, ErrorCode.AUTH_REQUIRED, "Authentication required", "Sign in to continue."
        )
    return ctx


Auth = Annotated[AccountContext, Depends(require_auth)]


def _origin_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


async def csrf_protect(
    request: Request,
    svc: Svc,
    ctx: Annotated[AccountContext | None, Depends(optional_auth)],
) -> None:
    """Origin check plus signed double-submit token on every state-changing request (D016)."""
    if request.method not in UNSAFE_METHODS:
        return
    expected = svc.settings.public_origin.rstrip("/")
    origin = request.headers.get("origin") or _origin_of(request.headers.get("referer"))
    failed = DomainError(
        403, ErrorCode.CSRF_FAILED, "Request blocked", "Refresh the page and try again."
    )
    if origin != expected:
        raise failed
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(svc.settings.csrf_cookie)
    binding = str(ctx.session_id) if ctx else "anon"
    if not header or not cookie or not hmac.compare_digest(header, cookie):
        raise failed
    if not svc.csrf.verify(header, binding):
        raise failed


CsrfProtected = Depends(csrf_protect)


def set_session_cookies(
    response: Response, settings: Settings, *, session_token: str, csrf_token: str
) -> None:
    secure = settings.is_prod
    response.set_cookie(
        settings.session_cookie,
        session_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
        max_age=int(SESSION_ABSOLUTE.total_seconds()),
    )
    set_csrf_cookie(response, settings, csrf_token)


def set_csrf_cookie(response: Response, settings: Settings, csrf_token: str) -> None:
    response.set_cookie(
        settings.csrf_cookie,
        csrf_token,
        httponly=True,
        secure=settings.is_prod,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie, path="/", secure=settings.is_prod, httponly=True, samesite="lax"
    )
