"""Optimistic concurrency: a stale PATCH is refused instead of clobbering.

The scenario, which this app previously resolved as last-write-wins with no
indication to either party: two project managers open the same record, both
edit, both save. Before `app/services/concurrency.py` the second write simply
overwrote the first.

Each entity gets the same three cases, because all three have to hold for the
guard to be worth anything:

  * stale token  -> 409, and the earlier write is still intact
  * current token -> 200
  * omitted token -> 200 (opt-in; pre-existing clients must not break)

The "still intact" half matters as much as the 409. A guard that rejects the
request but has already staged half of it would be worse than none, so the
tests re-read the row and assert the first editor's value survived.
"""
import pytest

pytestmark = pytest.mark.anyio


async def _register(client, company, email):
    await client.post(
        "/auth/register",
        json={
            "company_name": company,
            "admin_full_name": "Concurrency Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    login = await client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _make_project(client, headers, name="Concurrency Job"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# =============================================================================
# Project
# =============================================================================


async def test_stale_project_patch_is_refused_and_first_write_survives(client):
    headers = await _register(client, "Acme Construction", "conc-proj@acme.test")
    project = await _make_project(client, headers)
    stale_token = project["updated_at"]

    # Editor A saves first. This bumps updated_at, invalidating B's token.
    first = await client.patch(
        f"/projects/{project['id']}",
        json={"name": "Renamed by A", "expected_updated_at": stale_token},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["name"] == "Renamed by A"

    # Editor B, still holding the token from before A's save.
    second = await client.patch(
        f"/projects/{project['id']}",
        json={"name": "Renamed by B", "expected_updated_at": stale_token},
        headers=headers,
    )
    assert second.status_code == 409, second.text
    assert "changed by someone else" in second.json()["detail"]

    # A's write survived: the rejected request staged nothing. get_current_user
    # commits once after the handler returns, so a guard that raised too late
    # would have let B's name through on that same commit.
    current = await client.get(f"/projects/{project['id']}", headers=headers)
    assert current.json()["name"] == "Renamed by A"


async def test_project_patch_with_current_token_succeeds(client):
    headers = await _register(client, "Acme Construction", "conc-proj-ok@acme.test")
    project = await _make_project(client, headers)

    response = await client.patch(
        f"/projects/{project['id']}",
        json={"name": "Renamed", "expected_updated_at": project["updated_at"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    # The token must move, or a second stale write would be accepted.
    assert response.json()["updated_at"] != project["updated_at"]


async def test_project_patch_without_a_token_is_unchecked(client):
    """Opt-in: omitting the field keeps the previous last-write-wins behaviour
    so existing API clients are not broken by this change."""
    headers = await _register(client, "Acme Construction", "conc-proj-none@acme.test")
    project = await _make_project(client, headers)

    await client.patch(
        f"/projects/{project['id']}", json={"name": "First"}, headers=headers
    )
    second = await client.patch(
        f"/projects/{project['id']}", json={"name": "Second"}, headers=headers
    )
    assert second.status_code == 200, second.text
    assert second.json()["name"] == "Second"


async def test_expected_updated_at_is_not_written_onto_the_project(client):
    """`patch_project` setattr()s every key from model_dump onto the ORM
    object, so the concurrency token has to be excluded explicitly. If that
    exclusion regresses, this is what catches it."""
    headers = await _register(client, "Acme Construction", "conc-proj-leak@acme.test")
    project = await _make_project(client, headers)

    response = await client.patch(
        f"/projects/{project['id']}",
        json={"name": "Renamed", "expected_updated_at": project["updated_at"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    # Had the token been assigned onto the row, updated_at would echo back the
    # value the caller sent instead of a freshly-bumped one.
    assert response.json()["updated_at"] != project["updated_at"]


# =============================================================================
# Lead
# =============================================================================


async def test_stale_lead_patch_is_refused(client):
    headers = await _register(client, "Acme Construction", "conc-lead@acme.test")
    created = await client.post(
        "/leads",
        json={
            "contact_name": "Ada",
            "project_name": "Kitchen",
            "email": "ada@x.test",
            "project_type": "Remodel",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    lead = created.json()
    stale_token = lead["updated_at"]

    first = await client.patch(
        f"/leads/{lead['id']}",
        json={"contact_name": "Ada A", "expected_updated_at": stale_token},
        headers=headers,
    )
    assert first.status_code == 200, first.text

    second = await client.patch(
        f"/leads/{lead['id']}",
        json={"contact_name": "Ada B", "expected_updated_at": stale_token},
        headers=headers,
    )
    assert second.status_code == 409, second.text

    current = await client.get(f"/leads/{lead['id']}", headers=headers)
    assert current.json()["contact_name"] == "Ada A"


async def test_lead_stale_check_precedes_the_status_transition_check(client):
    """Ordering: the stale guard runs before the transition validation, so a
    request that is both stale AND carries an illegal transition reports the
    staleness. Either 409 would be defensible, but the order is asserted so it
    stays deliberate rather than incidental to line placement."""
    headers = await _register(client, "Acme Construction", "conc-lead-order@acme.test")
    created = await client.post(
        "/leads",
        json={
            "contact_name": "Bo",
            "project_name": "Bath",
            "email": "bo@x.test",
            "project_type": "Remodel",
        },
        headers=headers,
    )
    lead = created.json()
    stale_token = lead["updated_at"]

    bumped = await client.patch(
        f"/leads/{lead['id']}", json={"contact_name": "Bo B"}, headers=headers
    )
    assert bumped.status_code == 200

    response = await client.patch(
        f"/leads/{lead['id']}",
        json={"status": "won", "expected_updated_at": stale_token},
        headers=headers,
    )
    assert response.status_code == 409
    assert "changed by someone else" in response.json()["detail"]
