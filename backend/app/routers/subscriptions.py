"""Task 3.20: GET /subscriptions/me, POST /subscriptions/portal-session
(design spec Section 4).

RBAC deliberately splits from the RBAC matrix's blanket "Accounting/Billing:
Admin Full CRUD, Accountant Full CRUD" row: US-5.1 frames subscription
management as an Admin action specifically, and portal-session (change
payment method, cancel) is account-level on par with other Admin-only
actions elsewhere in this codebase. Accountant gets read-only visibility
via /subscriptions/me only. This narrower split is deliberate, not an
oversight — the blanket RBAC row's Accountant "Full CRUD" applies to the
LATER Invoicing/Expenses spec's own routes, not this one.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.deps import CurrentUser, require_role
from app.models import Subscription
from app.schemas.subscription import PortalSessionResponse, SubscriptionResponse
from app.services.billing import get_stripe_client

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

_READ_ROLES = ("admin", "accountant")
_PORTAL_ROLES = ("admin",)


async def _get_subscription_for_current(current: CurrentUser) -> Subscription:
    """`subscriptions` carries a deliberately non-standard, UPWARD-visibility
    RLS policy (migration `0010_billing_schema.py`) — a session scoped to a
    child branch resolves to its ROOT ancestor's single row via
    `get_root_company_id()`, not its own `company_id` directly. The WHERE
    clause below runs that resolution inline (one round trip, not two) —
    RLS itself is enforced independently via the session's own
    `current_tenant` setting, not by this query's WHERE clause.

    `.scalar_one()`, not `.scalar_one_or_none()`: every root company gets
    exactly one `Subscription` row atomically at registration
    (`app/routers/auth.py`'s `register` handler, Task 3.19), enforced by
    `uq_subscriptions_company_id`, and a root company can only ever be
    created there (`create_child_company`, `app/routers/companies.py`,
    always sets `parent_id`, never `NULL`). A session with no matching row
    at all means the RLS policy resolved to a tenant with no subscription,
    which should be unreachable — same "let it 500 rather than build
    defensive handling for an assumed-impossible case" judgment call this
    codebase already makes elsewhere (e.g. `estimate_calculation.py`'s
    equivalent `scalar_one()` use).
    """
    result = await current.session.execute(
        select(Subscription).where(
            Subscription.company_id == func.get_root_company_id(current.company_id)
        )
    )
    return result.scalar_one()


@router.get("/me", response_model=SubscriptionResponse)
async def get_my_subscription(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> SubscriptionResponse:
    subscription = await _get_subscription_for_current(current)
    return SubscriptionResponse.model_validate(subscription)


@router.post("/portal-session", response_model=PortalSessionResponse)
async def create_portal_session(
    current: CurrentUser = Depends(require_role(*_PORTAL_ROLES)),
) -> PortalSessionResponse:
    subscription = await _get_subscription_for_current(current)
    stripe_client = get_stripe_client()
    url = await stripe_client.create_portal_session(customer_id=subscription.stripe_customer_id)
    return PortalSessionResponse(url=url)
