"""A tenant's own deposit percentage and tax rate (migration 0033).

These were module constants documented as placeholders — `0.10` deposit and
`0.00` tax — until they started deciding what customers are actually billed
and what the profitability report puts on screen. Neither was ever really
one number: a deposit percentage is a commercial policy per builder, and a
tax rate differs by jurisdiction.
"""
from decimal import Decimal

from tests.conftest import register_and_login


async def _admin(client, name, email):
    return await register_and_login(client, name, email, tier="enterprise")


async def test_defaults_apply_when_nothing_is_set(client):
    admin = await _admin(client, "Fin Co 1", "fin-1@example.test")
    response = await client.get("/companies/financial-settings", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deposit_percentage"] is None
    assert body["tax_rate"] is None
    # The effective values are what is actually in force, which is the
    # distinction the response exists to draw.
    assert Decimal(body["effective_deposit_percentage"]) == Decimal("0.10")
    assert Decimal(body["effective_tax_rate"]) == Decimal("0.00")


async def test_setting_a_rate_takes_effect(client):
    admin = await _admin(client, "Fin Co 2", "fin-2@example.test")
    saved = await client.put(
        "/companies/financial-settings",
        json={"deposit_percentage": "0.25", "tax_rate": "0.08875"},
        headers=admin["headers"],
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    # Five decimal places survive: a real US sales-tax rate (8.875%) would
    # be silently rounded away by a two-place column.
    assert Decimal(body["tax_rate"]) == Decimal("0.08875")
    assert Decimal(body["effective_deposit_percentage"]) == Decimal("0.25")


async def test_a_percentage_entered_as_a_whole_number_is_refused(client):
    """`10` meaning "10 percent" would bill ten times the contract value.
    Caught at the schema with a 422 naming the field, rather than reaching
    the CHECK constraint and surfacing as a 500."""
    admin = await _admin(client, "Fin Co 3", "fin-3@example.test")
    refused = await client.put(
        "/companies/financial-settings",
        json={"deposit_percentage": "10", "tax_rate": None},
        headers=admin["headers"],
    )
    assert refused.status_code == 422, refused.text


async def test_clearing_a_value_returns_it_to_the_default(client):
    """Null means "inherit", which is why the request is explicit about
    both fields rather than patch-style — an absent-means-unchanged update
    could not express this at all."""
    admin = await _admin(client, "Fin Co 4", "fin-4@example.test")
    await client.put(
        "/companies/financial-settings",
        json={"deposit_percentage": "0.25", "tax_rate": "0.05"},
        headers=admin["headers"],
    )
    cleared = await client.put(
        "/companies/financial-settings",
        json={"deposit_percentage": None, "tax_rate": "0.05"},
        headers=admin["headers"],
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["deposit_percentage"] is None
    assert Decimal(body["effective_deposit_percentage"]) == Decimal("0.10")
    assert Decimal(body["effective_tax_rate"]) == Decimal("0.05")


# --- Inheritance -----------------------------------------------------------
#
# Exercised against the resolver rather than over HTTP, because nobody can
# ACT AS a branch: `POST /companies/{id}/children` creates the company but
# no membership in it, and `get_current_user` requires one. Inheritance is
# still reached by real data — a branch's own estimates and reports resolve
# by the branch's company_id — so the resolver is the honest seam to test,
# not a request nobody can currently make.


async def _company_pair(db_session):
    """A root and a branch under it."""
    import uuid

    from app.models import Company

    root = Company(id=uuid.uuid4(), parent_id=None, name="Fin Root")
    db_session.add(root)
    await db_session.flush()
    branch = Company(id=uuid.uuid4(), parent_id=root.id, name="Fin Branch")
    db_session.add(branch)
    await db_session.flush()
    return root, branch


async def _set(db_session, company_id, *, deposit=None, tax=None):
    import uuid

    from app.models import CompanyFinancialSettings

    db_session.add(
        CompanyFinancialSettings(
            id=uuid.uuid4(),
            company_id=company_id,
            deposit_percentage=deposit,
            tax_rate=tax,
        )
    )
    await db_session.flush()


async def test_a_branch_inherits_its_roots_settings(db_session):
    """A head office sets a policy once and branches follow it."""
    from app.services.financial_settings import resolve_financial_settings

    root, branch = await _company_pair(db_session)
    await _set(db_session, root.id, deposit=Decimal("0.20"), tax=Decimal("0.07"))

    resolved = await resolve_financial_settings(db_session, branch.id)
    assert resolved.deposit_percentage == Decimal("0.20")
    assert resolved.tax_rate == Decimal("0.07")


async def test_a_branch_can_override_one_value_and_still_inherit_the_other(db_session):
    """The case that makes this per-company rather than root-scoped: a
    branch in another state has its own tax rate, but no reason to restate
    the company's deposit policy. Values resolve independently, so stating
    one must not silently drag the other down to the code default."""
    from app.services.financial_settings import resolve_financial_settings

    root, branch = await _company_pair(db_session)
    await _set(db_session, root.id, deposit=Decimal("0.20"), tax=Decimal("0.07"))
    await _set(db_session, branch.id, deposit=None, tax=Decimal("0.0925"))

    resolved = await resolve_financial_settings(db_session, branch.id)
    assert resolved.tax_rate == Decimal("0.0925"), "the branch's own rate wins"
    assert resolved.deposit_percentage == Decimal("0.20"), (
        "still the root's deposit policy — stating a tax rate must not reset it to 0.10"
    )


async def test_a_root_with_no_row_still_gets_the_defaults(db_session):
    from app.services.financial_settings import resolve_financial_settings

    root, _branch = await _company_pair(db_session)
    resolved = await resolve_financial_settings(db_session, root.id)
    assert resolved.deposit_percentage == Decimal("0.10")
    assert resolved.tax_rate == Decimal("0.00")


async def test_a_project_manager_cannot_read_or_change_them(client):
    """Admin and accountant only. These decide what customers are billed."""
    admin = await _admin(client, "Fin Co 5", "fin-5@example.test")
    invite = await client.post(
        "/invitations",
        json={"email": "fin-pm@example.test", "role": "project_manager"},
        headers=admin["headers"],
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Fin PM", "password": "anothersecret123"},
    )
    login = await client.post(
        "/auth/login", json={"email": "fin-pm@example.test", "password": "anothersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (
        await client.get("/companies/financial-settings", headers=pm_headers)
    ).status_code == 403
    assert (
        await client.put(
            "/companies/financial-settings",
            json={"deposit_percentage": "0.5", "tax_rate": None},
            headers=pm_headers,
        )
    ).status_code == 403
