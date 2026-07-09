import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, get_current_user, require_role
from app.models import Company
from app.schemas.company import CompanyResponse, CreateChildCompanyRequest
from app.services.audit import write_audit_log

router = APIRouter(prefix="/companies", tags=["companies"])


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
