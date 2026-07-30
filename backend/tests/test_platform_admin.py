"""The platform console (migration 0023), asserted rather than assumed.

This feature adds a trust tier above every tenant, so most of what is worth
testing is what it must NOT permit. Four properties carry the design:

  1. **Scope separation.** A platform token is useless on the product API
     and a product token is useless on `/platform`. Both directions are
     tested, because only testing one leaves the interesting half open.
  2. **No route can grant the privilege.** Asserted against Postgres's own
     ACLs rather than by trying every endpoint: `app_user` and `scanner`
     must hold no INSERT/UPDATE/DELETE on `platform_admins`, so escalation
     is impossible by construction and stays impossible when someone adds
     a route this file has never heard of.
  3. **Overrides actually gate.** A `pro` module reached by a `starter`
     tenant because an override says so, and a module refused to an
     `enterprise` tenant because an override says not to. The second
     direction is the one a naive implementation gets wrong.
  4. **A manual status survives Stripe.** The webhook is last-write-wins;
     without the override flag an operator's decision silently reverts.

The catalog-driven checks follow the house pattern (test_rls_policy_coverage,
test_worker_db_roles): assert against what the database actually reports,
not against a transcribed list.
"""
import uuid

import asyncpg
import pyotp
import pytest

from app.core.security import create_platform_token
from app.services.token_encryption import decrypt_token
from tests.conftest import TEST_DATABASE_URL, register_and_login

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

PASSWORD = "supersecret123"

# The estimation module is `pro`, so a `starter` tenant is blocked from this
# route by tier alone -- which makes it the cheapest observable proof that a
# gate changed its mind.
_ESTIMATION_WRITE = "/markup-profiles"
_ESTIMATION_BODY = {"name": "Standard", "overhead_pct": "10", "profit_pct": "15"}


async def _owner_conn():
    return await asyncpg.connect(OWNER_DSN)


async def _grant_platform_admin(user_id) -> None:
    """What scripts/grant_platform_admin.py does, as the owner role -- which
    is the only role that can, and the point of the design."""
    conn = await _owner_conn()
    try:
        await conn.execute(
            "INSERT INTO platform_admins (user_id) VALUES ($1) "
            "ON CONFLICT (user_id) DO UPDATE SET revoked_at = NULL",
            uuid.UUID(str(user_id)),
        )
    finally:
        await conn.close()


async def _totp_code(user_id, *, step_offset: int = 0) -> str:
    """A TOTP code for the current 30s timestep, or a later one.

    `step_offset` is not a convenience: `verify_totp_code`'s replay guard
    refuses any timestep at or BEFORE the last one used successfully, so a
    test that activates enrolment and then immediately logs in cannot reuse
    the same code -- correctly, since that is precisely the interception it
    exists to stop. Activation uses the current step and login uses the
    next, which `verify_totp_code` still accepts (it allows one step of
    skew either side) while satisfying the guard.
    """
    import time

    conn = await _owner_conn()
    try:
        encrypted = await conn.fetchval(
            "SELECT totp_secret_encrypted FROM users WHERE id = $1", uuid.UUID(str(user_id))
        )
    finally:
        await conn.close()
    totp = pyotp.TOTP(decrypt_token(encrypted), interval=30)
    return totp.at(time.time() + step_offset * 30)


async def _platform_operator(client, email="ops@platform.example") -> dict:
    """A registered user, granted platform admin, with MFA enrolled and
    activated through the real endpoints -- not by writing the columns
    directly, so the enrolment flow is covered by every test that needs a
    signed-in operator."""
    actor = await register_and_login(client, "Ops Co", email)
    await _grant_platform_admin(actor["user_id"])

    enroll = await client.post(
        "/platform/auth/mfa/enroll", json={"email": email, "password": PASSWORD}
    )
    assert enroll.status_code == 200, enroll.text

    activate = await client.post(
        "/platform/auth/mfa/activate",
        json={
            "email": email,
            "password": PASSWORD,
            "totp_code": await _totp_code(actor["user_id"]),
        },
    )
    assert activate.status_code == 204, activate.text

    actor["platform_headers"] = {
        "Authorization": f"Bearer {create_platform_token(str(actor['user_id']))}"
    }
    return actor


def _signed_subscription_updated(stripe_subscription_id: str):
    """A validly-signed customer.subscription.updated moving status to
    'active' -- i.e. exactly the routine event that would silently undo a
    manual 'past_due'.

    Signed with the app's own header builder rather than a hand-rolled
    HMAC, so the signer and the verifier cannot drift into agreeing with
    each other about a format neither shares with Stripe (that function's
    own docstring makes the same point)."""
    import json
    import time

    from app.config import settings
    from app.services.stripe_client import build_stripe_signature_header

    body = json.dumps(
        {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": stripe_subscription_id, "status": "active"}},
        }
    ).encode()
    header = build_stripe_signature_header(
        payload=body, secret=settings.stripe_webhook_secret, timestamp=int(time.time())
    )
    return body, header


async def _stripe_subscription_id(company_id) -> str:
    conn = await _owner_conn()
    try:
        return await conn.fetchval(
            "SELECT stripe_subscription_id FROM subscriptions WHERE company_id = $1",
            uuid.UUID(str(company_id)),
        )
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# 1. Scope separation
# --------------------------------------------------------------------------


async def test_a_product_token_cannot_reach_the_platform_console(client):
    """The tenant admin's own token is maximally privileged INSIDE their
    company. That must buy them nothing here."""
    admin = await register_and_login(client, "Acme", "admin@acme.test")

    response = await client.get("/platform/companies", headers=admin["headers"])

    assert response.status_code == 401, response.text


async def test_a_platform_token_cannot_reach_the_product_api(client):
    """The reverse, which is the half a scope check usually forgets. A
    platform token names no tenant and must not authenticate a tenant
    request even for a user who legitimately has a company."""
    actor = await _platform_operator(client)

    response = await client.get("/leads", headers=actor["platform_headers"])

    assert response.status_code == 401, response.text


async def test_platform_admin_status_alone_does_not_elevate_a_product_token(client):
    """Being a platform admin must not quietly widen the ORDINARY session.
    The privilege attaches to the token's scope, not to the person."""
    actor = await _platform_operator(client)
    other = await register_and_login(client, "Other Co", "other@other.test")

    # The operator's normal token, aimed at a company they don't belong to.
    response = await client.get(
        "/leads", headers={**actor["headers"], "X-Tenant-ID": other["company_id"]}
    )

    assert response.status_code == 403, response.text


async def test_a_revoked_platform_admin_is_refused_on_the_next_request(client):
    """Revocation is checked per request, not baked into the token, so it
    takes effect within one request rather than one token lifetime."""
    actor = await _platform_operator(client)
    before = await client.get("/platform/companies", headers=actor["platform_headers"])
    assert before.status_code == 200, before.text

    conn = await _owner_conn()
    try:
        await conn.execute(
            "UPDATE platform_admins SET revoked_at = now() WHERE user_id = $1",
            uuid.UUID(str(actor["user_id"])),
        )
    finally:
        await conn.close()

    response = await client.get("/platform/companies", headers=actor["platform_headers"])
    assert response.status_code == 403, response.text


async def test_a_platform_token_for_a_non_admin_is_refused(client):
    """Holding the scope claim is not enough -- the grant is checked in the
    database on every request. Minting the token directly here stands in for
    an attacker who has somehow obtained one; the point is that the claim
    alone is not authorization."""
    victim = await register_and_login(client, "Acme", "admin@acme.test")
    headers = {"Authorization": f"Bearer {create_platform_token(str(victim['user_id']))}"}

    response = await client.get("/platform/companies", headers=headers)

    assert response.status_code == 403, response.text


# --------------------------------------------------------------------------
# 2. The privilege cannot be granted through the app
# --------------------------------------------------------------------------


async def test_no_runtime_role_can_write_the_platform_admins_table():
    """The escalation guard, asserted against Postgres's own ACLs.

    `app_user` is the request path and `scanner` is the worker. Neither may
    hold any write on this table, so no route and no background job can
    grant, forge or un-revoke platform administration -- including routes
    that do not exist yet. Migration 0001 and migration 0020 each leave a
    standing ALTER DEFAULT PRIVILEGES granting full DML on every new table,
    which is exactly why this needs asserting rather than assuming: the
    second of those was missed on the first draft of migration 0023 and
    handed the worker's role write access to this table.
    """
    conn = await _owner_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT grantee, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_name = 'platform_admins'
              AND grantee IN ('app_user', 'scanner')
              AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
            """
        )
    finally:
        await conn.close()

    assert rows == [], (
        "a runtime role can write platform_admins, which makes privilege "
        "escalation into the platform tier reachable from application code: "
        f"{[(r['grantee'], r['privilege_type']) for r in rows]!r}"
    )


async def test_no_runtime_role_can_write_module_overrides():
    """Same guard for entitlements: a tenant must not be able to grant
    itself a module the operator did not."""
    conn = await _owner_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT grantee, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_name = 'company_module_overrides'
              AND grantee IN ('app_user', 'scanner')
              AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
            """
        )
    finally:
        await conn.close()

    assert rows == [], f"a runtime role can write company_module_overrides: {rows!r}"


async def test_the_platform_role_cannot_write_tenant_business_data():
    """The console's own blast radius. `platform_admin` is BYPASSRLS, so the
    only thing between a console bug and a customer's records is the grant
    list -- assert it holds writes on nothing but the tables it is supposed
    to.

    Migration 0024 widened this set from three tables to six, and the three
    it added are exactly what creating a tenant writes: the company, the
    owner user, and the membership joining them. `projects`, `estimates`,
    `invoices`, `documents` and every other record a customer would mind
    losing remain unwritable, which is the property this test exists for --
    not the count."""
    conn = await _owner_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT table_name
            FROM information_schema.role_table_grants
            WHERE grantee = 'platform_admin'
              AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
            ORDER BY table_name
            """
        )
    finally:
        await conn.close()

    writable = {row["table_name"] for row in rows}
    assert writable == {
        "audit_log",
        "companies",
        "company_module_overrides",
        "company_users",
        "subscriptions",
        "users",
    }, (
        "the platform console's database role can write tables outside its "
        f"remit: {sorted(writable)!r}"
    )


async def test_the_platform_role_can_delete_nothing_but_module_overrides():
    """The invariant migration 0024 had to preserve while granting writes.

    A console that can UPDATE is a console that can make a mess; a console
    that can DELETE is one that can lose a customer's data outright. So the
    tenant "delete" is a soft one -- `companies.deleted_at` via UPDATE --
    and no DELETE privilege is held anywhere except clearing a module
    override, which is how that feature's third state is expressed and
    destroys nothing.

    Separated from the test above because it is a different claim: that one
    bounds WHICH tables are reachable, this one bounds WHAT may be done to
    them. A future migration granting DELETE on `companies` would keep the
    set above unchanged and still be the exact mistake worth catching."""
    conn = await _owner_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT table_name
            FROM information_schema.role_table_grants
            WHERE grantee = 'platform_admin' AND privilege_type = 'DELETE'
            ORDER BY table_name
            """
        )
    finally:
        await conn.close()

    deletable = {row["table_name"] for row in rows}
    assert deletable == {"company_module_overrides"}, (
        "the platform console's database role can DELETE rows outside "
        f"module overrides: {sorted(deletable)!r}"
    )


# --------------------------------------------------------------------------
# 3. Sign-in requires a second factor
# --------------------------------------------------------------------------


async def test_login_is_refused_without_enrolled_mfa(client):
    """A platform admin who has not enrolled cannot sign in at all -- the
    difference from /auth/login, where MFA is optional."""
    actor = await register_and_login(client, "Ops Co", "ops@platform.example")
    await _grant_platform_admin(actor["user_id"])

    response = await client.post(
        "/platform/auth/login", json={"email": "ops@platform.example", "password": PASSWORD}
    )

    assert response.status_code == 403, response.text
    assert "Two-factor" in response.json()["detail"]


async def test_login_requires_a_totp_code_and_then_succeeds(client):
    actor = await _platform_operator(client)

    without_code = await client.post(
        "/platform/auth/login", json={"email": actor["email"], "password": PASSWORD}
    )
    assert without_code.status_code == 401
    assert without_code.json()["detail"] == "TOTP code required"

    with_code = await client.post(
        "/platform/auth/login",
        json={
            "email": actor["email"],
            "password": PASSWORD,
            # Next timestep: activation above consumed the current one, and
            # the replay guard refuses to see it twice.
            "totp_code": await _totp_code(actor["user_id"], step_offset=1),
        },
    )
    assert with_code.status_code == 200, with_code.text
    assert with_code.json()["token_type"] == "bearer"

    # And the token it hands back really works on the console.
    token = with_code.json()["access_token"]
    listed = await client.get("/platform/companies", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200, listed.text


async def test_login_does_not_disclose_whether_an_email_is_a_platform_admin(client):
    """A tenant admin with the right password must be indistinguishable
    from a non-existent account."""
    await register_and_login(client, "Acme", "admin@acme.test")

    real_user_not_an_admin = await client.post(
        "/platform/auth/login", json={"email": "admin@acme.test", "password": PASSWORD}
    )
    no_such_user = await client.post(
        "/platform/auth/login", json={"email": "nobody@nowhere.test", "password": PASSWORD}
    )

    assert real_user_not_an_admin.status_code == no_such_user.status_code == 401
    assert real_user_not_an_admin.json() == no_such_user.json()


# --------------------------------------------------------------------------
# 4. The console reads and writes what it should
# --------------------------------------------------------------------------


async def test_the_console_sees_every_tenant(client):
    """The one thing that genuinely needs BYPASSRLS: an operator belonging
    to no customer's company still lists them all."""
    actor = await _platform_operator(client)
    await register_and_login(client, "Alpha Construction", "a@alpha.test")
    await register_and_login(client, "Beta Builders", "b@beta.test")

    response = await client.get("/platform/companies", headers=actor["platform_headers"])

    assert response.status_code == 200, response.text
    names = {item["name"] for item in response.json()["items"]}
    assert {"Alpha Construction", "Beta Builders"} <= names


async def test_changing_the_tier_changes_what_the_tenant_may_do(client):
    """End to end: the console moves a tenant down a tier and the product
    API starts refusing an estimation write for that tenant's own admin."""
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="pro")

    created = await client.post(_ESTIMATION_WRITE, json=_ESTIMATION_BODY, headers=tenant["headers"])
    assert created.status_code == 201, created.text

    patched = await client.patch(
        f"/platform/companies/{tenant['company_id']}/subscription",
        json={"tier": "starter"},
        headers=actor["platform_headers"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["tier"] == "starter"

    refused = await client.post(
        _ESTIMATION_WRITE, json={**_ESTIMATION_BODY, "name": "Second"}, headers=tenant["headers"]
    )
    assert refused.status_code == 403, refused.text


async def test_an_override_grants_a_module_the_tier_withholds(client):
    """Comp one feature without moving the customer's plan."""
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="starter")

    blocked = await client.post(_ESTIMATION_WRITE, json=_ESTIMATION_BODY, headers=tenant["headers"])
    assert blocked.status_code == 403, "precondition: starter must not reach estimation"

    granted = await client.put(
        f"/platform/companies/{tenant['company_id']}/modules/estimation",
        json={"enabled": True, "note": "comped during migration"},
        headers=actor["platform_headers"],
    )
    assert granted.status_code == 200, granted.text

    allowed = await client.post(_ESTIMATION_WRITE, json=_ESTIMATION_BODY, headers=tenant["headers"])
    assert allowed.status_code == 201, allowed.text


async def test_an_override_withholds_a_module_the_tier_allows(client):
    """The direction a naive implementation gets wrong: an override of
    False must REVOKE, not merely fail to grant."""
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="enterprise")

    await client.put(
        f"/platform/companies/{tenant['company_id']}/modules/estimation",
        json={"enabled": False, "note": "suspended pending payment"},
        headers=actor["platform_headers"],
    )

    refused = await client.post(_ESTIMATION_WRITE, json=_ESTIMATION_BODY, headers=tenant["headers"])
    assert refused.status_code == 403, refused.text


async def test_clearing_an_override_reverts_to_the_tier(client):
    """Deleting the row is a different act from setting it false."""
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="enterprise")

    await client.put(
        f"/platform/companies/{tenant['company_id']}/modules/estimation",
        json={"enabled": False},
        headers=actor["platform_headers"],
    )
    cleared = await client.delete(
        f"/platform/companies/{tenant['company_id']}/modules/estimation",
        headers=actor["platform_headers"],
    )
    assert cleared.status_code == 200, cleared.text

    allowed = await client.post(_ESTIMATION_WRITE, json=_ESTIMATION_BODY, headers=tenant["headers"])
    assert allowed.status_code == 201, allowed.text


async def test_setting_a_non_writable_status_makes_the_tenant_read_only(client):
    """"Access" in the operator's sense: the tenant keeps reading their data
    and every write is refused."""
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="pro")

    patched = await client.patch(
        f"/platform/companies/{tenant['company_id']}/subscription",
        json={"status": "past_due"},
        headers=actor["platform_headers"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["writes_enabled"] is False

    readable = await client.get("/leads", headers=tenant["headers"])
    assert readable.status_code == 200, "a read-only tenant must still read"

    refused = await client.post(
        "/leads",
        json={
            "contact_name": "Blocked",
            "project_name": "Deck",
            "email": "blocked@acme.test",
            "project_type": "residential",
        },
        headers=tenant["headers"],
    )
    assert refused.status_code == 403, refused.text


async def test_entitlement_changes_are_audited_into_the_target_tenant(client):
    """The customer can see that someone outside their company changed
    their entitlements, and to what."""
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="pro")

    await client.patch(
        f"/platform/companies/{tenant['company_id']}/subscription",
        json={"tier": "enterprise"},
        headers=actor["platform_headers"],
    )

    conn = await _owner_conn()
    try:
        row = await conn.fetchrow(
            "SELECT actor_id, action, log_metadata FROM audit_log "
            "WHERE company_id = $1 AND action = 'platform.subscription_updated'",
            uuid.UUID(str(tenant["company_id"])),
        )
    finally:
        await conn.close()

    assert row is not None, "no audit row written into the tenant"
    assert str(row["actor_id"]) == str(actor["user_id"])


async def test_a_child_branch_is_refused_by_name_rather_than_silently_redirected(client):
    """Entitlements belong to the whole tree. Editing a branch must not
    quietly change every sibling."""
    actor = await _platform_operator(client)
    parent = await register_and_login(client, "Group", "admin@group.test", tier="enterprise")

    child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Branch"},
        headers=parent["headers"],
    )
    assert child.status_code == 201, child.text
    child_id = child.json()["id"]

    response = await client.patch(
        f"/platform/companies/{child_id}/subscription",
        json={"tier": "starter"},
        headers=actor["platform_headers"],
    )

    assert response.status_code == 400, response.text
    assert str(parent["company_id"]) in response.json()["detail"]


# --------------------------------------------------------------------------
# 5. A manual status survives Stripe
# --------------------------------------------------------------------------


async def test_a_manually_set_status_is_not_reverted_by_a_stripe_event(client):
    """The webhook is last-write-wins on status. Without the override flag
    an operator's decision reverts on the next routine subscription event,
    silently -- no error, nothing logged, the change just stops being true.
    """
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="pro")

    await client.patch(
        f"/platform/companies/{tenant['company_id']}/subscription",
        json={"status": "past_due"},
        headers=actor["platform_headers"],
    )

    payload, signature = _signed_subscription_updated(
        await _stripe_subscription_id(tenant["company_id"])
    )
    delivered = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature}
    )
    assert delivered.status_code == 200, delivered.text

    after = await client.get(
        f"/platform/companies/{tenant['company_id']}", headers=actor["platform_headers"]
    )
    assert after.json()["status"] == "past_due", (
        "a Stripe event overwrote a status the operator set by hand"
    )


async def test_clearing_the_override_hands_status_back_to_stripe(client):
    actor = await _platform_operator(client)
    tenant = await register_and_login(client, "Acme", "admin@acme.test", tier="pro")

    await client.patch(
        f"/platform/companies/{tenant['company_id']}/subscription",
        json={"status": "past_due"},
        headers=actor["platform_headers"],
    )
    await client.patch(
        f"/platform/companies/{tenant['company_id']}/subscription",
        json={"clear_manual_status_override": True},
        headers=actor["platform_headers"],
    )

    payload, signature = _signed_subscription_updated(
        await _stripe_subscription_id(tenant["company_id"])
    )
    await client.post("/webhooks/stripe", content=payload, headers={"Stripe-Signature": signature})

    after = await client.get(
        f"/platform/companies/{tenant['company_id']}", headers=actor["platform_headers"]
    )
    assert after.json()["status"] == "active"


# --------------------------------------------------------------------------
# 6. Tenant lifecycle: create, rename, take out of service, restore
#    (migration 0024)
# --------------------------------------------------------------------------


async def _is_live(company_id) -> bool:
    """`is_company_live` as the database answers it.

    Read through the owner connection rather than asserted from an API
    response on purpose: the function is what the request path actually
    gates on, and a test that only checked the JSON would still pass if the
    ancestor walk were wrong.
    """
    conn = await _owner_conn()
    try:
        return await conn.fetchval(
            "SELECT is_company_live($1)", uuid.UUID(str(company_id))
        )
    finally:
        await conn.close()


async def _deleted_at(company_id):
    conn = await _owner_conn()
    try:
        return await conn.fetchval(
            "SELECT deleted_at FROM companies WHERE id = $1", uuid.UUID(str(company_id))
        )
    finally:
        await conn.close()


async def _is_active(company_id) -> bool:
    """The generated column's value, read from the database.

    Deliberately NOT from an API response: `is_active` being correct in
    `CompanyResponse` would also be true if some route were computing it,
    and the claim under test is that POSTGRES derives it.
    """
    conn = await _owner_conn()
    try:
        return await conn.fetchval(
            "SELECT is_active FROM companies WHERE id = $1", uuid.UUID(str(company_id))
        )
    finally:
        await conn.close()


async def test_creating_a_tenant_produces_a_working_customer(client):
    """The whole point of the create route: what comes out is a tenant its
    owner can actually sign in to and use, not four rows that look right.

    Asserted by signing in as the new owner with the one-time password and
    performing a tier-gated write -- if the membership, the tenant context
    or the trial subscription were wrong, that fails.
    """
    operator = await _platform_operator(client, "ops-create@platform.example")

    created = await client.post(
        "/platform/companies",
        json={
            "company_name": "Northwind Builders",
            "owner_email": "owner@northwind.example",
            "owner_full_name": "Owner One",
            "tier": "enterprise",
        },
        headers=operator["platform_headers"],
    )
    assert created.status_code == 201, created.text
    body = created.json()

    assert body["tenant"]["name"] == "Northwind Builders"
    assert body["tenant"]["tier"] == "enterprise"
    assert body["tenant"]["is_root"] is True
    assert body["tenant"]["user_count"] == 1
    assert body["tenant"]["deleted_at"] is None
    # Trials are write-enabled, so the customer can work on day one.
    assert body["tenant"]["writes_enabled"] is True

    # The credential is returned once and is genuinely the owner's password.
    login = await client.post(
        "/auth/login",
        json={"email": "owner@northwind.example", "password": body["temporary_password"]},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # `enterprise` includes estimation, so this write proves the tier
    # actually landed on a row the gates read -- not merely on the response.
    allowed = await client.post(_ESTIMATION_WRITE, json=_ESTIMATION_BODY, headers=headers)
    assert allowed.status_code == 201, allowed.text


async def test_creating_a_tenant_refuses_an_email_that_already_exists(client):
    """The duplicate check runs before the Stripe call, so a rejected create
    spends nothing and leaves nothing behind."""
    operator = await _platform_operator(client, "ops-dupe@platform.example")
    await register_and_login(client, "Existing Co", "taken@example.com")

    response = await client.post(
        "/platform/companies",
        json={
            "company_name": "Second Co",
            "owner_email": "taken@example.com",
            "owner_full_name": "Someone Else",
        },
        headers=operator["platform_headers"],
    )
    assert response.status_code == 409, response.text


async def test_renaming_a_tenant_is_audited_into_that_tenant(client):
    """A rename lands on the row AND in the customer's own audit log -- the
    same rule every other console mutation follows."""
    operator = await _platform_operator(client, "ops-rename@platform.example")
    tenant = await register_and_login(client, "Typoo Construction", "admin@typoo.example")

    response = await client.patch(
        f"/platform/companies/{tenant['company_id']}",
        json={"name": "Typo Construction"},
        headers=operator["platform_headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Typo Construction"

    conn = await _owner_conn()
    try:
        action = await conn.fetchval(
            "SELECT action FROM audit_log WHERE company_id = $1 "
            "AND action = 'platform.tenant_renamed'",
            uuid.UUID(str(tenant["company_id"])),
        )
    finally:
        await conn.close()
    assert action == "platform.tenant_renamed"


async def test_deactivating_a_tenant_stops_a_token_already_issued(client):
    """The half a login-only check would miss.

    `is_company_live` is read at the membership chokepoint on EVERY request,
    so an operator taking a customer out of service does not have to wait
    out the sessions already open -- the same property that makes revoking a
    platform admin bite within one request rather than one token lifetime.
    """
    operator = await _platform_operator(client, "ops-deact@platform.example")
    tenant = await register_and_login(client, "Lapsed Co", "admin@lapsed.example")
    headers = tenant["headers"]

    assert (await client.get("/leads", headers=headers)).status_code == 200

    response = await client.delete(
        f"/platform/companies/{tenant['company_id']}", headers=operator["platform_headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_at"] is not None

    # ...and the SAME token stops working immediately afterwards.
    after = await client.get("/leads", headers=headers)
    assert after.status_code == 403, after.text

    # A fresh login is refused too, rather than handing out a token that
    # would 403 on its first use. 403 rather than 401 because the password
    # WAS right -- and the message distinguishes "retired tenant" from "no
    # memberships at all", which are the same code and very different
    # support calls.
    relogin = await client.post(
        "/auth/login", json={"email": "admin@lapsed.example", "password": PASSWORD}
    )
    assert relogin.status_code == 403, relogin.text
    assert "no longer active" in relogin.json()["detail"]


async def test_deactivating_a_parent_takes_its_branches_with_it(client):
    """Why the liveness check is SECURITY DEFINER.

    A branch's RLS scope contains its descendants and never its ancestors,
    so a branch asking "has my parent been retired?" reads zero rows and
    would answer "no". Retiring a customer must not leave their branch
    offices signing in normally.
    """
    operator = await _platform_operator(client, "ops-tree@platform.example")
    parent = await register_and_login(client, "Head Office", "admin@head.example")

    branch_id = uuid.uuid4()
    conn = await _owner_conn()
    try:
        await conn.execute(
            # No `is_active`: generated from `deleted_at` since migration
            # 0025, and a generated column rejects an explicit value.
            "INSERT INTO companies (id, parent_id, name, created_at) "
            "VALUES ($1, $2, 'Branch Office', now())",
            branch_id,
            uuid.UUID(str(parent["company_id"])),
        )
    finally:
        await conn.close()

    assert await _is_live(branch_id) is True

    response = await client.delete(
        f"/platform/companies/{parent['company_id']}", headers=operator["platform_headers"]
    )
    assert response.status_code == 200, response.text

    # The branch's own row was never touched, and it is still out of service.
    assert await _deleted_at(branch_id) is None
    assert await _is_live(branch_id) is False


async def test_restoring_a_tenant_puts_it_back_exactly_as_it_was(client):
    """The reason the delete is soft: nothing was destroyed, so restoring is
    a column going back to NULL rather than a rebuild."""
    operator = await _platform_operator(client, "ops-restore@platform.example")
    tenant = await register_and_login(client, "Returning Co", "admin@returning.example")

    await client.delete(
        f"/platform/companies/{tenant['company_id']}", headers=operator["platform_headers"]
    )
    assert (await client.get("/leads", headers=tenant["headers"])).status_code == 403

    restored = await client.post(
        f"/platform/companies/{tenant['company_id']}/restore",
        headers=operator["platform_headers"],
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None

    login = await client.post(
        "/auth/login", json={"email": "admin@returning.example", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text


async def test_a_deactivated_tenant_is_hidden_from_the_list_but_findable(client):
    """A soft delete nobody can find again is a hard one with extra steps,
    so the console must be able to ask for them back."""
    operator = await _platform_operator(client, "ops-list@platform.example")
    tenant = await register_and_login(client, "Hidden Co", "admin@hidden.example")
    await client.delete(
        f"/platform/companies/{tenant['company_id']}", headers=operator["platform_headers"]
    )

    default = await client.get(
        "/platform/companies?search=Hidden Co", headers=operator["platform_headers"]
    )
    assert [t["name"] for t in default.json()["items"]] == []

    included = await client.get(
        "/platform/companies?search=Hidden Co&include_deleted=true",
        headers=operator["platform_headers"],
    )
    assert [t["name"] for t in included.json()["items"]] == ["Hidden Co"]


async def test_deactivating_twice_is_refused_rather_than_silently_restamped(client):
    """Otherwise the second call would overwrite the first one's timestamp,
    losing when the tenant actually went out of service."""
    operator = await _platform_operator(client, "ops-twice@platform.example")
    tenant = await register_and_login(client, "Once Co", "admin@once.example")

    first = await client.delete(
        f"/platform/companies/{tenant['company_id']}", headers=operator["platform_headers"]
    )
    assert first.status_code == 200
    second = await client.delete(
        f"/platform/companies/{tenant['company_id']}", headers=operator["platform_headers"]
    )
    assert second.status_code == 409, second.text


async def test_a_tenant_admin_cannot_reach_any_lifecycle_route(client):
    """The two-way boundary, re-asserted for the routes that now create and
    retire customers.

    These are the most dangerous verbs the console has ever exposed, so the
    fact that an ordinary company admin -- the highest identity inside a
    tenant -- cannot reach them is worth stating separately rather than
    inferring from the older scope test.
    """
    tenant = await register_and_login(client, "Ambitious Co", "admin@ambitious.example")
    headers = tenant["headers"]
    company_id = tenant["company_id"]

    create = await client.post(
        "/platform/companies",
        json={
            "company_name": "Mine Now",
            "owner_email": "new@ambitious.example",
            "owner_full_name": "New Owner",
        },
        headers=headers,
    )
    assert create.status_code == 401, create.text

    rename = await client.patch(
        f"/platform/companies/{company_id}", json={"name": "Renamed"}, headers=headers
    )
    assert rename.status_code == 401, rename.text

    deactivate = await client.delete(f"/platform/companies/{company_id}", headers=headers)
    assert deactivate.status_code == 401, deactivate.text

    restore = await client.post(
        f"/platform/companies/{company_id}/restore", headers=headers
    )
    assert restore.status_code == 401, restore.text


async def test_is_active_cannot_disagree_with_deleted_at(client):
    """The reason migration 0025 exists.

    0024 kept `is_active` in step with `deleted_at` by having the routes
    write both. That is an invariant maintained by discipline, and it holds
    until someone adds the third writer. Now Postgres computes it, so the
    two disagreeing is not a bug that can be introduced -- it is a state the
    schema cannot represent.

    Asserted through the real routes rather than by writing the columns, so
    what is checked is the behaviour an operator produces.
    """
    operator = await _platform_operator(client, "ops-generated@platform.example")
    tenant = await register_and_login(client, "Derived Co", "admin@derived.example")

    assert await _is_active(tenant["company_id"]) is True

    await client.delete(
        f"/platform/companies/{tenant['company_id']}", headers=operator["platform_headers"]
    )
    assert await _is_active(tenant["company_id"]) is False

    await client.post(
        f"/platform/companies/{tenant['company_id']}/restore",
        headers=operator["platform_headers"],
    )
    assert await _is_active(tenant["company_id"]) is True


async def test_is_active_rejects_being_written_at_all():
    """Not merely 'nothing writes it' -- nothing CAN.

    A plain column with no current writer looks identical to this one from
    the application's side; the difference is only visible by trying. Uses
    the table OWNER, the most privileged role available, so this says the
    column is unwritable rather than that some role lacks a grant.
    """
    conn = await _owner_conn()
    try:
        company_id = uuid.uuid4()
        with pytest.raises(asyncpg.PostgresError) as insert_attempt:
            await conn.execute(
                "INSERT INTO companies (id, parent_id, name, is_active, created_at) "
                "VALUES ($1, NULL, 'Should Fail', true, now())",
                company_id,
            )
        assert "generated" in str(insert_attempt.value).lower()

        # And the same for an UPDATE, which is the shape a future writer is
        # far more likely to take than an INSERT.
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name, created_at) "
            "VALUES ($1, NULL, 'Update Target', now())",
            company_id,
        )
        with pytest.raises(asyncpg.PostgresError) as update_attempt:
            await conn.execute(
                "UPDATE companies SET is_active = false WHERE id = $1", company_id
            )
        assert "generated" in str(update_attempt.value).lower()
    finally:
        await conn.execute("DELETE FROM companies WHERE id = $1", company_id)
        await conn.close()
