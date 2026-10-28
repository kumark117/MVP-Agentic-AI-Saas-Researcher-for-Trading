"""alloyiq-admin: the only administrative surface in the MVP (build prompt section 17; DV-02).

Run by the owner on the host or in the api container:
    docker compose exec api alloyiq-admin catalog list
It connects as the alloyiq_migrator role (DATABASE_MIGRATOR_URL), and every mutating
command writes an audit event with actor_kind=admin_cli.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from alloyiq import __version__
from alloyiq.repositories.db import Database

Command = Callable[[Database, argparse.Namespace], Awaitable[int]]
COMMANDS: dict[str, tuple[Command, Callable[[argparse.ArgumentParser], None], str]] = {}


def command(
    name: str, help_text: str, args: Callable[[argparse.ArgumentParser], None] = lambda p: None
) -> Callable[[Command], Command]:
    def register(fn: Command) -> Command:
        COMMANDS[name] = (fn, args, help_text)
        return fn

    return register


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _db() -> Database:
    url = os.environ.get("DATABASE_MIGRATOR_URL") or os.environ.get("ALEMBIC_DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_MIGRATOR_URL is not set (run inside the api container)")
    return Database(url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1), pool_size=2)


# ------------------------------------------------------------------ catalog
@command("catalog-list", "List instruments with availability and reasons")
async def catalog_list(db: Database, _: argparse.Namespace) -> int:
    from alloyiq.application.catalog import CatalogService

    items, unavailable = await CatalogService(db).instruments()
    _out(
        {
            "instruments": [
                {
                    "canonical_id": i.canonical_id,
                    "enabled": i.enabled,
                    "venue": i.venue,
                    "provider": i.provider,
                    "symbol": i.provider_symbol,
                    "reason": i.unavailable_reason,
                }
                for i in items
            ],
            "unavailable_combinations": [asdict(u) for u in unavailable],
        }
    )
    return 0


def _verify_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--live",
        action="store_true",
        help="call the real providers (requires LIVE_PROVIDER_TESTS=1)",
    )
    p.add_argument(
        "--apply", action="store_true", help="enable passing instruments and disable failing ones"
    )


@command("catalog-verify", "Run the adapter acceptance test for every instrument", _verify_args)
async def catalog_verify(db: Database, ns: argparse.Namespace) -> int:
    from alloyiq.application.catalog import CatalogVerifier
    from alloyiq.config import get_settings
    from alloyiq.repositories.audit import audit
    from alloyiq.repositories.db import DbContext
    from alloyiq.tools.factory import build_tool_service

    settings = get_settings()
    if settings.provider_mode == "live":
        if not ns.live or os.environ.get("LIVE_PROVIDER_TESTS") != "1":
            print(
                "Refusing: verification calls real providers (Yahoo, Alpha Vantage, Tavily).\n"
                "Re-run with --live and LIVE_PROVIDER_TESTS=1 once the owner approves.",
                file=sys.stderr,
            )
            return 3
    elif ns.apply:
        print(
            "Refusing to --apply results from PROVIDER_MODE=fixtures (synthetic data cannot "
            "enable an instrument).",
            file=sys.stderr,
        )
        return 3
    results = await CatalogVerifier(db, build_tool_service(settings, db)).verify_all(apply=ns.apply)
    if ns.apply:
        async with db.transaction(DbContext.for_system()) as conn:
            await audit(
                conn,
                action="catalog.verify",
                outcome="applied",
                account_id=None,
                actor_kind="admin_cli",
                metadata={r.canonical_id: r.passed for r in results},
            )
    _out(
        [
            {
                "canonical_id": r.canonical_id,
                "passed": r.passed,
                "reason": r.reason,
                "checks": r.checks,
            }
            for r in results
        ]
    )
    return 0 if all(r.passed for r in results) else 1


# Commands implemented by later phases register themselves on import.
_LATER = ("alloyiq.cli.admin_billing", "alloyiq.cli.admin_ops")


def main(argv: list[str] | None = None) -> int:
    import importlib

    for mod in _LATER:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            if exc.name != mod:
                raise
    parser = argparse.ArgumentParser(
        prog="alloyiq-admin", description="AlloyIQ owner administration (CLI only)"
    )
    parser.add_argument("--version", action="version", version=f"alloyiq {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_, add_args, help_text) in sorted(COMMANDS.items()):
        add_args(sub.add_parser(name, help=help_text))
    ns = parser.parse_args(argv)
    fn = COMMANDS[ns.command][0]

    async def run() -> int:
        db = _db()
        try:
            return await fn(db, ns)
        finally:
            await db.dispose()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
