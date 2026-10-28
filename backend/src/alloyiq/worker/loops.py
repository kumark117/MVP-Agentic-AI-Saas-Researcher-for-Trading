"""Worker loop registry. Each phase adds its periodic tasks here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alloyiq.adapters.email import SmtpEmailSender
from alloyiq.adapters.payments import DummyPaymentProvider
from alloyiq.application.billing import BillingService
from alloyiq.application.email_dispatch import EmailDispatcher
from alloyiq.application.wallet import WalletService
from alloyiq.config import Settings
from alloyiq.repositories.db import Database
from alloyiq.security.ratelimit import InMemoryRateLimiter

Loop = tuple[str, Callable[[], Awaitable[object]], float]


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


def build_loops(
    settings: Settings, worker_id: str
) -> tuple[list[Loop], Callable[[], Awaitable[None]]]:
    if not settings.database_worker_url:
        raise RuntimeError("DATABASE_WORKER_URL is not configured")
    db = Database(settings.database_worker_url.get_secret_value(), pool_size=10)
    email = EmailDispatcher(
        db, SmtpEmailSender(settings.smtp_host, settings.smtp_port, settings.email_from)
    )
    wallet, billing = build_billing(settings, db)
    loops: list[Loop] = [
        ("email", email.run_once, 2.0),
        ("payment_events", billing.process_events, 1.0),
        ("checkout_expiry", billing.expire_checkouts, 30.0),
        ("pending_hold_release", wallet.release_due_pending, 300.0),
    ]
    return loops, db.dispose
