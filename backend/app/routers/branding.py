"""GET/PUT /companies/branding, POST /companies/branding/logo — spec
Decision 8. Admin-only for writes (logo/accent/footer are company identity,
narrower than the Estimation module's own admin+PM write convention);
admin+PM read (the PDF template tab is admin-only per the spec, but PM
still benefits from seeing current branding while building an estimate).
No tier gate — branding isn't part of MODULE_MIN_TIER's estimation-specific
feature set, it applies to any company regardless of tier.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.models import CompanyBranding
from app.schemas.company_branding import CompanyBrandingPutRequest, CompanyBrandingResponse
from app.services.document_storage import UnsupportedLogoError, write_company_logo_file

router = APIRouter(prefix="/companies/branding", tags=["branding"])

_WRITE_ROLES = ("admin",)
_READ_ROLES = ("admin", "project_manager")


async def _get_or_create_branding(current: CurrentUser) -> CompanyBranding:
    result = await current.session.execute(
        select(CompanyBranding).where(CompanyBranding.company_id == current.company_id)
    )
    branding = result.scalar_one_or_none()
    if branding is None:
        branding = CompanyBranding(company_id=current.company_id)
        current.session.add(branding)
        await current.session.flush()
    return branding


@router.get("", response_model=CompanyBrandingResponse)
async def get_branding(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> CompanyBrandingResponse:
    branding = await _get_or_create_branding(current)
    return CompanyBrandingResponse.model_validate(branding)


@router.put("", response_model=CompanyBrandingResponse)
async def put_branding(
    payload: CompanyBrandingPutRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CompanyBrandingResponse:
    branding = await _get_or_create_branding(current)
    branding.accent_color = payload.accent_color
    branding.footer_text = payload.footer_text
    await current.session.flush()
    return CompanyBrandingResponse.model_validate(branding)


@router.post("/logo", response_model=CompanyBrandingResponse)
async def upload_branding_logo(
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CompanyBrandingResponse:
    branding = await _get_or_create_branding(current)
    content = await file.read()

    try:
        relative_path = write_company_logo_file(
            company_id=current.company_id,
            content_type=file.content_type or "",
            content=content,
        )
    except UnsupportedLogoError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    branding.logo_storage_path = relative_path
    await current.session.flush()
    return CompanyBrandingResponse.model_validate(branding)
