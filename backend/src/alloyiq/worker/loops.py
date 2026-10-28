"""Worker loop registry. Each phase adds its periodic tasks here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alloyiq.adapters.email import SmtpEmailSender
from alloyiq.application.email_dispatch import EmailDispatcher
from alloyiq.config import Settings
from alloyiq.repositories.db import Database

Loop = tuple[str, Callable[[], Awaitable[object]], float]


def build_loops(
    settings: Settings, worker_id: str
) -> tuple[list[Loop], Callable[[], Awaitable[None]]]:
    if not settings.database_worker_url:
        raise RuntimeError("DATABASE_WORKER_URL is not configured")
    db = Database(settings.database_worker_url.get_secret_value(), pool_size=10)
    email = EmailDispatcher(
        db, SmtpEmailSender(settings.smtp_host, settings.smtp_port, settings.email_from)
    )
    loops: list[Loop] = [("email", email.run_once, 2.0)]
    return loops, db.dispose
