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

        for subscription in subscriptions:
            count_result = await session.execute(
                select(func.count(func.distinct(CompanyUser.user_id))).where(
                    CompanyUser.company_id.in_(
                        select(func.get_all_descendant_ids(subscription.company_id))
                    )
                )
            )
            seat_count = count_result.scalar_one()

            overage = seat_count - subscription.included_seats
            if overage > 0:
                await stripe_client.report_seat_usage(
                    stripe_subscription_id=subscription.stripe_subscription_id, quantity=overage
                )


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
