"""CatalogService: catalogue, model profiles, prices and plans for the API, and the adapter
acceptance test behind `alloyiq-admin catalog verify` (R014-R017, D019, D091).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from alloyiq.domain.catalog import DATA_MODE_LABELS, unavailable_combinations
from alloyiq.domain.market import history_plan
from alloyiq.repositories.catalog import CatalogRepository, InstrumentRecord
from alloyiq.repositories.db import Database, DbContext
from alloyiq.tools.schemas import InstrumentArgs, NewsArgs, ToolEnvelope
from alloyiq.tools.service import ToolService

# Run limits (R023, D034): server policy, versioned, shown in every estimate.
RUN_LIMITS = {
    "run_model_calls": 24,
    "run_tool_calls": 30,
    "run_active_seconds": 120,
    "branch_model_calls": 6,
    "branch_tool_calls": 10,
    "structured_output_repairs": 1,
}
POLICY_VERSION = "policy-2026-10-28"


class CatalogService:
    def __init__(self, db: Database, repo: CatalogRepository | None = None) -> None:
        self.db = db
        self.repo = repo or CatalogRepository()

    async def instruments(self) -> tuple[list[InstrumentRecord], list[Any]]:
        async with self.db.transaction(DbContext.anonymous()) as conn:
            items = await self.repo.list_instruments(conn)
        available = {(i.metal, i.venue) for i in items if i.enabled}
        return items, unavailable_combinations(available)

    async def model_profiles(self) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        async with self.db.transaction(DbContext.anonymous()) as conn:
            return await self.repo.list_model_profiles(conn), await self.repo.current_rate_card(
                conn
            )

    async def rate_card(self) -> dict[str, Any] | None:
        async with self.db.transaction(DbContext.anonymous()) as conn:
            return await self.repo.current_rate_card(conn)

    async def plans(self) -> list[dict[str, Any]]:
        async with self.db.transaction(DbContext.anonymous()) as conn:
            return await self.repo.list_plans(conn)


def customer_rate(provider_usd: Decimal | None, fx: Decimal, markup: Decimal) -> Decimal | None:
    """Simulated INR per million tokens (demo assumption), rounded up to the paisa."""
    if provider_usd is None:
        return None
    return (provider_usd * fx * markup).quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def data_mode_label(mode: str) -> str:
    return DATA_MODE_LABELS.get(mode, mode)


# ------------------------------------------------------------------ adapter acceptance test
@dataclass
class VerifyResult:
    canonical_id: str
    passed: bool
    checks: list[dict[str, Any]]
    reason: str | None


class CatalogVerifier:
    """Calls each instrument's tools for every supported horizon; enables it only if all pass.

    This talks to real providers when PROVIDER_MODE=live, so the CLI requires --live and
    LIVE_PROVIDER_TESTS=1 (owner approval, build prompt section 0).
    """

    def __init__(
        self, db: Database, tools: ToolService, repo: CatalogRepository | None = None
    ) -> None:
        self.db, self.tools = db, tools
        self.repo = repo or CatalogRepository()

    async def _call(self, name: str, ins: InstrumentRecord, horizon: str) -> ToolEnvelope:
        a = InstrumentArgs(tool_call_id=uuid.uuid4(), instrument_id=ins.id, horizon=horizon)  # type: ignore[arg-type]
        if name == "get_quote":
            return await self.tools.get_quote(a, allow_disabled=True)
        if name == "get_history":
            return await self.tools.get_history(a, allow_disabled=True)
        if name == "get_technical_indicators":
            return await self.tools.get_technical_indicators(a, allow_disabled=True)
        return await self.tools.search_news(NewsArgs(**a.model_dump()), allow_disabled=True)

    async def verify(self, ins: InstrumentRecord) -> VerifyResult:
        checks: list[dict[str, Any]] = []
        for horizon in ins.supported_horizons:
            for name in ("get_quote", "get_history", "get_technical_indicators", "search_news"):
                env = await self._call(name, ins, horizon)
                ok = env.status == "ok"
                detail: str | None = None if ok else (env.error.code if env.error else "error")
                if ok and name == "get_technical_indicators" and env.data and env.data["missing"]:
                    wanted = history_plan(horizon, ins.instrument_type).indicators  # type: ignore[arg-type]
                    missing = [m["indicator"] for m in env.data["missing"]]
                    # ATR needs high/low; reference series legitimately lack it.
                    ok = all(
                        (m == "ATR14" and ins.instrument_type == "reference_series")
                        or m not in wanted
                        for m in missing
                    )
                    detail = None if ok else f"missing {missing}"
                if ok and name == "search_news" and env.data and not env.data["articles"]:
                    ok, detail = False, "no articles"
                checks.append(
                    {
                        "horizon": horizon,
                        "tool": name,
                        "passed": ok,
                        "detail": detail,
                        "source": env.source,
                    }
                )
        passed = all(c["passed"] for c in checks)
        failed = [f"{c['tool']}[{c['horizon']}]: {c['detail']}" for c in checks if not c["passed"]]
        reason = None if passed else "Adapter acceptance test failed: " + "; ".join(failed)[:400]
        return VerifyResult(ins.canonical_id, passed, checks, reason)

    async def verify_all(self, *, apply: bool) -> list[VerifyResult]:
        async with self.db.transaction(DbContext.for_system()) as conn:
            items = await self.repo.list_instruments(conn)
        results = []
        for ins in items:
            res = await self.verify(ins)
            results.append(res)
            if apply:
                async with self.db.transaction(DbContext.for_system()) as conn:
                    await self.repo.set_instrument_availability(
                        conn,
                        ins.id,
                        enabled=res.passed,
                        reason=res.reason,
                        evidence={
                            "verified_at": datetime.now(UTC).isoformat(),
                            "checks": res.checks,
                        },
                    )
        return results
