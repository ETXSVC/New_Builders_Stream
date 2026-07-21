import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import BomLine, BomLineReceipt, Project, Vendor
from app.models.base import utcnow
from app.schemas.bom_line import (
    BomLineListResponse,
    BomLineManualCreateRequest,
    BomLinePatchRequest,
    BomLineReceiptCreateRequest,
    BomLineReceiptResponse,
    BomLineResponse,
)
from app.services.audit import write_audit_log

router = APIRouter(tags=["bom"])

# design spec Decision 7 — see vendors.py's own comment on the same choice.
_ROLES = ("admin", "project_manager")


def _compute_status(quantity: Decimal, ordered: bool, quantity_received: Decimal) -> str:
    """design spec Decision 3's table, in priority order: a fully (or
    over-) received line is "received" even if `ordered` was never
    explicitly set — a receipt can be logged without a prior "mark
    ordered" action (e.g. a PM logging a delivery they didn't personally
    order)."""
    if quantity_received >= quantity:
        return "received"
    if quantity_received > 0:
        return "partially_received"
    if ordered:
        return "ordered"
    return "needed"


async def _received_totals(
    session: AsyncSession, bom_line_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not bom_line_ids:
        return {}
    result = await session.execute(
        select(BomLineReceipt.bom_line_id, func.coalesce(func.sum(BomLineReceipt.quantity), 0))
        .where(BomLineReceipt.bom_line_id.in_(bom_line_ids))
        .group_by(BomLineReceipt.bom_line_id)
    )
    return {row[0]: row[1] for row in result.all()}


def _bom_line_response(line: BomLine, quantity_received: Decimal) -> BomLineResponse:
    return BomLineResponse(
        id=line.id,
        company_id=line.company_id,
        project_id=line.project_id,
        cost_catalog_item_id=line.cost_catalog_item_id,
        vendor_id=line.vendor_id,
        description=line.description,
        unit=line.unit,
        quantity=line.quantity,
        ordered=line.ordered,
        ordered_at=line.ordered_at,
        source=line.source,
        quantity_received=quantity_received,
        status=_compute_status(line.quantity, line.ordered, quantity_received),
        created_at=line.created_at,
        updated_at=line.updated_at,
    )


async def _get_project_or_404(current: CurrentUser, project_id: uuid.UUID) -> Project:
    result = await current.session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


async def _paginated_bom_lines(
    current: CurrentUser, *, project_id: uuid.UUID | None, cursor: str | None, limit: int
) -> BomLineListResponse:
    """Shared by both list routes below. No server-side status filter —
    status is derived, not a DB column, and the standard `paginate()`
    helper (LIMIT-based, at the DB level) can't filter on a value it
    hasn't computed yet. The frontend walks every page and filters by
    status client-side, the same "load all, filter/group in the browser"
    pattern already established by MarkupProfilesTab/ChangeOrdersTab for
    every other moderate-cardinality list in this app."""
    query = select(BomLine)
    if project_id is not None:
        query = query.where(BomLine.project_id == project_id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=BomLine.created_at,
        id_col=BomLine.id,
        cursor=cursor,
        limit=limit,
    )

    totals = await _received_totals(current.session, [row.id for row in rows])
    items = [_bom_line_response(row, totals.get(row.id, Decimal("0"))) for row in rows]
    return BomLineListResponse(items=items, next_cursor=next_cursor)


@router.post(
    "/projects/{project_id}/materials",
    response_model=BomLineResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_bom_line(
    project_id: uuid.UUID,
    payload: BomLineManualCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> BomLineResponse:
    await _get_project_or_404(current, project_id)

    line = BomLine(
        company_id=current.company_id,
        project_id=project_id,
        cost_catalog_item_id=None,
        vendor_id=None,
        description=payload.description,
        unit=payload.unit,
        quantity=payload.quantity,
        ordered=False,
        ordered_at=None,
        source="manual",
    )
    current.session.add(line)
    await current.session.flush()
    return _bom_line_response(line, Decimal("0"))


@router.get("/projects/{project_id}/materials", response_model=BomLineListResponse)
async def list_project_bom_lines(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> BomLineListResponse:
    await _get_project_or_404(current, project_id)
    return await _paginated_bom_lines(current, project_id=project_id, cursor=cursor, limit=limit)


@router.get("/materials", response_model=BomLineListResponse)
async def list_bom_lines(
    current: CurrentUser = Depends(require_role(*_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> BomLineListResponse:
    return await _paginated_bom_lines(current, project_id=None, cursor=cursor, limit=limit)


async def _get_bom_line_or_404(current: CurrentUser, bom_line_id: uuid.UUID) -> BomLine:
    result = await current.session.execute(select(BomLine).where(BomLine.id == bom_line_id))
    line = result.scalar_one_or_none()
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Material not found")
    return line


@router.patch("/materials/{bom_line_id}", response_model=BomLineResponse)
async def update_bom_line(
    bom_line_id: uuid.UUID,
    payload: BomLinePatchRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> BomLineResponse:
    line = await _get_bom_line_or_404(current, bom_line_id)

    if payload.vendor_id is not None:
        # 404, not 403, on a nonexistent/invisible vendor — RLS already
        # scopes this SELECT to the caller's tenant, so a cross-tenant
        # vendor_id naturally returns None here (Inherited Invariant #8).
        vendor_result = await current.session.execute(
            select(Vendor.id).where(Vendor.id == payload.vendor_id)
        )
        if vendor_result.scalar_one_or_none() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
        line.vendor_id = payload.vendor_id

    if payload.ordered is True and not line.ordered:
        line.ordered = True
        line.ordered_at = utcnow()

    await current.session.flush()
    totals = await _received_totals(current.session, [line.id])
    return _bom_line_response(line, totals.get(line.id, Decimal("0")))


@router.post(
    "/materials/{bom_line_id}/receipts",
    response_model=BomLineReceiptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_bom_line_receipt(
    bom_line_id: uuid.UUID,
    payload: BomLineReceiptCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> BomLineReceiptResponse:
    """No row-lock, unlike record_invoice_payment's use of
    `.with_for_update()`: that lock exists because concurrent payments
    each independently decide whether to flip Invoice.status to "paid" —
    a genuine read-then-conditionally-write race on a STORED field. A
    BomLine has no stored status to race on; status is always recomputed
    fresh from a live SUM on every read, so two concurrent receipts just
    both get recorded and the next read sums both correctly regardless of
    interleaving."""
    line = await _get_bom_line_or_404(current, bom_line_id)

    receipt = BomLineReceipt(
        bom_line_id=line.id,
        company_id=line.company_id,
        quantity=payload.quantity,
        recorded_by_user_id=current.user.id,
    )
    current.session.add(receipt)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=line.company_id,
        actor_id=current.user.id,
        action="bom_line.receipt_recorded",
        entity_type="bom_line",
        entity_id=line.id,
        metadata={"receipt_id": str(receipt.id), "quantity": str(payload.quantity)},
    )

    return BomLineReceiptResponse.model_validate(receipt)
