"""Outbound email — the same Protocol + fake + config-selected-real-client
shape app/services/accounting_client.py and stripe_client.py established
for external services.

`SmtpEmailClient` uses the stdlib `smtplib` run via `asyncio.to_thread`
rather than adding an async-SMTP dependency: sending happens only inside
the Dramatiq worker (see app/tasks/send_invitation_email.py), where a
blocking call parked on a thread is fine, and one fewer pinned dependency
matters more than nonblocking SMTP throughput at this scale.

Client selection is config-driven, not environment-name-driven: an unset
`smtp_host` means the fake (which records instead of sending) — so local
dev, tests, and CI all get the fake with zero configuration, and setting
SMTP_HOST in the environment is the single switch that turns on real
delivery. The fake's `sent` list is module-level process state, cleared by
tests that inspect it (same in-process-fake convention as
FakeAccountingProviderClient's `_synced_keys`).
"""
import asyncio
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol

from app.config import settings


class EmailClient(Protocol):
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


@dataclass
class FakeEmailClient:
    sent: list[dict] = field(default_factory=list)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


class SmtpEmailClient:
    async def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = settings.smtp_from_address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        await asyncio.to_thread(self._send_sync, message)

    @staticmethod
    def _send_sync(message: EmailMessage) -> None:
        # get_email_client() only hands this client out when smtp_host is
        # set — this guard makes that invariant explicit (and narrows the
        # str | None for the type checker) rather than letting a violated
        # invariant surface as a confusing smtplib connection error.
        if settings.smtp_host is None:
            raise RuntimeError("SmtpEmailClient selected without SMTP_HOST configured")
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)


# Singleton fake so tests (and dev flows) can inspect what "went out"
# through the same instance get_email_client() hands to callers.
fake_email_client = FakeEmailClient()
_smtp_email_client = SmtpEmailClient()


def get_email_client() -> EmailClient:
    if settings.smtp_host:
        return _smtp_email_client
    return fake_email_client
