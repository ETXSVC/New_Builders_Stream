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
from email.utils import formataddr
from typing import Protocol

from app.config import settings
from app.services.tenant_smtp import SmtpConfig, resolve_and_check


class EmailClient(Protocol):
    async def send(
        self, *, to: str, subject: str, body: str, from_name: str | None = None
    ) -> None: ...


@dataclass
class FakeEmailClient:
    sent: list[dict] = field(default_factory=list)

    async def send(
        self, *, to: str, subject: str, body: str, from_name: str | None = None
    ) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body, "from_name": from_name})


class SmtpEmailClient:
    """Sends through one SMTP endpoint.

    Takes an explicit `SmtpConfig` since migration 0029, defaulting to the
    platform's own settings when given none. A tenant's mail server and the
    platform relay are then the same code path with different values,
    rather than one being a special case bolted onto the other — and the
    tenant's credentials never have to reach module-level state to be used.
    """

    def __init__(self, config: SmtpConfig | None = None) -> None:
        self._config = config

    def _resolved(self) -> SmtpConfig:
        if self._config is not None:
            return self._config
        # get_email_client() only hands out the platform client when
        # smtp_host is set — this guard makes that invariant explicit (and
        # narrows the str | None for the type checker) rather than letting a
        # violated invariant surface as a confusing smtplib connection error.
        if settings.smtp_host is None:
            raise RuntimeError("SmtpEmailClient selected without SMTP_HOST configured")
        return SmtpConfig(
            host=settings.smtp_host,
            port=settings.smtp_port,
            from_address=settings.smtp_from_address,
            starttls=settings.smtp_starttls,
            username=settings.smtp_username,
            password=settings.smtp_password,
            operator_configured=True,
        )

    async def send(
        self, *, to: str, subject: str, body: str, from_name: str | None = None
    ) -> None:
        config = self._resolved()
        message = EmailMessage()
        # `formataddr`, not an f-string: it quotes a name containing a comma
        # or a quote (which would otherwise split the header into two
        # addresses) and RFC 2047-encodes anything non-ASCII, so a company
        # called "Bergström & Co, Ltd" arrives intact instead of as a
        # malformed From that some relays reject outright.
        #
        # `from_name=None` keeps the bare address, which is what every
        # caller sent before this parameter existed — including any Dramatiq
        # message enqueued by the previous release and still in Redis.
        message["From"] = (
            formataddr((from_name, config.from_address)) if from_name else config.from_address
        )
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        await asyncio.to_thread(self._send_sync, config, message)

    @staticmethod
    def _send_sync(config: SmtpConfig, message: EmailMessage) -> None:
        # Re-checked here, not only where the settings were saved: a
        # hostname the tenant controls can be re-pointed at 127.0.0.1 after
        # it passed validation, and this is the moment we would actually
        # connect. Skipped for the operator's own relay, which may
        # legitimately be a private-network mail gateway — see
        # app/services/tenant_smtp.py for the whole argument.
        if not config.operator_configured:
            resolve_and_check(config.host, config.port)
        with smtplib.SMTP(config.host, config.port, timeout=30) as smtp:
            if config.starttls:
                smtp.starttls()
            if config.username and config.password:
                smtp.login(config.username, config.password)
            smtp.send_message(message)


# Singleton fake so tests (and dev flows) can inspect what "went out"
# through the same instance get_email_client() hands to callers.
fake_email_client = FakeEmailClient()
_smtp_email_client = SmtpEmailClient()


def get_email_client() -> EmailClient:
    if settings.smtp_host:
        return _smtp_email_client
    return fake_email_client
