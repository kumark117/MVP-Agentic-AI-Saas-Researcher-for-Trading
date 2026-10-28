"""Opaque keyset cursors for list endpoints (D082): base64url of (timestamp, id)."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from alloyiq.domain.errors import DomainError, ErrorCode


def encode_cursor(ts: datetime, row_id: UUID) -> str:
    raw = json.dumps([ts.isoformat(), str(row_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, rid = json.loads(base64.urlsafe_b64decode(cursor + pad))
        return datetime.fromisoformat(ts), UUID(rid)
    except (ValueError, TypeError) as exc:
        raise DomainError(
            400,
            ErrorCode.VALIDATION_FAILED,
            "Invalid cursor",
            "The paging cursor is not valid. Start again from the first page.",
            field_errors=[("cursor", "invalid")],
        ) from exc
