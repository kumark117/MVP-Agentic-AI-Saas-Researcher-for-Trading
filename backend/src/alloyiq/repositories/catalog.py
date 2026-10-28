"""Catalogue, model profile, rate card and plan reads (config tables; no RLS)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    id: UUID
    canonical_id: str
    display_name: str
    metal: str
    venue: str
    data_mode: str
    instrument_type: str
    contract_expiry: date | None
    quote_currency: str
    unit: str
    timezone: str
    calendar: str
    supported_horizons: list[str]
    freshness_policy: dict[str, Any]
    enabled: bool
    unavailable_reason: str | None
    provider: str
    provider_symbol: str
    function_or_interval: str

    def snapshot(self) -> dict[str, Any]:
        """Immutable copy stored on estimates/runs (D105 instrument_snapshot)."""
        return {
            "instrument_id": str(self.id),
            "canonical_id": self.canonical_id,
            "display_name": self.display_name,
            "metal": self.metal,
            "venue": self.venue,
            "data_mode": self.data_mode,
            "instrument_type": self.instrument_type,
            "contract_expiry": self.contract_expiry.isoformat() if self.contract_expiry else None,
            "quote_currency": self.quote_currency,
            "unit": self.unit,
            "timezone": self.timezone,
            "calendar": self.calendar,
            "provider": self.provider,
        }


_INSTRUMENT_SQL = (
    "SELECT i.id, i.canonical_id, i.display_name, i.metal, i.venue, i.data_mode,"
    " i.instrument_type, i.contract_expiry, i.quote_currency, i.unit, i.timezone, i.calendar,"
    " i.supported_horizons, i.freshness_policy, i.enabled, i.unavailable_reason,"
    " m.provider, m.provider_symbol, m.function_or_interval"
    " FROM instruments i JOIN LATERAL (SELECT * FROM provider_mappings pm"
    " WHERE pm.instrument_id = i.id ORDER BY pm.created_at LIMIT 1) m ON true"
)


def _instrument(row: Any) -> InstrumentRecord:
    d = dict(row._mapping)
    if isinstance(d["freshness_policy"], str):
        d["freshness_policy"] = json.loads(d["freshness_policy"])
    return InstrumentRecord(**d)


class CatalogRepository:
    async def list_instruments(self, conn: AsyncConnection) -> list[InstrumentRecord]:
        rows = await conn.execute(
            text(_INSTRUMENT_SQL + " ORDER BY i.metal, i.venue, i.canonical_id")
        )
        return [_instrument(r) for r in rows]

    async def get_instrument(
        self, conn: AsyncConnection, instrument_id: UUID
    ) -> InstrumentRecord | None:
        row = (
            await conn.execute(text(_INSTRUMENT_SQL + " WHERE i.id = :i"), {"i": instrument_id})
        ).first()
        return _instrument(row) if row else None

    async def set_instrument_availability(
        self,
        conn: AsyncConnection,
        instrument_id: UUID,
        *,
        enabled: bool,
        reason: str | None,
        evidence: dict[str, Any],
    ) -> None:
        await conn.execute(
            text(
                "UPDATE instruments SET enabled = :e, unavailable_reason = :r,"
                " verification_evidence = CAST(:ev AS jsonb), updated_at = now() WHERE id = :i"
            ),
            {
                "i": instrument_id,
                "e": enabled,
                "r": None if enabled else reason,
                "ev": json.dumps(evidence, default=str),
            },
        )
        await conn.execute(
            text(
                "UPDATE provider_mappings SET verified_at = CASE WHEN :e THEN now() ELSE NULL END"
                " WHERE instrument_id = :i"
            ),
            {"i": instrument_id, "e": enabled},
        )

    async def list_model_profiles(self, conn: AsyncConnection) -> list[dict[str, Any]]:
        rows = await conn.execute(
            text("SELECT * FROM model_profiles WHERE enabled ORDER BY is_default DESC, slug")
        )
        return [dict(r._mapping) for r in rows]

    async def get_model_profile(
        self, conn: AsyncConnection, profile_id: UUID
    ) -> dict[str, Any] | None:
        row = (
            await conn.execute(
                text("SELECT * FROM model_profiles WHERE id = :p"), {"p": profile_id}
            )
        ).first()
        return dict(row._mapping) if row else None

    async def current_rate_card(self, conn: AsyncConnection) -> dict[str, Any] | None:
        """The active card if one exists, otherwise the newest draft (labelled as such)."""
        row = (
            await conn.execute(
                text(
                    "SELECT * FROM rate_cards WHERE status IN ('active','draft')"
                    " ORDER BY (status = 'active') DESC, created_at DESC LIMIT 1"
                )
            )
        ).first()
        if not row:
            return None
        card = dict(row._mapping)
        items = await conn.execute(
            text(
                "SELECT model_id, token_category, provider_usd_per_mtok, verified"
                " FROM rate_card_items"
                " WHERE rate_card_id = :c ORDER BY model_id, token_category"
            ),
            {"c": card["id"]},
        )
        card["items"] = [dict(i._mapping) for i in items]
        return card

    async def default_plan(self, conn: AsyncConnection) -> dict[str, Any]:
        row = (
            await conn.execute(
                text("SELECT * FROM plans ORDER BY is_default DESC, created_at LIMIT 1")
            )
        ).one()
        return dict(row._mapping)

    async def list_plans(self, conn: AsyncConnection) -> list[dict[str, Any]]:
        return [
            dict(r._mapping) for r in await conn.execute(text("SELECT * FROM plans ORDER BY slug"))
        ]


def rate_lookup(card: dict[str, Any]) -> dict[tuple[str, str], Decimal | None]:
    return {(i["model_id"], i["token_category"]): i["provider_usd_per_mtok"] for i in card["items"]}


__all__ = ["CatalogRepository", "InstrumentRecord", "rate_lookup"]
