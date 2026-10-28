"""Worker loop registry. Each phase adds its periodic tasks and daemons here."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from alloyiq.adapters.email import SmtpEmailSender
from alloyiq.adapters.payments import DummyPaymentProvider
from alloyiq.adapters.pg_listen import PgListener
from alloyiq.application.billing import BillingService
from alloyiq.application.budget import BudgetService, SpendCaps
from alloyiq.application.email_dispatch import EmailDispatcher
from alloyiq.application.lifecycle import RunLifecycle
from alloyiq.application.recovery import RecoverySweeper
from alloyiq.application.settlement import SettlementService
from alloyiq.application.wallet import WalletService
from alloyiq.config import Settings
from alloyiq.repositories.db import Database
from alloyiq.repositories.runs import CANCEL_CHANNEL, JOBS_CHANNEL
from alloyiq.security.ratelimit import InMemoryRateLimiter
from alloyiq.worker.jobs import JobRunner
from alloyiq.workflows.runner import RunExecutor

Loop = tuple[str, Callable[[], Awaitable[object]], float]
Daemon = tuple[str, Callable[[asyncio.Event], Awaitable[None]]]


@dataclass
class WorkerParts:
    loops: list[Loop]
    daemons: list[Daemon]
    cleanup: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def build_billing(settings: Settings, db: Database) -> tuple[WalletService, BillingService]:
    wallet = WalletService(db, opening_credit=settings.demo_opening_credit_inr)
    billing = BillingService(
        db,
        wallet,
        DummyPaymentProvider(),
        InMemoryRateLimiter(),  # the worker serves no browser requests
        checkout_max=settings.demo_checkout_max_inr,
    )
    return wallet, billing


def build_lifecycle(settings: Settings, wallet: WalletService) -> RunLifecycle:
    caps = SpendCaps(
        settings.real_spend_per_run_usd,
        settings.real_spend_account_day_usd,
        settings.real_spend_platform_day_usd,
    )
    return RunLifecycle(SettlementService(wallet, BudgetService(caps)))


def build_executor(settings: Settings, db: Database) -> RunExecutor:
    from alloyiq.workflows.factory import build_workflow

    return build_workflow(settings, db)


def build_worker(settings: Settings, worker_id: str) -> WorkerParts:
    if not settings.database_worker_url:
        raise RuntimeError("DATABASE_WORKER_URL is not configured")
    url = settings.database_worker_url.get_secret_value()
    db = Database(url, pool_size=10)
    email = EmailDispatcher(
        db, SmtpEmailSender(settings.smtp_host, settings.smtp_port, settings.email_from)
    )
    wallet, billing = build_billing(settings, db)
    lifecycle = build_lifecycle(settings, wallet)
    listener = PgListener(url, (JOBS_CHANNEL, CANCEL_CHANNEL))
    runner = JobRunner(
        db,
        build_executor(settings, db),
        lifecycle,
        worker_id=worker_id,
        listener=listener,
        concurrency=settings.worker_concurrency,
    )
    sweeper = RecoverySweeper(db, lifecycle)

    async def jobs(stop: asyncio.Event) -> None:
        await listener.start()
        await runner.run_forever(stop)

    return WorkerParts(
        loops=[
            ("email", email.run_once, 2.0),
            ("payment_events", billing.process_events, 1.0),
            ("checkout_expiry", billing.expire_checkouts, 30.0),
            ("pending_hold_release", wallet.release_due_pending, 300.0),
            ("recovery_sweeper", sweeper.sweep, 15.0),
        ],
        daemons=[("jobs", jobs)],
        cleanup=[listener.stop, db.dispose],
    )
