import asyncpg

from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL


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
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def test_registration_writes_an_audit_log_entry(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-admin@acme.test")

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT action, entity_type FROM audit_log WHERE company_id = $1", admin["company_id"]
        )
    finally:
        await conn.close()

    actions = {row["action"] for row in rows}
    assert "company.registered" in actions


async def test_invitation_actions_are_audited(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-admin2@acme.test")
    await client.post(
        "/invitations",
        json={"email": "audited-invite@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT action FROM audit_log WHERE company_id = $1", admin["company_id"]
        )
    finally:
        await conn.close()

    actions = {row["action"] for row in rows}
    assert "invitation.created" in actions


# The task description names three call sites (register, child-company creation,
# invitation created/accepted) but the spec's own Step 1 code block above only
# exercises two of them (company.registered, invitation.created). The two tests
# below close that gap cheaply — they reuse the exact same helper and query
# pattern, so they cost little extra and directly strengthen the Phase 0 exit
# criterion ("Audit log table and a working write path") this task is meant to
# satisfy, rather than just covering two of the three known call sites.
async def test_child_company_creation_is_audited(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-admin3@acme.test")

    await client.post(
        f"/companies/{admin['company_id']}/children",
        json={"name": "Acme Construction - Denver"},
        headers=admin["headers"],
    )

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT action, entity_type FROM audit_log WHERE company_id = $1", admin["company_id"]
        )
    finally:
        await conn.close()

    actions = {row["action"] for row in rows}
    assert "company.child_created" in actions


async def test_invitation_acceptance_is_audited(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-admin4@acme.test")
    invite = await client.post(
        "/invitations",
        json={"email": "audited-accept@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )
    invitation_id = invite.json()["id"]

    await client.post(
        f"/invitations/{invitation_id}/accept",
        json={"full_name": "Audited Hire", "password": "anothersecret123"},
    )

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        # invitation.accepted's actor_id is the newly-created invitee, not the
        # inviting admin, but the write path still scopes the row under the
        # inviting company_id (see invitations.py's accept_invitation), so this
        # query is consistent with the other tests in this file.
        rows = await conn.fetch(
            "SELECT action FROM audit_log WHERE company_id = $1", admin["company_id"]
        )
    finally:
        await conn.close()

    actions = {row["action"] for row in rows}
    assert "invitation.accepted" in actions


async def test_audit_log_entries_are_tenant_scoped_under_rls(client):
    """Every test above queries audit_log via the RLS-bypassing owner
    connection — proving entries get written, but not that they're actually
    tenant-scoped, which is half of this task's own stated goal ("queryable
    and tenant-scoped"). This connects as app_user with app.current_tenant
    set to confirm the tenant_isolation policy (migration 0001) actually
    blocks a company from seeing another company's audit trail."""
    a = await _register_and_login(client, "Company A", "audit-tenant-a@acme.test")
    b = await _register_and_login(client, "Company B", "audit-tenant-b@acme.test")

    conn = await asyncpg.connect(TEST_APP_DATABASE_URL.replace("+asyncpg", ""))
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_tenant', $1, true)", a["company_id"])
            visible = await conn.fetch("SELECT company_id FROM audit_log")
    finally:
        await conn.close()

    visible_company_ids = {str(row["company_id"]) for row in visible}
    assert a["company_id"] in visible_company_ids
    assert b["company_id"] not in visible_company_ids
