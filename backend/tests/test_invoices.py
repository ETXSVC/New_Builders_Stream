from decimal import Decimal

import pytest


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


async def _create_project(client, headers, name="Test Project"):
    response = await client.post(
        "/projects",
        json={"name": name, "site_address": "123 Main St", "status": "active"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_invoice_assigns_sequential_number_and_draft_status(client):
    admin = await _register_and_login(client, "Invoice Co", "invoice-create@example.test")
    project = await _create_project(client, admin["headers"])

    response = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["invoice_number"].startswith("INV-")
    assert body["invoice_number"].endswith("-0001")
    assert body["outstanding_balance"] == "1000.00"


async def test_second_invoice_for_same_company_gets_the_next_number(client):
    admin = await _register_and_login(client, "Invoice Co 2", "invoice-seq@example.test")
    project = await _create_project(client, admin["headers"])

    first = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "500.00"}, headers=admin["headers"]
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "750.00"}, headers=admin["headers"]
    )
    assert second.status_code == 201, second.text
    assert second.json()["invoice_number"].endswith("-0002")


async def test_project_manager_cannot_create_invoice(client):
    admin = await _register_and_login(client, "Invoice Co 3", "invoice-pm@example.test")
    project = await _create_project(client, admin["headers"])

    invite = await client.post(
        "/invitations",
        json={"email": "pm@example.test", "role": "project_manager"},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    assert accept.status_code == 200, accept.text
    pm_login = await client.post(
        "/auth/login", json={"email": "pm@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=pm_headers
    )
    assert response.status_code == 403


async def test_list_invoices_for_project(client):
    admin = await _register_and_login(client, "Invoice Co 4", "invoice-list@example.test")
    project = await _create_project(client, admin["headers"])
    await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "200.00"}, headers=admin["headers"]
    )

    response = await client.get(f"/projects/{project['id']}/invoices", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_get_invoice_detail_includes_empty_payments_list(client):
    admin = await _register_and_login(client, "Invoice Co 5", "invoice-detail@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "300.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["payments"] == []
