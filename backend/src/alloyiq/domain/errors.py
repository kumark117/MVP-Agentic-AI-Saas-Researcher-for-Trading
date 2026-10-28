"""Stable error codes (D109) and the typed error that services raise. No HTTP imports."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    CSRF_FAILED = "CSRF_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_RUN_STATE = "INVALID_RUN_STATE"
    ESTIMATE_EXPIRED = "ESTIMATE_EXPIRED"
    ESTIMATE_MISMATCH = "ESTIMATE_MISMATCH"
    INSUFFICIENT_CREDIT = "INSUFFICIENT_CREDIT"
    PLAN_LIMIT_REACHED = "PLAN_LIMIT_REACHED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
    REPORT_NOT_READY = "REPORT_NOT_READY"
    EVENT_CURSOR_EXPIRED = "EVENT_CURSOR_EXPIRED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


PROBLEM_BASE = "https://alloyiq.local/problems/"


class DomainError(Exception):
    """A user-facing failure with a stable code; rendered as problem+json by the API."""

    def __init__(
        self,
        status: int,
        code: ErrorCode,
        title: str,
        detail: str,
        *,
        problem_type: str | None = None,
        field_errors: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.problem_type = PROBLEM_BASE + problem_type if problem_type else "about:blank"
        self.field_errors = field_errors
        self.headers = headers


def not_found(what: str = "Resource") -> DomainError:
    # 404 for both missing and inaccessible resources: existence is never revealed (D017).
    return DomainError(
        404, ErrorCode.RESOURCE_NOT_FOUND, f"{what} not found", f"{what} was not found."
    )


def reauth_required() -> DomainError:
    return DomainError(
        403,
        ErrorCode.AUTH_REQUIRED,
        "Re-authentication required",
        "Confirm your password to continue.",
        problem_type="reauthentication-required",
    )


def rate_limited(retry_after_s: int) -> DomainError:
    return DomainError(
        429,
        ErrorCode.RATE_LIMITED,
        "Too many requests",
        "Please wait before trying again.",
        headers={"Retry-After": str(retry_after_s)},
    )
