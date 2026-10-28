"""problem+json rendering (D081) for DomainError and framework errors.

Internal exception text never reaches the client; INTERNAL_ERROR carries
only the request ID and a generic detail.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from alloyiq.api.envelope import FieldError, Problem, request_id_of
from alloyiq.domain.errors import DomainError, ErrorCode
from alloyiq.observability.logging import get_logger

PROBLEM_JSON = "application/problem+json"

# Services raise DomainError; the API layer has no separate error type.
ApiError = DomainError

__all__ = [
    "PROBLEM_JSON",
    "ApiError",
    "DomainError",
    "ErrorCode",
    "install_error_handlers",
    "problem_response",
]

log = get_logger(__name__)


def problem_response(
    request: Request,
    status: int,
    code: ErrorCode,
    title: str,
    detail: str,
    *,
    problem_type: str = "about:blank",
    field_errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        type=problem_type,
        title=title,
        status=status,
        code=code.value,
        detail=detail,
        instance=request.url.path,
        request_id=request_id_of(request),
        field_errors=field_errors,
    )
    return JSONResponse(
        body.model_dump(exclude_none=True),
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=headers,
    )


_HTTP_CODES: dict[int, tuple[ErrorCode, str]] = {
    401: (ErrorCode.AUTH_REQUIRED, "Authentication required"),
    404: (ErrorCode.RESOURCE_NOT_FOUND, "Resource not found"),
    405: (ErrorCode.VALIDATION_FAILED, "Method not allowed"),
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        fields = [FieldError(field=f, message=m) for f, m in exc.field_errors or []] or None
        return problem_response(
            request,
            exc.status,
            exc.code,
            exc.title,
            exc.detail,
            problem_type=exc.problem_type,
            field_errors=fields,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            # Never echo the rejected input (it may be a password).
            FieldError(field=".".join(str(p) for p in e.get("loc", ())), message=str(e.get("msg")))
            for e in exc.errors()
        ]
        return problem_response(
            request,
            422,
            ErrorCode.VALIDATION_FAILED,
            "Validation failed",
            "The request did not match the expected schema.",
            field_errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, title = _HTTP_CODES.get(exc.status_code, (ErrorCode.VALIDATION_FAILED, "Bad request"))
        return problem_response(request, exc.status_code, code, title, title + ".")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return problem_response(
            request,
            500,
            ErrorCode.INTERNAL_ERROR,
            "Internal error",
            "An unexpected error occurred. Quote the request ID when reporting it.",
        )
