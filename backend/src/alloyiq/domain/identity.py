"""Identity value objects and session policy (R008-R013, D012-D017). Pure: no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

Role = Literal["member", "platform_admin", "support", "finance"]

SESSION_IDLE = timedelta(hours=24)
SESSION_ABSOLUTE = timedelta(days=30)
FRESH_AUTH = timedelta(minutes=5)
AUTH_TOKEN_TTL = timedelta(minutes=30)
LAST_SEEN_GRANULARITY = timedelta(minutes=1)
DELETION_GRACE = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Who a request acts for. Built only by the authentication dependency."""

    user_id: UUID
    account_id: UUID
    role: Role
    session_id: UUID
    fresh_until: datetime | None

    def is_fresh(self, now: datetime) -> bool:
        return self.fresh_until is not None and self.fresh_until > now


def normalise_email(email: str) -> str:
    return email.strip().lower()


def next_idle_expiry(now: datetime, absolute_expires_at: datetime) -> datetime:
    return min(now + SESSION_IDLE, absolute_expires_at)
