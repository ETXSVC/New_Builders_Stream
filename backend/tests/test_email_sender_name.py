"""The name outbound email goes out under, per tenant (migration 0027).

Every message this platform sends left under one bare address, so a
subcontractor with three builders on the system got three notices that all
looked identical until they read the subject line. The display name is set
per company, beside the logo and the PDF footer, because it is the same
decision: what this company looks like to somebody outside it.

Two properties carry the feature, and both are here:

  * **Empty means the company's own name.** Not "no name" — every tenant
    gets a sensible sender the moment this ships, without anyone typing
    one, which is the only reason it was safe to add a column rather than a
    settings page nobody would visit.
  * **It is resolved per tenant, at enqueue time.** A sweep that runs
    across every company in one pass must not send Riverside's mail under
    Ashgrove's name.
"""
import uuid

import pytest

from app.services import email as email_service
from app.services.email import FakeEmailClient, SmtpEmailClient
from app.tasks.send_invitation_email import _send_invitation_email, send_invitation_email
from tests.conftest import register_and_login


def _capture_enqueues(monkeypatch) -> list[dict]:
    """Record what the route hands Dramatiq, instead of reaching Redis.

    The same monkeypatch-the-actor's-send pattern
    `tests/test_send_invitation_email.py` uses — the question here is what
    the ROUTE resolved, and the actor's own forwarding is asserted
    separately below.
    """
    calls: list[dict] = []
    monkeypatch.setattr(send_invitation_email, "send", lambda **kwargs: calls.append(kwargs))
    return calls


async def _branding(client, host, **fields):
    response = await client.put(
        "/companies/branding",
        json={"accent_color": "#1e293b", "footer_text": "", **fields},
        headers=host["headers"],
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# The setting itself
# --------------------------------------------------------------------------


async def test_the_sender_name_round_trips_with_the_rest_of_the_branding(client):
    host = await register_and_login(client, "Riverside Builders", "admin@riverside.example")

    saved = await _branding(client, host, email_sender_name="Riverside Scheduling")
    assert saved["email_sender_name"] == "Riverside Scheduling"

    read_back = await client.get("/companies/branding", headers=host["headers"])
    assert read_back.json()["email_sender_name"] == "Riverside Scheduling"


async def test_a_name_of_spaces_is_stored_as_unset(client):
    """Otherwise every address this tenant sends from carries a quoted run
    of whitespace, which looks like a broken mail client rather than like
    nothing having been set."""
    host = await register_and_login(client, "Spacey Co", "admin@spacey.example")

    saved = await _branding(client, host, email_sender_name="   ")

    assert saved["email_sender_name"] == ""


async def test_the_sender_name_is_returned_as_stored_not_pre_resolved(client):
    """A screen showing "defaults to Ashgrove Homes" needs to tell "not set"
    from "set to exactly that", so the API does not helpfully fill it in."""
    host = await register_and_login(client, "Ashgrove Homes", "admin@ashgrove.example")

    branding = await client.get("/companies/branding", headers=host["headers"])

    assert branding.json()["email_sender_name"] == ""


# --------------------------------------------------------------------------
# What actually goes out
# --------------------------------------------------------------------------


async def test_an_invitation_goes_out_under_the_configured_name(client, monkeypatch):
    host = await register_and_login(client, "Riverside Builders", "admin@riverside2.example")
    await _branding(client, host, email_sender_name="Riverside Scheduling")
    calls = _capture_enqueues(monkeypatch)

    invited = await client.post(
        "/invitations",
        json={"email": "carpenter@example.com", "role": "field_crew"},
        headers=host["headers"],
    )
    assert invited.status_code == 201, invited.text

    (kwargs,) = calls
    assert kwargs["from_name"] == "Riverside Scheduling"


async def test_an_invitation_falls_back_to_the_company_name(client, monkeypatch):
    """The state every company is in until somebody edits their branding —
    which is to say, the common case."""
    host = await register_and_login(client, "Ashgrove Homes", "admin@ashgrove2.example")
    calls = _capture_enqueues(monkeypatch)

    await client.post(
        "/invitations",
        json={"email": "sparks@example.com", "role": "field_crew"},
        headers=host["headers"],
    )

    (kwargs,) = calls
    assert kwargs["from_name"] == "Ashgrove Homes"


async def test_one_tenants_sender_name_never_reaches_anothers_mail(client, monkeypatch):
    """The property a cross-tenant sweep could break silently: both
    companies' mail is enqueued from one process, and the name is looked up
    per company rather than once."""
    first = await register_and_login(client, "Riverside Builders", "admin@riverside3.example")
    second = await register_and_login(client, "Ashgrove Homes", "admin@ashgrove3.example")
    await _branding(client, first, email_sender_name="Riverside Scheduling")
    await _branding(client, second, email_sender_name="Ashgrove Site Office")
    calls = _capture_enqueues(monkeypatch)

    for host, address in ((first, "a@example.com"), (second, "b@example.com")):
        await client.post(
            "/invitations",
            json={"email": address, "role": "field_crew"},
            headers=host["headers"],
        )

    assert {c["to_email"]: c["from_name"] for c in calls} == {
        "a@example.com": "Riverside Scheduling",
        "b@example.com": "Ashgrove Site Office",
    }


async def test_the_actor_hands_the_name_to_the_email_client(monkeypatch):
    """The other half of the same journey: the enqueued kwarg reaches the
    client's `from_name`, which is what builds the From header."""
    fake = FakeEmailClient()
    monkeypatch.setattr(email_service, "get_email_client", lambda: fake)

    await _send_invitation_email(
        to_email="crew@example.com",
        company_name="Riverside Builders",
        role="field_crew",
        accept_url="http://localhost:3000/accept-invitation?id=abc",
        from_name="Riverside Scheduling",
    )

    (sent,) = fake.sent
    assert sent["from_name"] == "Riverside Scheduling"


# --------------------------------------------------------------------------
# The header it produces
# --------------------------------------------------------------------------


class _RecordingSMTP:
    """Captures the message instead of sending it — see
    tests/test_smtp_email_client.py, which owns the fuller version of this."""

    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, message):
        _RecordingSMTP.sent.append(message)


@pytest.fixture
def recording_smtp(monkeypatch):
    import smtplib

    _RecordingSMTP.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _RecordingSMTP)
    monkeypatch.setattr(email_service.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_service.settings, "smtp_starttls", False)
    monkeypatch.setattr(email_service.settings, "smtp_username", None)
    monkeypatch.setattr(email_service.settings, "smtp_from_address", "no-reply@platform.example")
    return _RecordingSMTP


async def test_the_from_header_pairs_the_name_with_the_platform_address(recording_smtp):
    await SmtpEmailClient().send(
        to="a@example.com", subject="s", body="b", from_name="Riverside Builders"
    )

    (message,) = recording_smtp.sent
    assert message["From"] == "Riverside Builders <no-reply@platform.example>"


async def test_a_name_with_a_comma_cannot_split_the_from_header(recording_smtp):
    """An f-string here would produce `Bergstrom, Ltd <a@b>` — two addresses
    as far as a parser is concerned, and a header some relays reject
    outright. `formataddr` quotes it."""
    await SmtpEmailClient().send(
        to="a@example.com", subject="s", body="b", from_name="Bergstrom, Ltd"
    )

    (message,) = recording_smtp.sent
    assert message["From"] == '"Bergstrom, Ltd" <no-reply@platform.example>'


async def test_a_non_ascii_name_survives_to_the_wire(recording_smtp):
    """Read back through `EmailMessage`, the header is the name as typed —
    that is the header registry decoding it. What actually crosses the
    network is the RFC 2047 encoding, which is the half worth asserting,
    because a raw non-ASCII byte in a header is what gets a message
    rejected."""
    await SmtpEmailClient().send(
        to="a@example.com", subject="s", body="b", from_name="Bergström & Co"
    )

    (message,) = recording_smtp.sent
    assert message["From"] == "Bergström & Co <no-reply@platform.example>"

    on_the_wire = next(
        line for line in message.as_string().splitlines() if line.startswith("From:")
    )
    assert "=?utf-8?" in on_the_wire
    assert on_the_wire.endswith("<no-reply@platform.example>")


async def test_no_name_still_sends_the_bare_address(recording_smtp):
    """Every caller before migration 0027 sent no name, including any
    Dramatiq message enqueued by the previous release and still in Redis."""
    await SmtpEmailClient().send(to="a@example.com", subject="s", body="b")

    (message,) = recording_smtp.sent
    assert message["From"] == "no-reply@platform.example"


# --------------------------------------------------------------------------
# The service the four enqueue sites share
# --------------------------------------------------------------------------


async def test_resolution_falls_back_when_a_company_has_no_branding_row(db_session):
    """The row is created on first edit, so "no row at all" is the state a
    company registers in — and the sweep must not crash on it."""
    from app.services.email_sender import sender_name_for

    assert await sender_name_for(db_session, uuid.uuid4(), "Some Company") == "Some Company"
