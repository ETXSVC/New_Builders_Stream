import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select

from app.core.deps import CurrentUser, block_if_read_only, get_current_user, require_role
from app.core.tier_gating import require_module
from app.models import Company, CompanyUser, LeadClient, ProjectClient, User
from app.schemas.company import (
    CompanyMemberListResponse,
    CompanyMemberResponse,
    CompanyRenameRequest,
    CompanyResponse,
    CreateChildCompanyRequest,
    MemberRoleUpdateRequest,
)
from app.services.audit import write_audit_log

router = APIRouter(prefix="/companies", tags=["companies"])

_MEMBER_LIST_ROLES = ("admin", "project_manager")


async def _get_membership_or_404(
    current: CurrentUser, user_id: uuid.UUID
) -> tuple[CompanyUser, User]:
    """The membership in the caller's ACTIVE tenant, plus its user.

    Scoped by `company_id == current.company_id` rather than relying on RLS
    alone: a parent-branch admin can see descendant memberships, and
    "remove this person" must mean "from the company I am acting as", never
    "from whichever branch this id happens to match first".
    """
    result = await current.session.execute(
        select(CompanyUser, User)
        .join(User, CompanyUser.user_id == User.id)
        .where(CompanyUser.user_id == user_id, CompanyUser.company_id == current.company_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    return row[0], row[1]


async def _require_another_admin_remains(current: CurrentUser, excluding_user_id: uuid.UUID) -> None:
    """Refuse to leave a company with no admins.

    Every administrative route in this codebase is `require_role("admin")`,
    so a company with zero admins cannot invite anyone, fix its own
    subscription, or restore an admin — it is locked out permanently, with
    no in-product recovery path.
    """
    result = await current.session.execute(
        select(CompanyUser.user_id).where(
            CompanyUser.company_id == current.company_id,
            CompanyUser.role == "admin",
            CompanyUser.user_id != excluding_user_id,
        )
    )
    if result.first() is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This is the company's last admin; promote another admin first",
        )




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
async def get_company(company_id: uuid.UUID, current: CurrentUser = Depends(get_current_user, scope="function")) -> CompanyResponse:
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


# --- Member management ------------------------------------------------------
#
# Until these existed, `GET /companies/members` was the ONLY membership route:
# there was no way to remove a member, change a role, or otherwise offboard
# an employee through the API at all. That is not a polish gap — a departing
# employee kept working access indefinitely, and the only remedy was a manual
# DELETE against the database.
#
# Admin only. Role assignment and offboarding are the two decisions that
# define who can do what inside a company; a project_manager who could grant
# admin could grant it to themselves.


@router.patch("/members/{user_id}", response_model=CompanyMemberResponse)
async def update_member_role(
    user_id: uuid.UUID,
    payload: MemberRoleUpdateRequest,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> CompanyMemberResponse:
    membership, user = await _get_membership_or_404(current, user_id)

    if membership.user_id == current.user.id and payload.role != "admin":
        # Self-demotion is how a company ends up with zero admins and no way
        # back in. The last-admin guard below would catch the specific case
        # of the final admin, but this rejects the whole class earlier and
        # with a clearer message.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You cannot change your own role; ask another admin",
        )
    if membership.role == "admin" and payload.role != "admin":
        await _require_another_admin_remains(current, user_id)

    previous_role = membership.role
    membership.role = payload.role
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="company.member_role_changed",
        entity_type="company_user",
        entity_id=membership.user_id,
        metadata={"from": previous_role, "to": payload.role},
    )
    return CompanyMemberResponse(
        user_id=membership.user_id,
        full_name=user.full_name,
        email=user.email,
        role=membership.role,
    )


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> None:
    """Offboard a member from the caller's active tenant.

    Deletes the `company_users` row, not the `users` row: an identity is
    global in this schema (one person can belong to several companies), so
    deleting the user would revoke their access everywhere and destroy the
    audit trail's actor references. Removing the membership is exactly
    "they no longer work here".

    Their `project_clients`/`lead_clients` rows cascade away with the user
    only on user deletion, so those are cleaned up explicitly here —
    otherwise a re-invited client would silently regain access to the jobs
    they used to be on.
    """
    membership, _user = await _get_membership_or_404(current, user_id)

    if membership.user_id == current.user.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You cannot remove yourself; ask another admin",
        )
    if membership.role == "admin":
        await _require_another_admin_remains(current, user_id)

    await current.session.execute(
        delete(ProjectClient).where(
            ProjectClient.user_id == user_id, ProjectClient.company_id == current.company_id
        )
    )
    await current.session.execute(
        delete(LeadClient).where(
            LeadClient.user_id == user_id, LeadClient.company_id == current.company_id
        )
    )
    await current.session.delete(membership)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="company.member_removed",
        entity_type="company_user",
        entity_id=user_id,
    )


@router.patch("/{company_id}", response_model=CompanyResponse)
async def rename_company(
    company_id: uuid.UUID,
    payload: CompanyRenameRequest,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> CompanyResponse:
    """Rename only. `parent_id` is deliberately not editable through any
    route: re-parenting a company moves an entire subtree between tenants,
    and the `tenant_update` policy's WITH CHECK exists to stop exactly that
    (migration 0021 tightened it further). A legitimate re-parent is a
    migration, not an API call."""
    if company_id != current.company_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Can only rename your active company"
        )

    result = await current.session.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")

    previous_name = company.name
    company.name = payload.name
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=company_id,
        actor_id=current.user.id,
        action="company.renamed",
        entity_type="company",
        entity_id=company_id,
        metadata={"from": previous_name, "to": payload.name},
    )
    return CompanyResponse.model_validate(company)
