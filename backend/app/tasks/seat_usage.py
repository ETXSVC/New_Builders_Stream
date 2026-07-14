"""Task 3.23: daily seat-overage metering (design spec Section 5).

Same genuinely-cross-tenant shape as check_compliance_expiry
(app/tasks/compliance_expiry.py) — this scan needs to look at EVERY root
company's subscription in one run, so it uses the identical owner-role
engine pattern (connects as settings.migrations_database_url, the
`postgres` table-owner role, RLS-exempt) for the same reason that module's
own docstring explains at length. Not repeated here in full; see that
file's docstring for the complete justification.

Unique-user counting (design spec Section 5): for each root company's
subscription, counts DISTINCT user_id across company_users rows for the
root AND every descendant branch combined (get_all_descendant_ids(root_id)
— a person with membership in two branches counts once). There is no
separate "active" flag on company_users in this codebase; a membership
row's mere existence is the signal.

Retry safety: this actor's `max_retries=3` re-invokes the WHOLE function
from scratch on failure, same as check_compliance_expiry's own actor.
That job is safe to retry because its only side effect (an INSERT) is
protected by a UniqueConstraint. report_seat_usage's side effect — a
Stripe API call — has no equivalent protection: FakeStripeClient's
`reported_usage` is a bare append with no dedup, and this codebase makes
no assumption about whether a future RealStripeClient's usage-record API
is idempotent ("set" semantics) or cumulative ("increment" semantics) —
that's for the RealStripeClient task itself to pin down, not this one.
Given that, the per-subscription try/except below deliberately does NOT
re-raise on failure (which would trigger Dramatiq's own retry and
re-report every OTHER subscription already successfully handled earlier
in the same run) — it skips just the failed subscription and lets
tomorrow's scheduled run pick it back up, since this job is a fresh daily
snapshot rather than a delta. A subscription skipped today is delayed by
one day, not lost, and no already-succeeded subscription risks a
duplicate report.

Snapshot consistency: each run computes seat counts from a single point
in time — a user added mid-scan is counted starting next run, not this
one. Inherent to any periodic-snapshot metering job; not something a
locking scheme should try to fix here.
"""
from __future__ import annotations

import dramatiq
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import CompanyUser, Subscription
from app.services.billing import get_stripe_client
from app.tasks import broker  # noqa: F401 - import-time side effect

_owner_engine = create_async_engine(settings.migrations_database_url, pool_pre_ping=True)
_OwnerSessionLocal = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)


async def _report_seat_usage(
    session_factory: async_sessionmaker[AsyncSession] = _OwnerSessionLocal,
) -> None:
    stripe_client = get_stripe_client()

    async with session_factory() as session:
        subscriptions_result = await session.execute(
            select(Subscription).where(Subscription.status.in_(("trialing", "active", "past_due")))
        )
        subscriptions = subscriptions_result.scalars().all()

        # Per-subscription queries, not a single batched query, mirroring
        # check_compliance_expiry's own accepted-tradeoff shape at today's
        # scale (tens to low hundreds of root companies) — see that
        # module's own "future scaling cliff" note for the equivalent
        # reasoning. A future optimization would prefetch every relevant
        # company_users row once and reduce in memory, the same way that
        # job prefetches its own two full-table scans.
        for subscription in subscriptions:
            # Per-subscription isolation: a failure reporting THIS
            # subscription's usage must not prevent (or, on Dramatiq
            # retry, cause a duplicate report of) every other
            # subscription already handled earlier in this same run — see
            # the module docstring's "Retry safety" section.
            try:
                descendant_ids = func.get_all_descendant_ids(subscription.company_id).table_valued("id")
                count_result = await session.execute(
                    select(func.count(func.distinct(CompanyUser.user_id))).where(
                        CompanyUser.company_id.in_(select(descendant_ids.c.id))
                    )
                )
                seat_count = count_result.scalar_one()

                overage = seat_count - subscription.included_seats
                if overage > 0:
                    await stripe_client.report_seat_usage(
                        stripe_subscription_id=subscription.stripe_subscription_id,
                        quantity=overage,
                    )
            except Exception:
                # Skip, don't re-raise: re-raising would make Dramatiq
                # retry the WHOLE actor (max_retries=3), which would
                # re-report every OTHER subscription already successfully
                # reported earlier in this same run — a worse outcome than
                # one subscription's overage staying unreported until
                # tomorrow's scheduled run picks it up again. This job is
                # daily and self-healing by construction (each run is a
                # fresh snapshot, not a delta), so a subscription skipped
                # today is not lost, only delayed by one day. No logging
                # framework exists anywhere in this codebase yet
                # (grep confirms zero usage) — adding one is out of scope
                # for this task; if operational visibility into skips
                # becomes necessary, that's a dedicated follow-up.
                continue


# The actual `@dramatiq.actor` — a thin wrapper around `_report_seat_usage`
# (see that function's own docstring, and compliance_expiry.py's
# `_check_compliance_expiry` docstring, for why the undecorated-function/
# decorated-actor split exists: Dramatiq wraps every `async def` actor's
# `fn` in `async_to_sync()`, which requires a running worker's event loop
# thread; tests must be able to `await` the bare coroutine directly
# instead). `actor_name` keeps the queued message's actor name matching
# this module-level name rather than the wrapped function's own `__name__`.
report_seat_usage = dramatiq.actor(max_retries=3, actor_name="report_seat_usage")(
    _report_seat_usage
)
