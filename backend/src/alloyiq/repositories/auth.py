"""Identity persistence. Pre-auth lookups go through the SECURITY DEFINER functions (0003)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class LoginRecord:
    user_id: UUID
    account_id: UUID
    password_hash: str | None
    status: str
    account_status: str
    email_verified_at: datetime | None
    display_name: str


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: UUID
    user_id: UUID
    account_id: UUID
    role: str
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    fresh_until: datetime | None
    user_status: str
    account_status: str


@dataclass(frozen=True, slots=True)
class TokenRecord:
    token_id: UUID
    user_id: UUID
    account_id: UUID
    expires_at: datetime
    consumed_at: datetime | None


class AuthRepository:
    async def lookup_user(self, conn: AsyncConnection, email: str) -> LoginRecord | None:
        row = (await conn.execute(text("SELECT * FROM auth_lookup_user(:e)"), {"e": email})).first()
        return LoginRecord(*row) if row else None

    async def resolve_session(
        self, conn: AsyncConnection, token_hash: bytes
    ) -> SessionRecord | None:
        row = (
            await conn.execute(text("SELECT * FROM auth_resolve_session(:h)"), {"h": token_hash})
        ).first()
        return SessionRecord(*row) if row else None

    async def find_token(
        self, conn: AsyncConnection, token_hash: bytes, kind: str
    ) -> TokenRecord | None:
        row = (
            await conn.execute(
                text("SELECT * FROM auth_find_token(:h, :k)"), {"h": token_hash, "k": kind}
            )
        ).first()
        return TokenRecord(*row) if row else None

    # ---- writes (run inside a transaction whose context is the user/account concerned)
    async def create_user(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        account_id: UUID,
        email: str,
        password_hash: str,
        display_name: str,
        terms_version: str,
        privacy_version: str,
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, display_name, terms_version,"
                " privacy_version) VALUES (:u, :e, :p, :d, :t, :pv)"
            ),
            {
                "u": user_id,
                "e": email,
                "p": password_hash,
                "d": display_name,
                "t": terms_version,
                "pv": privacy_version,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO accounts (id, owner_user_id, plan_id)"
                " VALUES (:a, :u,"
                " (SELECT id FROM plans WHERE is_default ORDER BY created_at LIMIT 1))"
            ),
            {"a": account_id, "u": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO account_memberships (account_id, user_id, role)"
                " VALUES (:a, :u, 'member')"
            ),
            {"a": account_id, "u": user_id},
        )
        await conn.execute(text("INSERT INTO wallets (account_id) VALUES (:a)"), {"a": account_id})

    async def create_auth_token(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        kind: str,
        token_hash: bytes,
        expires_at: datetime,
    ) -> None:
        # A new token of a kind supersedes older unconsumed ones.
        await conn.execute(
            text(
                "UPDATE auth_tokens SET consumed_at = now() WHERE user_id = :u AND kind = :k"
                " AND consumed_at IS NULL"
            ),
            {"u": user_id, "k": kind},
        )
        await conn.execute(
            text(
                "INSERT INTO auth_tokens (user_id, kind, token_hash, expires_at)"
                " VALUES (:u, :k, :h, :x)"
            ),
            {"u": user_id, "k": kind, "h": token_hash, "x": expires_at},
        )

    async def consume_token(self, conn: AsyncConnection, token_id: UUID) -> bool:
        res = await conn.execute(
            text(
                "UPDATE auth_tokens SET consumed_at = now() WHERE id = :i AND consumed_at IS NULL"
                " AND expires_at > now()"
            ),
            {"i": token_id},
        )
        return res.rowcount == 1

    async def mark_verified(self, conn: AsyncConnection, user_id: UUID) -> bool:
        res = await conn.execute(
            text(
                "UPDATE users SET email_verified_at = now(), updated_at = now()"
                " WHERE id = :u AND email_verified_at IS NULL"
            ),
            {"u": user_id},
        )
        return res.rowcount == 1

    async def set_password(self, conn: AsyncConnection, user_id: UUID, password_hash: str) -> None:
        await conn.execute(
            text("UPDATE users SET password_hash = :p, updated_at = now() WHERE id = :u"),
            {"u": user_id, "p": password_hash},
        )

    async def create_session(
        self,
        conn: AsyncConnection,
        *,
        user_id: UUID,
        account_id: UUID,
        token_hash: bytes,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        fresh_until: datetime,
        user_agent_summary: str,
        ip_prefix: str | None,
        csrf_secret: bytes,
    ) -> UUID:
        sid: UUID = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (token_hash, user_id, account_id, idle_expires_at,"
                    " absolute_expires_at, fresh_until, user_agent_summary, ip_prefix, csrf_secret)"
                    " VALUES (:h, :u, :a, :i, :x, :f, :ua, :ip, :c) RETURNING id"
                ),
                {
                    "h": token_hash,
                    "u": user_id,
                    "a": account_id,
                    "i": idle_expires_at,
                    "x": absolute_expires_at,
                    "f": fresh_until,
                    "ua": user_agent_summary,
                    "ip": ip_prefix,
                    "c": csrf_secret,
                },
            )
        ).scalar_one()
        return sid

    async def touch_session(
        self,
        conn: AsyncConnection,
        session_id: UUID,
        now: datetime,
        idle_expires_at: datetime,
        granularity_s: int,
    ) -> None:
        await conn.execute(
            text(
                "UPDATE sessions SET last_seen_at = :n, idle_expires_at = :i WHERE id = :s"
                " AND last_seen_at < :n - make_interval(secs => :g)"
            ),
            {"s": session_id, "n": now, "i": idle_expires_at, "g": granularity_s},
        )

    async def set_fresh(self, conn: AsyncConnection, session_id: UUID, until: datetime) -> None:
        await conn.execute(
            text("UPDATE sessions SET fresh_until = :f WHERE id = :s"),
            {"s": session_id, "f": until},
        )

    async def revoke_session(self, conn: AsyncConnection, session_id: UUID, reason: str) -> bool:
        res = await conn.execute(
            text(
                "UPDATE sessions SET revoked_at = now(), revoked_reason = :r"
                " WHERE id = :s AND revoked_at IS NULL"
            ),
            {"s": session_id, "r": reason},
        )
        return res.rowcount == 1

    async def session_exists(self, conn: AsyncConnection, session_id: UUID) -> bool:
        return bool(
            (
                await conn.execute(text("SELECT 1 FROM sessions WHERE id = :s"), {"s": session_id})
            ).first()
        )

    async def revoke_all(
        self, conn: AsyncConnection, user_id: UUID, reason: str, except_session: UUID | None = None
    ) -> list[UUID]:
        rows = await conn.execute(
            text(
                "UPDATE sessions SET revoked_at = now(), revoked_reason = :r"
                " WHERE user_id = :u AND revoked_at IS NULL"
                " AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid)) RETURNING id"
            ),
            {"u": user_id, "r": reason, "x": except_session},
        )
        return [r[0] for r in rows]

    async def list_sessions(self, conn: AsyncConnection, user_id: UUID) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT id, user_agent_summary, ip_prefix, created_at, last_seen_at,"
                " least(idle_expires_at, absolute_expires_at) AS expires_at FROM sessions"
                " WHERE user_id = :u AND revoked_at IS NULL AND idle_expires_at > now()"
                " AND absolute_expires_at > now() ORDER BY last_seen_at DESC"
            ),
            {"u": user_id},
        )
        return [dict(r._mapping) for r in rows]

    async def get_me(self, conn: AsyncConnection, user_id: UUID) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text(
                    "SELECT u.id AS user_id, u.email, u.display_name, u.email_verified_at,"
                    " u.locale,"
                    " u.timezone, u.default_model_profile_id, a.id AS account_id, a.status AS"
                    " account_status, coalesce(p.slug, 'demo') AS plan, m.role"
                    " FROM users u JOIN accounts a ON a.owner_user_id = u.id"
                    " JOIN account_memberships m ON m.account_id = a.id AND m.user_id = u.id"
                    " LEFT JOIN plans p ON p.id = a.plan_id WHERE u.id = :u"
                ),
                {"u": user_id},
            )
        ).first()
        return dict(row._mapping) if row else None

    async def update_profile(
        self, conn: AsyncConnection, user_id: UUID, changes: dict[str, Any]
    ) -> None:
        allowed = {"display_name", "locale", "timezone", "default_model_profile_id"}
        cols = [c for c in changes if c in allowed]
        if not cols:
            return
        sets = ", ".join(f"{c} = :{c}" for c in cols)
        await conn.execute(
            text(f"UPDATE users SET {sets}, updated_at = now() WHERE id = :u"),  # noqa: S608
            {**{c: changes[c] for c in cols}, "u": user_id},
        )

    async def model_profile_enabled(self, conn: AsyncConnection, profile_id: UUID) -> bool:
        return bool(
            (
                await conn.execute(
                    text("SELECT 1 FROM model_profiles WHERE id = :p AND enabled"),
                    {"p": profile_id},
                )
            ).first()
        )

    async def enqueue_email(
        self,
        conn: AsyncConnection,
        *,
        account_id: UUID | None,
        user_id: UUID | None,
        to_email: str,
        template: str,
        payload: dict[str, Any],
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO email_outbox (account_id, user_id, to_email, template, payload)"
                " VALUES (:a, :u, :to, :t, CAST(:p AS jsonb))"
            ),
            {
                "a": account_id,
                "u": user_id,
                "to": to_email,
                "t": template,
                "p": json.dumps(payload),
            },
        )

    async def create_deletion_request(
        self, conn: AsyncConnection, *, account_id: UUID, user_id: UUID, scheduled_at: datetime
    ) -> tuple[UUID, datetime]:
        existing = (
            await conn.execute(
                text(
                    "SELECT id, scheduled_at FROM deletion_requests WHERE account_id = :a"
                    " AND status IN ('scheduled','processing')"
                ),
                {"a": account_id},
            )
        ).first()
        if existing:
            return existing[0], existing[1]
        row = (
            await conn.execute(
                text(
                    "INSERT INTO deletion_requests (account_id, user_id, scheduled_at)"
                    " VALUES (:a, :u, :s) RETURNING id, scheduled_at"
                ),
                {"a": account_id, "u": user_id, "s": scheduled_at},
            )
        ).one()
        return row[0], row[1]
