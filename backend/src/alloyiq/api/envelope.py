"""Response envelopes (D081): successes are {data, meta}; errors are problem+json.

Every route returns a typed model; no undocumented generic dicts (D110).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field


class Meta(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    server_time: datetime


class Envelope[T](BaseModel):
    data: T
    meta: Meta


class FieldError(BaseModel):
    field: str
    message: str


class Problem(BaseModel):
    """application/problem+json body (D081, D109)."""

    type: str = "about:blank"
    title: str
    status: int
    code: str = Field(description="Stable machine error code (D109)")
    detail: str
    instance: str
    request_id: str
    field_errors: list[FieldError] | None = None


def request_id_of(request: Request) -> str:
    rid: str = request.state.request_id
    return rid


def make_meta(request: Request) -> Meta:
    return Meta(request_id=request_id_of(request), server_time=datetime.now(UTC))


def ok[T](request: Request, data: T) -> Envelope[T]:
    return Envelope[T](data=data, meta=make_meta(request))
