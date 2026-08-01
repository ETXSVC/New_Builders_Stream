"""A tenant's own mail server (migration 0029).

Migration 0027 let a company choose the NAME its mail goes out under and
deliberately left the address alone, because sending as `@thatbuilder.com`
needs that builder's DNS. This is the other half, and it brings two
hazards that the tests below are almost entirely about:

  1. **We store somebody else's mail password.** It must be encrypted at
     rest and must never come back out of a route — not in the GET, not in
     the audit log, not in an error.
  2. **We connect wherever the tenant says.** That is SSRF: a customer
     pointing `host` at 127.0.0.1 or a cloud metadata address would have
     the "test" button report back what it found.

The happy path — save a server, mail goes through it — is one test.
"""
import uuid

import asyncpg
import pytest

from app.services.tenant_smtp import (
    SmtpConfig,
    UnsafeMailHostError,
    load_for_company,
    resolve_and_check,
)
from app.services.token_encryption import decrypt_token
from tests.conftest import TEST_DATABASE_URL, register_and_login

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch):
    """Resolve names without touching real DNS.

    The guard refuses a host it cannot resolve, which is correct — an
    unroutable name is a setting that can never send — but it would make
    these tests depend on the network, and on `smtp.example.com` never
    existing. So: a literal IP resolves to itself (which is what keeps the
    refusal tests below honest), and any NAME resolves to a public address.
    The one test that needs a name pointing somewhere private patches this
    for itself.
    """
    import ipaddress

    from app.services import tenant_smtp

    def fake_addresses(host, port):
        # "localhost" is the case that matters here: it is a NAME, and it
        # really does resolve to loopback — stubbing it to a public address
        # would quietly disarm the test that says we refuse it.
        if host in ("localhost", "localhost."):
            return ["127.0.0.1"]
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return ["93.184.216.34"]  # a public unicast address

    # The module's own seam, NOT socket.getaddrinfo: patching that globally
    # re-points asyncpg's lookups too, and the database connection dies with
    # an error that says nothing about DNS.
    monkeypatch.setattr(tenant_smtp, "_addresses_for", fake_addresses)


_VALID = {
    "host": "smtp.example.com",
    "port": 587,
    "username": "mailer",
    "password": "s3cret",
    "from_address": "no-reply@riverside.example",
    "starttls": True,
    "enabled": True,
}


async def _put(client, admin, **overrides):
    """`admin`, not `host`: the settings themselves have a `host` field, and
    naming the caller's context the same thing made every override of it a
    TypeError."""
    return await client.put(
        "/companies/email-settings", json={**_VALID, **overrides}, headers=admin["headers"]
    )


# --------------------------------------------------------------------------
# The credential
# --------------------------------------------------------------------------


async def test_the_password_is_encrypted_at_rest(client):
    """A database dump must not be a set of working mail credentials — the
    same rule the integrations module's OAuth tokens follow, under the same
    key."""
    host = await register_and_login(client, "Riverside Builders", "admin@riverside-mail.example")
    assert (await _put(client, host)).status_code == 200

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        stored = await conn.fetchval(
            "SELECT password_encrypted FROM company_email_settings LIMIT 1"
        )
    finally:
        await conn.close()

    assert stored is not None
    assert "s3cret" not in stored
    assert decrypt_token(stored) == "s3cret"


async def test_the_password_never_comes_back_out(client):
    host = await register_and_login(client, "Quiet Co", "admin@quiet-mail.example")
    saved = await _put(client, host)

    read_back = await client.get("/companies/email-settings", headers=host["headers"])

    for body in (saved.json(), read_back.json()):
        assert "password" not in body
        assert "password_encrypted" not in body
        # What the form needs instead: whether one is stored at all.
        assert body["has_password"] is True


async def test_an_omitted_password_keeps_the_stored_one(client):
    """The form cannot show the current password, so "no password field
    submitted" has to mean "leave it alone" — otherwise editing the port
    would silently clear the credentials."""
    host = await register_and_login(client, "Keep Co", "admin@keep-mail.example")
    await _put(client, host)

    updated = await client.put(
        "/companies/email-settings",
        json={**_VALID, "port": 2525, "password": None},
        headers=host["headers"],
    )

    assert updated.status_code == 200
    assert updated.json()["port"] == 2525
    assert updated.json()["has_password"] is True


async def test_an_empty_password_removes_it(client):
    """The other half of that rule: a relay that needs no credentials is a
    real setup, so there has to be a way to say so."""
    host = await register_and_login(client, "Open Relay Co", "admin@openrelay.example")
    await _put(client, host)

    cleared = await client.put(
        "/companies/email-settings",
        json={**_VALID, "password": "", "username": None},
        headers=host["headers"],
    )

    assert cleared.json()["has_password"] is False


# --------------------------------------------------------------------------
# The destination
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host_value",
    [
        "127.0.0.1",  # our own worker
        "localhost",  # the same, by name
        "10.0.0.5",  # somewhere inside the network
        "169.254.169.254",  # cloud instance metadata
        "::1",
    ],
)
def test_private_destinations_are_refused(host_value):
    """A tenant naming a host is asking us to make an outbound connection
    on their behalf. Unguarded, the test button is a port scanner with a
    UI, and the metadata address is the one that hands over credentials."""
    with pytest.raises(UnsafeMailHostError):
        resolve_and_check(host_value, 587)


def test_a_name_that_resolves_privately_is_refused_too(monkeypatch):
    """The check is on the RESOLVED address, not the string: a hostname the
    tenant controls can point at 127.0.0.1 just as easily, and a DNS record
    can be changed after the settings were saved."""
    from app.services import tenant_smtp

    monkeypatch.setattr(tenant_smtp, "_addresses_for", lambda host, port: ["127.0.0.1"])

    with pytest.raises(UnsafeMailHostError):
        resolve_and_check("mail.looks-legitimate.example", 587)


async def test_saving_a_private_host_is_refused_by_the_route(client, monkeypatch):
    host = await register_and_login(client, "Sneaky Co", "admin@sneaky.example")

    refused = await _put(client, host, host="127.0.0.1")

    assert refused.status_code == 422
    assert "private" in refused.json()["detail"]


# --------------------------------------------------------------------------
# Which server a message goes through
# --------------------------------------------------------------------------


async def test_a_company_with_nothing_configured_uses_the_platform_relay(client, db_session):
    """The state almost every tenant is in, and the reason `load_for_company`
    returns None rather than raising."""
    host = await register_and_login(client, "Default Co", "admin@default-mail.example")

    assert await load_for_company(db_session, uuid.UUID(host["company_id"])) is None


async def test_turning_it_off_falls_back_without_losing_the_settings(client, db_session):
    """What somebody does while their provider is having an outage: mail
    continuing to flow from the wrong domain beats mail not flowing."""
    host = await register_and_login(client, "Paused Co", "admin@paused.example")
    await _put(client, host)
    await _put(client, host, enabled=False)

    assert await load_for_company(db_session, uuid.UUID(host["company_id"])) is None
    # Still there, still holding the password.
    assert (
        await client.get("/companies/email-settings", headers=host["headers"])
    ).json()["has_password"] is True


async def test_a_configured_company_resolves_to_its_own_server(client, db_session):
    host = await register_and_login(client, "Own Server Co", "admin@ownserver.example")
    await _put(client, host)

    config = await load_for_company(db_session, uuid.UUID(host["company_id"]))

    assert isinstance(config, SmtpConfig)
    assert (config.host, config.port, config.from_address) == (
        "smtp.example.com",
        587,
        "no-reply@riverside.example",
    )
    # Decrypted for use, in the worker, and nowhere else.
    assert config.password == "s3cret"


async def test_one_companys_mail_server_is_invisible_to_another(client):
    """`company_email_settings` is a tenant table like any other, and this
    is the row where getting that wrong hands over a password."""
    first = await register_and_login(client, "First Mail Co", "admin@first-mail.example")
    second = await register_and_login(client, "Second Mail Co", "admin@second-mail.example")
    await _put(client, first, host="smtp.first.example")

    seen_by_second = await client.get("/companies/email-settings", headers=second["headers"])

    assert seen_by_second.status_code == 200
    assert seen_by_second.json() is None


# --------------------------------------------------------------------------
# Roles, and the test button
# --------------------------------------------------------------------------


async def test_only_an_admin_touches_the_mail_server(client):
    """Narrower than branding's admin-write/PM-read: a project manager has
    no reason to read the username a mail provider issued."""
    host = await register_and_login(client, "Roles Mail Co", "admin@rolesmail.example")
    invite = await client.post(
        "/invitations",
        json={"email": "pm@rolesmail.example", "role": "project_manager"},
        headers=host["headers"],
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM", "password": "supersecret123"},
    )
    login = await client.post(
        "/auth/login", json={"email": "pm@rolesmail.example", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (
        await client.get("/companies/email-settings", headers=pm_headers)
    ).status_code == 403
    assert (
        await client.put("/companies/email-settings", json=_VALID, headers=pm_headers)
    ).status_code == 403


async def test_testing_without_a_configured_server_says_so(client):
    host = await register_and_login(client, "Untested Co", "admin@untested.example")

    response = await client.post("/companies/email-settings/test", headers=host["headers"])

    assert response.status_code == 400
    assert "No mail server" in response.json()["detail"]


async def test_a_failing_test_reports_the_reason_rather_than_erroring(client):
    """A refused login is a normal outcome of testing a configuration. The
    relay's own words are what the person fixing it needs, so this is a 200
    carrying ok=false rather than a 5xx."""
    host = await register_and_login(client, "Broken Mail Co", "admin@brokenmail.example")
    # A real public address that will not answer SMTP on this port — the
    # host check passes, the connection does not.
    await _put(client, host, host="example.com", port=2599)

    response = await client.post("/companies/email-settings/test", headers=host["headers"])

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert response.json()["detail"]


async def test_saving_clears_a_previous_verification(client):
    """A host that has changed has not been proved, and a screen that went
    on showing "verified" would be lying about the thing most worth
    knowing."""
    host = await register_and_login(client, "Verified Co", "admin@verified.example")
    await _put(client, host)

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute("UPDATE company_email_settings SET verified_at = now()")
    finally:
        await conn.close()

    resaved = await _put(client, host, host="smtp2.example.com")

    assert resaved.json()["verified_at"] is None


async def test_removing_the_settings_returns_the_company_to_the_platform_relay(client, db_session):
    host = await register_and_login(client, "Removed Co", "admin@removed.example")
    await _put(client, host)

    removed = await client.delete("/companies/email-settings", headers=host["headers"])

    assert removed.status_code == 204
    assert (await client.get("/companies/email-settings", headers=host["headers"])).json() is None
    assert await load_for_company(db_session, uuid.UUID(host["company_id"])) is None
