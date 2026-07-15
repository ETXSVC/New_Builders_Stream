"""Tier-based module gating (docs/superpowers/specs/2026-07-15-tier-gating-design.md,
transcribing docs/08-pricing-subscription-model.md Section 3's feature table).

require_module gates a route on the caller's ROOT company's subscription
TIER; it is orthogonal to require_role (who within the company may act) and
block_if_read_only (is the subscription in good standing) — all three
coexist as per-route dependencies on the same routes. It is attached ONLY
to mutating routes (spec Decision 3: writes blocked below tier, reads stay
open), and it deliberately does NO method sniffing: the Integrations
module's mutating routes (connect/callback) are GETs, so any
request.method-based pass-through would exempt exactly the routes that
most need gating. The writes-only semantics live in WHERE the dependency
is attached, enforced complete by test_tier_gating.py's introspection test.

tier_allows is the same one-query tier resolution for the two consumers
with no CurrentUser: the OAuth callback (which authenticates via a signed
state token) and the event handlers (which gate event-driven writes into
gated modules, spec Decision 4).

Missing subscription row fails OPEN in both — mirroring block_if_read_only's
documented "unreachable state (every root company gets a subscription
atomically at registration), not something to build defensive handling
for" stance.
"""
import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.models import Subscription

TIER_RANK = {"starter": 0, "pro": 1, "enterprise": 2}

MODULE_MIN_TIER = {
    "estimation": "pro",         # Estimation Engine incl. e-signature + change orders
    "compliance": "pro",         # Compliance Tracking
    "accounting": "enterprise",  # client invoicing (AR), bills (AP), expenses
    "integrations": "enterprise",  # QuickBooks/FreshBooks
    "child_branches": "enterprise",  # nested child-company creation
}


async def _root_tier(session: AsyncSession, company_id: uuid.UUID) -> str | None:
    # ONE query, root resolution inlined in the WHERE clause — the identical
    # pattern block_if_read_only and subscriptions.py's
    # _get_subscription_for_current already use, and which already works
    # under the subscriptions table's upward-visibility RLS policy from a
    # tenant-scoped session.
    result = await session.execute(
        select(Subscription.tier).where(
            Subscription.company_id == func.get_root_company_id(company_id)
        )
    )
    return result.scalar_one_or_none()


async def tier_allows(session: AsyncSession, company_id: uuid.UUID, module: str) -> bool:
    tier = await _root_tier(session, company_id)
    if tier is None:
        return True  # fail open — see module docstring
    return TIER_RANK[tier] >= TIER_RANK[MODULE_MIN_TIER[module]]


def require_module(module: str):
    # KeyError HERE (module-import time, when the router file is imported)
    # for a typo'd module name — not at request time.
    min_tier = MODULE_MIN_TIER[module]

    async def dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        tier = await _root_tier(current.session, current.company_id)
        if tier is not None and TIER_RANK[tier] < TIER_RANK[min_tier]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This feature requires the {min_tier} plan",
            )
        return current

    # Stamped for test_tier_gating.py's completeness introspection test —
    # lets it assert not just that SOME gate is present on a route, but that
    # it is the CORRECT module's gate.
    dependency.tier_module = module
    return dependency
