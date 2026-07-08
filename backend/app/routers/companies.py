import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, get_current_user
from app.models import Company
from app.schemas.company import CompanyResponse

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
