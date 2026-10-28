"""AuthService: registration, verification, login, sessions, recovery (R008, R010-R013).

Enumeration resistance: register, resend-verification and forgot-password always return
the same generic result, do comparable work (an Argon2 hash or verify) whether or not the
email exists, and only differ in which email (if any) is queued.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import available_timezones

from sqlalchemy.ext.asyncio import AsyncConnection

from alloyiq.domain.errors import DomainError, ErrorCode, not_found, rate_limited, reauth_required
from alloyiq.domain.identity import (
    AUTH_TOKEN_TTL,
    DELETION_GRACE,
    FRESH_AUTH,
    LAST_SEEN_GRANULARITY,
    SESSION_ABSOLUTE,
    SESSION_IDLE,
    AccountContext,
    Role,
    next_idle_expiry,
    normalise_email,
)
from alloyiq.repositories.audit import audit, security_event
from alloyiq.repositories.auth import AuthRepository
from alloyiq.repositories.db import Database, DbContext
from alloyiq.security.passwords import PasswordService
from alloyiq.security.ratelimit import LIMITS, RateLimiter
from alloyiq.security.tokens import device_summary, ip_prefix, new_token, token_hash

# Called inside the verification transaction (e.g. the opening demo grant in P4).
OnVerified = Callable[[AsyncConnection, UUID, UUID], Awaitable[None]]
# Called after sessions are revoked (e.g. account SSE notification in P5).
OnRevoked = Callable[[AsyncConnection, UUID, list[UUID], str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class NewSession:
    token: str
    session_id: UUID
    user_id: UUID
    account_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    session: NewSession
    email: str
    display_name: str


def _ctx(user_id: UUID, account_id: UUID | None = None) -> DbContext:
    return DbContext(account_id=account_id, user_id=user_id)


class AuthService:
    def __init__(
        self,
        db: Database,
        passwords: PasswordService,
        limiter: RateLimiter,
        *,
        public_origin: str,
        repo: AuthRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.passwords = passwords
        self.limiter = limiter
        self.public_origin = public_origin.rstrip("/")
        self.repo = repo or AuthRepository()
        self.now = clock
        self.on_verified: list[OnVerified] = []
        self.on_revoked: list[OnRevoked] = []

    # ------------------------------------------------------------------ helpers
    async def _limit(self, *keys: tuple[str, str | None]) -> None:
        for name, subject in keys:
            if subject is None:
                continue
            decision = await self.limiter.hit(f"{name}:{subject}", LIMITS[name])
            if not decision.allowed:
                raise rate_limited(decision.retry_after_s)

    def _link(self, path: str, token: str) -> str:
        # Token in the URL fragment: never sent to servers, proxies or Referer (R013).
        return f"{self.public_origin}{path}#token={token}"

    async def _issue_token(self, conn: AsyncConnection, user_id: UUID, kind: str) -> str:
        token = new_token()
        await self.repo.create_auth_token(
            conn,
            user_id=user_id,
            kind=kind,
            token_hash=token_hash(token),
            expires_at=self.now() + AUTH_TOKEN_TTL,
        )
        return token

    async def _notify_revoked(
        self, conn: AsyncConnection, account_id: UUID, ids: list[UUID], reason: str
    ) -> None:
        if ids:
            for hook in self.on_revoked:
                await hook(conn, account_id, ids, reason)

    # ------------------------------------------------------------------ registration
    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        terms_version: str,
        privacy_version: str,
        client: ClientInfo,
    ) -> None:
        email = normalise_email(email)
        await self._limit(("register:ip", client.ip))
        password_hash = self.passwords.hash(password)  # always, for comparable timing
        async with self.db.transaction(DbContext.anonymous()) as conn:
            existing = await self.repo.lookup_user(conn, email)
        if existing:
            async with self.db.transaction(_ctx(existing.user_id, existing.account_id)) as conn:
                if existing.email_verified_at is None:
                    token = await self._issue_token(conn, existing.user_id, "verify")
                    await self.repo.enqueue_email(
                        conn,
                        account_id=existing.account_id,
                        user_id=existing.user_id,
                        to_email=email,
                        template="verify_email",
                        payload={"link": self._link("/verify-email", token)},
                    )
                else:
                    await self.repo.enqueue_email(
                        conn,
                        account_id=existing.account_id,
                        user_id=existing.user_id,
                        to_email=email,
                        template="account_exists",
                        payload={"login": f"{self.public_origin}/login"},
                    )
                await audit(
                    conn,
                    action="auth.register",
                    outcome="existing_email",
                    account_id=existing.account_id,
                    request_id=client.request_id,
                )
            return
        user_id, account_id = uuid4(), uuid4()
        async with self.db.transaction(_ctx(user_id, account_id)) as conn:
            await self.repo.create_user(
                conn,
                user_id=user_id,
                account_id=account_id,
                email=email,
                password_hash=password_hash,
                display_name=display_name.strip(),
                terms_version=terms_version,
                privacy_version=privacy_version,
            )
            token = await self._issue_token(conn, user_id, "verify")
            await self.repo.enqueue_email(
                conn,
                account_id=account_id,
                user_id=user_id,
                to_email=email,
                template="verify_email",
                payload={"link": self._link("/verify-email", token)},
            )
            await audit(
                conn,
                action="auth.register",
                outcome="created",
                account_id=account_id,
                actor_user_id=user_id,
                target_type="user",
                target_id=str(user_id),
                request_id=client.request_id,
            )

    async def resend_verification(self, email: str, client: ClientInfo) -> None:
        email = normalise_email(email)
        await self._limit(("recover:ip", client.ip), ("recover:email", email))
        async with self.db.transaction(DbContext.anonymous()) as conn:
            user = await self.repo.lookup_user(conn, email)
        if not user or user.email_verified_at is not None or user.status != "active":
            return
        async with self.db.transaction(_ctx(user.user_id, user.account_id)) as conn:
            token = await self._issue_token(conn, user.user_id, "verify")
            await self.repo.enqueue_email(
                conn,
                account_id=user.account_id,
                user_id=user.user_id,
                to_email=email,
                template="verify_email",
                payload={"link": self._link("/verify-email", token)},
            )

    async def verify_email(self, token: str, client: ClientInfo) -> None:
        await self._limit(("verify:ip", client.ip))
        invalid = DomainError(
            422,
            ErrorCode.VALIDATION_FAILED,
            "Invalid or expired link",
            "This verification link is invalid or has expired.",
        )
        async with self.db.transaction(DbContext.anonymous()) as conn:
            rec = await self.repo.find_token(conn, token_hash(token), "verify")
        if not rec or rec.consumed_at is not None or rec.expires_at <= self.now():
            raise invalid
        async with self.db.transaction(_ctx(rec.user_id, rec.account_id)) as conn:
            if not await self.repo.consume_token(conn, rec.token_id):
                raise invalid
            if await self.repo.mark_verified(conn, rec.user_id):
                for hook in self.on_verified:
                    await hook(conn, rec.account_id, rec.user_id)
            await audit(
                conn,
                action="auth.verify_email",
                outcome="verified",
                account_id=rec.account_id,
                actor_user_id=rec.user_id,
                request_id=client.request_id,
            )

    # ------------------------------------------------------------------ login / sessions
    async def login(
        self, *, email: str, password: str, client: ClientInfo, previous_token: str | None
    ) -> LoginOutcome:
        email = normalise_email(email)
        await self._limit(("login:ip", client.ip), ("login:email", email))
        async with self.db.transaction(DbContext.anonymous()) as conn:
            user = await self.repo.lookup_user(conn, email)
        ok = self.passwords.verify(user.password_hash if user else None, password)
        if not user or not ok:
            async with self.db.transaction(
                _ctx(user.user_id, user.account_id) if user else DbContext.anonymous()
            ) as conn:
                await security_event(
                    conn,
                    kind="auth.login_failed",
                    severity="low",
                    account_id=user.account_id if user else None,
                    request_id=client.request_id,
                    metadata={"ip_prefix": ip_prefix(client.ip)},
                )
            raise DomainError(
                401, ErrorCode.AUTH_REQUIRED, "Login failed", "The email or password is incorrect."
            )
        if user.status != "active" or user.account_status != "active":
            raise DomainError(
                403,
                ErrorCode.ACCOUNT_SUSPENDED,
                "Account unavailable",
                "This account is suspended. Contact support.",
            )
        if user.email_verified_at is None:
            raise DomainError(
                403,
                ErrorCode.AUTH_REQUIRED,
                "Email not verified",
                "Verify your email address first. Check your inbox or request a new link.",
                problem_type="email-unverified",
            )
        session = await self._create_session(user.user_id, user.account_id, client)
        async with self.db.transaction(_ctx(user.user_id, user.account_id)) as conn:
            if self.passwords.needs_rehash(user.password_hash or ""):
                await self.repo.set_password(conn, user.user_id, self.passwords.hash(password))
            await audit(
                conn,
                action="auth.login",
                outcome="success",
                account_id=user.account_id,
                actor_user_id=user.user_id,
                target_type="session",
                target_id=str(session.session_id),
                request_id=client.request_id,
            )
        if previous_token:  # rotate: the old cookie's session ends
            await self._revoke_by_token(previous_token, "rotated_on_login")
        return LoginOutcome(session=session, email=email, display_name=user.display_name)

    async def _create_session(
        self, user_id: UUID, account_id: UUID, client: ClientInfo
    ) -> NewSession:
        now = self.now()
        token = new_token()
        absolute = now + SESSION_ABSOLUTE
        idle = min(now + SESSION_IDLE, absolute)
        async with self.db.transaction(_ctx(user_id, account_id)) as conn:
            sid = await self.repo.create_session(
                conn,
                user_id=user_id,
                account_id=account_id,
                token_hash=token_hash(token),
                idle_expires_at=idle,
                absolute_expires_at=absolute,
                fresh_until=now + FRESH_AUTH,
                user_agent_summary=device_summary(client.user_agent),
                ip_prefix=ip_prefix(client.ip),
                csrf_secret=secrets.token_bytes(32),
            )
        return NewSession(token, sid, user_id, account_id, idle)

    async def _revoke_by_token(self, token: str, reason: str) -> None:
        async with self.db.transaction(DbContext.anonymous()) as conn:
            rec = await self.repo.resolve_session(conn, token_hash(token))
        if rec:
            async with self.db.transaction(_ctx(rec.user_id, rec.account_id)) as conn:
                if await self.repo.revoke_session(conn, rec.session_id, reason):
                    await self._notify_revoked(conn, rec.account_id, [rec.session_id], reason)

    async def authenticate(self, token: str | None) -> AccountContext | None:
        """Resolve a session cookie. Returns None for missing, revoked or expired sessions."""
        if not token or len(token) > 200:
            return None
        async with self.db.transaction(DbContext.anonymous()) as conn:
            rec = await self.repo.resolve_session(conn, token_hash(token))
        if not rec or rec.user_status != "active" or rec.account_status != "active":
            return None
        now = self.now()
        if now - rec.last_seen_at >= LAST_SEEN_GRANULARITY:
            async with self.db.transaction(_ctx(rec.user_id, rec.account_id)) as conn:
                await self.repo.touch_session(
                    conn,
                    rec.session_id,
                    now,
                    next_idle_expiry(now, rec.absolute_expires_at),
                    int(LAST_SEEN_GRANULARITY.total_seconds()),
                )
        role: Role = rec.role  # type: ignore[assignment]
        return AccountContext(rec.user_id, rec.account_id, role, rec.session_id, rec.fresh_until)

    async def session_active(self, ctx: AccountContext, token: str | None) -> bool:
        """Cheap re-check used by long-lived SSE streams (R012: close within 15 s)."""
        current = await self.authenticate(token)
        return current is not None and current.session_id == ctx.session_id

    async def logout(self, ctx: AccountContext, client: ClientInfo) -> None:
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            if await self.repo.revoke_session(conn, ctx.session_id, "logout"):
                await self._notify_revoked(conn, ctx.account_id, [ctx.session_id], "logout")
            await audit(
                conn,
                action="auth.logout",
                outcome="success",
                account_id=ctx.account_id,
                actor_user_id=ctx.user_id,
                request_id=client.request_id,
            )

    async def logout_all(self, ctx: AccountContext, client: ClientInfo) -> int:
        self._require_fresh(ctx)
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            ids = await self.repo.revoke_all(conn, ctx.user_id, "logout_all")
            await self._notify_revoked(conn, ctx.account_id, ids, "logout_all")
            await audit(
                conn,
                action="auth.logout_all",
                outcome="success",
                account_id=ctx.account_id,
                actor_user_id=ctx.user_id,
                request_id=client.request_id,
                metadata={"revoked": len(ids)},
            )
        return len(ids)

    def _require_fresh(self, ctx: AccountContext) -> None:
        if not ctx.is_fresh(self.now()):
            raise reauth_required()

    async def reauthenticate(
        self, ctx: AccountContext, password: str, client: ClientInfo
    ) -> datetime:
        await self._limit(("reauth:account", str(ctx.account_id)))
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            me = await self.repo.get_me(conn, ctx.user_id)
        assert me is not None  # noqa: S101 - session implies the user exists
        async with self.db.transaction(DbContext.anonymous()) as conn:
            user = await self.repo.lookup_user(conn, me["email"])
        if not user or not self.passwords.verify(user.password_hash, password):
            raise DomainError(
                401,
                ErrorCode.AUTH_REQUIRED,
                "Re-authentication failed",
                "The password is incorrect.",
            )
        until = self.now() + FRESH_AUTH
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            await self.repo.set_fresh(conn, ctx.session_id, until)
            await audit(
                conn,
                action="auth.reauthenticate",
                outcome="success",
                account_id=ctx.account_id,
                actor_user_id=ctx.user_id,
                request_id=client.request_id,
            )
        return until

    # ------------------------------------------------------------------ recovery
    async def forgot_password(self, email: str, client: ClientInfo) -> None:
        email = normalise_email(email)
        await self._limit(("recover:ip", client.ip), ("recover:email", email))
        async with self.db.transaction(DbContext.anonymous()) as conn:
            user = await self.repo.lookup_user(conn, email)
        if not user or user.status != "active":
            return
        async with self.db.transaction(_ctx(user.user_id, user.account_id)) as conn:
            token = await self._issue_token(conn, user.user_id, "reset")
            await self.repo.enqueue_email(
                conn,
                account_id=user.account_id,
                user_id=user.user_id,
                to_email=email,
                template="reset_password",
                payload={"link": self._link("/reset-password", token)},
            )
            await audit(
                conn,
                action="auth.forgot_password",
                outcome="token_issued",
                account_id=user.account_id,
                request_id=client.request_id,
            )

    async def reset_password(self, token: str, new_password: str, client: ClientInfo) -> None:
        await self._limit(("verify:ip", client.ip))
        invalid = DomainError(
            422,
            ErrorCode.VALIDATION_FAILED,
            "Invalid or expired link",
            "This reset link is invalid or has expired.",
        )
        async with self.db.transaction(DbContext.anonymous()) as conn:
            rec = await self.repo.find_token(conn, token_hash(token), "reset")
        if not rec or rec.consumed_at is not None or rec.expires_at <= self.now():
            raise invalid
        new_hash = self.passwords.hash(new_password)
        async with self.db.transaction(_ctx(rec.user_id, rec.account_id)) as conn:
            if not await self.repo.consume_token(conn, rec.token_id):
                raise invalid
            await self.repo.set_password(conn, rec.user_id, new_hash)
            # A reset proves control of the mailbox: treat the address as verified.
            await self.repo.mark_verified(conn, rec.user_id)
            ids = await self.repo.revoke_all(conn, rec.user_id, "password_reset")
            await self._notify_revoked(conn, rec.account_id, ids, "password_reset")
            await audit(
                conn,
                action="auth.reset_password",
                outcome="success",
                account_id=rec.account_id,
                actor_user_id=rec.user_id,
                request_id=client.request_id,
                metadata={"revoked": len(ids)},
            )

    async def change_password(
        self, ctx: AccountContext, new_password: str, client: ClientInfo
    ) -> NewSession:
        self._require_fresh(ctx)
        new_hash = self.passwords.hash(new_password)
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            await self.repo.set_password(conn, ctx.user_id, new_hash)
            ids = await self.repo.revoke_all(conn, ctx.user_id, "password_changed")
            await self._notify_revoked(conn, ctx.account_id, ids, "password_changed")
            await audit(
                conn,
                action="auth.change_password",
                outcome="success",
                account_id=ctx.account_id,
                actor_user_id=ctx.user_id,
                request_id=client.request_id,
                metadata={"revoked": len(ids)},
            )
        # Rotation: this device continues on a brand-new session (D015).
        return await self._create_session(ctx.user_id, ctx.account_id, client)

    # ------------------------------------------------------------------ profile and sessions
    async def me(self, ctx: AccountContext) -> dict[str, Any]:
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            me = await self.repo.get_me(conn, ctx.user_id)
        if me is None:
            raise not_found("Account")
        return me

    async def update_me(self, ctx: AccountContext, changes: dict[str, Any]) -> dict[str, Any]:
        tz = changes.get("timezone")
        if tz is not None and tz not in available_timezones():
            raise DomainError(
                422,
                ErrorCode.VALIDATION_FAILED,
                "Invalid timezone",
                "Use an IANA timezone name such as Asia/Kolkata.",
                field_errors=[("timezone", "unknown timezone")],
            )
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            pid = changes.get("default_model_profile_id")
            if pid is not None and not await self.repo.model_profile_enabled(conn, pid):
                raise DomainError(
                    422,
                    ErrorCode.VALIDATION_FAILED,
                    "Unknown model profile",
                    "Choose an enabled model profile.",
                    field_errors=[("default_model_profile_id", "not enabled")],
                )
            await self.repo.update_profile(conn, ctx.user_id, changes)
            me = await self.repo.get_me(conn, ctx.user_id)
        assert me is not None  # noqa: S101
        return me

    async def sessions(self, ctx: AccountContext) -> list[dict[str, Any]]:
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            return await self.repo.list_sessions(conn, ctx.user_id)

    async def revoke_session(
        self, ctx: AccountContext, session_id: UUID, client: ClientInfo
    ) -> None:
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            if not await self.repo.session_exists(conn, session_id):  # RLS: own sessions only
                raise not_found("Session")
            if await self.repo.revoke_session(conn, session_id, "revoked_by_user"):
                await self._notify_revoked(conn, ctx.account_id, [session_id], "revoked_by_user")
                await audit(
                    conn,
                    action="auth.revoke_session",
                    outcome="success",
                    account_id=ctx.account_id,
                    actor_user_id=ctx.user_id,
                    target_type="session",
                    target_id=str(session_id),
                    request_id=client.request_id,
                )

    async def request_deletion(
        self, ctx: AccountContext, client: ClientInfo
    ) -> tuple[UUID, datetime]:
        self._require_fresh(ctx)
        async with self.db.transaction(_ctx(ctx.user_id, ctx.account_id)) as conn:
            rid, when = await self.repo.create_deletion_request(
                conn,
                account_id=ctx.account_id,
                user_id=ctx.user_id,
                scheduled_at=self.now() + DELETION_GRACE,
            )
            await audit(
                conn,
                action="account.deletion_requested",
                outcome="scheduled",
                account_id=ctx.account_id,
                actor_user_id=ctx.user_id,
                target_type="deletion_request",
                target_id=str(rid),
                request_id=client.request_id,
            )
        return rid, when


__all__ = ["AuthService", "ClientInfo", "LoginOutcome", "NewSession"]
