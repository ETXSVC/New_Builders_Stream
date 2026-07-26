import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import Vendor
from app.schemas.vendor import VendorCreateRequest, VendorListResponse, VendorPatchRequest, VendorResponse

router = APIRouter(tags=["vendors"])

# design spec Decision 7: read and write access to BOM/Vendor data is
# admin/PM only — deliberately narrower than catalogs.py's _READ_ROLES
# (which also grants accountant read), since this is a different explicit
# access decision for this feature, not an oversight.
_ROLES = ("admin", "project_manager")


@router.post("/vendors", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
async def create_vendor(
    payload: VendorCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> VendorResponse:
    vendor = Vendor(
        company_id=current.company_id,
        name=payload.name,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
        notes=payload.notes,
    )
    current.session.add(vendor)
    await current.session.flush()
    return VendorResponse.model_validate(vendor)


@router.get("/vendors", response_model=VendorListResponse)
async def list_vendors(
    current: CurrentUser = Depends(require_role(*_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> VendorListResponse:
    rows, next_cursor = await paginate(
        current.session,
        select(Vendor),
        created_at_col=Vendor.created_at,
        id_col=Vendor.id,
        cursor=cursor,
        limit=limit,
    )
    return VendorListResponse(
        items=[VendorResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


async def _get_vendor_or_404(current: CurrentUser, vendor_id: uuid.UUID) -> Vendor:
    result = await current.session.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
    return vendor


@router.patch("/vendors/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: uuid.UUID,
    payload: VendorPatchRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> VendorResponse:
    vendor = await _get_vendor_or_404(current, vendor_id)

    if payload.name is not None:
        vendor.name = payload.name
    if payload.contact_email is not None:
        vendor.contact_email = payload.contact_email
    if payload.contact_phone is not None:
        vendor.contact_phone = payload.contact_phone
    if payload.notes is not None:
        vendor.notes = payload.notes

    await current.session.flush()
    return VendorResponse.model_validate(vendor)
