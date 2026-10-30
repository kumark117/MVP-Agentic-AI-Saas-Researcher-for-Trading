"""Delivers queued emails from email_outbox (worker; system scope).

The payload holds one-time links, so it is cleared once the message is sent. Failures are
retried with the attempt count recorded; after MAX_ATTEMPTS the row is left for operators.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from alloyiq.adapters.email import EmailSender, OutgoingEmail
from alloyiq.observability.logging import get_logger
from alloyiq.repositories.db import Database, DbContext

MAX_ATTEMPTS = 5
log = get_logger(__name__)

_FOOTER = "\n\nAlloyIQ - research only. Payments are simulated; no real money is collected."


def render(template: str, payload: dict[str, Any]) -> tuple[str, str]:
    if template == "verify_email":
        return (
            "Verify your AlloyIQ email",
            "Confirm your email address to finish creating your AlloyIQ account:\n\n"
            f"{payload['link']}\n\nThe link expires in 30 minutes and works once." + _FOOTER,
        )
    if template == "reset_password":
        return (
            "Reset your AlloyIQ password",
            "Someone asked to reset the password for this AlloyIQ account. If it was you, "
            f"open:\n\n{payload['link']}\n\nThe link expires in 30 minutes. Resetting signs "
            "out every device. If it was not you, ignore this email." + _FOOTER,
        )
    if template == "owner_digest":
        from alloyiq.application.owner_digest import render_digest

        return render_digest(payload)
    if template == "account_exists":
        return (
            "Your AlloyIQ account",
            "Someone tried to register with this email address, which already has an "
            f"AlloyIQ account. You can sign in at {payload['login']} or reset your password "
            "from the sign-in page." + _FOOTER,
        )
    raise ValueError(f"unknown email template {template!r}")


class EmailDispatcher:
    def __init__(self, db: Database, sender: EmailSender, batch: int = 20) -> None:
        self.db, self.sender, self.batch = db, sender, batch

    async def run_once(self) -> int:
        sent = 0
        async with self.db.transaction(DbContext.for_system()) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, to_email, template, payload FROM email_outbox"
                        " WHERE sent_at IS NULL AND attempts < :max ORDER BY created_at"
                        " LIMIT :n FOR UPDATE SKIP LOCKED"
                    ),
                    {"max": MAX_ATTEMPTS, "n": self.batch},
                )
            ).all()
            for row in rows:
                try:
                    subject, body = render(row.template, row.payload)
                    await self.sender.send(OutgoingEmail(row.to_email, subject, body))
                except Exception as exc:
                    log.warning(
                        "email_send_failed", email_id=str(row.id), error_type=type(exc).__name__
                    )
                    await conn.execute(
                        text(
                            "UPDATE email_outbox SET attempts = attempts + 1, last_error = :e"
                            " WHERE id = :i"
                        ),
                        {"i": row.id, "e": type(exc).__name__},
                    )
                    continue
                await conn.execute(
                    text(
                        "UPDATE email_outbox SET sent_at = now(), attempts = attempts + 1,"
                        " payload = '{}'::jsonb WHERE id = :i"
                    ),
                    {"i": row.id},
                )
                sent += 1
        return sent
