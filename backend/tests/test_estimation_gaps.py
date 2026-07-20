"""Tenant-isolation, role, and error-path tests for every route this plan
adds on top of the estimation domain: PDF download, catalog/markup edit and
delete, estimate edit/delete, change-order single-GET and company-wide
list, catalog bulk import, and company branding.

Deviation from the plan doc's own test listing (`docs/superpowers/plans/
2026-07-20-estimation-esignature-frontend.md`, Task 1): the plan's sample
code assumed a `create_company_and_admin`/`authed_client`/`async_client`
conftest surface that does not exist in this codebase. `tests/conftest.py`
only provides a plain `client` fixture (a bare httpx AsyncClient) plus
`set_subscription_tier`; every other test module in this suite (see
`test_estimate_pdf_export.py`'s Task 2.15 section, `test_markup_profiles.py`,
`test_change_orders.py`) defines its own local `_register_and_login` helper
that registers a company, logs in, and returns `{"company_id", "user_id",
"headers"}`. This file follows that same established convention instead of
inventing a new one, and uses the already-returned `admin["user_id"]`
directly in place of the plan's placeholder `_admin_user_id(admin_token)`
helper, matching `test_estimate_pdf_export.py:562`'s exact precedent for
calling `_generate_estimate_pdf` directly.
"""

from app.tasks.estimate_pdf import _generate_estimate_pdf


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
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


def _project_payload(**overrides):
    payload = {"name": "Deck", "site_address": "1 Main St"}
    payload.update(overrides)
    return payload


async def _create_project(client, headers, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _markup_profile_payload(**overrides):
    payload = {"name": "Standard"}
    payload.update(overrides)
    return payload


async def _create_markup_profile(client, headers, **overrides):
    response = await client.post(
        "/markup-profiles", json=_markup_profile_payload(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_estimate(client, headers, *, project_id, markup_profile_id):
    response = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup_profile_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


# -----------------------------------------------------------------------
# GET /estimates/{id}/pdf
# -----------------------------------------------------------------------


async def test_pdf_download_404_before_export(client):
    admin = await _register_and_login(client, "Acme Construction", "pdf-download-admin@acme.test")
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    response = await client.get(f"/estimates/{estimate['id']}/pdf", headers=admin["headers"])
    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "not ready" in detail or "pdf_status" in detail


async def test_pdf_download_streams_bytes_once_ready(client):
    admin = await _register_and_login(
        client, "Acme Construction", "pdf-download-ready-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    export_response = await client.post(
        f"/estimates/{estimate['id']}/export", headers=admin["headers"]
    )
    assert export_response.status_code == 202, export_response.text

    # generate_estimate_pdf is a Dramatiq actor enqueued via .send(); run its
    # plain-coroutine implementation directly, same pattern
    # test_estimate_pdf_export.py's Task 2.15 tests already established for
    # this exact actor.
    await _generate_estimate_pdf(estimate["id"], admin["user_id"])

    response = await client.get(f"/estimates/{estimate['id']}/pdf", headers=admin["headers"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


async def test_pdf_download_cross_tenant_404(client):
    admin_a = await _register_and_login(
        client, "Acme Construction", "pdf-download-a-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin_a["headers"])
    project = await _create_project(client, admin_a["headers"])
    estimate = await _create_estimate(
        client, admin_a["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    admin_b = await _register_and_login(client, "Beta Builders", "pdf-download-b-admin@acme.test")

    response = await client.get(f"/estimates/{estimate['id']}/pdf", headers=admin_b["headers"])
    assert response.status_code == 404
