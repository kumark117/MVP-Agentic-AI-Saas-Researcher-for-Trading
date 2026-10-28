"""EmailSender interface, SMTP implementation (Mailpit in dev) and a recording fake."""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: OutgoingEmail) -> None: ...


class SmtpEmailSender:
    def __init__(self, host: str, port: int, sender: str, timeout_s: float = 10.0) -> None:
        self.host, self.port, self.sender, self.timeout_s = host, port, sender, timeout_s

    def _send_blocking(self, message: OutgoingEmail) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = message.to
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
            smtp.send_message(msg)

    async def send(self, message: OutgoingEmail) -> None:
        await asyncio.to_thread(self._send_blocking, message)


@dataclass
class FakeEmailSender:
    sent: list[OutgoingEmail] = field(default_factory=list)
    fail: bool = False

    async def send(self, message: OutgoingEmail) -> None:
        if self.fail:
            raise ConnectionError("fake SMTP failure")
        self.sent.append(message)
