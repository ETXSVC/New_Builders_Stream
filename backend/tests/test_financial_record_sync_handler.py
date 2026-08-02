"""Task 4.11 (design spec Section 4): handle_financial_record_created —
enqueues one sync_financial_record message per active connection, does no
sync work itself."""
import uuid

import asyncpg

from app.core.event_handlers import register_event_handlers
from app.core import after_commit as after_commit_module
from app.core.after_commit import pending_count, run_after_commit
from app.core.events import publish
from tests.conftest import TEST_DATABASE_URL, register_and_login

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _register_and_login(client, company_name, email):
    """Thin wrapper: this module's tests need the enterprise tier.
    See tests/conftest.py's register_and_login."""
    return await register_and_login(client, company_name, email, tier="enterprise")



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
    # Nothing to drain and nothing sent — the handler returned before
    # queueing anything.
    assert pending_count(db_session) == 0
    run_after_commit(db_session)
    assert calls == []


async def test_one_active_connection_enqueues_one_message(client, monkeypatch, db_session):
    from app.services.integration_oauth_state import sign_oauth_state

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Co 2", "sync-2@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connect_response = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connect_response.status_code == 303, connect_response.text
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        connection_id = str(
            await conn.fetchval(
                "SELECT id FROM integration_connections WHERE company_id = $1 AND provider = 'quickbooks'",
                admin["company_id"],
            )
        )
    finally:
        await conn.close()

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

    # Publishing only QUEUES the send now; the request transaction's commit
    # is what releases it (app/core/after_commit.py). Asserting the pending
    # count first is what distinguishes "deferred" from "never registered",
    # which a bare `calls == []` cannot tell apart.
    assert pending_count(db_session) == 1
    assert calls == []
    run_after_commit(db_session)
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

    assert pending_count(db_session) == 2
    assert calls == []
    run_after_commit(db_session)
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


async def test_a_rolled_back_transaction_enqueues_nothing(client, monkeypatch, db_session):
    """The bug this deferral exists for.

    The handler runs INSIDE the request transaction (app/core/events.py
    dispatches inline on purpose, so a handler's exception can roll the
    whole thing back). Redis does not roll back with it. So a `.send()` at
    publish time queued work naming a row that a later rollback erased, and
    the worker then burned all three retries against an id that never
    existed — quietly, because a dead-lettered message made no noise.

    This asserts the ordering directly: publish, roll back, drain. Nothing
    is sent, because the drain only ever happens on the commit path
    (app/core/deps.py).
    """
    from app.services.integration_oauth_state import sign_oauth_state

    register_event_handlers()
    admin = await _register_and_login(client, "Sync Rollback Co", "sync-rb@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connect_response = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connect_response.status_code == 303, connect_response.text

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
    # Queued, not sent.
    assert pending_count(db_session) == 1
    assert calls == []

    await db_session.rollback()

    # The rollback path in get_current_user never calls run_after_commit,
    # so the queued send dies with the transaction. Draining here would be
    # testing the wrong thing — what matters is that the commit path is the
    # ONLY thing that releases it.
    assert calls == []


async def test_a_failing_callback_does_not_break_the_commit(db_session, monkeypatch):
    """By the time these run the data is durable, so a queue hiccup must not
    become the caller's problem — telling them their invoice was not created
    would be false. The log line is the failure surface instead.
    """
    from app.core.after_commit import enqueue_after_commit

    def boom(**_kwargs):
        raise RuntimeError("redis is down")

    enqueue_after_commit(db_session, boom, entity_id="x")
    assert pending_count(db_session) == 1

    # The module's logger is replaced outright rather than captured with a
    # handler or with pytest's `caplog`. Neither works reliably here:
    # app/core/logging.py calls basicConfig(force=True) at import, which
    # drops every root handler including caplog's, and the resulting level/
    # propagation state made handler-based capture silently observe nothing.
    # Substituting the logger tests the thing that actually matters — that
    # the failure IS reported — without depending on logging configuration
    # at all.
    reported: list[str] = []

    class _Recorder:
        def exception(self, msg, *args):
            reported.append(msg % args if args else msg)

    monkeypatch.setattr(after_commit_module, "logger", _Recorder())

    run_after_commit(db_session)  # must not raise

    assert pending_count(db_session) == 0
    assert reported == ["after-commit callback failed; the enqueue was lost"]


async def test_recording_a_payment_enqueues_a_sync(client, monkeypatch):
    """The end of the gap: a tenant's accounting software saw every invoice
    and never learned any of them had been paid.

    Goes through the real payment route rather than publishing the event by
    hand — the point being that recording a payment publishes at all, which
    is what was missing, not that the handler works once it does.
    """
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Pay Sync Co", "pay-sync@example.test")

    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connected = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connected.status_code == 303, connected.text

    project = await client.post(
        "/projects",
        json={"name": "Payment Project", "site_address": "1 Main St", "status": "active"},
        headers=admin["headers"],
    )
    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices",
        json={"amount": "500.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append(kw))

    paid = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "500.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert paid.status_code == 201, paid.text

    payment_syncs = [c for c in calls if c.get("entity_type") == "payment"]
    assert len(payment_syncs) == 1, f"expected one payment sync, got {calls}"
    assert payment_syncs[0]["entity_id"] == paid.json()["id"]


async def test_a_partial_payment_also_enqueues_a_sync(client, monkeypatch):
    """Published per PAYMENT, not once when the invoice settles. An
    "INVOICE_PAID" event firing only on the final instalment would leave the
    first two payments absent from the tenant's books entirely, and the
    invoice reading as unpaid until the last one landed."""
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Partial Pay Co", "partial-pay@example.test")

    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    assert (
        await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    ).status_code == 303

    project = await client.post(
        "/projects",
        json={"name": "Partial Project", "site_address": "1 Main St", "status": "active"},
        headers=admin["headers"],
    )
    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices",
        json={"amount": "500.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append(kw))

    first = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "200.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert first.status_code == 201, first.text
    assert len([c for c in calls if c.get("entity_type") == "payment"]) == 1, (
        "a partial payment must sync too, not wait for the invoice to settle"
    )
