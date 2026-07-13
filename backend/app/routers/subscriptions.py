# backend/app/routers/subscriptions.py
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
from app.schemas.subscription import SubscriptionResponse
from app.services.billing import get_stripe_client

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

_READ_ROLES = ("admin", "accountant")
_PORTAL_ROLES = ("admin",)


async def _get_subscription_for_current(current: CurrentUser) -> Subscription:
    root_id_result = await current.session.execute(
        select(func.get_root_company_id(current.company_id))
    )
    root_id = root_id_result.scalar_one()
    result = await current.session.execute(
        select(Subscription).where(Subscription.company_id == root_id)
    )
    return result.scalar_one()


@router.get("/me", response_model=SubscriptionResponse)
async def get_my_subscription(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> SubscriptionResponse:
    subscription = await _get_subscription_for_current(current)
    return SubscriptionResponse.model_validate(subscription)


@router.post("/portal-session")
async def create_portal_session(
    current: CurrentUser = Depends(require_role(*_PORTAL_ROLES)),
) -> dict[str, str]:
    subscription = await _get_subscription_for_current(current)
    stripe_client = get_stripe_client()
    url = await stripe_client.create_portal_session(customer_id=subscription.stripe_customer_id)
    return {"url": url}
