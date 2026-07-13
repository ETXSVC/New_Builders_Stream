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
implementation this whole plan wires everywhere: FakeStripeClient. A
future RealStripeClient swap is a change to this one function's return
value (config-gated), not to any of its callers.
"""
from __future__ import annotations

from app.services.stripe_client import FakeStripeClient, StripeClient

TIER_INCLUDED_SEATS: dict[str, int] = {"starter": 3, "pro": 10, "enterprise": 25}

# Module-level singleton: every caller within one process shares the same
# FakeStripeClient instance, so a test that reports seat usage and then
# asserts on `reported_usage` sees calls made anywhere in that same test's
# request/task chain, not just within a single dependency resolution.
_stripe_client: StripeClient = FakeStripeClient()


def get_stripe_client() -> StripeClient:
    return _stripe_client
