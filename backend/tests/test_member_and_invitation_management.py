"""The management routes that did not exist: member offboarding, role
changes, company rename, and invitation list/revoke.

Before these, `GET /companies/members` was the only membership route in the
API — an admin could invite someone and then had no way to change their
role, remove them, or even see what invitations were outstanding. Removing
a departing employee's access meant editing the database by hand.

Two properties get the most attention here because they are the ones that
turn a convenience feature into a footgun:

  * **a company can never be left without an admin.** Every administrative
    route is `require_role("admin")`, so zero admins means the company
    cannot invite anyone, fix its own subscription, or restore an admin —
    permanently locked out with no in-product recovery.
  * **revoking an invitation actually withdraws the grant.** The invitation
    id IS the credential `POST /invitations/{id}/accept` checks, so one
    sent to a wrong address is a live grant to a stranger's mailbox until
    the row is gone.
"""
import uuid

from tests.conftest import set_subscription_tier


async def _register(client, company_name, email):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert response.status_code == 201, response.text
    await set_subscription_tier(response.json()["company_id"], "enterprise")
    login = await client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    )
    return {
        "company_id": response.json()["company_id"],
        "user_id": response.json()["user_id"],
        "email": email,
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _invite(client, admin, role, email, *, accept=True):
    invite = await client.post(
        "/invitations", json={"email": email, "role": role}, headers=admin["headers"]
    )
    assert invite.status_code == 201, invite.text
    if not accept:
        return {"invitation_id": invite.json()["id"], "email": email}

    accepted = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited User", "password": "anothersecret123"},
    )
    assert accepted.status_code == 200, accepted.text
    login = await client.post(
        "/auth/login", json={"email": email, "password": "anothersecret123"}
    )
    members = await client.get("/companies/members", headers=admin["headers"])
    user_id = next(m["user_id"] for m in members.json()["items"] if m["email"] == email)
    return {
        "invitation_id": invite.json()["id"],
        "user_id": user_id,
        "email": email,
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


# =============================================================================
# Role changes
# =============================================================================


async def test_admin_can_change_a_members_role(client):
    admin = await _register(client, "Role Co", "role-admin@acme.test")
    member = await _invite(client, admin, "field_crew", "role-member@acme.test")

    response = await client.patch(
        f"/companies/members/{member['user_id']}",
        json={"role": "project_manager"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "project_manager"

    members = await client.get("/companies/members", headers=admin["headers"])
    roles = {m["email"]: m["role"] for m in members.json()["items"]}
    assert roles[member["email"]] == "project_manager"


async def test_role_change_takes_effect_on_the_next_request(client):
    """Not a formality: the point of a role change is what the member can
    then do, and role is resolved per-request from company_users rather than
    baked into the token."""
    admin = await _register(client, "Role Effect Co", "role-effect-admin@acme.test")
    member = await _invite(client, admin, "field_crew", "role-effect@acme.test")

    denied = await client.get("/estimates", headers=member["headers"])
    assert denied.status_code == 403, denied.text

    await client.patch(
        f"/companies/members/{member['user_id']}",
        json={"role": "accountant"},
        headers=admin["headers"],
    )

    allowed = await client.get("/estimates", headers=member["headers"])
    assert allowed.status_code == 200, allowed.text


async def test_an_invalid_role_is_rejected(client):
    admin = await _register(client, "Bad Role Co", "bad-role-admin@acme.test")
    member = await _invite(client, admin, "field_crew", "bad-role@acme.test")

    response = await client.patch(
        f"/companies/members/{member['user_id']}",
        json={"role": "superuser"},
        headers=admin["headers"],
    )
    assert response.status_code == 422, response.text


async def test_a_non_admin_cannot_change_roles(client):
    admin = await _register(client, "PM Role Co", "pm-role-admin@acme.test")
    pm = await _invite(client, admin, "project_manager", "pm-role-pm@acme.test")
    member = await _invite(client, admin, "field_crew", "pm-role-member@acme.test")

    response = await client.patch(
        f"/companies/members/{member['user_id']}",
        json={"role": "admin"},
        headers=pm["headers"],
    )
    assert response.status_code == 403, response.text


# =============================================================================
# The last-admin guard
# =============================================================================


async def test_an_admin_cannot_change_their_own_role(client):
    admin = await _register(client, "Self Demote Co", "self-demote@acme.test")

    response = await client.patch(
        f"/companies/members/{admin['user_id']}",
        json={"role": "accountant"},
        headers=admin["headers"],
    )
    assert response.status_code == 409, response.text


async def test_an_admin_cannot_remove_themselves(client):
    admin = await _register(client, "Self Remove Co", "self-remove@acme.test")

    response = await client.delete(
        f"/companies/members/{admin['user_id']}", headers=admin["headers"]
    )
    assert response.status_code == 409, response.text


async def test_the_last_admin_cannot_be_demoted_by_another_admin(client):
    """Two admins, each removing the other is fine — until one is left. The
    guard has to look at who REMAINS, not at who is acting."""
    admin = await _register(client, "Last Admin Co", "last-admin@acme.test")
    second = await _invite(client, admin, "admin", "second-admin@acme.test")

    # Two admins: demoting one is allowed.
    demote = await client.patch(
        f"/companies/members/{admin['user_id']}",
        json={"role": "accountant"},
        headers=second["headers"],
    )
    assert demote.status_code == 200, demote.text

    # One admin left: the company must not be able to lose them.
    self_demote = await client.patch(
        f"/companies/members/{second['user_id']}",
        json={"role": "accountant"},
        headers=second["headers"],
    )
    assert self_demote.status_code == 409, self_demote.text
    assert "last admin" in self_demote.text or "your own role" in self_demote.text


# =============================================================================
# Offboarding
# =============================================================================


async def test_removing_a_member_revokes_their_access(client):
    admin = await _register(client, "Offboard Co", "offboard-admin@acme.test")
    member = await _invite(client, admin, "project_manager", "offboard-pm@acme.test")

    before = await client.get("/projects", headers=member["headers"])
    assert before.status_code == 200, before.text

    removed = await client.delete(
        f"/companies/members/{member['user_id']}", headers=admin["headers"]
    )
    assert removed.status_code == 204, removed.text

    # 403: the token is still valid, but they are no longer a member of the
    # company it names — which is exactly what offboarding has to mean.
    after = await client.get("/projects", headers=member["headers"])
    assert after.status_code == 403, after.text

    members = await client.get("/companies/members", headers=admin["headers"])
    assert member["email"] not in [m["email"] for m in members.json()["items"]]


async def test_removing_a_client_member_clears_their_project_access(client):
    """`project_clients` rows cascade on USER deletion, and offboarding
    deletes the membership rather than the user — so without an explicit
    cleanup a re-invited client would silently regain their old jobs."""
    admin = await _register(client, "Offboard Client Co", "offboard-c-admin@acme.test")
    member = await _invite(client, admin, "client", "offboard-client@acme.test")

    project = await client.post(
        "/projects",
        json={"name": "Their Job", "site_address": "1 Oak"},
        headers=admin["headers"],
    )
    granted = await client.post(
        f"/projects/{project.json()['id']}/clients",
        json={"user_id": member["user_id"]},
        headers=admin["headers"],
    )
    assert granted.status_code == 201, granted.text

    await client.delete(
        f"/companies/members/{member['user_id']}", headers=admin["headers"]
    )

    remaining = await client.get(
        f"/projects/{project.json()['id']}/clients", headers=admin["headers"]
    )
    assert remaining.status_code == 200, remaining.text
    assert remaining.json()["items"] == []


async def test_removing_a_member_of_another_company_is_a_404(client):
    a = await _register(client, "Tenant A", "member-cross-a@acme.test")
    b = await _register(client, "Tenant B", "member-cross-b@acme.test")
    stranger = await _invite(client, b, "field_crew", "member-cross-b-crew@acme.test")

    response = await client.delete(
        f"/companies/members/{stranger['user_id']}", headers=a["headers"]
    )
    assert response.status_code == 404, response.text


# =============================================================================
# Company rename
# =============================================================================


async def test_admin_can_rename_their_company(client):
    admin = await _register(client, "Old Name Co", "rename-admin@acme.test")

    response = await client.patch(
        f"/companies/{admin['company_id']}",
        json={"name": "New Name Co"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "New Name Co"


async def test_renaming_another_company_is_refused(client):
    a = await _register(client, "Rename A", "rename-a@acme.test")
    b = await _register(client, "Rename B", "rename-b@acme.test")

    response = await client.patch(
        f"/companies/{b['company_id']}", json={"name": "Hijacked"}, headers=a["headers"]
    )
    assert response.status_code == 403, response.text


# =============================================================================
# Invitations
# =============================================================================


async def test_outstanding_invitations_are_listed_and_accepted_ones_are_not(client):
    admin = await _register(client, "Invite List Co", "invite-list-admin@acme.test")
    pending = await _invite(client, admin, "field_crew", "pending@acme.test", accept=False)
    await _invite(client, admin, "accountant", "accepted@acme.test")

    response = await client.get("/invitations", headers=admin["headers"])
    assert response.status_code == 200, response.text
    emails = [item["email"] for item in response.json()["items"]]
    assert emails == ["pending@acme.test"]
    assert response.json()["items"][0]["id"] == pending["invitation_id"]

    full = await client.get("/invitations?include_accepted=true", headers=admin["headers"])
    assert sorted(item["email"] for item in full.json()["items"]) == [
        "accepted@acme.test",
        "pending@acme.test",
    ]


async def test_revoking_an_invitation_makes_it_unacceptable(client):
    """The id is the credential, so this is what actually withdraws access
    from whoever holds the link."""
    admin = await _register(client, "Revoke Co", "revoke-admin@acme.test")
    invitation = await _invite(
        client, admin, "field_crew", "revoked@acme.test", accept=False
    )

    revoked = await client.delete(
        f"/invitations/{invitation['invitation_id']}", headers=admin["headers"]
    )
    assert revoked.status_code == 204, revoked.text

    accept = await client.post(
        f"/invitations/{invitation['invitation_id']}/accept",
        json={"full_name": "Too Late", "password": "anothersecret123"},
    )
    assert accept.status_code == 404, accept.text


async def test_revoking_an_already_accepted_invitation_is_a_conflict(client):
    """A no-op would be misleading: deleting the row does not un-create the
    membership it produced."""
    admin = await _register(client, "Revoke Late Co", "revoke-late-admin@acme.test")
    member = await _invite(client, admin, "field_crew", "revoke-late@acme.test")

    response = await client.delete(
        f"/invitations/{member['invitation_id']}", headers=admin["headers"]
    )
    assert response.status_code == 409, response.text


async def test_invitations_of_another_company_are_invisible_and_unrevokable(client):
    a = await _register(client, "Inv Tenant A", "inv-cross-a@acme.test")
    b = await _register(client, "Inv Tenant B", "inv-cross-b@acme.test")
    theirs = await _invite(client, b, "field_crew", "inv-cross-b-crew@acme.test", accept=False)

    listing = await client.get("/invitations", headers=a["headers"])
    assert [item["id"] for item in listing.json()["items"]] == []

    revoke = await client.delete(
        f"/invitations/{theirs['invitation_id']}", headers=a["headers"]
    )
    assert revoke.status_code == 404, revoke.text


async def test_a_non_admin_cannot_list_or_revoke_invitations(client):
    admin = await _register(client, "Inv RBAC Co", "inv-rbac-admin@acme.test")
    pm = await _invite(client, admin, "project_manager", "inv-rbac-pm@acme.test")

    listing = await client.get("/invitations", headers=pm["headers"])
    assert listing.status_code == 403, listing.text

    revoke = await client.delete(f"/invitations/{uuid.uuid4()}", headers=pm["headers"])
    assert revoke.status_code == 403, revoke.text
