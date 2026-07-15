"""Task 3.47 (design spec Section 9): fixture-driven profitability-report
tests against known seed data."""
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
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _create_project(client, headers, name="Report Project"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_profitability_equals_billed_revenue_minus_expenses_and_bills(client):
    admin = await _register_and_login(client, "Report Co 1", "report-1@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "Materials", "amount": "150.00", "incurred_on": "2026-08-05"},
        headers=admin["headers"],
    )
    await client.post(
        "/bills",
        json={"project_id": project["id"], "vendor_name": "Sub Co", "amount": "300.00"},
        headers=admin["headers"],
    )

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["billed_revenue"] == "1000.00"
    assert entry["actual_cost"] == "450.00"
    assert entry["profitability"] == "550.00"


async def test_draft_invoice_is_excluded_from_billed_revenue(client):
    admin = await _register_and_login(client, "Report Co 2", "report-2@example.test")
    project = await _create_project(client, admin["headers"])

    await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "1000.00"}, headers=admin["headers"]
    )

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert matching == [], "a draft invoice must not appear as billed revenue anywhere"


async def test_invoice_outside_date_range_is_excluded(client):
    admin = await _register_and_login(client, "Report Co 3", "report-3@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2020-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert matching == []


async def test_ar_aging_bucket_for_a_sent_invoice_31_60_days_past_due(client):
    admin = await _register_and_login(client, "Report Co 4", "report-4@example.test")
    project = await _create_project(client, admin["headers"])

    from datetime import date, timedelta

    stale_due_date = (date.today() - timedelta(days=45)).isoformat()
    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "500.00", "due_date": stale_due_date},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [e for e in body["ar_aging"] if e["id"] == invoice_id]
    assert len(matching) == 1
    assert matching[0]["bucket"] == "31-60"
    assert matching[0]["outstanding_balance"] == "500.00"


async def test_ap_aging_reflects_partial_payment(client):
    admin = await _register_and_login(client, "Report Co 5", "report-5@example.test")

    create_bill = await client.post(
        "/bills",
        json={"vendor_name": "Aging Vendor", "amount": "400.00", "due_date": "2020-01-01"},
        headers=admin["headers"],
    )
    bill_id = create_bill.json()["id"]
    await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "150.00", "paid_date": "2026-01-01"}, headers=admin["headers"]
    )

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [e for e in body["ap_aging"] if e["id"] == bill_id]
    assert len(matching) == 1
    assert matching[0]["outstanding_balance"] == "250.00"
    assert matching[0]["bucket"] == "90+"


async def test_paid_invoice_is_excluded_from_ar_aging(client):
    admin = await _register_and_login(client, "Report Co 6", "report-6@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "200.00", "due_date": "2020-01-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])
    await client.post(
        f"/invoices/{invoice_id}/payments", json={"amount": "200.00", "paid_date": "2026-01-01"}, headers=admin["headers"]
    )

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [e for e in body["ar_aging"] if e["id"] == invoice_id]
    assert matching == []


async def test_project_manager_cannot_read_profitability_report(client):
    admin = await _register_and_login(client, "Report Co 7", "report-7@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-report@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-report@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=pm_headers
    )
    assert response.status_code == 403


async def test_void_bill_is_excluded_from_actual_cost(client):
    """Covers reports.py's `Bill.status != "void"` filter — none of the 7
    tests above create a void bill, so a regression widening or dropping
    that filter would go undetected."""
    admin = await _register_and_login(client, "Report Co 8", "report-8@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    create_bill = await client.post(
        "/bills",
        json={"project_id": project["id"], "vendor_name": "Voided Sub", "amount": "300.00"},
        headers=admin["headers"],
    )
    bill_id = create_bill.json()["id"]
    await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["actual_cost"] == "0.00", "a voided bill must not count toward actual_cost"
    assert entry["profitability"] == "1000.00"


async def test_company_overhead_bill_does_not_leak_into_per_project_cost(client):
    """Covers reports.py's `Bill.project_id.isnot(None)` filter — a bill
    with no project_id (company overhead, per design spec Section 4) must
    never be attributed to any specific project's actual_cost."""
    admin = await _register_and_login(client, "Report Co 9", "report-9@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    await client.post(
        "/bills",
        json={"vendor_name": "Overhead Vendor", "amount": "300.00"},
        headers=admin["headers"],
    )

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["actual_cost"] == "0.00", "a project_id-less overhead bill must not leak into a project's actual_cost"
    assert entry["profitability"] == "1000.00"


async def test_tax_liability_estimate_is_quantized_and_reflects_billed_revenue(client, monkeypatch):
    """Covers reports.py's tax_liability_estimate computation and its
    CENTS/ROUND_HALF_UP quantization (Task 3.46's own code-quality review
    fix). None of the 7 tests above assert on this field at all, and every
    one of them runs under the real DEFAULT_TAX_RATE (0.00), which would
    mask even a broken multiplication. Monkeypatches the rate the router
    module actually uses (imported by value at module scope, per Python's
    `from ... import NAME` semantics) rather than the source constant in
    app.services.invoicing, which this router doesn't re-read at call time."""
    import app.routers.reports as reports_module
    from decimal import Decimal

    monkeypatch.setattr(reports_module, "DEFAULT_TAX_RATE", Decimal("0.10"))

    admin = await _register_and_login(client, "Report Co 10", "report-10@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "999.99", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # 999.99 * 0.10 = 99.999 -> quantized ROUND_HALF_UP to 2 places = 100.00,
    # not the unquantized 99.999 a bare Decimal multiplication would produce.
    assert body["tax_liability_estimate"] == "100.00"


async def test_invoice_created_on_the_end_date_boundary_is_included(client):
    """Permanent regression coverage for the date-boundary bug Task 3.46's
    own code-quality review found and fixed empirically via a throwaway
    (deleted) probe: Invoice.created_at is a real wall-clock DateTime, not
    midnight, so comparing it directly against a bare end_date would
    silently exclude any invoice created later in the day on end_date
    itself. Uses end_date=today (created_at is always "now" at creation
    time, so this invoice is always created within today's date) rather
    than mocking the clock, to prove the real code path end-to-end.

    "Today" here means UTC's calendar date, not date.today() (local time):
    created_at is stored via utcnow() (app/models/base.py's TimestampMixin),
    and local time can land on a different calendar date than UTC depending
    on the machine's timezone and time of day (e.g. 22:15 local vs. 03:15
    UTC the next day). Using date.today() would make this test itself flaky
    near local-midnight-but-not-UTC-midnight, for a reason unrelated to the
    date-boundary behavior it's meant to prove."""
    from datetime import datetime, timezone

    admin = await _register_and_login(client, "Report Co 11", "report-11@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "700.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    today = datetime.now(timezone.utc).date().isoformat()
    response = await client.get(
        f"/reports/profitability?start_date={today}&end_date={today}", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert len(matching) == 1, (
        "an invoice created today must appear in a report whose end_date is "
        "also today, regardless of what time of day it was created"
    )
    assert matching[0]["billed_revenue"] == "700.00"
