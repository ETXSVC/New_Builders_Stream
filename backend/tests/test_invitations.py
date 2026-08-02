from datetime import datetime, timedelta, timezone
from tests.conftest import register_and_login





async def test_admin_can_invite_a_user(client):
    admin = await register_and_login(client, "Acme Construction", "admin@acme.test")

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
    admin = await register_and_login(client, "Acme Construction", "admin2@acme.test")

    response = await client.post(
        "/invitations",
        json={"email": "newhire@acme.test", "role": "not_a_real_role"},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_accept_invitation_creates_user_and_membership(client):
    admin = await register_and_login(client, "Acme Construction", "admin3@acme.test")

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
    admin = await register_and_login(client, "Acme Construction", "admin4@acme.test")

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


async def test_accept_already_accepted_invitation_is_rejected(client):
    admin = await register_and_login(client, "Acme Construction", "admin5@acme.test")

    invite = await client.post(
        "/invitations",
        json={"email": "twice@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )
    invitation_id = invite.json()["id"]

    first = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "First Accept", "password": "anothersecret123"},
    )
    assert first.status_code == 200

    second = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "Second Accept", "password": "anothersecret123"},
    )
    assert second.status_code == 409


async def _invite(client, headers, email, role="field_crew") -> str:
    response = await client.post(
        "/invitations", json={"email": email, "role": role}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_accepting_for_an_existing_address_without_a_session_cannot_take_it_over(client):
    """THE security property of multi-company membership (migration 0031).

    Whoever holds the invitation link controls the accept payload, and the
    invited ADDRESS is chosen by whoever sent the invitation. So if this
    route honoured a supplied password for an address that already has an
    account, anyone could invite victim@example.com, click their own link,
    and take over that account along with every company it already belongs
    to. This asserts the refusal AND â€” the half that actually matters â€”
    that the existing password still works afterwards.

    Replaces an older test that asserted 409 "Email already registered".
    The refusal is the same; the reason is now specific, and the account is
    now provably untouched rather than merely not created twice.
    """
    admin = await register_and_login(client, "Acme Construction", "admin6@acme.test")
    victim = await register_and_login(client, "Victim Builders", "victim@acme.test")

    invitation_id = await _invite(client, admin["headers"], victim["email"])
    attempt = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "Not Them", "password": "attacker-chosen-password"},
    )
    assert attempt.status_code == 401, attempt.text

    # The account is untouched: the original password still works, and the
    # attacker's chosen one does not.
    still_theirs = await client.post(
        "/auth/login", json={"email": victim["email"], "password": "supersecret123"}
    )
    assert still_theirs.status_code == 200
    hijacked = await client.post(
        "/auth/login",
        json={"email": victim["email"], "password": "attacker-chosen-password"},
    )
    assert hijacked.status_code == 401


async def test_accepting_with_someone_elses_session_is_refused(client):
    """Holding *a* valid session is not the same as holding *this account's*
    session."""
    admin = await register_and_login(client, "Acme Construction", "admin6b@acme.test")
    victim = await register_and_login(client, "Victim Builders", "victim-b@acme.test")
    bystander = await register_and_login(client, "Other Co", "bystander@acme.test")

    invitation_id = await _invite(client, admin["headers"], victim["email"])
    attempt = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "Not Them", "password": "irrelevant"},
        headers=bystander["headers"],
    )
    assert attempt.status_code == 401, attempt.text


async def test_an_authenticated_existing_user_joins_a_second_company(client):
    """The whole point of migration 0031: before it, this returned 409 and
    no user could ever belong to two companies."""
    admin = await register_and_login(client, "Acme Construction", "admin6c@acme.test")
    joiner = await register_and_login(client, "Joiner Builders", "joiner@acme.test")

    invitation_id = await _invite(client, admin["headers"], joiner["email"], role="project_manager")
    accepted = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": None, "password": None},
        headers=joiner["headers"],
    )
    assert accepted.status_code == 200, accepted.text

    memberships = await client.get("/companies/memberships", headers=joiner["headers"])
    assert memberships.status_code == 200, memberships.text
    by_company = {m["company_id"]: m for m in memberships.json()["memberships"]}
    assert set(by_company) == {joiner["company_id"], admin["company_id"]}
    # The role comes from the invitation, not from their other membership.
    assert by_company[admin["company_id"]]["role"] == "project_manager"
    assert by_company[joiner["company_id"]]["role"] == "admin"


async def test_the_supplied_password_is_ignored_when_the_account_exists(client):
    """Even from the rightful owner. The invitation grants access TO the
    inviting company; it is not an occasion to rewrite the invitee's own
    credentials, and treating it as one is the takeover primitive above
    wearing a friendlier hat."""
    admin = await register_and_login(client, "Acme Construction", "admin6d@acme.test")
    joiner = await register_and_login(client, "Joiner Two", "joiner2@acme.test")

    invitation_id = await _invite(client, admin["headers"], joiner["email"])
    accepted = await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "New Name", "password": "a-brand-new-password"},
        headers=joiner["headers"],
    )
    assert accepted.status_code == 200

    changed = await client.post(
        "/auth/login", json={"email": joiner["email"], "password": "a-brand-new-password"}
    )
    assert changed.status_code == 401, "the accept payload must not set a password"
    unchanged = await client.post(
        "/auth/login", json={"email": joiner["email"], "password": "supersecret123"}
    )
    assert unchanged.status_code == 200


async def test_accepting_a_second_invitation_to_a_company_already_joined_conflicts(client):
    admin = await register_and_login(client, "Acme Construction", "admin6e@acme.test")
    joiner = await register_and_login(client, "Joiner Three", "joiner3@acme.test")

    first = await _invite(client, admin["headers"], joiner["email"])
    assert (
        await client.post(
            f"/invitations/{first}/accept",
            json={"full_name": None, "password": None},
            headers=joiner["headers"],
        )
    ).status_code == 200

    second = await _invite(client, admin["headers"], joiner["email"], role="accountant")
    duplicate = await client.post(
        f"/invitations/{second}/accept",
        json={"full_name": None, "password": None},
        headers=joiner["headers"],
    )
    assert duplicate.status_code == 409, duplicate.text


async def test_non_admin_cannot_create_invitations(client):
    """Task 14 is the only route that ever creates a non-admin membership
    (test_deps.py's test_require_role_blocks_non_admin_role has to insert one
    directly via SQL for exactly this reason) â€” so this is the first place a
    non-admin actually exists to exercise require_role("admin") end-to-end."""
    admin = await register_and_login(client, "Acme Construction", "admin7@acme.test")

    invite = await client.post(
        "/invitations",
        json={"email": "fieldcrew@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Field Crew", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post(
        "/auth/login", json={"email": "fieldcrew@acme.test", "password": "anothersecret123"}
    )
    non_admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.post(
        "/invitations",
        json={"email": "another@acme.test", "role": "field_crew"},
        headers=non_admin_headers,
    )
    assert response.status_code == 403

