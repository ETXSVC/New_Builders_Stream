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

Since migration 0023 a per-tenant OVERRIDE takes precedence over the tier.
`company_module_overrides` is written only by the platform console (the
runtime `app_user` role holds SELECT on it and nothing else), and a row
there answers the question outright: `enabled=True` grants a module the
plan would withhold, `enabled=False` withholds one the plan would grant,
and no row at all defers to the tier as before. `_module_allowed` is the
single place that resolves this, so `require_module`, the OAuth callback
and the event handlers cannot drift apart on what a tenant may use.

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
from app.models import CompanyModuleOverride, Subscription

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


async def _module_override(
    session: AsyncSession, company_id: uuid.UUID, module: str
) -> bool | None:
    """The platform-console override for this module, or None to defer.

    Three states, and the middle one is why this returns `bool | None`
    rather than a bool: no row means "use the tier", `True` grants a module
    the tier would withhold, and `False` withholds one the tier would grant.
    Collapsing the first two would make "off" unexpressible.

    Same one-query, root-resolution-inlined shape as `_root_tier` above, and
    it reads through the caller's own RLS-scoped session — the overrides
    table carries the same upward-visibility policy `subscriptions` does
    (migration 0023), so a child branch sees its root's row.
    """
    result = await session.execute(
        select(CompanyModuleOverride.enabled).where(
            CompanyModuleOverride.company_id == func.get_root_company_id(company_id),
            CompanyModuleOverride.module == module,
        )
    )
    return result.scalar_one_or_none()


async def _module_allowed(session: AsyncSession, company_id: uuid.UUID, module: str) -> bool:
    """THE resolution path. Both public entry points below go through this
    so the OAuth callback, the event handlers and every gated route can
    never disagree about what a tenant is entitled to.
    """
    override = await _module_override(session, company_id, module)
    if override is not None:
        return override

    tier = await _root_tier(session, company_id)
    if tier is None:
        return True  # fail open — see module docstring
    return TIER_RANK[tier] >= TIER_RANK[MODULE_MIN_TIER[module]]


async def tier_allows(session: AsyncSession, company_id: uuid.UUID, module: str) -> bool:
    return await _module_allowed(session, company_id, module)


def require_module(module: str):
    # KeyError HERE (module-import time, when the router file is imported)
    # for a typo'd module name — not at request time.
    min_tier = MODULE_MIN_TIER[module]

    async def dependency(current: CurrentUser = Depends(get_current_user, scope="function")) -> CurrentUser:
        if not await _module_allowed(current.session, current.company_id, module):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This feature requires the {min_tier} plan",
            )
        return current

    # Stamped for test_tier_gating.py's completeness introspection test —
    # lets it assert not just that SOME gate is present on a route, but that
    # it is the CORRECT module's gate.
    dependency.tier_module = module  # type: ignore[attr-defined]
    return dependency
