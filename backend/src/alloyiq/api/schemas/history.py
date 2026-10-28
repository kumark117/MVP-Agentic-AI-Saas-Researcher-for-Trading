"""Conversation and export boundary models (Design section 14-15; R033-R035)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from alloyiq.api.schemas.common import Out, Strict


class CreateConversationRequest(Strict):
    title: str | None = Field(default=None, max_length=200)


class Conversation(Out):
    conversation_id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class CreateMessageRequest(Strict):
    content: str = Field(min_length=1, max_length=4000)
    client_message_id: UUID


class Message(Out):
    message_id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant", "system"]
    content: str
    run_id: UUID | None
    created_at: datetime


class ExportRequest(Strict):
    kind: Literal["runs", "usage", "account_data"]
    from_: datetime = Field(alias="from")
    to: datetime
    format: Literal["csv", "json"]

    model_config = Strict.model_config | {"populate_by_name": True}


class ExportJob(Out):
    export_id: UUID
    kind: str
    format: str
    status: Literal["queued", "running", "ready", "failed", "expired"]
    status_url: str
    download_url: str | None
    download_expires_at: datetime | None
    row_count: int | None
    created_at: datetime
