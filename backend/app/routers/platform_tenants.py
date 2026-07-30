"""The platform console's tenant administration (migration 0023).

What an operator can change here is deliberately small: a tenant's
subscription tier, its subscription status, its seat allowance, and
per-module overrides on top of the tier. That is the whole mutable
surface, and the `platform_admin` database role holds write privileges on
exactly those two tables plus `audit_log` — so a bug in this file cannot
damage a customer's projects, estimates or invoices. The privilege is not
held, rather than merely not exercised.

ROOT-ONLY, AND LOUDLY SO. `subscriptions` rows may only hang off a company
with `parent_id IS NULL`, and both gates that read them resolve through
`get_root_company_id()`. Entitlements are therefore a property of a whole
tenant tree, not of one branch. A PATCH aimed at a child company is
REFUSED with the root's id in the message rather than silently redirected
upward: "I edited this branch" and "I edited the parent and every branch
under it" are very different intentions, and guessing between them from a
URL is how an operator changes more than they meant to.

Every mutation writes an `audit_log` row into the TARGET tenant, not into
some operator-only stream. The customer can see that their entitlements
were changed, by whom, and from what to what — which is the correct
default when the change was made by someone outside their company.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.platform_deps import PlatformActor, get_platform_admin
from app.core.tier_gating import MODULE_MIN_TIER, TIER_RANK
from app.models import Company, CompanyModuleOverride, CompanyUser, Subscription
from app.schemas.platform import (
    ModuleEntitlement,
    ModuleOverrideRequest,
    SubscriptionUpdateRequest,
    TenantDetail,
    TenantPage,
    TenantSummary,
)
from app.services.audit import write_audit_log

router = APIRouter(prefix="/platform/companies", tags=["platform"])

# Mirrors block_if_read_only's rule (app/core/deps.py): these two mean
# "may write", anything else means read-only. Listed here so the console can
# warn that a status is about to make a tenant read-only, rather than the
# operator discovering it from a customer's support ticket.
_WRITE_ENABLED_STATUSES = ("trialing", "active")


async def _get_company(actor: PlatformActor, company_id: uuid.UUID) -> Company:
    result = await actor.session.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such company")
    return company


async def _root_company_id(actor: PlatformActor, company_id: uuid.UUID) -> uuid.UUID:
    """The root of `company_id`'s tenant tree.

    A thin wrapper because `session.scalar` is typed `Any | None` and every
    caller here needs a `uuid.UUID`. The None is unreachable rather than
    merely unlikely: `get_root_company_id` walks `parent_id` up from a row
    that `_get_company` has already 404'd on if it did not exist, and the
    walk is total because migration 0021 makes `parent_id` immutable — so
    the tree cannot be re-pointed into a cycle or an orphan mid-request.
    Asserted rather than silently coerced so that reasoning fails loudly if
    it ever stops holding.
    """
    root_id = await actor.session.scalar(select(func.get_root_company_id(company_id)))
    assert root_id is not None, f"get_root_company_id returned NULL for {company_id}"
    return root_id


async def _require_root(actor: PlatformActor, company_id: uuid.UUID) -> Company:
    """Entitlements live on the root; refuse anything else by name."""
    company = await _get_company(actor, company_id)
    if company.parent_id is None:
        return company

    root_id = await _root_company_id(actor, company_id)
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"{company.name} is a child branch; its entitlements are held by its root "
        f"company. Target {root_id} instead — note that this changes every branch "
        "in that tree, not just this one.",
    )


async def _subscription_for_root(actor: PlatformActor, root_id: uuid.UUID) -> Subscription | None:
    result = await actor.session.execute(
        select(Subscription).where(Subscription.company_id == root_id)
    )
    return result.scalar_one_or_none()


async def _entitlements(
    actor: PlatformActor, root_id: uuid.UUID, tier: str | None
) -> list[ModuleEntitlement]:
    """Resolve every module the same way `tier_gating` will at request time.

    Kept in step with that module by construction: it iterates
    MODULE_MIN_TIER rather than a transcribed list, so a module added there
    appears here without anyone remembering to update this file.
    """
    result = await actor.session.execute(
        select(CompanyModuleOverride).where(CompanyModuleOverride.company_id == root_id)
    )
    overrides = {row.module: row for row in result.scalars().all()}

    entitlements = []
    for module, min_tier in MODULE_MIN_TIER.items():
        # Mirrors tier_allows' fail-open on a missing subscription row.
        allowed_by_tier = tier is None or TIER_RANK[tier] >= TIER_RANK[min_tier]
        override = overrides.get(module)
        entitlements.append(
            ModuleEntitlement(
                module=module,
                allowed_by_tier=allowed_by_tier,
                override=override.enabled if override is not None else None,
                effective=override.enabled if override is not None else allowed_by_tier,
                note=override.note if override is not None else None,
            )
        )
    return entitlements


async def _summarise(actor: PlatformActor, company: Company) -> TenantSummary:
    root_id = await _root_company_id(actor, company.id)
    subscription = await _subscription_for_root(actor, root_id)
    user_count = await actor.session.scalar(
        select(func.count()).select_from(CompanyUser).where(CompanyUser.company_id == company.id)
    )
    return TenantSummary(
        company_id=company.id,
        name=company.name,
        parent_id=company.parent_id,
        is_root=company.parent_id is None,
        tier=subscription.tier if subscription else None,
        status=subscription.status if subscription else None,
        included_seats=subscription.included_seats if subscription else None,
        manual_status_override=bool(subscription and subscription.manual_status_override),
        # Fails open exactly as block_if_read_only does, so the console shows
        # what the tenant will actually experience rather than what the row
        # literally says. A missing subscription reads as "writes allowed",
        # because that is what the gate decides.
        writes_enabled=subscription is None or subscription.status in _WRITE_ENABLED_STATUSES,
        user_count=user_count or 0,
        created_at=company.created_at,
    )


@router.get("", response_model=TenantPage)
async def list_tenants(
    search: str | None = Query(default=None, max_length=255),
    roots_only: bool = Query(
        default=False,
        description="Only companies with no parent — the level entitlements are held at.",
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    actor: PlatformActor = Depends(get_platform_admin),
) -> TenantPage:
    query = select(Company)
    if search:
        query = query.where(Company.name.ilike(f"%{search}%"))
    if roots_only:
        query = query.where(Company.parent_id.is_(None))

    companies, next_cursor = await paginate(
        actor.session,
        query,
        created_at_col=Company.created_at,
        id_col=Company.id,
        cursor=cursor,
        limit=limit,
    )

    return TenantPage(
        items=[await _summarise(actor, company) for company in companies],
        next_cursor=next_cursor,
    )


@router.get("/{company_id}", response_model=TenantDetail)
async def get_tenant(
    company_id: uuid.UUID, actor: PlatformActor = Depends(get_platform_admin)
) -> TenantDetail:
    company = await _get_company(actor, company_id)
    summary = await _summarise(actor, company)

    root_id = await _root_company_id(actor, company.id)
    children = await actor.session.execute(
        select(Company.id).where(Company.parent_id == company.id)
    )

    return TenantDetail(
        **summary.model_dump(),
        modules=await _entitlements(actor, root_id, summary.tier),
        child_company_ids=list(children.scalars().all()),
    )


@router.patch("/{company_id}/subscription", response_model=TenantDetail)
async def update_subscription(
    company_id: uuid.UUID,
    payload: SubscriptionUpdateRequest,
    actor: PlatformActor = Depends(get_platform_admin),
) -> TenantDetail:
    company = await _require_root(actor, company_id)
    subscription = await _subscription_for_root(actor, company.id)
    if subscription is None:
        # Both gates fail OPEN on a missing row, so this tenant currently
        # has everything. Creating a subscription here is out of scope --
        # the row is a mirror of Stripe's own state and is minted at
        # registration -- so say so plainly rather than pretending to act.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This company has no subscription row, so tier and status gates "
            "currently fail open for it. That row is created at registration "
            "and mirrored from Stripe; it cannot be created here.",
        )

    before = {
        "tier": subscription.tier,
        "status": subscription.status,
        "included_seats": subscription.included_seats,
        "manual_status_override": subscription.manual_status_override,
    }

    if payload.tier is not None:
        subscription.tier = payload.tier
    if payload.status is not None:
        subscription.status = payload.status
        # A hand-set status must survive the next Stripe event; see
        # webhooks.py and migration 0023's column comment.
        subscription.manual_status_override = True
    if payload.included_seats is not None:
        subscription.included_seats = payload.included_seats
    if payload.clear_manual_status_override:
        # Applied last so a request may both set a status and hand control
        # straight back, though the useful case is clearing it on its own.
        subscription.manual_status_override = False

    after = {
        "tier": subscription.tier,
        "status": subscription.status,
        "included_seats": subscription.included_seats,
        "manual_status_override": subscription.manual_status_override,
    }

    await write_audit_log(
        actor.session,
        company_id=company.id,
        actor_id=actor.user.id,
        action="platform.subscription_updated",
        entity_type="subscription",
        entity_id=subscription.id,
        metadata={"before": before, "after": after, "by": actor.user.email},
    )

    summary = await _summarise(actor, company)
    return TenantDetail(
        **summary.model_dump(),
        modules=await _entitlements(actor, company.id, summary.tier),
        child_company_ids=[],
    )


@router.put("/{company_id}/modules/{module}", response_model=TenantDetail)
async def set_module_override(
    company_id: uuid.UUID,
    module: str,
    payload: ModuleOverrideRequest,
    actor: PlatformActor = Depends(get_platform_admin),
) -> TenantDetail:
    if module not in MODULE_MIN_TIER:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such module: {module}")

    company = await _require_root(actor, company_id)

    result = await actor.session.execute(
        select(CompanyModuleOverride).where(
            CompanyModuleOverride.company_id == company.id,
            CompanyModuleOverride.module == module,
        )
    )
    override = result.scalar_one_or_none()
    previous = override.enabled if override is not None else None

    if override is None:
        override = CompanyModuleOverride(
            company_id=company.id, module=module, enabled=payload.enabled, note=payload.note
        )
        actor.session.add(override)
    else:
        override.enabled = payload.enabled
        override.note = payload.note
    override.set_by = actor.user.id
    await actor.session.flush()

    await write_audit_log(
        actor.session,
        company_id=company.id,
        actor_id=actor.user.id,
        action="platform.module_override_set",
        entity_type="company_module_override",
        entity_id=override.id,
        metadata={
            "module": module,
            "previous": previous,
            "enabled": payload.enabled,
            "note": payload.note,
            "by": actor.user.email,
        },
    )

    summary = await _summarise(actor, company)
    return TenantDetail(
        **summary.model_dump(),
        modules=await _entitlements(actor, company.id, summary.tier),
        child_company_ids=[],
    )


@router.delete("/{company_id}/modules/{module}", response_model=TenantDetail)
async def clear_module_override(
    company_id: uuid.UUID,
    module: str,
    actor: PlatformActor = Depends(get_platform_admin),
) -> TenantDetail:
    """Revert to whatever the tier says. Distinct from setting the override
    to false, which withholds a module the tier would otherwise allow."""
    if module not in MODULE_MIN_TIER:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such module: {module}")

    company = await _require_root(actor, company_id)

    result = await actor.session.execute(
        select(CompanyModuleOverride).where(
            CompanyModuleOverride.company_id == company.id,
            CompanyModuleOverride.module == module,
        )
    )
    override = result.scalar_one_or_none()
    if override is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No override set for {module} on this company"
        )

    override_id = override.id
    previous = override.enabled
    await actor.session.delete(override)
    await actor.session.flush()

    await write_audit_log(
        actor.session,
        company_id=company.id,
        actor_id=actor.user.id,
        action="platform.module_override_cleared",
        entity_type="company_module_override",
        entity_id=override_id,
        metadata={"module": module, "previous": previous, "by": actor.user.email},
    )

    summary = await _summarise(actor, company)
    return TenantDetail(
        **summary.model_dump(),
        modules=await _entitlements(actor, company.id, summary.tier),
        child_company_ids=[],
    )
