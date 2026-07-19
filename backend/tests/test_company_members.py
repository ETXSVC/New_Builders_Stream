"""GET /companies/members — the assignee-picker data source (CRM+PM
frontend plan Task 10.5, a spec-gap fix: tasks have assignee_id but no
endpoint listed who could be assigned)."""
import asyncpg

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


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
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def _invite_and_login_as(client, admin, role, email):
    """Same pattern as test_projects.py's helper of the same name: also
    returns the invited user's id (queried directly — neither the
    invitation-accept nor the login response carries it), needed here to
    seed/assert `tasks.assignee_id` for the field_crew ownership tests."""
    invite = await client.post(
        "/invitations",
        json={"email": email, "role": role},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited User", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        user_id = await conn.fetchval("SELECT id FROM users WHERE email = $1", email)
    finally:
        await conn.close()

    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}, "user_id": str(user_id)}


async def test_members_lists_company_users_with_names_and_roles(client):
    admin = await _register_and_login(client, "Members Co", "members-admin@acme.test")
    await _invite_and_login_as(client, admin, "field_crew", "members-crew@acme.test")

    listed = await client.get("/companies/members", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    by_email = {m["email"]: m for m in items}
    assert by_email["members-admin@acme.test"]["role"] == "admin"
    assert by_email["members-crew@acme.test"]["role"] == "field_crew"
    assert by_email["members-crew@acme.test"]["full_name"] == "Invited User"
    assert "user_id" in by_email["members-crew@acme.test"]


async def test_members_denied_for_field_crew(client):
    admin = await _register_and_login(client, "Members Deny Co", "members-deny@acme.test")
    crew = await _invite_and_login_as(client, admin, "field_crew", "members-deny-crew@acme.test")
    response = await client.get("/companies/members", headers=crew["headers"])
    assert response.status_code == 403
