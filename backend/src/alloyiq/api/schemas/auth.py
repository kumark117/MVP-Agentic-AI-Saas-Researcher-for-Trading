"""Identity, profile and session boundary models (Design section 13; R008-R013)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import EmailStr, Field

from alloyiq.api.schemas.common import Out, Strict

Password = Field(min_length=12, max_length=256)


class CsrfToken(Out):
    csrf_token: str


class RegisterRequest(Strict):
    email: EmailStr
    password: str = Password
    display_name: str = Field(min_length=1, max_length=80)
    terms_version: str = Field(min_length=1, max_length=40)
    privacy_version: str = Field(min_length=1, max_length=40)


class GenericAccepted(Out):
    """Same response whether or not the email exists (no enumeration, R008)."""

    accepted: Literal[True] = True
    message: str = "If the address can be used, you will receive an email shortly."


class RegisterAccepted(GenericAccepted):
    verification_required: Literal[True] = True


class TokenRequest(Strict):
    token: str = Field(min_length=20, max_length=200)


class Verified(Out):
    verified: Literal[True] = True


class EmailRequest(Strict):
    email: EmailStr


class LoginRequest(Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserSummary(Out):
    user_id: UUID
    email: str
    display_name: str
    email_verified: bool


class LoginResult(Out):
    user: UserSummary
    session_expires_at: datetime
    csrf_token: str  # new session, new CSRF binding


class ResetPasswordRequest(Strict):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Password


class ResetDone(Out):
    reset: Literal[True] = True


class ReauthenticateRequest(Strict):
    password: str = Field(min_length=1, max_length=256)


class Fresh(Out):
    result_type: Literal["fresh"] = "fresh"
    fresh_until: datetime


class Revoked(Out):
    revoked: Literal[True] = True


class RevokedCount(Out):
    revoked_count: int


class ChangePasswordRequest(Strict):
    new_password: str = Password


class PasswordChanged(Out):
    changed: Literal[True] = True
    csrf_token: str  # the session was rotated


class AccountSummary(Out):
    account_id: UUID
    currency: Literal["INR"] = "INR"
    status: str
    plan: str


class Preferences(Out):
    locale: str
    timezone: str
    default_model_profile_id: UUID | None


class Me(Out):
    user: UserSummary
    account: AccountSummary
    permissions: list[str]
    preferences: Preferences
    current_session_id: UUID


class UpdateMeRequest(Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    locale: str | None = Field(default=None, min_length=2, max_length=20)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    default_model_profile_id: UUID | None = None


class SessionInfo(Out):
    session_id: UUID
    device: str  # approximate; not an authoritative fingerprint (D015)
    ip_prefix: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    current: bool


class DeletionRequest(Strict):
    confirmation: Literal[True]


class DeletionScheduled(Out):
    request_id: UUID
    scheduled_at: datetime
