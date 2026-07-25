"""Two clients of the SAME company must not see or act on each other's work.

This is the regression suite for the finding migration 0019 closes. RLS is
company-scoped, so every test in `test_*_tenant_isolation.py` proves tenant
A can't reach tenant B — and all of them passed while the hole below was
wide open, because the hole was *inside* one tenant. The client-facing
routes narrowed by document status (`sent` estimates, `pending` change
orders, non-draft invoices) and never by identity, so a construction
company with two customers showed each of them the other's pricing,
margins, invoices and executed contracts — and `POST
/estimates/{id}/approve` let either of them legally e-sign the other's
contract, under a signature block whose name and email were free text
never compared to the caller.

Every test here is written as "two clients, one company": the shared
company is the point, and a version of these tests that used two companies
would pass against the vulnerable code.
"""
import uuid

import asyncpg

from tests.conftest import TEST_DATABASE_URL, grant_client_access, set_subscription_tier

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

SIGNATURE_FILES = {"signature_artifact": ("signature.png", b"fake-png-bytes", "image/png")}


async def _register(client, company_name, email):
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
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "email": email,
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _invite(client, admin, role, email):
    invite = await client.post(
        "/invitations", json={"email": email, "role": role}, headers=admin["headers"]
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited User", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text

    members = await client.get("/companies/members", headers=admin["headers"])
    user_id = next(m["user_id"] for m in members.json()["items"] if m["email"] == email)
    return {
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
        "user_id": user_id,
        "email": email,
    }


async def _project(client, admin, name):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Oak St"}, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _sent_estimate(client, admin, project_id, *, unit_rate="45.00"):
    """A fully calculated estimate on `project_id`, advanced to `sent`."""
    markup = await client.post(
        "/markup-profiles",
        json={"name": f"MP {unit_rate}", "overhead_pct": "10.00", "profit_pct": "15.00"},
        headers=admin["headers"],
    )
    item = await client.post(
        "/catalogs/items",
        json={
            "category": "materials",
            "name": f"Lumber {unit_rate}",
            "unit": "board_ft",
            "unit_rate": unit_rate,
        },
        headers=admin["headers"],
    )
    created = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup.json()["id"]},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    estimate_id = created.json()["id"]
    await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": item.json()["id"], "quantity": "2.00"}]},
        headers=admin["headers"],
    )
    await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    sent = await client.post(
        f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"]
    )
    assert sent.status_code == 200, sent.text
    return sent.json()


async def _two_clients_one_company(client, slug):
    """The shared fixture shape for this whole module: one company, two
    projects, one client on each."""
    admin = await _register(client, f"Shared Co {slug}", f"{slug}-admin@acme.test")

    alice = await _invite(client, admin, "client", f"{slug}-alice@acme.test")
    bob = await _invite(client, admin, "client", f"{slug}-bob@acme.test")

    alice_project = await _project(client, admin, "Alice Kitchen")
    bob_project = await _project(client, admin, "Bob Bathroom")

    await grant_client_access(
        client, admin, project_id=alice_project["id"], email=alice["email"]
    )
    await grant_client_access(client, admin, project_id=bob_project["id"], email=bob["email"])

    return admin, alice, bob, alice_project, bob_project


# =============================================================================
# Estimates
# =============================================================================


async def test_client_list_excludes_another_clients_sent_estimate(client):
    admin, alice, _bob, alice_project, bob_project = await _two_clients_one_company(
        client, "est-list"
    )
    mine = await _sent_estimate(client, admin, alice_project["id"], unit_rate="45.00")
    theirs = await _sent_estimate(client, admin, bob_project["id"], unit_rate="99.00")

    response = await client.get("/estimates", headers=alice["headers"])
    assert response.status_code == 200, response.text
    ids = [item["id"] for item in response.json()["items"]]

    assert mine["id"] in ids
    assert theirs["id"] not in ids, (
        "a client can see another client's sent estimate — status scoping is not "
        "identity scoping"
    )


async def test_client_cannot_read_another_clients_estimate_by_id(client):
    admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "est-get"
    )
    theirs = await _sent_estimate(client, admin, bob_project["id"])

    response = await client.get(f"/estimates/{theirs['id']}", headers=alice["headers"])
    assert response.status_code == 404, response.text


async def test_client_cannot_approve_another_clients_estimate(client):
    """The sharpest edge of the finding: this is a legally binding signature
    on a contract that was never this person's."""
    admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "est-approve"
    )
    theirs = await _sent_estimate(client, admin, bob_project["id"])

    response = await client.post(
        f"/estimates/{theirs['id']}/approve",
        data={"signer_name": "Alice", "signer_email": alice["email"]},
        files=SIGNATURE_FILES,
        headers=alice["headers"],
    )
    assert response.status_code == 404, response.text

    # ...and the estimate is untouched: still awaiting the real client.
    after = await client.get(f"/estimates/{theirs['id']}", headers=admin["headers"])
    assert after.json()["status"] == "sent"
    assert after.json()["esignature_id"] is None


async def test_client_cannot_reject_another_clients_estimate(client):
    admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "est-reject"
    )
    theirs = await _sent_estimate(client, admin, bob_project["id"])

    response = await client.post(
        f"/estimates/{theirs['id']}/reject",
        json={"reason": "not mine to reject"},
        headers=alice["headers"],
    )
    assert response.status_code == 404, response.text


async def test_revoking_access_removes_visibility(client):
    admin, alice, _bob, alice_project, _bob_project = await _two_clients_one_company(
        client, "est-revoke"
    )
    mine = await _sent_estimate(client, admin, alice_project["id"])

    before = await client.get(f"/estimates/{mine['id']}", headers=alice["headers"])
    assert before.status_code == 200, before.text

    revoked = await client.delete(
        f"/projects/{alice_project['id']}/clients/{alice['user_id']}",
        headers=admin["headers"],
    )
    assert revoked.status_code == 204, revoked.text

    after = await client.get(f"/estimates/{mine['id']}", headers=alice["headers"])
    assert after.status_code == 404, after.text


# =============================================================================
# Signer identity
# =============================================================================


async def test_approve_rejects_a_signer_email_that_is_not_the_callers(client):
    admin, alice, bob, alice_project, _bob_project = await _two_clients_one_company(
        client, "signer-mismatch"
    )
    mine = await _sent_estimate(client, admin, alice_project["id"])

    response = await client.post(
        f"/estimates/{mine['id']}/approve",
        # Alice signing her own contract, but attributing it to Bob.
        data={"signer_name": "Bob Homeowner", "signer_email": bob["email"]},
        files=SIGNATURE_FILES,
        headers=alice["headers"],
    )
    assert response.status_code == 422, response.text
    assert "signer_email" in response.text


async def test_approve_accepts_the_callers_email_in_any_case(client):
    """An email is case-insensitive; failing a client at the signature step
    over capitalization would be a dead end with no way forward."""
    admin, alice, _bob, alice_project, _bob_project = await _two_clients_one_company(
        client, "signer-case"
    )
    mine = await _sent_estimate(client, admin, alice_project["id"])

    response = await client.post(
        f"/estimates/{mine['id']}/approve",
        data={"signer_name": "Alice", "signer_email": f"  {alice['email'].upper()} "},
        files=SIGNATURE_FILES,
        headers=alice["headers"],
    )
    assert response.status_code == 200, response.text


async def test_signature_records_the_authenticated_account(client):
    admin, alice, _bob, alice_project, _bob_project = await _two_clients_one_company(
        client, "signer-fk"
    )
    mine = await _sent_estimate(client, admin, alice_project["id"])

    approved = await client.post(
        f"/estimates/{mine['id']}/approve",
        # A name that is deliberately NOT the account's own: signer_name
        # stays free text on purpose (people sign in varied forms), and the
        # FK is what carries the identity claim.
        data={"signer_name": "A. Homeowner", "signer_email": alice["email"]},
        files=SIGNATURE_FILES,
        headers=alice["headers"],
    )
    assert approved.status_code == 200, approved.text

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        signed_by = await conn.fetchval(
            "SELECT signed_by_user_id FROM esignatures WHERE id = $1",
            uuid.UUID(approved.json()["esignature_id"]),
        )
    finally:
        await conn.close()

    assert str(signed_by) == alice["user_id"], (
        "the signature must name the account that produced it — without this the "
        "ESIGN record is an assertion, not evidence"
    )


# =============================================================================
# Invoices, projects, change orders
# =============================================================================


async def test_client_cannot_read_another_clients_invoice(client):
    admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "inv"
    )
    created = await client.post(
        f"/projects/{bob_project['id']}/invoices",
        json={"amount": "500.00"},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    invoice_id = created.json()["id"]
    # Sent, not draft: the pre-0019 client filter was `status != "draft"`, so
    # a draft invoice would be hidden for the wrong reason and this test
    # would pass against the vulnerable code.
    sent = await client.post(
        f"/invoices/{invoice_id}/send",
        json={"due_date": "2026-12-31"},
        headers=admin["headers"],
    )
    assert sent.status_code == 200, sent.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=alice["headers"])
    assert detail.status_code == 404, detail.text

    listing = await client.get(
        f"/projects/{bob_project['id']}/invoices", headers=alice["headers"]
    )
    assert listing.status_code == 404, listing.text


async def test_client_cannot_read_another_clients_project_dashboard(client):
    _admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "proj"
    )
    response = await client.get(f"/projects/{bob_project['id']}", headers=alice["headers"])
    assert response.status_code == 404, response.text


async def test_client_cannot_see_or_approve_another_clients_change_order(client):
    admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "co"
    )
    await client.patch(
        f"/projects/{bob_project['id']}/status",
        json={"status": "pre_construction"},
        headers=admin["headers"],
    )
    await client.patch(
        f"/projects/{bob_project['id']}/status",
        json={"status": "active"},
        headers=admin["headers"],
    )
    created = await client.post(
        f"/projects/{bob_project['id']}/change-orders",
        json={
            "description": "Add a skylight",
            "cost_delta": "1200.00",
            "schedule_impact_days": 3,
        },
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    # Not in the cross-project discovery list...
    listing = await client.get("/change-orders", headers=alice["headers"])
    assert listing.status_code == 200, listing.text
    assert change_order_id not in [item["id"] for item in listing.json()["items"]]

    # ...not readable by id...
    detail = await client.get(f"/change-orders/{change_order_id}", headers=alice["headers"])
    assert detail.status_code == 404, detail.text

    # ...and not signable.
    approve = await client.post(
        f"/change-orders/{change_order_id}/approve",
        data={"signer_name": "Alice", "signer_email": alice["email"]},
        files=SIGNATURE_FILES,
        headers=alice["headers"],
    )
    assert approve.status_code == 404, approve.text


# =============================================================================
# Managing the grants themselves
# =============================================================================


async def test_a_client_cannot_grant_themselves_access(client):
    _admin, alice, _bob, _alice_project, bob_project = await _two_clients_one_company(
        client, "self-grant"
    )
    response = await client.post(
        f"/projects/{bob_project['id']}/clients",
        json={"user_id": alice["user_id"]},
        headers=alice["headers"],
    )
    assert response.status_code == 403, response.text


async def test_granting_access_to_a_non_client_role_is_rejected(client):
    """The grant is only meaningful for `client`. A staff account already
    reads company-wide, so accepting one here would imply a restriction
    that doesn't exist."""
    admin = await _register(client, "Grant Role Co", "grant-role-admin@acme.test")
    pm = await _invite(client, admin, "project_manager", "grant-role-pm@acme.test")
    project = await _project(client, admin, "Some Job")

    response = await client.post(
        f"/projects/{project['id']}/clients",
        json={"user_id": pm["user_id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 404, response.text


async def test_granting_access_to_another_tenants_user_is_rejected(client):
    admin_a = await _register(client, "Tenant A", "grant-cross-a@acme.test")
    admin_b = await _register(client, "Tenant B", "grant-cross-b@acme.test")
    stranger = await _invite(client, admin_b, "client", "grant-cross-b-client@acme.test")

    project = await _project(client, admin_a, "A's Job")
    response = await client.post(
        f"/projects/{project['id']}/clients",
        json={"user_id": stranger["user_id"]},
        headers=admin_a["headers"],
    )
    assert response.status_code == 404, response.text


async def test_regranting_the_same_access_is_a_conflict_not_a_duplicate(client):
    admin, alice, _bob, alice_project, _bob_project = await _two_clients_one_company(
        client, "regrant"
    )
    again = await client.post(
        f"/projects/{alice_project['id']}/clients",
        json={"user_id": alice["user_id"]},
        headers=admin["headers"],
    )
    assert again.status_code == 409, again.text

    listing = await client.get(
        f"/projects/{alice_project['id']}/clients", headers=admin["headers"]
    )
    assert listing.status_code == 200, listing.text
    assert len(listing.json()["items"]) == 1, "a re-grant must not create a second row"
