"""Task 3.18: per-tier seat defaults and the StripeClient dependency-
injection point (design spec Sections 2 and 3).

TIER_INCLUDED_SEATS values are explicit PLACEHOLDERS — the design spec's
own Section 3 note is clear that exact pricing/seat numbers are a pending
business decision, not a technical one. These exist so the rest of this
plan's code and tests have concrete values to work with. Changing them
later is a one-line edit here, not a migration, since the actual
per-subscription value lives in the `included_seats` column (set from
this dict once, at creation time, in Task 3.19).

`get_stripe_client()` is a plain function (not a class attribute or a
FastAPI Depends() callable, though it's used as one via Depends(get_stripe_client)
in the routers/tasks that need it) returning the one StripeClient
implementation wired everywhere. Which one it is depends on a single
setting, and on nothing else — see `_build_stripe_client` below. Not one
caller changed when RealStripeClient arrived, which was the point of the
Protocol.
"""
from __future__ import annotations

from app.config import settings
from app.services.stripe_client import FakeStripeClient, RealStripeClient, StripeClient

TIER_INCLUDED_SEATS: dict[str, int] = {"starter": 3, "pro": 10, "enterprise": 25}


def _build_stripe_client() -> StripeClient:
    """FakeStripeClient unless STRIPE_API_KEY is set.

    The switch is that one variable and is never inferred from anything
    else — not from app_env, not from whether the other Stripe settings
    happen to be filled in. "Looks like production, so probably real
    Stripe" is not a judgement call worth letting a config file make about
    whether to charge somebody's card.

    Failures here are deliberately left to propagate: this runs at import
    time, so a misconfigured RealStripeClient (a missing Price id, a
    missing httpx) refuses to boot the process rather than serving traffic
    that 500s at the first registration.
    """
    if not settings.stripe_api_key:
        # webhook_secret from Settings so a deployment can use a non-public
        # value even while the fake client stays (the production config
        # validator refuses the committed default). Tests that instantiate
        # FakeStripeClient() directly keep the dataclass's own default.
        return FakeStripeClient(webhook_secret=settings.stripe_webhook_secret)

    return RealStripeClient(
        api_key=settings.stripe_api_key,
        webhook_secret=settings.stripe_webhook_secret,
        # A key per tier in TIER_INCLUDED_SEATS, so adding a tier there
        # surfaces as "no Price configured for <tier>" at boot rather than
        # as a KeyError during that tier's first subscription.
        tier_price_ids={
            tier: getattr(settings, f"stripe_price_id_{tier}") or ""
            for tier in TIER_INCLUDED_SEATS
        },
        portal_return_url=settings.stripe_portal_return_url or settings.frontend_base_url,
        seat_overage_price_id=settings.stripe_seat_overage_price_id,
    )


# Module-level singleton: every caller within one process shares the same
# instance, so a test that reports seat usage and then asserts on
# FakeStripeClient's `reported_usage` sees calls made anywhere in that same
# test's request/task chain, not just within a single dependency resolution.
_stripe_client: StripeClient = _build_stripe_client()


def get_stripe_client() -> StripeClient:
    return _stripe_client
