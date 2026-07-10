"""Task 2.4: Cost Catalog inheritance resolution tests
(`app/services/catalog_resolution.py`).

`resolve_visible_catalog_items` never touches tenant context itself — it
trusts that the session handed to it already has its RLS context
established (normally by `get_current_user`, once, at the start of a real
request). These tests build that RLS context directly, the same way
`get_current_user` does (`set_current_user` then `set_current_tenant`,
inside one open transaction), against a real Postgres database with
migration 0005's bidirectional RLS policy actually enforced — not a mock,
not an in-memory stub.

Company hierarchies here are seeded directly via the RLS-exempt owner
connection, not through `POST /companies/{id}/children`: this file tests a
service function's own resolution/hop-distance logic, not the
company-creation route, and several of these scenarios need a bare
3rd-generation grandchild chain, which through the API would require
juggling cross-tenant `company_users` grants and `X-Tenant-ID` header
spoofing (see `test_tenant_isolation_phase1.py`'s sibling-branch tests)
purely as test-setup plumbing unrelated to what's actually under test here.
`resolve_visible_catalog_items` itself has no concept of users/membership at
all (`cost_catalog_items`' policy is purely `company_id`-based), so there's
nothing lost in accuracy by seeding companies this way instead of through
real registration/login. Cost Catalog items are seeded the same way
(`_insert_catalog_item_directly`), following this codebase's established
pattern (`_insert_lead_directly` et al., `test_tenant_isolation_phase1.py`)
for tables with no create route reachable yet — catalog items get one in
Task 2.5.
"""
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import asyncpg

from app.db import SessionLocal, set_current_tenant, set_current_user
from app.services.catalog_resolution import (
    _compute_hop_distance,
    _root_identity_id,
    resolve_visible_catalog_items,
)
from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

# resolve_visible_catalog_items doesn't check membership/self_membership at
# all (cost_catalog_items' policy is purely company_id-based, no
# self_membership involved the way company_users-scoped policies are), so
# any well-formed UUID works for set_current_user here — see this file's
# module docstring.
_FAKE_ACTOR_ID = uuid.uuid4()


async def _insert_company_directly(name, parent_id=None):
    """Seeds a `companies` row directly via the owner connection — see this
    file's module docstring for why direct seeding, not the
    `POST /companies/{id}/children` route, is used for company hierarchies
    in this file specifically."""
    company_id = str(uuid.uuid4())
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name, is_active, created_at) "
            "VALUES ($1, $2, $3, true, now())",
            company_id,
            parent_id,
            name,
        )
    finally:
        await conn.close()
    return company_id


async def _insert_catalog_item_directly(company_id, **overrides):
    """Seeds a `cost_catalog_items` row directly via the owner connection —
    there's no create route for this table until Task 2.5, so this is the
    only way to get rows in place to test resolve_visible_catalog_items
    against. Same rationale as _insert_lead_directly
    (test_tenant_isolation_phase1.py)."""
    item_id = str(uuid.uuid4())
    fields = {
        "parent_catalog_item_id": None,
        "category": "framing",
        "name": "2x4 Lumber",
        "unit": "each",
        "unit_rate": Decimal("5.00"),
    }
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO cost_catalog_items "
            "(id, company_id, parent_catalog_item_id, category, name, unit, unit_rate, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, now())",
            item_id,
            company_id,
            fields["parent_catalog_item_id"],
            fields["category"],
            fields["name"],
            fields["unit"],
            fields["unit_rate"],
        )
    finally:
        await conn.close()
    return item_id


async def _delete_catalog_item_directly(item_id):
    """Deletes a cost_catalog_items row via the owner connection — there's
    no delete route for this table yet, and this is exactly the scenario
    the ON DELETE SET NULL edge case (flagged during Task 2.1's spec review)
    needs: simulating a branch choosing to stop overriding an item by
    removing its override row outright."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute("DELETE FROM cost_catalog_items WHERE id = $1", item_id)
    finally:
        await conn.close()


async def _touch_catalog_item_directly(item_id, *, hours_ago: int):
    """Backdates a cost_catalog_items row's updated_at via the owner
    connection — used to force an unambiguous, non-flaky ordering in the
    diamond-tie test below, where insertion order alone isn't a reliable
    enough signal to prove the tie-break genuinely keys off updated_at
    rather than coincidence."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "UPDATE cost_catalog_items SET updated_at = now() - make_interval(hours => $2) WHERE id = $1",
            item_id,
            hours_ago,
        )
    finally:
        await conn.close()


@asynccontextmanager
async def _scoped_session(company_id):
    """Mirrors get_current_user's own context-setting sequence
    (set_current_user then set_current_tenant, inside one open transaction)
    without a real HTTP request. resolve_visible_catalog_items itself
    deliberately never calls these (see its docstring) — a test exercising
    it directly must set up the RLS context exactly the way the real
    request path would, once, before calling it, then let it run entirely
    on trust from there.

    Commits (doesn't roll back) on the way out, even though this file never
    mutates catalog data through the ORM here: Session.rollback() expires
    every attribute on every object still attached to the session
    (independent of expire_on_commit=False, which only governs commit()),
    so the CostCatalogItem instances resolve_visible_catalog_items returns
    would raise DetachedInstanceError the moment a test tries to read an
    attribute off them after this context manager exits. Committing a
    read-only transaction has no data-changing effect and keeps
    expire_on_commit=False's guarantee that already-loaded attributes stay
    readable after the session closes — exactly what these tests need."""
    session = SessionLocal()
    try:
        await session.begin()
        await set_current_user(session, str(_FAKE_ACTOR_ID))
        await set_current_tenant(session, str(company_id))
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _resolve(company_id):
    async with _scoped_session(company_id) as session:
        return await resolve_visible_catalog_items(session, uuid.UUID(company_id))


# =============================================================================
# Task 2.4's five spec-mandated scenarios
# =============================================================================


async def test_plain_company_with_no_parent_sees_only_its_own_catalog():
    company_id = await _insert_company_directly("Acme Construction")
    item_a = await _insert_catalog_item_directly(company_id, name="2x4 Lumber")
    item_b = await _insert_catalog_item_directly(company_id, name="Concrete Mix", category="concrete")

    resolved = await _resolve(company_id)

    assert {str(item.id) for item in resolved} == {item_a, item_b}
    assert all(str(item.company_id) == company_id for item in resolved)


async def test_child_branch_with_no_overrides_sees_parents_full_catalog():
    parent_id = await _insert_company_directly("Parent Co")
    child_id = await _insert_company_directly("Seattle Branch", parent_id=parent_id)

    item_a = await _insert_catalog_item_directly(parent_id, name="2x4 Lumber")
    item_b = await _insert_catalog_item_directly(parent_id, name="Concrete Mix", category="concrete")

    resolved = await _resolve(child_id)

    assert {str(item.id) for item in resolved} == {item_a, item_b}
    assert all(str(item.company_id) == parent_id for item in resolved)


async def test_child_branch_with_one_override_sees_it_as_own_and_rest_as_parents():
    parent_id = await _insert_company_directly("Parent Co")
    child_id = await _insert_company_directly("Seattle Branch", parent_id=parent_id)

    parent_item_1 = await _insert_catalog_item_directly(
        parent_id, name="2x4 Lumber", unit_rate=Decimal("5.00")
    )
    parent_item_2 = await _insert_catalog_item_directly(
        parent_id, name="Concrete Mix", category="concrete", unit_rate=Decimal("120.00")
    )
    child_override = await _insert_catalog_item_directly(
        child_id,
        name="2x4 Lumber",
        unit_rate=Decimal("6.50"),
        parent_catalog_item_id=parent_item_1,
    )

    resolved = await _resolve(child_id)
    resolved_by_id = {str(item.id): item for item in resolved}

    assert set(resolved_by_id) == {child_override, parent_item_2}
    assert resolved_by_id[child_override].unit_rate == Decimal("6.50")
    assert str(resolved_by_id[child_override].company_id) == child_id
    assert resolved_by_id[parent_item_2].unit_rate == Decimal("120.00")
    assert str(resolved_by_id[parent_item_2].company_id) == parent_id


async def test_grandchild_overriding_parents_override_sees_closest_not_grandparents_original():
    grandparent_id = await _insert_company_directly("HQ")
    parent_id = await _insert_company_directly("Regional Branch", parent_id=grandparent_id)
    grandchild_id = await _insert_company_directly("Local Office", parent_id=parent_id)

    grandparent_item = await _insert_catalog_item_directly(
        grandparent_id, name="2x4 Lumber", unit_rate=Decimal("5.00")
    )
    parent_override = await _insert_catalog_item_directly(
        parent_id,
        name="2x4 Lumber",
        unit_rate=Decimal("6.00"),
        parent_catalog_item_id=grandparent_item,
    )
    grandchild_override = await _insert_catalog_item_directly(
        grandchild_id,
        name="2x4 Lumber",
        unit_rate=Decimal("7.25"),
        parent_catalog_item_id=parent_override,
    )

    resolved = await _resolve(grandchild_id)

    assert len(resolved) == 1
    assert str(resolved[0].id) == grandchild_override
    assert resolved[0].unit_rate == Decimal("7.25")


async def test_parents_own_view_is_unaffected_by_childs_override():
    """US-4.6's explicit acceptance criterion: "the parent catalog itself is
    unaffected". The parent's session ALSO sees its child's override
    (ordinary downward RLS visibility, same as every other table) — this
    specifically confirms the parent's own row (hop 0, from the parent's own
    point of view) wins over the child's override (a descendant, never
    "closer" than the caller's own row per hop_distance's design) rather
    than the child's override leaking upward and silently replacing it in
    the parent's own resolved view."""
    parent_id = await _insert_company_directly("Regional Branch")
    child_id = await _insert_company_directly("Local Office", parent_id=parent_id)

    parent_item = await _insert_catalog_item_directly(
        parent_id, name="2x4 Lumber", unit_rate=Decimal("6.00")
    )
    await _insert_catalog_item_directly(
        child_id,
        name="2x4 Lumber",
        unit_rate=Decimal("7.25"),
        parent_catalog_item_id=parent_item,
    )

    resolved = await _resolve(parent_id)

    assert len(resolved) == 1
    assert str(resolved[0].id) == parent_item
    assert resolved[0].unit_rate == Decimal("6.00")
    assert str(resolved[0].company_id) == parent_id


# =============================================================================
# Diamond-tie tie-break — see catalog_resolution.py's module docstring
# ("Second known, documented, accepted limitation") for the full mechanics.
# =============================================================================


async def test_diamond_tie_between_unrelated_sibling_overrides_breaks_deterministically():
    """HQ (G) has NO catalog item of its own — the original item belongs to
    a Regional branch (P, a child of G), and TWO Local offices (A, B —
    children of P, siblings of each other) each independently override P's
    item. Resolving from G's own point of view: G created no override, so
    every candidate in this identity group (P's original, A's override, B's
    override) is a DESCENDANT of G, not an ancestor — hop_distance() clamps
    ALL THREE to +inf (a 3-way tie, not just a 2-way one between the
    siblings). catalog_resolution.py's (updated_at DESC, id ASC) tie-break
    must pick the most-recently-updated candidate deterministically, not
    whichever row the database happened to return first."""
    hq_id = await _insert_company_directly("HQ")
    regional_id = await _insert_company_directly("Regional Branch", parent_id=hq_id)
    local_a_id = await _insert_company_directly("Local Office A", parent_id=regional_id)
    local_b_id = await _insert_company_directly("Local Office B", parent_id=regional_id)

    regional_item = await _insert_catalog_item_directly(
        regional_id, name="2x4 Lumber", unit_rate=Decimal("5.00")
    )
    older_override = await _insert_catalog_item_directly(
        local_a_id, name="2x4 Lumber", unit_rate=Decimal("6.00"), parent_catalog_item_id=regional_item
    )
    newer_override = await _insert_catalog_item_directly(
        local_b_id, name="2x4 Lumber", unit_rate=Decimal("7.00"), parent_catalog_item_id=regional_item
    )

    # Force an unambiguous updated_at ordering — insertion order alone isn't
    # a reliable enough signal to prove the tie-break genuinely keys off
    # updated_at rather than coincidence.
    await _touch_catalog_item_directly(regional_item, hours_ago=2)
    await _touch_catalog_item_directly(older_override, hours_ago=1)
    await _touch_catalog_item_directly(newer_override, hours_ago=0)

    resolved = await _resolve(hq_id)

    # All three (Regional's original, Local A's override, Local B's
    # override) are genuinely visible to HQ (ordinary downward RLS grant)
    # and none is on HQ's own ancestor chain — but only ONE survives the
    # identity-group dedup: the most-recently-updated one, deterministically.
    assert len(resolved) == 1
    assert str(resolved[0].id) == newer_override
    assert resolved[0].unit_rate == Decimal("7.00")


# =============================================================================
# Forward-looking edge case flagged during Task 2.1's spec review:
# `parent_catalog_item_id` uses ON DELETE SET NULL, and a multi-level
# override chain's middle row being deleted orphans everything below it.
# =============================================================================


async def test_deleting_middle_override_orphans_grandchilds_override():
    """3-level chain: grandparent's original -> parent's override ->
    grandchild's override-of-the-override. Deleting the PARENT's override
    row (as if that branch chose to stop overriding — there's no delete
    route yet, so this deletes directly, the same way the row was seeded)
    fires ON DELETE SET NULL on the grandchild's row's
    parent_catalog_item_id, silently severing it from the chain.

    Observed, asserted-on behavior (also documented as a known, accepted
    Phase 2 limitation in app/services/catalog_resolution.py's module
    docstring): the grandchild's now-orphaned row is treated as a brand-new,
    independent conceptual identity (its own root, since its
    parent_catalog_item_id is now NULL) rather than still being recognized
    as part of the original chain. Resolving from the grandchild's
    perspective therefore returns TWO separate entries for what a human
    would still consider "the same" conceptual item: the grandparent's
    original (now with no surviving override between it and the grandchild)
    AND the grandchild's orphaned override, standing alone with no
    relationship between them anymore. This is NOT fixed by this task —
    it's deliberately left as documented, tested, accepted behavior; a
    future phase could special-case re-linking orphaned overrides if this
    ever becomes a real product problem."""
    grandparent_id = await _insert_company_directly("HQ")
    parent_id = await _insert_company_directly("Regional Branch", parent_id=grandparent_id)
    grandchild_id = await _insert_company_directly("Local Office", parent_id=parent_id)

    grandparent_item = await _insert_catalog_item_directly(
        grandparent_id, name="2x4 Lumber", unit_rate=Decimal("5.00")
    )
    parent_override = await _insert_catalog_item_directly(
        parent_id,
        name="2x4 Lumber",
        unit_rate=Decimal("6.00"),
        parent_catalog_item_id=grandparent_item,
    )
    grandchild_override = await _insert_catalog_item_directly(
        grandchild_id,
        name="2x4 Lumber",
        unit_rate=Decimal("7.25"),
        parent_catalog_item_id=parent_override,
    )

    await _delete_catalog_item_directly(parent_override)

    # Sanity check the ON DELETE SET NULL actually fired, via the owner
    # connection, before asserting on resolve_visible_catalog_items' own
    # behavior — if this fails, the scenario itself isn't set up correctly
    # and the assertions below would be meaningless.
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT parent_catalog_item_id FROM cost_catalog_items WHERE id = $1",
            grandchild_override,
        )
    finally:
        await conn.close()
    assert row["parent_catalog_item_id"] is None, (
        "setup sanity check failed: deleting the parent override should "
        "have nulled out the grandchild's parent_catalog_item_id via ON "
        "DELETE SET NULL"
    )

    resolved = await _resolve(grandchild_id)
    resolved_by_id = {str(item.id): item for item in resolved}

    # The actual, observed, accepted-limitation behavior: TWO separate
    # entries, not one, for what's conceptually still "the same" catalog
    # item to a human — see this test's own docstring.
    assert set(resolved_by_id) == {grandparent_item, grandchild_override}
    assert resolved_by_id[grandparent_item].unit_rate == Decimal("5.00")
    assert resolved_by_id[grandchild_override].unit_rate == Decimal("7.25")
    assert resolved_by_id[grandchild_override].parent_catalog_item_id is None


# =============================================================================
# Fast, DB-free unit tests for _compute_hop_distance / _root_identity_id
# (extracted as top-level functions during this task's code-quality review,
# matching lead_transitions.is_legal_transition()'s own pure-function
# precedent) — direct coverage of the two trickiest pieces of pure logic in
# catalog_resolution.py, including _root_identity_id's cycle guard, which
# no live-DB test above exercises (no current write path can actually
# create a parent_catalog_item_id cycle, so this is purely a defensive
# backstop — but it's still real code that should have real coverage).
# =============================================================================

def test_compute_hop_distance_self_is_zero():
    active = uuid.uuid4()
    assert _compute_hop_distance(active, chain_lengths={active: 3}, active_chain_length=3) == 0


def test_compute_hop_distance_ancestor_is_positive():
    active = uuid.uuid4()
    parent = uuid.uuid4()
    grandparent = uuid.uuid4()
    chain_lengths = {active: 3, parent: 2, grandparent: 1}
    assert _compute_hop_distance(parent, chain_lengths=chain_lengths, active_chain_length=3) == 1
    assert _compute_hop_distance(grandparent, chain_lengths=chain_lengths, active_chain_length=3) == 2


def test_compute_hop_distance_descendant_clamps_to_infinity():
    active = uuid.uuid4()
    child = uuid.uuid4()
    chain_lengths = {active: 2, child: 3}
    distance = _compute_hop_distance(child, chain_lengths=chain_lengths, active_chain_length=2)
    assert distance == float("inf")


class _FakeItem:
    """Minimal stand-in for a CostCatalogItem ORM instance — _root_identity_id
    only ever touches `.id`/`.parent_catalog_item_id`, so a real DB row
    isn't needed to unit-test its pure graph-walking logic."""

    def __init__(self, item_id, parent_id=None):
        self.id = item_id
        self.parent_catalog_item_id = parent_id


def test_root_identity_id_no_parent_link_is_its_own_root():
    item = _FakeItem(uuid.uuid4())
    assert _root_identity_id(item, items_by_id={item.id: item}) == item.id


def test_root_identity_id_walks_multi_level_chain_to_the_root():
    root = _FakeItem(uuid.uuid4())
    middle = _FakeItem(uuid.uuid4(), parent_id=root.id)
    leaf = _FakeItem(uuid.uuid4(), parent_id=middle.id)
    items_by_id = {root.id: root, middle.id: middle, leaf.id: leaf}

    assert _root_identity_id(leaf, items_by_id=items_by_id) == root.id
    assert _root_identity_id(middle, items_by_id=items_by_id) == root.id
    assert _root_identity_id(root, items_by_id=items_by_id) == root.id


def test_root_identity_id_stops_at_a_parent_link_not_in_items_by_id():
    """Mirrors the ON DELETE SET NULL orphaning scenario at the pure-logic
    level: a parent_catalog_item_id pointing at something not present in
    the already-fetched row set (deleted, or simply never fetched) must
    stop the walk there, not raise."""
    orphan = _FakeItem(uuid.uuid4(), parent_id=uuid.uuid4())  # parent not in items_by_id
    assert _root_identity_id(orphan, items_by_id={orphan.id: orphan}) == orphan.id


def test_root_identity_id_cycle_guard_terminates_instead_of_looping_forever():
    """No current write path can create a parent_catalog_item_id cycle, but
    the guard exists as a defensive backstop — this is its only test."""
    a = _FakeItem(uuid.uuid4())
    b = _FakeItem(uuid.uuid4())
    a.parent_catalog_item_id = b.id
    b.parent_catalog_item_id = a.id  # a -> b -> a, a genuine cycle
    items_by_id = {a.id: a, b.id: b}

    # Must terminate (this test itself would hang if the guard were broken)
    # and return SOME id from the cycle, not raise.
    result = _root_identity_id(a, items_by_id=items_by_id)
    assert result in (a.id, b.id)
