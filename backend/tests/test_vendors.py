"""POST/GET /vendors, PATCH /vendors/{id} router tests
(app/routers/vendors.py). Plain company-scoped resource, no inheritance
concept — same shape as test_markup_profiles.py.
"""

from tests.conftest import register_and_login, set_subscription_tier


async def _register_and_login(client, company_name, email):
    """Thin wrapper: this module's tests need the pro tier.
    See tests/conftest.py's register_and_login."""
    return await register_and_login(client, company_name, email, tier="pro")



def _vendor_payload(**overrides):
    payload = {"name": "ABC Lumber", "contact_email": "sales@abclumber.test", "contact_phone": "555-0100"}
    payload.update(overrides)
    return payload


async def test_admin_can_create_and_list_vendors(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")

    create = await client.post("/vendors", json=_vendor_payload(), headers=admin["headers"])
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "ABC Lumber"
    assert body["contact_email"] == "sales@abclumber.test"
    assert body["company_id"] == admin["company_id"]

    listed = await client.get("/vendors", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 1


async def test_create_vendor_without_optional_fields(client):
    admin = await _register_and_login(client, "Acme Construction", "min-admin@acme.test")

    response = await client.post("/vendors", json={"name": "No-frills Supply"}, headers=admin["headers"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["contact_email"] is None
    assert body["contact_phone"] is None
    assert body["notes"] is None


async def test_patch_vendor_updates_only_supplied_fields(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-admin@acme.test")
    created = await client.post("/vendors", json=_vendor_payload(), headers=admin["headers"])
    vendor_id = created.json()["id"]

    patched = await client.patch(
        f"/vendors/{vendor_id}", json={"contact_phone": "555-9999"}, headers=admin["headers"]
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["contact_phone"] == "555-9999"
    assert body["name"] == "ABC Lumber"  # untouched


async def test_patch_unknown_vendor_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "404-admin@acme.test")

    response = await client.patch(
        "/vendors/00000000-0000-0000-0000-000000000000",
        json={"name": "Ghost Vendor"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_starter_tier_cannot_create_vendor(client):
    admin = await _register_and_login(client, "Acme Construction", "starter-admin@acme.test")
    await set_subscription_tier(admin["company_id"], "starter")

    response = await client.post("/vendors", json=_vendor_payload(), headers=admin["headers"])
    assert response.status_code == 403
