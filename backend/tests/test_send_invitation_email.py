"""Invitation email delivery: the actor's own behavior (against the fake
email client) and the create-invitation route's enqueue, following the
same monkeypatch-the-actor's-send pattern test_financial_record_sync_
handler-adjacent tests use for Dramatiq enqueues.
"""
from app.services.email import FakeEmailClient, fake_email_client, get_email_client
from app.tasks.send_invitation_email import _send_invitation_email, send_invitation_email


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


def test_unconfigured_smtp_selects_the_recording_fake():
    # smtp_host is unset in the test environment — the selector must hand
    # back the fake, never attempt a real connection.
    assert get_email_client() is fake_email_client


async def test_actor_sends_via_the_email_client(monkeypatch):
    import app.services.email as email_service

    fake = FakeEmailClient()
    monkeypatch.setattr(email_service, "get_email_client", lambda: fake)

    await _send_invitation_email(
        to_email="crew@acme.test",
        company_name="Acme Builders",
        role="field_crew",
        accept_url="http://localhost:3000/accept-invitation?id=abc123",
    )

    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["to"] == "crew@acme.test"
    assert "Acme Builders" in sent["subject"]
    assert "http://localhost:3000/accept-invitation?id=abc123" in sent["body"]
    assert "field crew" in sent["body"]  # role rendered human-readably


async def test_create_invitation_enqueues_the_email(client, monkeypatch):
    admin = await _register_and_login(client, "Email Invite Co", "email-invite-admin@acme.test")

    calls = []
    monkeypatch.setattr(send_invitation_email, "send", lambda *a, **kw: calls.append((a, kw)))

    invite = await client.post(
        "/invitations",
        json={"email": "invitee@acme.test", "role": "project_manager"},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["to_email"] == "invitee@acme.test"
    assert kwargs["role"] == "project_manager"
    assert kwargs["company_name"] == "Email Invite Co"
    # The link targets the pre-auth accept page with this invitation's id.
    assert kwargs["accept_url"].endswith(f"/accept-invitation?id={invite.json()['id']}")
