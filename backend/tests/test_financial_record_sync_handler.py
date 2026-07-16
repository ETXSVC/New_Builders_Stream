"""Task 4.11 (design spec Section 4): handle_financial_record_created —
enqueues one sync_financial_record message per active connection, does no
sync work itself."""
import uuid

from app.core.event_handlers import register_event_handlers
from app.core.events import publish
from tests.conftest import set_subscription_tier


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
    # Tier gating (Task 5.5): these suites exercise Enterprise-gated
    # accounting routes; registration can only produce trialing/pro.
    # (The plan expected this file to pass unchanged, but its three
    # "via the real route" tests POST to the gated accounting routes.)
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def test_zero_connections_enqueues_nothing(client, monkeypatch, db_session):
    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 1", "sync-1@example.test")

    from app.tasks.accounting_sync import sync_financial_record

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    await publish(
        "INVOICE_CREATED",
        session=db_session,
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        company_id=uuid.UUID(admin["company_id"]),
    )
    assert calls == []


async def test_one_active_connection_enqueues_one_message(client, monkeypatch, db_session):
    from app.services.integration_oauth_state import sign_oauth_state

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 2", "sync-2@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connect_response = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connect_response.status_code == 200, connect_response.text
    connection_id = connect_response.json()["id"]

    from app.tasks.accounting_sync import sync_financial_record

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    entity_id = uuid.uuid4()
    await publish(
        "INVOICE_CREATED",
        session=db_session,
        entity_type="invoice",
        entity_id=entity_id,
        company_id=uuid.UUID(admin["company_id"]),
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["connection_id"] == connection_id
    assert kwargs["entity_type"] == "invoice"
    assert kwargs["entity_id"] == str(entity_id)


async def test_two_active_connections_enqueue_two_messages(client, monkeypatch, db_session):
    from app.services.integration_oauth_state import sign_oauth_state

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 3", "sync-3@example.test")
    qb_state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={qb_state}")
    fb_state = sign_oauth_state(company_id=admin["company_id"], provider="freshbooks")
    await client.get(f"/integrations/freshbooks/callback?code=fake&state={fb_state}")

    from app.tasks.accounting_sync import sync_financial_record

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    await publish(
        "BILL_CREATED",
        session=db_session,
        entity_type="bill",
        entity_id=uuid.uuid4(),
        company_id=uuid.UUID(admin["company_id"]),
    )

    assert len(calls) == 2
    providers_synced = {kw["connection_id"] for _, kw in calls}
    assert len(providers_synced) == 2


async def test_creating_an_invoice_via_the_real_route_enqueues_a_sync(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 4", "sync-4@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")

    project = await client.post(
        "/projects", json={"name": "Sync Project", "site_address": "1 Main St"}, headers=admin["headers"]
    )
    assert project.status_code == 201, project.text

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    assert invoice.status_code == 201, invoice.text

    assert len(calls) == 1
    assert calls[0][1]["entity_type"] == "invoice"
    # The enqueued entity_id must be the CREATED entity's id — asserting
    # entity_type alone would not catch a route passing e.g. project.id.
    assert calls[0][1]["entity_id"] == invoice.json()["id"]


async def test_creating_a_bill_via_the_real_route_enqueues_a_sync(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 5", "sync-5@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    bill = await client.post(
        "/bills", json={"vendor_name": "Ace Plumbing", "amount": "300.00"}, headers=admin["headers"]
    )
    assert bill.status_code == 201, bill.text

    assert len(calls) == 1
    assert calls[0][1]["entity_type"] == "bill"
    assert calls[0][1]["entity_id"] == bill.json()["id"]


async def test_creating_an_expense_via_the_real_route_enqueues_a_sync(client, monkeypatch):
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 6", "sync-6@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")

    project = await client.post(
        "/projects", json={"name": "Expense Sync Project", "site_address": "1 Main St"}, headers=admin["headers"]
    )
    assert project.status_code == 201, project.text

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    expense = await client.post(
        f"/projects/{project.json()['id']}/expenses",
        json={"description": "Materials", "amount": "50.00", "incurred_on": "2026-08-01"},
        headers=admin["headers"],
    )
    assert expense.status_code == 201, expense.text

    assert len(calls) == 1
    assert calls[0][1]["entity_type"] == "expense"
    assert calls[0][1]["entity_id"] == expense.json()["id"]
