"""Task 3.6: `GET /compliance/dashboard`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`) follows
the established per-test-file convention (see test_subcontractors.py,
test_change_orders.py, test_leads.py, test_projects.py) rather than sharing
them via conftest.py.
"""

from datetime import date, timedelta


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
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def _invite_and_login_as(client, admin, role, email):
    invite = await client.post(
        "/invitations",
        json={"email": email, "role": role},
        headers=admin["headers"],
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


async def _create_subcontractor(client, actor, **overrides):
    payload = {
        "name": "Ace Plumbing Co",
        "trade": "plumbing",
        "contact_email": "contact@aceplumbing.test",
    }
    payload.update(overrides)
    return await client.post("/subcontractors", json=payload, headers=actor["headers"])


async def _upload_compliance_document(
    client, actor, subcontractor_id, *, doc_type="insurance_certificate", expires_on,
    file_name="certificate.pdf", content: bytes = b"pdf-bytes",
):
    return await client.post(
        f"/subcontractors/{subcontractor_id}/compliance-documents",
        data={"doc_type": doc_type, "expires_on": expires_on},
        files={"file": (file_name, content, "application/octet-stream")},
        headers=actor["headers"],
    )


async def test_expiring_soon_document_appears_with_correct_status(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-expsoon@acme.test")
    create = await _create_subcontractor(client, admin, name="Expiring Soon Sub")
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    expires_on = (date.today() + timedelta(days=25)).isoformat()
    upload = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]

    response = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    entry = items[0]
    assert entry["compliance_document_id"] == document_id
    assert entry["subcontractor_id"] == subcontractor_id
    assert entry["subcontractor_name"] == "Expiring Soon Sub"
    assert entry["doc_type"] == "insurance_certificate"
    assert entry["expires_on"] == expires_on
    assert entry["status"] == "expiring_soon"


async def test_expired_document_appears_with_correct_status(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-expired@acme.test")
    create = await _create_subcontractor(client, admin, name="Expired Sub")
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    expires_on = (date.today() - timedelta(days=5)).isoformat()
    upload = await _upload_compliance_document(
        client, admin, subcontractor_id, doc_type="license", expires_on=expires_on
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]

    response = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    entry = items[0]
    assert entry["compliance_document_id"] == document_id
    assert entry["subcontractor_id"] == subcontractor_id
    assert entry["subcontractor_name"] == "Expired Sub"
    assert entry["doc_type"] == "license"
    assert entry["expires_on"] == expires_on
    assert entry["status"] == "expired"


async def test_document_expiring_far_in_future_does_not_appear(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-farfuture@acme.test")
    create = await _create_subcontractor(client, admin, name="Far Future Sub")
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    expires_on = (date.today() + timedelta(days=60)).isoformat()
    upload = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )
    assert upload.status_code == 201, upload.text

    response = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


async def test_dashboard_mixes_expiring_soon_expired_and_omits_far_future_in_one_query(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-mixed@acme.test")
    create = await _create_subcontractor(client, admin, name="Mixed Sub")
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    expiring_soon_on = (date.today() + timedelta(days=10)).isoformat()
    expired_on = (date.today() - timedelta(days=1)).isoformat()
    far_future_on = (date.today() + timedelta(days=365)).isoformat()

    for expires_on, file_name in (
        (expiring_soon_on, "soon.pdf"),
        (expired_on, "expired.pdf"),
        (far_future_on, "future.pdf"),
    ):
        upload = await _upload_compliance_document(
            client, admin, subcontractor_id, expires_on=expires_on, file_name=file_name
        )
        assert upload.status_code == 201, upload.text

    response = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 2
    statuses_by_expiry = {item["expires_on"]: item["status"] for item in items}
    assert statuses_by_expiry == {
        expiring_soon_on: "expiring_soon",
        expired_on: "expired",
    }


async def test_dashboard_boundary_days_produce_correct_status(client):
    # Spec-compliance review of Task 3.6 verified these boundaries manually
    # (via a throwaway script, since deleted) and found the implementation
    # correct, but flagged that the permanent suite didn't pin them down —
    # a future off-by-one in the `<=`/`<` comparisons wouldn't be caught.
    # Locks in: exactly 30 days out (the inclusive edge of the "expiring
    # soon" window) is still "expiring_soon"; exactly 31 days out is absent
    # entirely; exactly today is "expiring_soon", not "expired"; exactly 1
    # day overdue is "expired".
    admin = await _register_and_login(client, "Acme Construction", "dash-boundary@acme.test")
    create = await _create_subcontractor(client, admin, name="Boundary Sub")
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    thirty_days_out = (date.today() + timedelta(days=30)).isoformat()
    thirty_one_days_out = (date.today() + timedelta(days=31)).isoformat()
    exactly_today = date.today().isoformat()
    one_day_overdue = (date.today() - timedelta(days=1)).isoformat()

    for expires_on, file_name in (
        (thirty_days_out, "thirty.pdf"),
        (thirty_one_days_out, "thirtyone.pdf"),
        (exactly_today, "today.pdf"),
        (one_day_overdue, "overdue.pdf"),
    ):
        upload = await _upload_compliance_document(
            client, admin, subcontractor_id, expires_on=expires_on, file_name=file_name
        )
        assert upload.status_code == 201, upload.text

    response = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert response.status_code == 200, response.text
    statuses_by_expiry = {item["expires_on"]: item["status"] for item in response.json()["items"]}
    assert statuses_by_expiry == {
        thirty_days_out: "expiring_soon",
        exactly_today: "expiring_soon",
        one_day_overdue: "expired",
    }
    assert thirty_one_days_out not in statuses_by_expiry


async def test_field_crew_cannot_view_compliance_dashboard(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "dash-fc-403-fc@acme.test")

    response = await client.get("/compliance/dashboard", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_client_cannot_view_compliance_dashboard(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-client-403@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "dash-client-403-c@acme.test")

    response = await client.get("/compliance/dashboard", headers=client_role["headers"])
    assert response.status_code == 403


async def test_admin_pm_accountant_can_view_compliance_dashboard(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-rbac@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "dash-rbac-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "dash-rbac-acct@acme.test")
    create = await _create_subcontractor(client, admin)
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]
    expires_on = (date.today() + timedelta(days=5)).isoformat()
    upload = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )
    assert upload.status_code == 201, upload.text

    for actor in (admin, pm, accountant):
        response = await client.get("/compliance/dashboard", headers=actor["headers"])
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1


async def test_compliance_dashboard_is_scoped_to_callers_own_company(client):
    a = await _register_and_login(client, "Company A", "dash-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "dash-cross-b@acme.test")

    create_a = await _create_subcontractor(client, a, name="Company A Sub")
    assert create_a.status_code == 201, create_a.text
    create_b = await _create_subcontractor(client, b, name="Company B Sub")
    assert create_b.status_code == 201, create_b.text

    expires_on = (date.today() + timedelta(days=5)).isoformat()
    upload_a = await _upload_compliance_document(
        client, a, create_a.json()["id"], expires_on=expires_on
    )
    assert upload_a.status_code == 201, upload_a.text
    upload_b = await _upload_compliance_document(
        client, b, create_b.json()["id"], expires_on=expires_on
    )
    assert upload_b.status_code == 201, upload_b.text

    response_a = await client.get("/compliance/dashboard", headers=a["headers"])
    assert response_a.status_code == 200, response_a.text
    items_a = response_a.json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["subcontractor_name"] == "Company A Sub"

    response_b = await client.get("/compliance/dashboard", headers=b["headers"])
    assert response_b.status_code == 200, response_b.text
    items_b = response_b.json()["items"]
    assert len(items_b) == 1
    assert items_b[0]["subcontractor_name"] == "Company B Sub"


async def test_dashboard_empty_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "dash-empty@acme.test")

    response = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}
