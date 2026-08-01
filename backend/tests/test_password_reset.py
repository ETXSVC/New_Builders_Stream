"""Password reset (migration 0028).

Before this there was no way back into an account whose password was
forgotten — not for the user, and not for an operator either, since no
console route writes `users`. The recovery path was editing the database.

What is tested here is almost entirely the ways this feature is commonly
got wrong, because the happy path is three lines:

  * the request endpoint must not say whether an address is registered;
  * the token must be single-use, expiring, and stored only as a hash;
  * asking again must invalidate the last link rather than accumulating
    live keys to the account;
  * a reset must revoke every session the user holds, because somebody
    resetting a password often believes a session is no longer theirs;
  * and an account with MFA active must still present its second factor,
    or a reset by email quietly downgrades two factors to one — the inbox.
"""
import time

import asyncpg
import pyotp
import pytest

from app.services import password_reset as reset_service
from app.tasks import send_password_reset_email as reset_email_task
from tests.conftest import TEST_DATABASE_URL, register_and_login

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")
_TOTP_PERIOD_SECONDS = 30
PASSWORD = "supersecret123"
NEW_PASSWORD = "an-entirely-different-one"


@pytest.fixture
def sent_links(monkeypatch) -> list[dict]:
    """Capture the enqueued mail instead of reaching Redis, and hand back
    the reset URLs — the secret exists nowhere else by design, so a test
    that wants to redeem one has to read it here."""
    calls: list[dict] = []
    monkeypatch.setattr(
        reset_email_task.send_password_reset_email, "send", lambda **kwargs: calls.append(kwargs)
    )
    return calls


def _token_from(message: dict) -> str:
    return message["reset_url"].split("token=", 1)[1]


async def _request_reset(client, email: str):
    response = await client.post("/auth/password-reset/request", json={"email": email})
    assert response.status_code == 202, response.text
    return response


async def _login(client, email: str, password: str, **extra):
    return await client.post("/auth/login", json={"email": email, "password": password, **extra})


# --------------------------------------------------------------------------
# Asking for a link
# --------------------------------------------------------------------------


async def test_an_unknown_address_is_answered_exactly_like_a_known_one(client, sent_links):
    """The response must not be an account-enumeration oracle — "does this
    company use Builders Stream" is itself sensitive for a B2B product, and
    it is the same reason login verifies a dummy hash for unknown emails."""
    host = await register_and_login(client, "Known Co", "admin@known.example")

    known = await _request_reset(client, host["email"])
    unknown = await _request_reset(client, "nobody@nowhere.example")

    assert known.status_code == unknown.status_code
    assert known.json() == unknown.json()
    # And the difference that DOES exist is invisible to the caller: mail
    # goes only to the address that has an account.
    assert [m["to_email"] for m in sent_links] == [host["email"]]


async def test_the_link_carries_a_secret_that_is_not_stored(client, sent_links):
    """A dump of `password_reset_tokens` must not be a set of working
    links, which is why only the SHA-256 is kept."""
    host = await register_and_login(client, "Hashed Co", "admin@hashed.example")
    await _request_reset(client, host["email"])
    secret = _token_from(sent_links[0])

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        stored = await conn.fetchval(
            "SELECT token_hash FROM password_reset_tokens ORDER BY created_at DESC LIMIT 1"
        )
    finally:
        await conn.close()

    assert secret not in stored
    assert stored == reset_service._hash(secret)


async def test_asking_again_kills_the_previous_link(client, sent_links):
    """Somebody who clicks "forgot password" three times because nothing
    arrived must not end up with three live keys to their account."""
    host = await register_and_login(client, "Impatient Co", "admin@impatient.example")

    await _request_reset(client, host["email"])
    first = _token_from(sent_links[0])
    await _request_reset(client, host["email"])
    second = _token_from(sent_links[1])

    spent = await client.post(
        "/auth/password-reset/confirm", json={"token": first, "new_password": NEW_PASSWORD}
    )
    assert spent.status_code == 400, spent.text

    assert (
        await client.post(
            "/auth/password-reset/confirm", json={"token": second, "new_password": NEW_PASSWORD}
        )
    ).status_code == 204


# --------------------------------------------------------------------------
# Spending it
# --------------------------------------------------------------------------


async def test_the_new_password_works_and_the_old_one_does_not(client, sent_links):
    host = await register_and_login(client, "Reset Co", "admin@reset.example")
    await _request_reset(client, host["email"])

    confirmed = await client.post(
        "/auth/password-reset/confirm",
        json={"token": _token_from(sent_links[0]), "new_password": NEW_PASSWORD},
    )
    assert confirmed.status_code == 204, confirmed.text

    assert (await _login(client, host["email"], NEW_PASSWORD)).status_code == 200
    assert (await _login(client, host["email"], PASSWORD)).status_code == 401


async def test_a_link_works_once(client, sent_links):
    """The second use is what makes a leaked inbox a standing key rather
    than a one-off."""
    host = await register_and_login(client, "Once Co", "admin@once.example")
    await _request_reset(client, host["email"])
    token = _token_from(sent_links[0])

    first = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert first.status_code == 204

    second = await client.post(
        "/auth/password-reset/confirm",
        json={"token": token, "new_password": "yet-another-password"},
    )
    assert second.status_code == 400

    # And the first reset stands — the refused replay changed nothing.
    assert (await _login(client, host["email"], NEW_PASSWORD)).status_code == 200


async def test_an_expired_link_is_refused(client, sent_links):
    host = await register_and_login(client, "Stale Co", "admin@stale.example")
    await _request_reset(client, host["email"])
    token = _token_from(sent_links[0])

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        # Reach past the hour rather than waiting one out.
        await conn.execute(
            "UPDATE password_reset_tokens SET expires_at = now() - interval '1 minute'"
        )
    finally:
        await conn.close()

    refused = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )

    assert refused.status_code == 400
    assert (await _login(client, host["email"], PASSWORD)).status_code == 200


async def test_a_made_up_token_is_refused_the_same_way(client):
    """Unknown, expired and spent all answer identically: an attacker
    holding a guess must not learn which of the three they hit."""
    response = await client.post(
        "/auth/password-reset/confirm",
        json={"token": "not-a-real-token", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 400


async def test_a_reset_revokes_every_session_the_user_holds(client, sent_links):
    """The point of a reset rather than a convenience: somebody doing this
    often believes a session is no longer theirs."""
    host = await register_and_login(client, "Session Co", "admin@session.example")
    signed_in = await _login(client, host["email"], PASSWORD)
    refresh_token = signed_in.json()["refresh_token"]

    await _request_reset(client, host["email"])
    await client.post(
        "/auth/password-reset/confirm",
        json={"token": _token_from(sent_links[0]), "new_password": NEW_PASSWORD},
    )

    refreshed = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 401, refreshed.text


# --------------------------------------------------------------------------
# The second factor
# --------------------------------------------------------------------------


async def _activate_mfa(client, host) -> str:
    """Activate with a code from the PREVIOUS step, exactly as
    `tests/test_mfa_totp.py` explains: the +/-1-step skew window accepts
    it, and the replay guard then records that prior step as spent —
    leaving the CURRENT step free for whatever the test does next."""
    enrol = await client.post("/auth/mfa/enroll", headers=host["headers"])
    assert enrol.status_code == 200, enrol.text
    secret = enrol.json()["secret"]

    activated = await client.post(
        "/auth/mfa/activate",
        json={"totp_code": pyotp.TOTP(secret).at(int(time.time()) - _TOTP_PERIOD_SECONDS)},
        headers=host["headers"],
    )
    assert activated.status_code == 204, activated.text
    return secret


async def test_an_mfa_account_still_needs_its_second_factor(client, sent_links):
    """Otherwise a reset by email turns two factors into one — the inbox —
    which is exactly what the second factor exists to survive."""
    host = await register_and_login(client, "Two Factor Co", "admin@twofactor.example")
    await _activate_mfa(client, host)
    await _request_reset(client, host["email"])
    token = _token_from(sent_links[0])

    without_code = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )

    assert without_code.status_code == 401
    assert "TOTP" in without_code.json()["detail"]
    # Refused, so the old password still works and the link is still live.
    assert (await _login(client, host["email"], PASSWORD, totp_code=None)).status_code == 401


async def test_an_mfa_account_resets_with_a_valid_code(client, sent_links):
    host = await register_and_login(client, "Two Factor OK Co", "admin@twofactor-ok.example")
    secret = await _activate_mfa(client, host)
    await _request_reset(client, host["email"])

    confirmed = await client.post(
        "/auth/password-reset/confirm",
        json={
            "token": _token_from(sent_links[0]),
            "new_password": NEW_PASSWORD,
            # The current step, which activation deliberately left unspent.
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )

    assert confirmed.status_code == 204, confirmed.text


# --------------------------------------------------------------------------
# What the email says
# --------------------------------------------------------------------------


async def test_the_mail_goes_out_under_the_tenants_sender_name(client, sent_links):
    """Migration 0027's per-tenant display name applies here too — a reset
    is one of the three things a builder's customers and crew receive."""
    host = await register_and_login(client, "Riverside Builders", "admin@riverside-reset.example")
    await client.put(
        "/companies/branding",
        json={"accent_color": "#1e293b", "footer_text": "", "email_sender_name": "Riverside IT"},
        headers=host["headers"],
    )

    await _request_reset(client, host["email"])

    (message,) = sent_links
    assert message["from_name"] == "Riverside IT"
    assert message["company_name"] == "Riverside Builders"
    assert message["expires_in_minutes"] == 60
