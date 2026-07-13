"""Task 3.10: `GET /compliance/notifications`, `POST
/compliance/notifications/{id}/dismiss`.

New file, not an extension of test_compliance_dashboard.py: that file
(283 lines going into this task) already covers a distinct route
(`GET /compliance/dashboard`) with its own RBAC tuple (`_READ_ROLES` — three
roles); these two notification routes are governed by a stricter,
admin-only tuple (`_NOTIFICATION_ROLES`) and need their own helper for
generating real `ComplianceNotification` rows. Splitting by route group
mirrors this file's own precedent — test_compliance_dashboard.py itself is a
route-group-scoped file, not a shared "all compliance tests" file — same
"split when a file gets large enough to warrant it" judgment
test_tenant_isolation_phase1/2/3.py already established, applied here at the
route-group granularity instead.

Helper duplication (`_register_and_login`, `_invite_and_login_as`,
`_create_subcontractor`, `_upload_compliance_document`) follows the
established per-test-file convention (see test_compliance_dashboard.py,
test_subcontractors.py, test_compliance_expiry_task.py) rather than sharing
them via conftest.py.

Notifications are generated via a REAL run of `_check_compliance_expiry`
(Task 3.8), not direct-inserted `ComplianceNotification` rows — same
owner-session-engine pattern test_compliance_expiry_task.py already
establishes, reused here rather than reinvented, so these tests exercise the
actual notification rows the background job produces rather than
hand-crafted stand-ins that might drift from the job's real shape.
"""

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.tasks.compliance_expiry import _check_compliance_expiry
from tests.conftest import TEST_DATABASE_URL


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
    response = await client.post("/subcontractors", json=payload, headers=actor["headers"])
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload_compliance_document(
    client, actor, subcontractor_id, *, doc_type="insurance_certificate", expires_on,
    file_name="certificate.pdf", content: bytes = b"pdf-bytes",
):
    response = await client.post(
        f"/subcontractors/{subcontractor_id}/compliance-documents",
        data={"doc_type": doc_type, "expires_on": expires_on},
        files={"file": (file_name, content, "application/octet-stream")},
        headers=actor["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _seed_notifications(client, admin, *, days_out=5, name="Ace Plumbing Co"):
    """Creates a subcontractor + compliance document `days_out` days from
    expiry, then runs the real background job so real `ComplianceNotification`
    rows exist. 5 days out fires all three thresholds at once (per
    test_compliance_expiry_task.py's own `test_document_5_days_out_fires_all_three_thresholds_simultaneously`).
    Returns `(subcontractor_id, document_id)`.

    Uses a FRESH, single-use owner-role engine for the `_check_compliance_expiry`
    call, created and disposed within this one call, rather than a shared
    module-level pooled engine reused across this file's 16 tests (some of
    which call this helper twice — see
    test_notifications_list_is_scoped_to_callers_own_company). A pooled engine
    shared across the whole file means a connection checked back into that
    pool sits there, idle, until the file's own autouse disposal fixture runs
    at test end — by which point this same test has already gone on to make
    MORE requests (via `client`, a completely separate `app_user`-role pool)
    against the exact rows `_check_compliance_expiry` just wrote. Scoping the
    owner engine to the lifetime of a single call removes that window
    entirely: the connection is opened, used, committed, and physically
    closed before this function returns, so nothing from it can still be
    outstanding by the time the caller's next `client` request — or the next
    fixture's `TRUNCATE` in conftest.py's `_clean_tables` — runs."""
    subcontractor_id = await _create_subcontractor(client, admin, name=name)
    expires_on = (date.today() + timedelta(days=days_out)).isoformat()
    document_id = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )

    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    owner_session_factory = async_sessionmaker(
        owner_engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        await _check_compliance_expiry(session_factory=owner_session_factory)
    finally:
        await owner_engine.dispose()

    return subcontractor_id, document_id


# =============================================================================
# GET /compliance/notifications — RBAC
# =============================================================================


async def test_admin_can_list_notifications_with_full_display_context(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-admin@acme.test")
    subcontractor_id, document_id = await _seed_notifications(client, admin, name="Full Context Sub")

    response = await client.get("/compliance/notifications", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"]
    assert len(items) == 3  # 5 days out fires 30_day, 14_day, and 7_day at once
    thresholds = {item["threshold"] for item in items}
    assert thresholds == {"30_day", "14_day", "7_day"}
    for item in items:
        assert item["compliance_document_id"] == document_id
        assert item["subcontractor_name"] == "Full Context Sub"
        assert item["doc_type"] == "insurance_certificate"
        assert item["fired_at"] is not None
        assert item["read_at"] is None


async def test_project_manager_cannot_list_notifications(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-pm-403@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "notif-pm-403-pm@acme.test")

    response = await client.get("/compliance/notifications", headers=pm["headers"])
    assert response.status_code == 403


async def test_accountant_cannot_list_notifications(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-acct-403@acme.test")
    accountant = await _invite_and_login_as(
        client, admin, "accountant", "notif-acct-403-acct@acme.test"
    )

    response = await client.get("/compliance/notifications", headers=accountant["headers"])
    assert response.status_code == 403


async def test_field_crew_cannot_list_notifications(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-fc-403@acme.test")
    field_crew = await _invite_and_login_as(
        client, admin, "field_crew", "notif-fc-403-fc@acme.test"
    )

    response = await client.get("/compliance/notifications", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_client_cannot_list_notifications(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-client-403@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "notif-client-403-c@acme.test"
    )

    response = await client.get("/compliance/notifications", headers=client_role["headers"])
    assert response.status_code == 403


async def test_notifications_list_is_scoped_to_callers_own_company(client):
    a = await _register_and_login(client, "Company A", "notif-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "notif-cross-b@acme.test")

    await _seed_notifications(client, a, name="Company A Sub")
    await _seed_notifications(client, b, name="Company B Sub")

    response_a = await client.get("/compliance/notifications", headers=a["headers"])
    assert response_a.status_code == 200, response_a.text
    items_a = response_a.json()["items"]
    assert len(items_a) == 3
    assert all(item["subcontractor_name"] == "Company A Sub" for item in items_a)

    response_b = await client.get("/compliance/notifications", headers=b["headers"])
    assert response_b.status_code == 200, response_b.text
    items_b = response_b.json()["items"]
    assert len(items_b) == 3
    assert all(item["subcontractor_name"] == "Company B Sub" for item in items_b)


async def test_notifications_list_empty_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-empty@acme.test")

    response = await client.get("/compliance/notifications", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


# =============================================================================
# GET /compliance/notifications?unread_only=true
# =============================================================================


async def test_unread_only_filters_out_dismissed_notifications(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-unread@acme.test")
    await _seed_notifications(client, admin, name="Unread Filter Sub")

    all_response = await client.get("/compliance/notifications", headers=admin["headers"])
    assert all_response.status_code == 200, all_response.text
    all_items = all_response.json()["items"]
    assert len(all_items) == 3

    # Dismiss one of the three.
    dismiss = await client.post(
        f"/compliance/notifications/{all_items[0]['id']}/dismiss", headers=admin["headers"]
    )
    assert dismiss.status_code == 200, dismiss.text

    unread_response = await client.get(
        "/compliance/notifications", params={"unread_only": "true"}, headers=admin["headers"]
    )
    assert unread_response.status_code == 200, unread_response.text
    unread_items = unread_response.json()["items"]
    assert len(unread_items) == 2
    assert all(item["read_at"] is None for item in unread_items)
    assert all_items[0]["id"] not in {item["id"] for item in unread_items}

    # Without the filter, all three (one now read) are still returned.
    default_response = await client.get("/compliance/notifications", headers=admin["headers"])
    assert default_response.status_code == 200, default_response.text
    assert len(default_response.json()["items"]) == 3


# =============================================================================
# POST /compliance/notifications/{id}/dismiss
# =============================================================================


async def test_dismiss_notification_sets_read_at(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-dismiss@acme.test")
    await _seed_notifications(client, admin, name="Dismiss Sub")

    listing = await client.get("/compliance/notifications", headers=admin["headers"])
    notification_id = listing.json()["items"][0]["id"]

    response = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == notification_id
    assert body["read_at"] is not None


async def test_dismiss_notification_is_idempotent_on_double_dismiss(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-idempotent@acme.test")
    await _seed_notifications(client, admin, name="Idempotent Sub")

    listing = await client.get("/compliance/notifications", headers=admin["headers"])
    notification_id = listing.json()["items"][0]["id"]

    first = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=admin["headers"]
    )
    assert first.status_code == 200, first.text
    first_read_at = first.json()["read_at"]
    assert first_read_at is not None

    second = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=admin["headers"]
    )
    assert second.status_code == 200, second.text
    second_read_at = second.json()["read_at"]

    # A 200 no-op, not a 409 — and `read_at` is not bumped to a later
    # timestamp on the repeat call.
    assert second_read_at == first_read_at


async def test_dismiss_notification_cross_tenant_returns_404(client):
    a = await _register_and_login(client, "Company A", "notif-dismiss-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "notif-dismiss-cross-b@acme.test")

    await _seed_notifications(client, a, name="Company A Dismiss Sub")

    listing_a = await client.get("/compliance/notifications", headers=a["headers"])
    notification_id = listing_a.json()["items"][0]["id"]

    response = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=b["headers"]
    )
    assert response.status_code == 404


async def test_dismiss_notification_nonexistent_id_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-dismiss-404@acme.test")

    response = await client.post(
        "/compliance/notifications/00000000-0000-0000-0000-000000000000/dismiss",
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_project_manager_cannot_dismiss_notification(client):
    admin = await _register_and_login(client, "Acme Construction", "notif-dismiss-pm-403@acme.test")
    pm = await _invite_and_login_as(
        client, admin, "project_manager", "notif-dismiss-pm-403-pm@acme.test"
    )
    await _seed_notifications(client, admin, name="PM Dismiss Sub")
    listing = await client.get("/compliance/notifications", headers=admin["headers"])
    notification_id = listing.json()["items"][0]["id"]

    response = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=pm["headers"]
    )
    assert response.status_code == 403


async def test_accountant_cannot_dismiss_notification(client):
    admin = await _register_and_login(
        client, "Acme Construction", "notif-dismiss-acct-403@acme.test"
    )
    accountant = await _invite_and_login_as(
        client, admin, "accountant", "notif-dismiss-acct-403-acct@acme.test"
    )
    await _seed_notifications(client, admin, name="Accountant Dismiss Sub")
    listing = await client.get("/compliance/notifications", headers=admin["headers"])
    notification_id = listing.json()["items"][0]["id"]

    response = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=accountant["headers"]
    )
    assert response.status_code == 403


async def test_field_crew_cannot_dismiss_notification(client):
    admin = await _register_and_login(
        client, "Acme Construction", "notif-dismiss-fc-403@acme.test"
    )
    field_crew = await _invite_and_login_as(
        client, admin, "field_crew", "notif-dismiss-fc-403-fc@acme.test"
    )
    await _seed_notifications(client, admin, name="Field Crew Dismiss Sub")
    listing = await client.get("/compliance/notifications", headers=admin["headers"])
    notification_id = listing.json()["items"][0]["id"]

    response = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=field_crew["headers"]
    )
    assert response.status_code == 403


async def test_client_cannot_dismiss_notification(client):
    admin = await _register_and_login(
        client, "Acme Construction", "notif-dismiss-client-403@acme.test"
    )
    client_role = await _invite_and_login_as(
        client, admin, "client", "notif-dismiss-client-403-c@acme.test"
    )
    await _seed_notifications(client, admin, name="Client Dismiss Sub")
    listing = await client.get("/compliance/notifications", headers=admin["headers"])
    notification_id = listing.json()["items"][0]["id"]

    response = await client.post(
        f"/compliance/notifications/{notification_id}/dismiss", headers=client_role["headers"]
    )
    assert response.status_code == 403
