import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, get_current_user, require_role
from app.core.tier_gating import require_module
from app.models import Company, CompanyUser, User
from app.schemas.company import (
    CompanyMemberListResponse,
    CompanyMemberResponse,
    CompanyResponse,
    CreateChildCompanyRequest,
)
from app.services.audit import write_audit_log

router = APIRouter(prefix="/companies", tags=["companies"])

_MEMBER_LIST_ROLES = ("admin", "project_manager")


# Declared ABOVE GET /{company_id}: FastAPI matches routes in declaration
# order, and the UUID path-param route would otherwise swallow the literal
# /members segment (422 UUID parse error).
@router.get("/members", response_model=CompanyMemberListResponse)
async def list_company_members(
    current: CurrentUser = Depends(require_role(*_MEMBER_LIST_ROLES)),
) -> CompanyMemberListResponse:
    """Members of the caller's active tenant, for task-assignee pickers.
    company_users' RLS scopes rows to the active tenant; the explicit
    company_id filter narrows a parent-company session (which can see
    descendant memberships) to the active tenant only — an assignee picker
    should offer this company's people, not the whole subtree's."""
    result = await current.session.execute(
        select(CompanyUser, User.full_name, User.email)
        .join(User, CompanyUser.user_id == User.id)
        .where(CompanyUser.company_id == current.company_id)
        .order_by(User.full_name, User.email)
    )
    return CompanyMemberListResponse(
        items=[
            CompanyMemberResponse(
                user_id=membership.user_id,
                full_name=full_name,
                email=email,
                role=membership.role,
            )
            for membership, full_name, email in result.all()
        ]
    )


@router.get("/{company_id}/users", response_model=CompanyMemberListResponse)
async def list_company_users(
    company_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_MEMBER_LIST_ROLES)),
) -> CompanyMemberListResponse:
    """Members of a specific company by id (the API spec's
    `GET /companies/{id}/users`, previously unimplemented) — the
    parameterized sibling of `/members` above, for a parent-company session
    inspecting a descendant branch's roster.

    The explicit Company visibility check comes first because a freshly
    created child branch legitimately has zero members — an empty member
    list must mean "visible company, no members" (200 + []), never stand in
    for "company not found". RLS makes another tenant's company invisible,
    so the 404 covers both "doesn't exist" and "exists but isn't yours",
    same intentional indistinguishability as `get_company` below.

    No declaration-order concern with `GET /{company_id}`: the extra
    literal `/users` segment gives this a different path shape (only
    same-shape routes like `/members` need the declared-above trick)."""
    company = (
        await current.session.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")

    result = await current.session.execute(
        select(CompanyUser, User.full_name, User.email)
        .join(User, CompanyUser.user_id == User.id)
        .where(CompanyUser.company_id == company_id)
        .order_by(User.full_name, User.email)
    )
    return CompanyMemberListResponse(
        items=[
            CompanyMemberResponse(
                user_id=membership.user_id,
                full_name=full_name,
                email=email,
                role=membership.role,
            )
            for membership, full_name, email in result.all()
        ]
    )


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company(company_id: uuid.UUID, current: CurrentUser = Depends(get_current_user)) -> CompanyResponse:
    result = await current.session.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if company is None:
        # RLS makes another tenant's company invisible, so this 404 covers
        # both "doesn't exist" and "exists but isn't yours" — intentionally
        # indistinguishable from the outside, which is the point.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return CompanyResponse.model_validate(company)


@router.post("/{company_id}/children", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_child_company(
    company_id: uuid.UUID,
    payload: CreateChildCompanyRequest,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("child_branches")),
) -> CompanyResponse:
    if company_id != current.company_id:
        # Admin must be acting within the parent's own tenant context (not
        # someone else's), enforced at the application layer in addition to
        # the tenant_insert RLS policy's parent_id check.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Can only create children of your active company")

    child = Company(parent_id=company_id, name=payload.name)
    current.session.add(child)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=company_id,
        actor_id=current.user.id,
        action="company.child_created",
        entity_type="company",
        entity_id=child.id,
    )
    # No explicit commit here — get_current_user (design decision #8) commits
    # current.session once, after this handler returns. An inline commit here
    # wouldn't be wrong (SQLAlchemy tolerates a second no-op commit), but it's
    # redundant and muddies who owns the transaction; one owner, one commit.

    return CompanyResponse.model_validate(child)
