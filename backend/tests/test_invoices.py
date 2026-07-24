
import asyncpg

from tests.conftest import TEST_DATABASE_URL, set_subscription_tier

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
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    # Tier gating (Task 5.5): these suites exercise Enterprise-gated
    # accounting routes; registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
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


async def _invite_and_login_as(client, admin, role, email):
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
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


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


async def test_create_invoice_rejects_zero_or_negative_amount(client):
    admin = await _register_and_login(client, "Invoice Co Neg", "invoice-neg@example.test")
    project = await _create_project(client, admin["headers"])

    zero = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "0.00"}, headers=admin["headers"]
    )
    assert zero.status_code == 422

    negative = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "-10.00"}, headers=admin["headers"]
    )
    assert negative.status_code == 422


async def test_create_invoice_quantizes_amount_to_two_decimal_places(client):
    """Without quantizing before persisting, the create response (built
    from the in-memory ORM object) would show the raw unrounded value
    while Postgres's NUMERIC(12,2) column silently rounds it on INSERT —
    a later GET would then disagree with what create originally
    returned."""
    admin = await _register_and_login(client, "Invoice Co Quant", "invoice-quant@example.test")
    project = await _create_project(client, admin["headers"])

    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.005"}, headers=admin["headers"]
    )
    assert create.status_code == 201, create.text
    invoice_id = create.json()["id"]
    assert create.json()["amount"] == "100.01"

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    assert detail.json()["amount"] == "100.01"


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


async def _set_invoice_status_directly(invoice_id, status_value):
    """Closes a gap that has nothing to do with what this file tests:
    Task 3.36's send route (the only way status legitimately becomes
    'sent') doesn't exist yet — same rationale test_estimates.py's own
    _set_estimate_status_directly gives."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute("UPDATE invoices SET status = $1 WHERE id = $2", status_value, invoice_id)
    finally:
        await conn.close()


async def test_list_invoices_as_client_shows_non_draft_only(client):
    admin = await _register_and_login(client, "Invoice Co 6", "invoice-client-list@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-list@example.test")
    project = await _create_project(client, admin["headers"])
    await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    sent = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "200.00"}, headers=admin["headers"]
    )
    await _set_invoice_status_directly(sent.json()["id"], "sent")

    response = await client.get(f"/projects/{project['id']}/invoices", headers=client_role["headers"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == sent.json()["id"]
    assert items[0]["status"] == "sent"


async def test_get_invoice_as_client_404s_on_draft(client):
    admin = await _register_and_login(client, "Invoice Co 7", "invoice-client-detail@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-detail@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "150.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.get(f"/invoices/{invoice_id}", headers=client_role["headers"])
    assert response.status_code == 404, response.text

    await _set_invoice_status_directly(invoice_id, "sent")
    response = await client.get(f"/invoices/{invoice_id}", headers=client_role["headers"])
    assert response.status_code == 200, response.text


async def test_send_invoice_with_no_due_date_at_creation_requires_one_in_the_request(client):
    admin = await _register_and_login(client, "Send Co 1", "send-1@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "400.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    missing_due_date = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"]
    )
    assert missing_due_date.status_code == 422

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={"due_date": "2026-08-15"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "sent"
    assert body["due_date"] == "2026-08-15"


async def test_send_invoice_that_already_has_a_due_date_does_not_require_one_again(client):
    admin = await _register_and_login(client, "Send Co 2", "send-2@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "400.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create.json()["id"]

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["due_date"] == "2026-09-01"


async def test_sending_a_non_draft_invoice_returns_409(client):
    admin = await _register_and_login(client, "Send Co 3", "send-3@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "400.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"]
    )
    assert response.status_code == 409


async def test_client_cannot_send_invoice(client):
    admin = await _register_and_login(client, "Send Co 4", "send-4@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-send@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "400.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create.json()["id"]

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=client_role["headers"]
    )
    assert response.status_code == 403


async def _create_and_send_invoice(client, headers, project_id, amount, due_date="2026-09-01"):
    create = await client.post(
        f"/projects/{project_id}/invoices",
        json={"amount": amount, "due_date": due_date},
        headers=headers,
    )
    assert create.status_code == 201, create.text
    invoice_id = create.json()["id"]
    send = await client.post(f"/invoices/{invoice_id}/send", json={}, headers=headers)
    assert send.status_code == 200, send.text
    return invoice_id


async def test_partial_payment_leaves_invoice_sent_with_reduced_outstanding_balance(client):
    admin = await _register_and_login(client, "Pay Co 1", "pay-1@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "1000.00")

    response = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "400.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "sent"
    assert body["outstanding_balance"] == "600.00"
    assert len(body["payments"]) == 1


async def test_cumulative_payment_reaching_full_amount_auto_marks_paid(client):
    admin = await _register_and_login(client, "Pay Co 2", "pay-2@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "500.00")

    await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "300.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    second = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "200.00", "paid_date": "2026-08-02"},
        headers=admin["headers"],
    )
    assert second.status_code == 201, second.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "0.00"


async def test_overpayment_exceeding_remaining_balance_returns_409(client):
    """A single payment larger than the invoice's remaining balance must be
    rejected outright, not silently accepted into a negative
    outstanding_balance — the invoice stays at 'sent' with no payment row
    recorded."""
    admin = await _register_and_login(client, "Pay Co 3", "pay-3@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")

    response = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "150.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert response.status_code == 409, response.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "sent"
    assert body["outstanding_balance"] == "100.00"
    assert body["payments"] == []


async def test_cumulative_overpayment_exceeding_remaining_balance_returns_409(client):
    """Same rule as the single-payment case above, but against a partially
    paid invoice: a second payment larger than what's LEFT (not the
    original total) must be rejected."""
    admin = await _register_and_login(client, "Pay Co 3b", "pay-3b@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")

    first = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "60.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "50.00", "paid_date": "2026-08-02"},
        headers=admin["headers"],
    )
    assert second.status_code == 409, second.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "sent"
    assert body["outstanding_balance"] == "40.00"
    assert len(body["payments"]) == 1


async def test_payment_against_an_already_paid_invoice_returns_409(client):
    """Once an invoice is fully paid, a further payment must be rejected —
    not silently accepted, stacking unlimited additional "payments" on a
    settled invoice."""
    admin = await _register_and_login(client, "Pay Co 3c", "pay-3c@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")

    paid_in_full = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "100.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert paid_in_full.status_code == 201, paid_in_full.text

    further = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "10.00", "paid_date": "2026-08-02"},
        headers=admin["headers"],
    )
    assert further.status_code == 409, further.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "0.00"
    assert len(body["payments"]) == 1


async def test_payment_against_a_draft_invoice_returns_409(client):
    admin = await _register_and_login(client, "Pay Co 4", "pay-4@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "50.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert response.status_code == 409


async def test_client_cannot_record_invoice_payment(client):
    admin = await _register_and_login(client, "Pay Co 5", "pay-5@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-pay@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")

    response = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "50.00", "paid_date": "2026-08-01"},
        headers=client_role["headers"],
    )
    assert response.status_code == 403


async def test_zero_or_negative_payment_amount_returns_422(client):
    admin = await _register_and_login(client, "Pay Co 6", "pay-6@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")

    zero = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "0.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert zero.status_code == 422

    negative = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "-10.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert negative.status_code == 422


async def test_void_a_draft_invoice(client):
    admin = await _register_and_login(client, "Void Co 1", "void-1@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"


async def test_void_a_sent_invoice(client):
    admin = await _register_and_login(client, "Void Co 2", "void-2@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "200.00")

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"


async def test_void_a_paid_invoice_returns_409(client):
    admin = await _register_and_login(client, "Void Co 3", "void-3@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")
    await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "100.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 409


async def test_void_an_already_void_invoice_returns_409(client):
    admin = await _register_and_login(client, "Void Co 4", "void-4@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]
    await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 409


async def test_client_cannot_void_invoice(client):
    admin = await _register_and_login(client, "Void Co 5", "void-5@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-void@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.post(f"/invoices/{invoice_id}/void", headers=client_role["headers"])
    assert response.status_code == 403
