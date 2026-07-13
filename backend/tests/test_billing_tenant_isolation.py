"""Task 3.22: subscriptions' deliberately non-standard RLS policy (upward
visibility to a session's own root, not downward to descendants — see
migration 0010_billing_schema.py's own module docstring and design spec
Section 1). These tests prove the two things that make this policy
different from every other table's:

1. A CHILD branch session can see its ROOT's subscription row (upward —
   the whole reason this table doesn't reuse the standard policy shape).
2. A SIBLING branch cannot see another sibling's root's subscription
   (there is only one root per tree, so this reduces to: a genuinely
   unrelated company cannot see this company's subscription at all —
   same header-spoofing proof shape every other phase's tenant-isolation
   file already establishes).

New file rather than extending test_tenant_isolation_phase3.py: that
file's own module docstring already establishes the "new file per
phase/module" convention, and this is a distinct module (billing) with a
distinct, non-standard RLS policy shape worth isolating in its own file.

Helper duplication (`_register_and_login`, `_add_membership_directly`,
`_create_child_with_membership`) follows the established per-test-file
convention (test_tenant_isolation_phase3.py's own module docstring)
rather than sharing them via conftest.py.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_email": email,
            "admin_password": "correct horse battery staple",
            "admin_full_name": "Admin",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "correct horse battery staple"})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {
        "headers": {"Authorization": f"Bearer {token}"},
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
    }


async def _add_membership_directly(user_id, company_id, role):
    """Identical helper to every other phase's tenant-isolation file — a
    genuine company_users row via a direct owner-role connection, bypassing
    the invitation-accept flow purely for test setup speed."""
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO company_users (company_id, user_id, role) "
                    "VALUES (:company_id, :user_id, :role)"
                ),
                {"company_id": company_id, "user_id": user_id, "role": role},
            )
    finally:
        await engine.dispose()


async def _create_child_with_membership(client, parent, name, role="admin"):
    create = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": name}, headers=parent["headers"]
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


async def test_child_branch_session_can_see_its_root_companys_subscription(client):
    parent = await _register_and_login(client, "Root Co Sub", "root-sub-admin@bill.test")
    child_id = await _create_child_with_membership(client, parent, "Child Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    response = await client.get("/subscriptions/me", headers=child_headers)

    assert response.status_code == 200, response.text
    # The row's own company_id is the ROOT's id, not the child's — proving
    # this really is upward visibility into the parent's single row, not a
    # child-owned copy.
    assert response.json()["company_id"] == parent["company_id"]


async def test_genuinely_unrelated_company_cannot_see_this_companys_subscription(client):
    owner = await _register_and_login(client, "Owner Co Sub", "owner-sub-admin@bill.test")
    outsider = await _register_and_login(client, "Outsider Co Sub", "outsider-sub-admin@bill.test")

    # Spoofed X-Tenant-ID: outsider's own JWT (their own membership), but
    # claiming owner's company_id — the exact header-spoofing shape every
    # other phase's tenant-isolation file already proves is blocked at the
    # membership-check layer in get_current_user, before any row lookup.
    spoofed_headers = {**outsider["headers"], "X-Tenant-ID": owner["company_id"]}

    response = await client.get("/subscriptions/me", headers=spoofed_headers)

    assert response.status_code == 403
