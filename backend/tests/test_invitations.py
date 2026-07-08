from datetime import datetime, timedelta, timezone


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
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def test_admin_can_invite_a_user(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")

    response = await client.post(
        "/invitations",
        json={"email": "newhire@acme.test", "role": "project_manager"},
        headers=admin["headers"],
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "newhire@acme.test"
    assert body["role"] == "project_manager"
    assert body["accepted_at"] is None


async def test_invitation_rejects_invalid_role(client):
    admin = await _register_and_login(client, "Acme Construction", "admin2@acme.test")

    response = await client.post(
        "/invitations",
        json={"email": "newhire@acme.test", "role": "not_a_real_role"},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_accept_invitation_creates_user_and_membership(client):
    admin = await _register_and_login(client, "Acme Construction", "admin3@acme.test")

    invite = await client.post(
        "/invitations",
        json={"email": "newhire3@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )
    invitation_id = invite.json()["id"]

    accept = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "New Hire", "password": "anothersecret123"},
    )
    assert accept.status_code == 200

    login = await client.post("/auth/login", json={"email": "newhire3@acme.test", "password": "anothersecret123"})
    assert login.status_code == 200
    assert login.json()["default_company_id"] == admin["company_id"]


async def test_accept_expired_invitation_is_rejected(client, monkeypatch):
    admin = await _register_and_login(client, "Acme Construction", "admin4@acme.test")

    invite = await client.post(
        "/invitations",
        json={"email": "toolate@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )
    invitation_id = invite.json()["id"]

    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        await conn.execute(
            "UPDATE invitations SET expires_at = $1 WHERE id = $2",
            datetime.now(timezone.utc) - timedelta(days=1),
            invitation_id,
        )
    finally:
        await conn.close()

    accept = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "Too Late", "password": "anothersecret123"},
    )
    assert accept.status_code == 410
