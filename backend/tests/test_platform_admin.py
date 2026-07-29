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
    list -- assert it holds writes on nothing but the three tables it is
    supposed to."""
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
    assert writable == {"company_module_overrides", "subscriptions", "audit_log"}, (
        "the platform console's database role can write tables outside its "
        f"remit: {sorted(writable)!r}"
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
