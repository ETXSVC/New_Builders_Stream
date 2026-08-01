"""The SMTP client itself, which nothing exercised.

`tests/test_send_invitation_email.py` covers the actor and the enqueue, both
against `FakeEmailClient`. `tests/test_config_validation.py` covers the
STARTTLS rule. Between them sat `SmtpEmailClient`, the one piece of this
path that ONLY ever runs in production — the least exercised code in the
repo and the most expensive to get wrong, because the failure arrives as a
customer saying they never got their invitation.

There is no SMTP server here and there does not need to be: the questions
worth asking are about the conversation this client has, and a stand-in
`smtplib.SMTP` records it exactly. What it must get right is a short list,
and every item on it is a real way mail breaks or leaks:

  * the message carries From/To/Subject and the body, as a plain-text part;
  * STARTTLS is offered before anything else, or the login below crosses
    the network in the clear;
  * it logs in only when it actually has credentials — an unauthenticated
    relay is a valid, common self-hosted setup, and calling `login()` with
    empty strings turns it into an error;
  * the session is closed even when the send raises;
  * being handed out with no host configured is a loud failure rather than
    a confusing connection error.
"""
import smtplib
from email.message import EmailMessage

import pytest

from app.services import email as email_service
from app.services.email import SmtpEmailClient


class FakeSMTP:
    """Records the conversation, in order, instead of having it."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.credentials: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []
        self.closed = False
        self.raise_on_send: Exception | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append("login")
        self.credentials = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send_message")
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.messages.append(message)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _configure(monkeypatch, **overrides) -> None:
    """Point the module's `settings` at an SMTP setup.

    Patched attribute by attribute rather than by rebuilding `Settings`,
    because `app.services.email` reads `settings` at call time and a fresh
    instance would not be the one it reads.
    """
    defaults = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": None,
        "smtp_password": None,
        "smtp_from_address": "no-reply@builders.example",
        "smtp_starttls": True,
    }
    for name, value in {**defaults, **overrides}.items():
        monkeypatch.setattr(email_service.settings, name, value)


async def test_the_message_carries_what_the_recipient_needs(smtp, monkeypatch):
    _configure(monkeypatch)

    await SmtpEmailClient().send(
        to="invitee@example.com", subject="You're invited", body="Follow this link."
    )

    (session,) = smtp.instances
    assert (session.host, session.port) == ("smtp.example.com", 587)
    # A send that hangs must not hold a worker thread forever.
    assert session.timeout == 30

    (message,) = session.messages
    assert message["From"] == "no-reply@builders.example"
    assert message["To"] == "invitee@example.com"
    assert message["Subject"] == "You're invited"
    assert message.get_content().strip() == "Follow this link."
    assert message.get_content_type() == "text/plain"


async def test_starttls_comes_before_the_password(smtp, monkeypatch):
    """Order, not presence. STARTTLS after LOGIN would be the same set of
    calls and would still put the mail password on the wire in the clear."""
    _configure(monkeypatch, smtp_username="mailer", smtp_password="s3cret")

    await SmtpEmailClient().send(to="a@example.com", subject="s", body="b")

    (session,) = smtp.instances
    assert session.calls == ["starttls", "login", "send_message"]
    assert session.credentials == ("mailer", "s3cret")


async def test_an_unauthenticated_relay_is_never_asked_to_log_in(smtp, monkeypatch):
    """The common self-hosted setup: a relay that accepts mail from inside
    the network. `login()` with no credentials is an error, not a no-op."""
    _configure(monkeypatch, smtp_username=None, smtp_password=None)

    await SmtpEmailClient().send(to="a@example.com", subject="s", body="b")

    (session,) = smtp.instances
    assert "login" not in session.calls


async def test_starttls_is_skipped_when_it_is_turned_off(smtp, monkeypatch):
    _configure(monkeypatch, smtp_starttls=False)

    await SmtpEmailClient().send(to="a@example.com", subject="s", body="b")

    (session,) = smtp.instances
    assert session.calls == ["send_message"]


async def test_a_failed_send_still_closes_the_session(smtp, monkeypatch):
    """Dramatiq retries this actor three times. Three leaked connections per
    failing message is how a mail server starts refusing the next one."""
    _configure(monkeypatch)

    client = SmtpEmailClient()
    # Arm the failure on the instance the client is about to create.
    original_init = FakeSMTP.__init__

    def failing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.raise_on_send = smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr(FakeSMTP, "__init__", failing_init)

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        await client.send(to="a@example.com", subject="s", body="b")

    (session,) = smtp.instances
    assert session.closed is True


async def test_being_selected_without_a_host_says_so(smtp, monkeypatch):
    """Unreachable through `get_email_client()`, which is the point: the
    invariant is asserted rather than left to surface as a connection error
    against the string "None"."""
    _configure(monkeypatch, smtp_host=None)

    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        await SmtpEmailClient().send(to="a@example.com", subject="s", body="b")

    assert smtp.instances == []
