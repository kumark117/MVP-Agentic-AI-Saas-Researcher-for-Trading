"""Seed configuration data (build prompt P1): Demo plan, model profiles, draft rate card v1
and the reference-data instrument catalogue.

Idempotent: re-running updates configuration rows in place. It never activates a rate card
and never enables an instrument; `alloyiq-admin rate-card activate` and
`alloyiq-admin catalog verify` do that after owner confirmation / adapter acceptance tests.

Run: scripts\\seed.ps1   (python -m alloyiq.cli.seed, as the alloyiq_migrator role)
"""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, create_engine, text

NS = uuid.UUID("6f1c1f0e-3a39-4c63-9a4b-a11091000000")  # deterministic seed IDs


def sid(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(NS, f"{kind}:{key}")


RATE_CARD_VERSION = "v1-2026-10-06"
RATE_SOURCE = "Anthropic pricing page as cached on 2026-10-06; owner must verify (OD-3)"

# (model_id, input, output, cache_read, cache_write); None = "verify" in the build prompt.
RATES: list[tuple[str, str | None, str | None, str | None, str | None]] = [
    ("claude-opus-5-5", "4.00", "20.00", "0.20", None),
    ("claude-sonnet-5-5", "2.00", "10.00", "0.20", None),
    # Haiku: base tier for prompts up to 100K tokens; per-call prompts stay well below it.
    ("claude-haiku-5-5", "0.10", "0.50", None, None),
]

PROFILES: list[dict[str, Any]] = [
    {
        "slug": "deep",
        "display_name": "Deep (Opus 5.5)",
        "primary": "claude-opus-5-5",
        "fallbacks": ["claude-sonnet-5-5"],
        "branch_effort": "low",
        "signal_effort": "high",
        "is_default": True,
    },
    {
        "slug": "balanced",
        "display_name": "Balanced (Sonnet 5.5)",
        "primary": "claude-sonnet-5-5",
        "fallbacks": [],
        "branch_effort": "low",
        "signal_effort": "medium",
        "is_default": False,
    },
    {
        "slug": "fast",
        "display_name": "Fast (Haiku 5.5)",
        "primary": "claude-haiku-5-5",
        "fallbacks": [],
        "branch_effort": "low",
        "signal_effort": "medium",
        "is_default": False,
    },
]

UNVERIFIED = (
    "Not yet verified: run `alloyiq-admin catalog-verify --live --apply` (adapter acceptance test)"
)

_FRESH_FUTURES = {
    "max_quote_age_s": {"intraday": 3600, "swing": 4 * 86400, "long_term": 8 * 86400},
    "expected_delay": "Delayed reference quotes (typically 10-20 minutes); unofficial source",
}

INSTRUMENTS: list[dict[str, Any]] = [
    {
        "canonical_id": "comex-gold-cont-ref",
        "display_name": "Gold - COMEX continuous front month (reference)",
        "metal": "gold",
        "venue": "COMEX",
        "instrument_type": "futures_continuous",
        "quote_currency": "USD",
        "unit": "troy_oz",
        "timezone": "America/New_York",
        "calendar": "CME_GLOBEX_METALS",
        "horizons": ["intraday", "swing", "long_term"],
        "freshness": _FRESH_FUTURES,
        "provider": "yahoo",
        "symbol": "GC=F",
        "interval": "",
        "entitlement": "unlicensed-dev-only",
    },
    {
        "canonical_id": "comex-silver-cont-ref",
        "display_name": "Silver - COMEX continuous front month (reference)",
        "metal": "silver",
        "venue": "COMEX",
        "instrument_type": "futures_continuous",
        "quote_currency": "USD",
        "unit": "troy_oz",
        "timezone": "America/New_York",
        "calendar": "CME_GLOBEX_METALS",
        "horizons": ["intraday", "swing", "long_term"],
        "freshness": _FRESH_FUTURES,
        "provider": "yahoo",
        "symbol": "SI=F",
        "interval": "",
        "entitlement": "unlicensed-dev-only",
    },
    {
        "canonical_id": "comex-copper-cont-ref",
        "display_name": "Copper - COMEX continuous front month (reference)",
        "metal": "copper",
        "venue": "COMEX",
        "instrument_type": "futures_continuous",
        "quote_currency": "USD",
        "unit": "lb",
        "timezone": "America/New_York",
        "calendar": "CME_GLOBEX_METALS",
        "horizons": ["intraday", "swing", "long_term"],
        "freshness": _FRESH_FUTURES,
        "provider": "yahoo",
        "symbol": "HG=F",
        "interval": "",
        "entitlement": "unlicensed-dev-only",
    },
    {
        "canonical_id": "global-copper-monthly-ref",
        "display_name": "Copper - global monthly price (reference series)",
        "metal": "copper",
        "venue": "GLOBAL_REF",
        "instrument_type": "reference_series",
        "quote_currency": "USD",
        "unit": "metric_ton",
        "timezone": "UTC",
        "calendar": "MONTHLY",
        "horizons": ["long_term"],
        "freshness": {
            "max_quote_age_s": {"long_term": 62 * 86400},
            "expected_delay": "Monthly global reference price; published with a lag",
        },
        "provider": "alphavantage",
        "symbol": "COPPER",
        "interval": "monthly",
        "entitlement": "alphavantage-api-key",
    },
]


def _upsert(
    conn: Connection, table: str, row: dict[str, Any], conflict: str, update: list[str]
) -> None:
    cols = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in update) or "id = EXCLUDED.id"
    conn.execute(
        text(
            f"INSERT INTO {table} ({cols}) VALUES ({vals}) "  # noqa: S608 - fixed identifiers
            f"ON CONFLICT ({conflict}) DO UPDATE SET {sets}"
        ),
        row,
    )


def seed(
    conn: Connection, *, demo_fx: Decimal, markup: Decimal, topup_limit: Decimal
) -> dict[str, int]:
    plan_id = sid("plan", "demo")
    _upsert(
        conn,
        "plans",
        {
            "id": plan_id,
            "slug": "demo",
            "display_name": "Demo",
            "active_runs": 2,
            "queued_runs": 5,
            "sse_streams": 3,
            "daily_topup_limit_inr": topup_limit,
            "is_default": True,
        },
        "slug",
        [
            "display_name",
            "active_runs",
            "queued_runs",
            "sse_streams",
            "daily_topup_limit_inr",
            "is_default",
        ],
    )

    for p in PROFILES:
        _upsert(
            conn,
            "model_profiles",
            {
                "id": sid("model_profile", p["slug"]),
                "slug": p["slug"],
                "display_name": p["display_name"],
                "provider": "anthropic",
                "primary_model_id": p["primary"],
                "approved_fallback_model_ids": p["fallbacks"],
                "branch_effort": p["branch_effort"],
                "signal_effort": p["signal_effort"],
                "max_tokens_branch": 2000,
                "max_tokens_signal": 6000,
                "max_tokens_repair": 3000,
                "run_input_token_budget": 150_000,
                "run_output_token_budget": 20_000,
                "context_limit_tokens": 200_000,
                "enabled": True,
                "is_default": p["is_default"],
            },
            "slug",
            [
                "display_name",
                "primary_model_id",
                "approved_fallback_model_ids",
                "branch_effort",
                "signal_effort",
                "max_tokens_branch",
                "max_tokens_signal",
                "max_tokens_repair",
                "run_input_token_budget",
                "run_output_token_budget",
                "context_limit_tokens",
                "enabled",
                "is_default",
            ],
        )

    card_id = sid("rate_card", RATE_CARD_VERSION)
    status = conn.execute(
        text("SELECT status FROM rate_cards WHERE version = :v"), {"v": RATE_CARD_VERSION}
    ).scalar()
    if status in (None, "draft"):
        _upsert(
            conn,
            "rate_cards",
            {
                "id": card_id,
                "version": RATE_CARD_VERSION,
                "status": "draft",
                "demo_fx_usd_inr": demo_fx,
                "demo_markup": markup,
                "fx_rule": "demo assumption DEMO_FX_USD_INR; not a market rate",
                "source_note": RATE_SOURCE,
            },
            "version",
            ["demo_fx_usd_inr", "demo_markup", "fx_rule", "source_note"],
        )
        for model_id, *prices in RATES:
            for category, price in zip(
                ("input", "output", "cache_read", "cache_write"), prices, strict=True
            ):
                _upsert(
                    conn,
                    "rate_card_items",
                    {
                        "id": sid("rate_item", f"{RATE_CARD_VERSION}:{model_id}:{category}"),
                        "rate_card_id": card_id,
                        "model_id": model_id,
                        "token_category": category,
                        "provider_usd_per_mtok": Decimal(price) if price else None,
                        "verified": False,
                        "source_note": RATE_SOURCE if price else "verify: no confirmed price",
                    },
                    "rate_card_id, model_id, token_category",
                    ["provider_usd_per_mtok", "source_note"],
                )

    for ins in INSTRUMENTS:
        iid = sid("instrument", ins["canonical_id"])
        _upsert(
            conn,
            "instruments",
            {
                "id": iid,
                "canonical_id": ins["canonical_id"],
                "display_name": ins["display_name"],
                "metal": ins["metal"],
                "venue": ins["venue"],
                "data_mode": "reference",
                "instrument_type": ins["instrument_type"],
                "contract_expiry": None,
                "quote_currency": ins["quote_currency"],
                "unit": ins["unit"],
                "timezone": ins["timezone"],
                "calendar": ins["calendar"],
                "supported_horizons": ins["horizons"],
                "freshness_policy": json.dumps(ins["freshness"]),
                "enabled": False,
                "unavailable_reason": UNVERIFIED,
            },
            "canonical_id",
            [
                "display_name",
                "metal",
                "venue",
                "instrument_type",
                "quote_currency",
                "unit",
                "timezone",
                "calendar",
                "supported_horizons",
                "freshness_policy",
            ],
        )
        _upsert(
            conn,
            "provider_mappings",
            {
                "id": sid("mapping", ins["canonical_id"]),
                "instrument_id": iid,
                "provider": ins["provider"],
                "provider_symbol": ins["symbol"],
                "function_or_interval": ins["interval"],
                "entitlement": ins["entitlement"],
            },
            "instrument_id, provider, function_or_interval",
            ["provider_symbol", "entitlement"],
        )

    return {
        "plans": 1,
        "model_profiles": len(PROFILES),
        "rate_cards": 1,
        "instruments": len(INSTRUMENTS),
    }


def main() -> int:
    url = os.environ.get("ALEMBIC_DATABASE_URL")
    if not url:
        raise SystemExit("Set ALEMBIC_DATABASE_URL (scripts/seed.ps1 does this)")
    engine = create_engine(url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1))
    with engine.begin() as conn:
        counts = seed(
            conn,
            demo_fx=Decimal(os.environ.get("DEMO_FX_USD_INR", "85.00")),
            markup=Decimal(os.environ.get("DEMO_MARKUP", "1.25")),
            topup_limit=Decimal(os.environ.get("DEMO_DAILY_TOPUP_LIMIT_INR", "1000.00")),
        )
    print("seeded:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
