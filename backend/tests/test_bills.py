"""Task 3.41 (design spec Section 4): POST/GET /bills, GET /bills/{id}."""
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
    # Tier gating (Task 5.5): these suites exercise Enterprise-gated
    # accounting routes; registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _add_membership_directly(user_id, company_id, role):
    """Test-setup plumbing, identical rationale to
    test_subcontractor_assignments.py's own helper of the same name."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role, created_at) "
            "VALUES ($1, $2, $3, now())",
            company_id,
            user_id,
            role,
        )
    finally:
        await conn.close()


async def _create_child_with_membership(client, parent, name, role="admin"):
    """Identical to test_subcontractor_assignments.py's helper of the same
    name — duplicated rather than imported across test modules, matching
    this codebase's existing convention."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": name},
        headers=parent["headers"],
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


async def _create_project(client, headers):
    response = await client.post(
        "/projects", json={"name": "Bill Project", "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_subcontractor(client, headers):
    response = await client.post(
        "/subcontractors", json={"name": "Ace Plumbing", "trade": "plumbing"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_bill_against_a_subcontractor(client):
    admin = await _register_and_login(client, "Bill Co 1", "bill-1@example.test")
    project = await _create_project(client, admin["headers"])
    subcontractor = await _create_subcontractor(client, admin["headers"])

    response = await client.post(
        "/bills",
        json={"project_id": project["id"], "subcontractor_id": subcontractor["id"], "amount": "800.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "unpaid"
    assert body["outstanding_balance"] == "800.00"


async def test_create_bill_against_a_free_text_vendor_with_no_project(client):
    admin = await _register_and_login(client, "Bill Co 2", "bill-2@example.test")

    response = await client.post(
        "/bills", json={"vendor_name": "City Power & Light", "amount": "150.00"}, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["project_id"] is None


async def test_create_bill_with_neither_subcontractor_nor_vendor_name_returns_422(client):
    admin = await _register_and_login(client, "Bill Co 3", "bill-3@example.test")

    response = await client.post("/bills", json={"amount": "50.00"}, headers=admin["headers"])
    assert response.status_code == 422


async def test_create_bill_rejects_zero_or_negative_amount(client):
    admin = await _register_and_login(client, "Bill Co Neg", "bill-neg@example.test")

    zero = await client.post(
        "/bills", json={"vendor_name": "Vendor Zero", "amount": "0.00"}, headers=admin["headers"]
    )
    assert zero.status_code == 422

    negative = await client.post(
        "/bills", json={"vendor_name": "Vendor Neg", "amount": "-10.00"}, headers=admin["headers"]
    )
    assert negative.status_code == 422


async def test_create_bill_quantizes_amount_to_two_decimal_places(client):
    """Without quantizing before persisting, the create response (built
    from the in-memory ORM object) would show the raw unrounded value
    while Postgres's NUMERIC(12,2) column silently rounds it on INSERT —
    a later GET would then disagree with what create originally
    returned."""
    admin = await _register_and_login(client, "Bill Co Quant", "bill-quant@example.test")

    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Quant", "amount": "100.005"}, headers=admin["headers"]
    )
    assert create.status_code == 201, create.text
    bill_id = create.json()["id"]
    assert create.json()["amount"] == "100.01"

    detail = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    assert detail.json()["amount"] == "100.01"


# =============================================================================
# company_id sourcing: parent-company session (unswitched headers) creating a
# Bill against a child-branch Project/Subcontractor. Same empirical shape as
# test_subcontractor_assignments.py's own
# test_creating_assignment_under_child_branch_project_and_subcontractor_uses_child_company_id.
# =============================================================================


async def test_creating_bill_against_child_branch_project_uses_child_company_id(client):
    """The new Bill's company_id must come from the referenced Project's
    own company_id (the CHILD), never current.company_id (the PARENT
    acting session). The Project is created under the CHILD branch (via
    X-Tenant-ID-switched headers, backed by a genuine company_users row);
    the Bill is then created using the PARENT's own DEFAULT headers —
    deliberately NOT X-Tenant-ID-switched — so RLS's
    get_all_descendant_ids() grant alone is what makes the child's
    Project visible/writable to this session, which is the only way
    current.company_id (parent) and project.company_id (child) genuinely
    diverge without an explicit header switch."""
    parent = await _register_and_login(client, "Parent Co", "bill-parent-co@example.test")
    child_id = await _create_child_with_membership(client, parent, "Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    project = await _create_project(client, child_headers)

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child.
    response = await client.post(
        "/bills",
        json={"project_id": project["id"], "vendor_name": "City Power & Light", "amount": "150.00"},
        headers=parent["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_id"] == child_id, (
        "Bill created against a child-branch Project must belong to the "
        "PROJECT's own company (the child), not the acting session's "
        f"company (the parent) — got {body['company_id']!r}, expected "
        f"child_id={child_id!r}"
    )

    # Read it back via the child's own tenant context to confirm it's
    # genuinely visible there too, not just correctly labeled.
    get_response = await client.get(f"/bills/{body['id']}", headers=child_headers)
    assert get_response.status_code == 200, get_response.text


async def test_creating_bill_against_child_branch_subcontractor_with_no_project_uses_child_company_id(
    client,
):
    """Same bug class as the Project-based test above, for the sibling
    "company overhead bill" path: no Project, only a Subcontractor. The
    new Bill's company_id must come from the referenced Subcontractor's
    own company_id (the CHILD), never current.company_id (the PARENT
    acting session)."""
    parent = await _register_and_login(client, "Parent Co", "bill-sub-parent@example.test")
    child_id = await _create_child_with_membership(client, parent, "Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    subcontractor = await _create_subcontractor(client, child_headers)

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child.
    response = await client.post(
        "/bills",
        json={"subcontractor_id": subcontractor["id"], "amount": "300.00"},
        headers=parent["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_id"] == child_id, (
        "Bill created against a child-branch Subcontractor (no Project) "
        "must belong to the SUBCONTRACTOR's own company (the child), not "
        f"the acting session's company (the parent) — got "
        f"{body['company_id']!r}, expected child_id={child_id!r}"
    )

    get_response = await client.get(f"/bills/{body['id']}", headers=child_headers)
    assert get_response.status_code == 200, get_response.text


async def test_project_manager_cannot_create_bill(client):
    admin = await _register_and_login(client, "Bill Co 4", "bill-4@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-bill@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-bill@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        "/bills", json={"vendor_name": "Some Vendor", "amount": "50.00"}, headers=pm_headers
    )
    assert response.status_code == 403


async def test_client_cannot_read_bills(client):
    admin = await _register_and_login(client, "Bill Co 5", "bill-5@example.test")
    invite = await client.post(
        "/invitations", json={"email": "client-bill@example.test", "role": "client"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Client User", "password": "supersecret123"},
    )
    client_login = await client.post(
        "/auth/login", json={"email": "client-bill@example.test", "password": "supersecret123"}
    )
    client_headers = {"Authorization": f"Bearer {client_login.json()['access_token']}"}

    response = await client.get("/bills", headers=client_headers)
    assert response.status_code == 403


async def test_list_bills_filtered_by_project(client):
    admin = await _register_and_login(client, "Bill Co 6", "bill-6@example.test")
    project = await _create_project(client, admin["headers"])
    await client.post(
        "/bills", json={"project_id": project["id"], "vendor_name": "Vendor X", "amount": "10.00"}, headers=admin["headers"]
    )
    await client.post("/bills", json={"vendor_name": "Overhead Vendor", "amount": "20.00"}, headers=admin["headers"])

    scoped = await client.get(f"/bills?project_id={project['id']}", headers=admin["headers"])
    assert len(scoped.json()["items"]) == 1

    all_bills = await client.get("/bills", headers=admin["headers"])
    assert len(all_bills.json()["items"]) == 2


async def test_get_bill_detail_includes_empty_payments_list(client):
    admin = await _register_and_login(client, "Bill Co 7", "bill-7@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Y", "amount": "60.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["payments"] == []


async def test_client_cannot_read_bill_detail(client):
    """GET /bills, GET /bills/{id}, and POST /bills all share the identical
    require_role("admin", "accountant") dependency (app/routers/bills.py) —
    test_client_cannot_read_bills already proves this for the list route,
    this proves it directly for the detail route too, rather than leaving
    get_bill's own RBAC enforcement only indirectly exercised."""
    admin = await _register_and_login(client, "Bill Co 8", "bill-8@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Z", "amount": "40.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    invite = await client.post(
        "/invitations", json={"email": "client-bill-detail@example.test", "role": "client"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Client User", "password": "supersecret123"},
    )
    client_login = await client.post(
        "/auth/login", json={"email": "client-bill-detail@example.test", "password": "supersecret123"}
    )
    client_headers = {"Authorization": f"Bearer {client_login.json()['access_token']}"}

    response = await client.get(f"/bills/{bill_id}", headers=client_headers)
    assert response.status_code == 403


async def test_get_bill_detail_returns_404_for_nonexistent_bill(client):
    admin = await _register_and_login(client, "Bill Co 9", "bill-9@example.test")

    response = await client.get(
        "/bills/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_cumulative_bill_payment_reaching_full_amount_auto_marks_paid(client):
    admin = await _register_and_login(client, "Bill Pay Co 1", "bill-pay-1@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Z", "amount": "300.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "100.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    second = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "200.00", "paid_date": "2026-08-02"}, headers=admin["headers"]
    )
    assert second.status_code == 201, second.text

    detail = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "0.00"


async def test_bill_overpayment_exceeding_remaining_balance_returns_409(client):
    """A single payment larger than the bill's remaining balance must be
    rejected outright, not silently accepted into a negative
    outstanding_balance — same rule as test_invoices.py's own
    test_overpayment_exceeding_remaining_balance_returns_409."""
    admin = await _register_and_login(client, "Bill Pay Co 3", "bill-pay-3@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Y", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "150.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert response.status_code == 409, response.text

    detail = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "unpaid"
    assert body["outstanding_balance"] == "100.00"
    assert body["payments"] == []


async def test_cumulative_bill_overpayment_exceeding_remaining_balance_returns_409(client):
    """Same rule as the single-payment case above, but against a partially
    paid bill: a second payment larger than what's LEFT (not the original
    total) must be rejected."""
    admin = await _register_and_login(client, "Bill Pay Co 3b", "bill-pay-3b@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Y2", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    first = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "60.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "50.00", "paid_date": "2026-08-02"}, headers=admin["headers"]
    )
    assert second.status_code == 409, second.text

    detail = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "unpaid"
    assert body["outstanding_balance"] == "40.00"
    assert len(body["payments"]) == 1


async def test_payment_against_an_already_paid_bill_returns_409(client):
    """Once a bill is fully paid, a further payment must be rejected — not
    silently accepted, stacking unlimited additional "payments" on a
    settled bill (previously only `void` was blocked, not `paid`)."""
    admin = await _register_and_login(client, "Bill Pay Co 3c", "bill-pay-3c@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Y3", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    paid_in_full = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "100.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert paid_in_full.status_code == 201, paid_in_full.text

    further = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "10.00", "paid_date": "2026-08-02"}, headers=admin["headers"]
    )
    assert further.status_code == 409, further.text

    detail = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "0.00"
    assert len(body["payments"]) == 1


async def test_payment_against_void_bill_returns_409(client):
    admin = await _register_and_login(client, "Bill Pay Co 2", "bill-pay-2@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor W", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]
    await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])

    response = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "50.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert response.status_code == 409


async def test_void_an_unpaid_bill(client):
    admin = await _register_and_login(client, "Bill Void Co 1", "bill-void-1@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor V", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"


async def test_void_a_paid_bill_returns_409(client):
    admin = await _register_and_login(client, "Bill Void Co 2", "bill-void-2@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor U", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]
    await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "100.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )

    response = await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])
    assert response.status_code == 409


async def test_void_an_already_void_bill_returns_409(client):
    """Mirrors test_invoices.py's own test_void_an_already_void_invoice_
    returns_409 — test_void_a_paid_bill_returns_409 above only covers the
    paid->void deny path, not void->void."""
    admin = await _register_and_login(client, "Bill Void Co 3", "bill-void-3@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor T", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]
    await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])

    response = await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])
    assert response.status_code == 409


async def _invite_and_login_as_client(client, admin, email):
    invite = await client.post(
        "/invitations", json={"email": email, "role": "client"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Client User", "password": "supersecret123"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def test_client_cannot_record_bill_payment(client):
    """Mirrors test_invoices.py's own test_client_cannot_record_invoice_
    payment — record_bill_payment's require_role("admin", "accountant")
    guard (bills.py) was otherwise only exercised indirectly via the
    create/list/get routes' equivalent tests, never directly for this
    route."""
    admin = await _register_and_login(client, "Bill Pay Co 3", "bill-pay-3@example.test")
    client_role = await _invite_and_login_as_client(client, admin, "client-bill-pay@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor S", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.post(
        f"/bills/{bill_id}/payments",
        json={"amount": "50.00", "paid_date": "2026-08-01"},
        headers=client_role["headers"],
    )
    assert response.status_code == 403


async def test_client_cannot_void_bill(client):
    """Mirrors test_invoices.py's own test_client_cannot_void_invoice — same
    rationale as test_client_cannot_record_bill_payment above, for void_bill's
    guard."""
    admin = await _register_and_login(client, "Bill Void Co 4", "bill-void-4@example.test")
    client_role = await _invite_and_login_as_client(client, admin, "client-bill-void@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor R", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.post(f"/bills/{bill_id}/void", headers=client_role["headers"])
    assert response.status_code == 403


async def test_zero_or_negative_bill_payment_amount_returns_422(client):
    """Mirrors test_invoices.py's own test_zero_or_negative_payment_amount_
    returns_422 — verifies BillPaymentCreateRequest.amount's Field(gt=0)
    (schemas/bill.py) is actually enforced, not just present in the schema."""
    admin = await _register_and_login(client, "Bill Pay Co 4", "bill-pay-4@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Q", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    zero = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "0.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert zero.status_code == 422

    negative = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "-10.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert negative.status_code == 422
