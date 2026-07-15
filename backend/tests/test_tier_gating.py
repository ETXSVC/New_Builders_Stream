"""Tier-based module gating (docs/superpowers/specs/2026-07-15-tier-gating-design.md).

This file holds all three layers for the feature, matching
test_read_only_enforcement.py's one-file precedent: core truth-table tests
(this task), per-module behavior tests (Tasks 5.3-5.8), and the
completeness introspection test (Task 5.9).
"""
import uuid

import asyncpg
import pytest

from app.core.tier_gating import MODULE_MIN_TIER, TIER_RANK, tier_allows
from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


def test_every_module_maps_to_a_valid_tier():
    for module, tier in MODULE_MIN_TIER.items():
        assert tier in TIER_RANK, f"{module!r} maps to unknown tier {tier!r}"

    # Drift guard (Task 5.1 code-quality review): TIER_RANK re-declares the
    # tier set that subscription.py's VALID_TIERS/DB CHECK already own. If a
    # fourth tier is ever added there without updating TIER_RANK, every
    # gated request from that tenant would 500 on TIER_RANK[tier] — this
    # assertion makes the divergence a loud test failure instead.
    from app.models.subscription import VALID_TIERS

    assert set(TIER_RANK) == set(VALID_TIERS)


def test_tier_rank_orders_the_three_tiers():
    assert TIER_RANK["starter"] < TIER_RANK["pro"] < TIER_RANK["enterprise"]


async def _seed_company_with_tier(tier):
    """Seeds a root company + subscription row directly via the owner
    connection — tier_allows is a plain session-level helper, so its truth
    table doesn't need the HTTP registration flow (and registration can only
    ever produce trialing/pro anyway)."""
    company_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name) VALUES ($1, NULL, 'Tier Co')",
            company_id,
        )
        await conn.execute(
            "INSERT INTO subscriptions (id, company_id, stripe_customer_id, "
            "stripe_subscription_id, tier, status, included_seats) "
            "VALUES ($1, $2, $3, $4, $5, 'active', 5)",
            uuid.uuid4(),
            company_id,
            f"cus_fake_{uuid.uuid4().hex[:12]}",
            f"sub_fake_{uuid.uuid4().hex[:12]}",
            tier,
        )
    finally:
        await conn.close()
    return company_id


@pytest.mark.parametrize(
    "tier,module,expected",
    [
        # starter: universal modules aren't in MODULE_MIN_TIER at all, so the
        # grid below covers every gated module at every tier - 3x5 = 15 rows.
        ("starter", "estimation", False),
        ("starter", "compliance", False),
        ("starter", "accounting", False),
        ("starter", "integrations", False),
        ("starter", "child_branches", False),
        ("pro", "estimation", True),
        ("pro", "compliance", True),
        ("pro", "accounting", False),
        ("pro", "integrations", False),
        ("pro", "child_branches", False),
        ("enterprise", "estimation", True),
        ("enterprise", "compliance", True),
        ("enterprise", "accounting", True),
        ("enterprise", "integrations", True),
        ("enterprise", "child_branches", True),
    ],
)
async def test_tier_allows_truth_table(db_session, tier, module, expected):
    company_id = await _seed_company_with_tier(tier)
    assert await tier_allows(db_session, company_id, module) is expected


async def test_tier_allows_fails_open_on_missing_subscription(db_session):
    """Mirrors block_if_read_only's documented stance: no subscription row is
    an unreachable state (every root company gets one atomically at
    registration), treated as allow, not something to build defensive
    handling for."""
    company_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name) VALUES ($1, NULL, 'No Sub Co')",
            company_id,
        )
    finally:
        await conn.close()
    assert await tier_allows(db_session, company_id, "accounting") is True


async def test_tier_allows_resolves_the_root_company_for_a_child_branch(db_session):
    """A child branch has no subscriptions row of its own (root-only
    ownership) — tier resolution must walk up via get_root_company_id, the
    same way block_if_read_only does."""
    root_id = await _seed_company_with_tier("enterprise")
    child_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name) VALUES ($1, $2, 'Tier Branch')",
            child_id,
            root_id,
        )
    finally:
        await conn.close()
    assert await tier_allows(db_session, child_id, "accounting") is True
