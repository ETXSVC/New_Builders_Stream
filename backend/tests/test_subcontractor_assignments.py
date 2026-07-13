"""Task 3.11: `POST/GET /projects/{project_id}/subcontractor-assignments`.

Covers the Admin-override-required expired-compliance rule
(`create_subcontractor_assignment`, `app/routers/subcontractor_assignments.py`)
and the plain paginated list route. Helper duplication
(`_register_and_login`, `_invite_and_login_as`, `_create_project`,
`_create_subcontractor`, `_upload_compliance_document`,
`_add_membership_directly`, `_create_child_with_membership`) follows the
established per-test-file convention (see test_subcontractors.py's own
module docstring, which cites test_change_orders.py/test_leads.py/
test_projects.py for the same norm) rather than sharing them via
conftest.py.
"""

import json

import asyncpg

from tests.conftest import TEST_DATABASE_URL

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


async def _add_membership_directly(user_id, company_id, role):
    """Test-setup plumbing, identical rationale to
    test_tenant_isolation_phase3.py's own helper of the same name."""
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
    """Identical to test_tenant_isolation_phase3.py's helper of the same
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


def _project_payload(**overrides):
    payload = {
        "name": "Kitchen Remodel",
        "site_address": "123 Main St",
        "projected_start_date": "2026-08-01",
    }
    payload.update(overrides)
    return payload


async def _create_project(client, actor, **overrides):
    response = await client.post(
        "/projects", json=_project_payload(**overrides), headers=actor["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _subcontractor_payload(**overrides):
    payload = {
        "name": "Ace Plumbing Co",
        "trade": "plumbing",
        "contact_email": "contact@aceplumbing.test",
    }
    payload.update(overrides)
    return payload


async def _create_subcontractor(client, actor, **overrides):
    response = await client.post(
        "/subcontractors", json=_subcontractor_payload(**overrides), headers=actor["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload_compliance_document(
    client, actor, subcontractor_id, *, doc_type="insurance_certificate", expires_on="2027-01-01",
    file_name="certificate.pdf", content: bytes = b"pdf-bytes",
):
    response = await client.post(
        f"/subcontractors/{subcontractor_id}/compliance-documents",
        data={"doc_type": doc_type, "expires_on": expires_on},
        files={"file": (file_name, content, "application/octet-stream")},
        headers=actor["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _assign(client, actor, project_id, subcontractor_id, **overrides):
    payload = {"subcontractor_id": subcontractor_id, **overrides}
    return await client.post(
        f"/projects/{project_id}/subcontractor-assignments",
        json=payload,
        headers=actor["headers"],
    )


# =============================================================================
# The four PM/Admin x expired/compliant combinations (design spec Section 5).
# =============================================================================


async def test_project_manager_blocked_with_409_when_subcontractor_has_expired_document(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-pm-expired-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sa-pm-expired-pm@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(client, pm, project_id, subcontractor["id"])
    assert response.status_code == 409, response.text


async def test_project_manager_blocked_with_409_even_if_override_reason_supplied(client):
    """A PM can NEVER override, full stop — supplying an override_reason
    does not change the outcome. This is the core assertion distinguishing
    "PM is blocked" from "PM is blocked only when no reason is given"."""
    admin = await _register_and_login(client, "Acme Construction", "sa-pm-reason-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sa-pm-reason-pm@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(
        client, pm, project_id, subcontractor["id"], override_reason="Emergency, need this crew now"
    )
    assert response.status_code == 409, response.text


async def test_project_manager_can_assign_compliant_subcontractor_no_override_needed(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-pm-ok-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sa-pm-ok-pm@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2099-01-01"
    )

    response = await _assign(client, pm, project_id, subcontractor["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subcontractor_id"] == subcontractor["id"]
    assert body["project_id"] == project_id
    assert body["override_reason"] is None


async def test_admin_can_override_expired_document_with_valid_reason(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-override@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(
        client, admin, project_id, subcontractor["id"], override_reason="Only crew available, verbal renewal confirmed"
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["override_reason"] == "Only crew available, verbal renewal confirmed"
    assert body["company_id"] == admin["company_id"]
    assert body["assigned_by"] == admin["user_id"]


async def test_admin_override_writes_audit_log_entry_verified_via_raw_sql(client):
    """Verifies the audit log entry's CONTENT (action, entity_type,
    metadata, company_id), not just the route's own 201 response — via a
    raw SQL query against the owner connection, same pattern
    test_audit_log.py's own tests use."""
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-audit@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(
        client, admin, project_id, subcontractor["id"], override_reason="Documented verbal override"
    )
    assert response.status_code == 201, response.text
    assignment_id = response.json()["id"]

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT action, entity_type, entity_id, company_id, log_metadata "
            "FROM audit_log WHERE entity_id = $1 AND action = $2",
            assignment_id,
            "subcontractor.assigned_with_expired_docs",
        )
    finally:
        await conn.close()

    assert row is not None, "expected an audit_log row for the override"
    assert row["entity_type"] == "subcontractor_assignment"
    assert str(row["company_id"]) == admin["company_id"]

    metadata = json.loads(row["log_metadata"]) if isinstance(row["log_metadata"], str) else row["log_metadata"]
    assert metadata["reason"] == "Documented verbal override"
    assert metadata["subcontractor_id"] == subcontractor["id"]


async def test_admin_override_with_no_reason_returns_422(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-noreason@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(client, admin, project_id, subcontractor["id"])
    assert response.status_code == 422, response.text


async def test_admin_override_with_empty_string_reason_returns_422(client):
    """Empty string is not the same as omitted at the JSON level, but must
    be rejected identically — a caller who sends "" hasn't stated a reason
    any more than one who sent nothing at all."""
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-blankreason@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(client, admin, project_id, subcontractor["id"], override_reason="")
    assert response.status_code == 422, response.text


async def test_admin_override_with_whitespace_only_reason_returns_422(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-wsreason@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2020-01-01"
    )

    response = await _assign(client, admin, project_id, subcontractor["id"], override_reason="   ")
    assert response.status_code == 422, response.text


async def test_admin_can_assign_compliant_subcontractor_freely(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-ok@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2099-01-01"
    )

    response = await _assign(client, admin, project_id, subcontractor["id"])
    assert response.status_code == 201, response.text
    assert response.json()["override_reason"] is None


async def test_override_reason_ignored_when_subcontractor_is_compliant(client):
    """A supplied override_reason against a COMPLIANT subcontractor is not
    silently persisted — SubcontractorAssignment.override_reason's own
    docstring says it's populated "only when the assignment overrides an
    expired-compliance block"."""
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-ignored@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    await _upload_compliance_document(
        client, admin, subcontractor["id"], expires_on="2099-01-01"
    )

    response = await _assign(
        client, admin, project_id, subcontractor["id"], override_reason="Not actually needed"
    )
    assert response.status_code == 201, response.text
    assert response.json()["override_reason"] is None


# =============================================================================
# Absent documents (zero compliance_documents rows) does not block, either role.
# =============================================================================


async def test_admin_can_assign_subcontractor_with_zero_compliance_documents(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-admin-nodocs@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    # Deliberately no compliance documents uploaded at all.

    response = await _assign(client, admin, project_id, subcontractor["id"])
    assert response.status_code == 201, response.text
    assert response.json()["override_reason"] is None


async def test_project_manager_can_assign_subcontractor_with_zero_compliance_documents(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-pm-nodocs-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sa-pm-nodocs-pm@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    # Deliberately no compliance documents uploaded at all.

    response = await _assign(client, pm, project_id, subcontractor["id"])
    assert response.status_code == 201, response.text
    assert response.json()["override_reason"] is None


# =============================================================================
# RBAC: accountant/field_crew/client cannot create; list access.
# =============================================================================


async def test_accountant_cannot_create_subcontractor_assignment(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-acct-403@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "sa-acct-403-a@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)

    response = await _assign(client, accountant, project_id, subcontractor["id"])
    assert response.status_code == 403


async def test_field_crew_cannot_create_subcontractor_assignment(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "sa-fc-403-fc@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)

    response = await _assign(client, field_crew, project_id, subcontractor["id"])
    assert response.status_code == 403


async def test_client_cannot_create_subcontractor_assignment(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-client-403@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "sa-client-403-c@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)

    response = await _assign(client, client_role, project_id, subcontractor["id"])
    assert response.status_code == 403


async def test_admin_pm_accountant_can_list_subcontractor_assignments(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-list-rbac-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sa-list-rbac-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "sa-list-rbac-acct@acme.test")
    project_id = await _create_project(client, admin)
    subcontractor = await _create_subcontractor(client, admin)
    assign = await _assign(client, admin, project_id, subcontractor["id"])
    assert assign.status_code == 201, assign.text

    for actor in (admin, pm, accountant):
        response = await client.get(
            f"/projects/{project_id}/subcontractor-assignments", headers=actor["headers"]
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1


async def test_field_crew_cannot_list_subcontractor_assignments(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-list-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "sa-list-fc-403-fc@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(
        f"/projects/{project_id}/subcontractor-assignments", headers=field_crew["headers"]
    )
    assert response.status_code == 403


async def test_client_cannot_list_subcontractor_assignments(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-list-client-403@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "sa-list-client-403-c@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(
        f"/projects/{project_id}/subcontractor-assignments", headers=client_role["headers"]
    )
    assert response.status_code == 403


async def test_list_subcontractor_assignments_empty_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-list-empty@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(
        f"/projects/{project_id}/subcontractor-assignments", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


# =============================================================================
# Cross-tenant / nonexistent 404s.
# =============================================================================


async def test_create_subcontractor_assignment_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "sa-cross-project-a@acme.test")
    b = await _register_and_login(client, "Company B", "sa-cross-project-b@acme.test")
    project_id = await _create_project(client, b)
    subcontractor = await _create_subcontractor(client, a)

    response = await _assign(client, a, project_id, subcontractor["id"])
    assert response.status_code == 404


async def test_create_subcontractor_assignment_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-nonexistent-project@acme.test")
    subcontractor = await _create_subcontractor(client, admin)

    response = await _assign(
        client, admin, "00000000-0000-0000-0000-000000000000", subcontractor["id"]
    )
    assert response.status_code == 404


async def test_create_subcontractor_assignment_cross_tenant_subcontractor_returns_404(client):
    a = await _register_and_login(client, "Company A", "sa-cross-sub-a@acme.test")
    b = await _register_and_login(client, "Company B", "sa-cross-sub-b@acme.test")
    project_id = await _create_project(client, a)
    subcontractor = await _create_subcontractor(client, b)

    response = await _assign(client, a, project_id, subcontractor["id"])
    assert response.status_code == 404


async def test_create_subcontractor_assignment_nonexistent_subcontractor_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-nonexistent-sub@acme.test")
    project_id = await _create_project(client, admin)

    response = await _assign(
        client, admin, project_id, "00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404


async def test_list_subcontractor_assignments_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "sa-list-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "sa-list-cross-b@acme.test")
    project_id = await _create_project(client, b)

    response = await client.get(
        f"/projects/{project_id}/subcontractor-assignments", headers=a["headers"]
    )
    assert response.status_code == 404


async def test_list_subcontractor_assignments_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "sa-list-nonexistent@acme.test")

    response = await client.get(
        "/projects/00000000-0000-0000-0000-000000000000/subcontractor-assignments",
        headers=admin["headers"],
    )
    assert response.status_code == 404


# =============================================================================
# company_id sourcing: parent-company session (unswitched headers) assigning
# against a child-branch Project AND a child-branch Subcontractor.
# =============================================================================


async def test_creating_assignment_under_child_branch_project_and_subcontractor_uses_child_company_id(
    client,
):
    """Both the new SubcontractorAssignment row's company_id and the audit
    log entry's company_id must come from project.company_id (the CHILD),
    never current.company_id (the PARENT acting session) — same empirical
    shape as test_tenant_isolation_phase2.py's own
    test_creating_change_order_under_child_branch_project_uses_project_company_id
    and test_tenant_isolation_phase3.py's own
    test_creating_compliance_document_under_child_branch_subcontractor_uses_subcontractor_company_id.

    Both the Project AND the Subcontractor (and its expired
    ComplianceDocument) are created under the CHILD branch (via
    X-Tenant-ID-switched headers, backed by a genuine company_users row).
    The assignment itself is then created using the PARENT's own DEFAULT
    headers — deliberately NOT X-Tenant-ID-switched — so RLS's
    get_all_descendant_ids() grant alone is what makes both the child's
    Project and child's Subcontractor visible/writable to this session,
    which is the only way current.company_id (parent) and
    project.company_id (child) genuinely diverge without an explicit header
    switch.
    """
    parent = await _register_and_login(client, "Parent Co", "sa-parent-co@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}
    child_actor = {"headers": child_headers}

    project_id = await _create_project(client, child_actor)
    subcontractor = await _create_subcontractor(client, child_actor)
    await _upload_compliance_document(
        client, child_actor, subcontractor["id"], expires_on="2020-01-01"
    )
    assert subcontractor["company_id"] == child_id

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child.
    response = await _assign(
        client, parent, project_id, subcontractor["id"], override_reason="Parent-session override"
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_id"] == child_id, (
        "SubcontractorAssignment created against a child-branch Project must "
        "belong to the PROJECT's own company (the child), not the acting "
        f"session's company (the parent) — got {body['company_id']!r}, "
        f"expected child_id={child_id!r}"
    )

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT company_id FROM audit_log WHERE entity_id = $1 AND action = $2",
            body["id"],
            "subcontractor.assigned_with_expired_docs",
        )
    finally:
        await conn.close()

    assert row is not None, "expected an audit_log row for the override"
    assert str(row["company_id"]) == child_id, (
        "audit_log entry for a child-branch override must carry the CHILD's "
        f"company_id, got {row['company_id']!r}, expected child_id={child_id!r}"
    )

    # Read it back via the child's own tenant context to confirm it's
    # genuinely visible there too, not just correctly labeled.
    list_response = await client.get(
        f"/projects/{project_id}/subcontractor-assignments", headers=child_headers
    )
    assert list_response.status_code == 200, list_response.text
    ids = {item["id"] for item in list_response.json()["items"]}
    assert body["id"] in ids
