"""Regression sweep: a malformed UUID in an id-keyed path parameter must
422, not 500 or 404, for every module — not just the one place this was
previously exercised (`test_tenant_isolation.py`'s own
`test_malformed_company_id_path_param_is_rejected`, for `/companies/{id}`).

FastAPI/Pydantic auto-422s a path parameter typed `uuid.UUID` when the raw
string fails to parse, before the route handler body ever runs — this is
framework behavior, not application logic, so these tests don't exercise
anything module-specific. They exist because each module previously relied
on that untested generic behavior rather than a per-route assertion: a
custom UUID-handling regression in any one router (e.g. a path param
mistakenly typed `str` instead of `uuid.UUID`, which would let a malformed
value reach the handler body and surface as an unhandled 500 the first
time it's used in a query) wouldn't have been caught locally. One
representative GET-by-id route per module below, not an exhaustive sweep
of every id-keyed route in the app — the failure mode this guards against
is the SAME framework-level behavior regardless of which route exercises
it, so one hit per module is enough to catch a route whose id param was
typed wrong.
"""


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
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def test_malformed_lead_id_path_param_is_rejected(client):
    admin = await _register_and_login(client, "Malformed UUID Co 1", "malformed-lead@acme.test")
    response = await client.get("/leads/not-a-uuid", headers=admin["headers"])
    assert response.status_code == 422


async def test_malformed_project_id_path_param_is_rejected(client):
    admin = await _register_and_login(client, "Malformed UUID Co 2", "malformed-project@acme.test")
    response = await client.get("/projects/not-a-uuid", headers=admin["headers"])
    assert response.status_code == 422


async def test_malformed_estimate_id_path_param_is_rejected(client):
    admin = await _register_and_login(client, "Malformed UUID Co 3", "malformed-estimate@acme.test")
    response = await client.get("/estimates/not-a-uuid", headers=admin["headers"])
    assert response.status_code == 422


async def test_malformed_invoice_id_path_param_is_rejected(client):
    admin = await _register_and_login(client, "Malformed UUID Co 4", "malformed-invoice@acme.test")
    response = await client.get("/invoices/not-a-uuid", headers=admin["headers"])
    assert response.status_code == 422


async def test_malformed_bill_id_path_param_is_rejected(client):
    admin = await _register_and_login(client, "Malformed UUID Co 5", "malformed-bill@acme.test")
    response = await client.get("/bills/not-a-uuid", headers=admin["headers"])
    assert response.status_code == 422


async def test_malformed_subcontractor_id_path_param_is_rejected(client):
    admin = await _register_and_login(client, "Malformed UUID Co 6", "malformed-subcontractor@acme.test")
    response = await client.get("/subcontractors/not-a-uuid", headers=admin["headers"])
    assert response.status_code == 422
