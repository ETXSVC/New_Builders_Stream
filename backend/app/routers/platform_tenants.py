"""The platform console's tenant administration (migrations 0023, 0024).

What an operator can change here is deliberately small: a tenant's
subscription tier, its subscription status, its seat allowance,
per-module overrides on top of the tier, its name, whether it is in
service at all — and, in the one creating route, a new tenant's existence.
The `platform_admin` database role holds write privileges on exactly the
tables those need, so a bug in this file cannot damage a customer's
projects, estimates or invoices. The privilege is not held, rather than
merely not exercised.

**No route here deletes anything, and the role could not if one tried.**
Migration 0024 grants INSERT and UPDATE where they are needed and DELETE
nowhere (the sole exception predates it: clearing a module override, which
is how the third state is expressed). `DELETE /{company_id}` sets
`companies.deleted_at`; destroying a tenant for real means ~40 tables of
NO ACTION foreign keys and belongs to `scripts/prune_dev_tenants.py`,
behind a shell and the table owner, not behind a web session.

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
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.platform_deps import PlatformActor, get_platform_admin
from app.core.security import hash_password
from app.core.tier_gating import MODULE_MIN_TIER, TIER_RANK
from app.models import Company, CompanyModuleOverride, CompanyUser, Subscription, User
from app.models.base import utcnow
from app.schemas.platform import (
    ModuleEntitlement,
    ModuleOverrideRequest,
    SubscriptionUpdateRequest,
    TenantCreateRequest,
    TenantCreateResponse,
    TenantDetail,
    TenantPage,
    TenantSummary,
    TenantUpdateRequest,
)
from app.services.audit import write_audit_log
from app.services.billing import TIER_INCLUDED_SEATS, get_stripe_client

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
        deleted_at=company.deleted_at,
    )


@router.get("", response_model=TenantPage)
async def list_tenants(
    search: str | None = Query(default=None, max_length=255),
    roots_only: bool = Query(
        default=False,
        description="Only companies with no parent — the level entitlements are held at.",
    ),
    include_deleted: bool = Query(
        default=False,
        description=(
            "Include tenants taken out of service. Excluded by default so the "
            "list answers 'who are my customers', but reachable — a soft delete "
            "nobody can find again is a hard one with extra steps."
        ),
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
    if not include_deleted:
        # This company's own flag, not `is_company_live`: a live branch of a
        # deleted parent IS out of service, but hiding it here would leave an
        # operator unable to see the tree they just retired. The detail view
        # is where the ancestor rule is explained.
        query = query.where(Company.deleted_at.is_(None))

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


@router.post("", response_model=TenantCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreateRequest, actor: PlatformActor = Depends(get_platform_admin)
) -> TenantCreateResponse:
    """Bring a customer into existence: company, owner, membership, trial.

    The same four rows `POST /auth/register` writes, because it is the same
    act performed by a different actor — an operator onboarding a customer
    who was sold the product rather than one who found the signup form. The
    ordering is deliberately register's ordering too, including the Stripe
    calls BEFORE the transaction opens: see the long comment there for why
    a third-party call inside an open transaction is the thing to avoid.

    Not shared with register as one function, despite the overlap. That
    route additionally owns rate limiting, the refresh-token family and the
    response shape a browser expects, and it runs as `app_user` under RLS
    where this runs as `platform_admin` under none. Folding them together
    would mean a function that behaves differently in nearly every respect
    depending on who called it, which is how the branch that skips the RLS
    step gets taken by accident.
    """
    # Duplicate check before spending a Stripe call, exactly as register
    # does — it is the common way this fails.
    existing = await actor.session.execute(select(User.id).where(User.email == payload.owner_email))
    if existing.first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account")

    company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    # URL-safe and 32 bytes of entropy. Returned once (see the response
    # schema) and stored only as an Argon2id hash, like every other password.
    temporary_password = secrets.token_urlsafe(24)
    password_hash = hash_password(temporary_password)

    stripe_client = get_stripe_client()
    stripe_customer_id = await stripe_client.create_customer(
        email=payload.owner_email, name=payload.company_name
    )
    stripe_subscription = await stripe_client.create_trialing_subscription(
        customer_id=stripe_customer_id, tier=payload.tier, trial_days=14
    )

    # No set_current_tenant anywhere below: this session is `platform_admin`,
    # which is BYPASSRLS, so the WITH CHECK dance register performs between
    # its inserts has nothing to do here. Called out because the absence of
    # those lines is the kind of thing a reader assumes was forgotten.
    actor.session.add(Company(id=company_id, parent_id=None, name=payload.company_name))
    await actor.session.flush()

    actor.session.add(
        User(
            id=user_id,
            email=payload.owner_email,
            password_hash=password_hash,
            full_name=payload.owner_full_name,
        )
    )
    try:
        await actor.session.flush()
    except IntegrityError:
        # Lost the race with another signup between the check above and here.
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account")

    actor.session.add(CompanyUser(company_id=company_id, user_id=user_id, role="admin"))
    actor.session.add(
        Subscription(
            company_id=company_id,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription.stripe_subscription_id,
            tier=payload.tier,
            status=stripe_subscription.status,
            included_seats=TIER_INCLUDED_SEATS[payload.tier],
            current_period_end=stripe_subscription.current_period_end,
        )
    )
    await actor.session.flush()

    await write_audit_log(
        actor.session,
        company_id=company_id,
        actor_id=actor.user.id,
        action="platform.tenant_created",
        entity_type="company",
        entity_id=company_id,
        # The password is deliberately absent here. It exists in the response
        # and nowhere else.
        metadata={
            "name": payload.company_name,
            "tier": payload.tier,
            "owner_email": payload.owner_email,
            "by": actor.user.email,
        },
    )

    company = await _get_company(actor, company_id)
    summary = await _summarise(actor, company)
    return TenantCreateResponse(
        tenant=TenantDetail(
            **summary.model_dump(),
            modules=await _entitlements(actor, company_id, summary.tier),
            child_company_ids=[],
        ),
        owner_user_id=user_id,
        owner_email=payload.owner_email,
        temporary_password=temporary_password,
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


async def _detail_after_write(actor: PlatformActor, company: Company) -> TenantDetail:
    """The tenant as it now stands, for the mutation routes to return.

    `child_company_ids` is empty here, as it is on every other write route:
    these handlers do not re-query the tree. The console keeps the list its
    GET returned rather than replacing it — see TenantDetailView's `apply`.
    """
    root_id = await _root_company_id(actor, company.id)
    summary = await _summarise(actor, company)
    return TenantDetail(
        **summary.model_dump(),
        modules=await _entitlements(actor, root_id, summary.tier),
        child_company_ids=[],
    )


@router.patch("/{company_id}", response_model=TenantDetail)
async def rename_tenant(
    company_id: uuid.UUID,
    payload: TenantUpdateRequest,
    actor: PlatformActor = Depends(get_platform_admin),
) -> TenantDetail:
    """Correct a company's name.

    Deliberately NOT root-only, unlike the entitlement routes. A name is a
    property of the one company that carries it, so renaming a branch
    changes that branch and nothing else — there is no tree-wide effect to
    warn about, and refusing here would leave a typo in a branch office's
    name permanently uncorrectable.
    """
    company = await _get_company(actor, company_id)
    before = company.name
    if payload.name == before:
        # Nothing changed; do not write an audit row saying it did.
        return await _detail_after_write(actor, company)

    company.name = payload.name
    await actor.session.flush()

    await write_audit_log(
        actor.session,
        company_id=company.id,
        actor_id=actor.user.id,
        action="platform.tenant_renamed",
        entity_type="company",
        entity_id=company.id,
        metadata={"before": before, "after": payload.name, "by": actor.user.email},
    )
    return await _detail_after_write(actor, company)


@router.delete("/{company_id}", response_model=TenantDetail)
async def deactivate_tenant(
    company_id: uuid.UUID, actor: PlatformActor = Depends(get_platform_admin)
) -> TenantDetail:
    """Take a tenant out of service. SOFT — no row is removed.

    DELETE as the method because that is what an operator means and what the
    console's button says; what it does is set `companies.deleted_at`. The
    console holds no DELETE privilege on `companies` at all (migration
    0024), so this route could not destroy the row if it tried.

    Takes effect within one request, not one token lifetime: every
    authenticated request re-checks `is_company_live` at the membership
    chokepoint, so sessions already open stop working too. Branches go with
    the parent — that walk is why the check is SECURITY DEFINER.
    """
    company = await _get_company(actor, company_id)
    if company.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This tenant is already out of service")

    # `is_active` follows automatically: migration 0025 made it a generated
    # column over this one, so there is nothing to keep in step by hand.
    company.deleted_at = utcnow()
    await actor.session.flush()

    await write_audit_log(
        actor.session,
        company_id=company.id,
        actor_id=actor.user.id,
        action="platform.tenant_deactivated",
        entity_type="company",
        entity_id=company.id,
        metadata={"name": company.name, "by": actor.user.email},
    )
    return await _detail_after_write(actor, company)


@router.post("/{company_id}/restore", response_model=TenantDetail)
async def restore_tenant(
    company_id: uuid.UUID, actor: PlatformActor = Depends(get_platform_admin)
) -> TenantDetail:
    """Put a tenant back into service, exactly as it was.

    The reason the delete above is soft: nothing was lost, so nothing has to
    be rebuilt. A tenant whose PARENT is still out of service will restore
    its own row and remain unreachable, which is correct and is why the
    response carries `deleted_at` for the whole tree rather than a single
    "restored" boolean.
    """
    company = await _get_company(actor, company_id)
    if company.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This tenant is already in service")

    company.deleted_at = None
    await actor.session.flush()

    await write_audit_log(
        actor.session,
        company_id=company.id,
        actor_id=actor.user.id,
        action="platform.tenant_restored",
        entity_type="company",
        entity_id=company.id,
        metadata={"name": company.name, "by": actor.user.email},
    )
    return await _detail_after_write(actor, company)
