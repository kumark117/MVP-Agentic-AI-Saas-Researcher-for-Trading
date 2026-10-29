"""EmailSender interface, SMTP implementation (Mailpit in dev) and a recording fake."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
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
    """Plain SMTP for Mailpit; STARTTLS plus login for hosted SMTP such as Gmail."""

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        timeout_s: float = 10.0,
        *,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
    ) -> None:
        self.host, self.port, self.sender, self.timeout_s = host, port, sender, timeout_s
        self.username, self.password, self.starttls = username, password, starttls

    def _send_blocking(self, message: OutgoingEmail) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = message.to
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
            if self.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username and self.password:
                smtp.login(self.username, self.password)
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
