"""Task 3.41 (design spec Section 4): POST/GET /bills, GET /bills/{id}.

Top-level routes, not project-nested — bills.project_id is optional (company
overhead bills), unlike invoices which are always project-scoped. RBAC per
docs/07-security-compliance.md Section 2's "Accounting/Billing (AP)" row:
Admin/Accountant only, ZERO Client access — Bills are the company's own
internal obligations, never client-facing (unlike Invoices).
"""
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Bill, BillPayment
from app.routers.projects import _get_project_or_404
from app.routers.subcontractors import _get_subcontractor_or_404
from app.schemas.bill import (
    BillCreateRequest,
    BillDetailResponse,
    BillListResponse,
    BillPaymentResponse,
    BillResponse,
)

router = APIRouter(prefix="/bills", tags=["bills"])

_ROLES = ("admin", "accountant")


async def _get_bill_or_404(current: CurrentUser, bill_id: uuid.UUID) -> Bill:
    result = await current.session.execute(select(Bill).where(Bill.id == bill_id))
    bill = result.scalar_one_or_none()
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found")
    return bill


async def _paid_amount(current: CurrentUser, bill_id: uuid.UUID) -> Decimal:
    result = await current.session.execute(
        select(func.coalesce(func.sum(BillPayment.amount), 0)).where(BillPayment.bill_id == bill_id)
    )
    return result.scalar_one()


async def _bill_response(current: CurrentUser, bill: Bill) -> BillResponse:
    paid = await _paid_amount(current, bill.id)
    return BillResponse(
        id=bill.id,
        company_id=bill.company_id,
        project_id=bill.project_id,
        subcontractor_id=bill.subcontractor_id,
        vendor_name=bill.vendor_name,
        bill_number=bill.bill_number,
        amount=bill.amount,
        status=bill.status,
        due_date=bill.due_date,
        created_at=bill.created_at,
        outstanding_balance=bill.amount - paid,
    )


@router.post("", response_model=BillResponse, status_code=status.HTTP_201_CREATED)
async def create_bill(
    body: BillCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> BillResponse:
    project_id: uuid.UUID | None = None
    if body.project_id is not None:
        project = await _get_project_or_404(current, body.project_id)
        project_id = project.id

    vendor_name = body.vendor_name
    if body.subcontractor_id is not None:
        subcontractor = await _get_subcontractor_or_404(current, body.subcontractor_id)
        vendor_name = vendor_name or subcontractor.name

    bill = Bill(
        id=uuid.uuid4(),
        company_id=current.company_id,
        project_id=project_id,
        subcontractor_id=body.subcontractor_id,
        vendor_name=vendor_name,
        bill_number=body.bill_number,
        amount=body.amount,
        status="unpaid",
        due_date=body.due_date,
    )
    current.session.add(bill)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    return await _bill_response(current, bill)


@router.get("", response_model=BillListResponse)
async def list_bills(
    current: CurrentUser = Depends(require_role(*_ROLES)),
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> BillListResponse:
    query = select(Bill)
    if project_id is not None:
        query = query.where(Bill.project_id == project_id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Bill.created_at,
        id_col=Bill.id,
        cursor=cursor,
        limit=limit,
    )

    if not rows:
        return BillListResponse(items=[], next_cursor=next_cursor)

    # One bounded query over the whole page's bill_ids, not one
    # _paid_amount() call per row — same "second query scoped to the
    # current page, not an N+1" fix list_invoices (app/routers/invoices.py)
    # already applies for the identical AR/AP-mirrored trade-off.
    bill_ids = [row.id for row in rows]
    paid_result = await current.session.execute(
        select(BillPayment.bill_id, func.coalesce(func.sum(BillPayment.amount), 0))
        .where(BillPayment.bill_id.in_(bill_ids))
        .group_by(BillPayment.bill_id)
    )
    paid_by_bill_id: dict[uuid.UUID, Decimal] = dict(paid_result.all())

    items = [
        BillResponse(
            id=row.id,
            company_id=row.company_id,
            project_id=row.project_id,
            subcontractor_id=row.subcontractor_id,
            vendor_name=row.vendor_name,
            bill_number=row.bill_number,
            amount=row.amount,
            status=row.status,
            due_date=row.due_date,
            created_at=row.created_at,
            outstanding_balance=row.amount - paid_by_bill_id.get(row.id, Decimal("0")),
        )
        for row in rows
    ]
    return BillListResponse(items=items, next_cursor=next_cursor)


@router.get("/{bill_id}", response_model=BillDetailResponse)
async def get_bill(
    bill_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> BillDetailResponse:
    bill = await _get_bill_or_404(current, bill_id)

    payments_result = await current.session.execute(
        select(BillPayment)
        .where(BillPayment.bill_id == bill.id)
        .order_by(BillPayment.paid_date.asc(), BillPayment.id.asc())
    )
    payments = [
        BillPaymentResponse.model_validate(payment) for payment in payments_result.scalars().all()
    ]

    base = await _bill_response(current, bill)
    return BillDetailResponse(**base.model_dump(), payments=payments)
