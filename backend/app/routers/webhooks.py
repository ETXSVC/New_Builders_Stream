"""Task 3.21: POST /webhooks/stripe (design spec Section 4). Public — no
JWT — but signature-verified before anything else runs.

Handles exactly three event types, the minimum needed to keep
`subscriptions` an accurate mirror of Stripe's own state:
customer.subscription.updated, customer.subscription.deleted,
invoice.payment_failed. Any other event type is acknowledged (200, so
Stripe doesn't retry) and otherwise ignored — out of scope, not silently
swallowed.

No dedup/event-log table: all three handlers are idempotent last-write-wins
UPDATEs keyed on stripe_subscription_id, so Stripe's at-least-once webhook
redelivery is harmless without extra bookkeeping.

RLS note: `db.session_scope()`'s engine connects as the RLS-constrained
`app_user` role, and `subscriptions`' `tenant_isolation` policy (migration
0010) requires `app.current_tenant` to already resolve to a row's own root
company before that row is visible at all. This handler starts with no
tenant to scope to — it only has a `stripe_subscription_id` from the event
payload, and the whole point of the lookup is discovering which company
that belongs to (a chicken-and-egg problem: the RLS policy needs the tenant
to already be known, but resolving it normally requires reading the very
row RLS is hiding). `get_subscription_company_id()` (migration
0011_subscription_company_lookup.py) is a narrow, `SECURITY DEFINER`
function built exactly for this: it resolves `company_id` for one
`stripe_subscription_id` without requiring a tenant context. This handler
calls it once to learn the tenant, calls `set_current_tenant()`, and only
then does the actual read/mutate of the `Subscription` row through the
normal, RLS-scoped `app_user` session — same pattern every other write
path in this codebase uses (see app/routers/auth.py's register/login).

Uses `db.session_scope()` directly (like auth.py's register/login), not
`get_current_user` — there is no authenticated caller here, and this route
is deliberately excluded from `block_if_read_only` (Task 3.24, not yet
built) for the same reason: it's how a lapsed subscription's status even
GETS updated back to active after a real payment succeeds.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from app.db import session_scope, set_current_tenant
from app.models import Subscription
from app.services.audit import write_audit_log
from app.services.billing import get_stripe_client
from app.services.stripe_client import StripeSignatureError

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(request: Request) -> dict[str, bool]:
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature", "")

    stripe_client = get_stripe_client()
    try:
        event = stripe_client.verify_webhook_signature(payload=payload, signature_header=signature)
    except StripeSignatureError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid webhook signature")
    except (TypeError, ValueError, AttributeError):
        # An anonymous, unauthenticated caller controls every input to this
        # route (body + every header) — a non-ASCII Stripe-Signature header
        # makes hmac.compare_digest itself raise TypeError before any
        # signature comparison happens, and a validly-signed-but-malformed
        # (non-JSON, or JSON that isn't an object) body raises from
        # json.loads or the dict-shaped access below. Both must 400, not
        # leak an unhandled 500 with a stack trace on a public endpoint.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed webhook request")

    event_type = event.get("type") if isinstance(event, dict) else None
    obj = event.get("data", {}).get("object", {}) if isinstance(event, dict) else {}

    try:
        if event_type == "customer.subscription.updated":
            await _sync_subscription(
                stripe_subscription_id=obj["id"],
                new_status=obj["status"],
                current_period_end=obj.get("current_period_end"),
            )
        elif event_type == "customer.subscription.deleted":
            await _sync_subscription(
                stripe_subscription_id=obj["id"], new_status="canceled", current_period_end=None
            )
        elif event_type == "invoice.payment_failed":
            await _sync_subscription(
                stripe_subscription_id=obj["subscription"],
                new_status="past_due",
                current_period_end=None,
            )
        # Any other event type: acknowledged, ignored — out of scope.
    except KeyError:
        # A validly-signed event of a type we handle, but missing a field
        # our own handling for that type requires (a shape mismatch, not a
        # signature problem) — 400, not an unhandled 500.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed webhook event payload")

    return {"received": True}


async def _sync_subscription(
    *, stripe_subscription_id: str, new_status: str, current_period_end
) -> None:
    async with session_scope() as session:
        async with session.begin():
            # Resolve the tenant first, via the narrow SECURITY DEFINER
            # lookup (migration 0011) — this works without any tenant
            # context set yet, since the function itself bypasses RLS for
            # exactly this one lookup shape.
            company_id_result = await session.execute(
                select(func.get_subscription_company_id(stripe_subscription_id))
            )
            company_id = company_id_result.scalar_one_or_none()
            if company_id is None:
                # Unknown subscription id — nothing in our data to sync.
                # Acknowledged at the route level regardless; nothing to do
                # here.
                return

            await set_current_tenant(session, str(company_id))

            # Now RLS-visible: subscriptions' policy resolves to itself
            # when the tenant IS already root (get_root_company_id's own
            # base-case convention), which is guaranteed here since
            # company_id above came straight from this same row.
            result = await session.execute(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == stripe_subscription_id
                )
            )
            subscription = result.scalar_one()

            previous_status = subscription.status
            if current_period_end is not None:
                subscription.current_period_end = datetime.fromtimestamp(
                    current_period_end, tz=timezone.utc
                )

            if previous_status == new_status:
                # No actual status change — could be a genuine redelivery,
                # or a legitimate renewal event that only advances
                # current_period_end (already persisted above) without
                # changing status. Either way, per design spec Section 4:
                # audit entries fire on status transitions only, not on
                # routine current_period_end-only refreshes — so no
                # write_audit_log call here is the correct, deliberate
                # behavior, not a gap.
                return

            subscription.status = new_status
            await write_audit_log(
                session,
                company_id=subscription.company_id,
                actor_id=None,
                action="subscription.status_changed",
                entity_type="subscription",
                entity_id=subscription.id,
                metadata={"previous_status": previous_status, "new_status": new_status},
            )
