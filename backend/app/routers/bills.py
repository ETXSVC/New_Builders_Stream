"""Task 3.41 (design spec Section 4): POST/GET /bills, GET /bills/{id}.

Top-level routes, not project-nested — bills.project_id is optional (company
overhead bills), unlike invoices which are always project-scoped. RBAC per
docs/07-security-compliance.md Section 2's "Accounting/Billing (AP)" row:
Admin/Accountant only, ZERO Client access — Bills are the company's own
internal obligations, never client-facing (unlike Invoices).
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.events import publish
from app.core.money import CENTS
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import Bill, BillPayment
from app.routers.projects import _get_project_or_404
from app.routers.subcontractors import _get_subcontractor_or_404
from app.schemas.bill import (
    BillCreateRequest,
    BillDetailResponse,
    BillListResponse,
    BillPaymentCreateRequest,
    BillPaymentResponse,
    BillResponse,
)
from app.services.audit import write_audit_log

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
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> BillResponse:
    # `resolved_company_id` — the referenced Project's (or, absent a
    # Project, the referenced Subcontractor's) own `company_id`, NOT
    # `current.company_id` — is what actually stamps the new Bill below.
    # A parent company's session can legitimately create a Bill against a
    # descendant branch's Project/Subcontractor without switching
    # `X-Tenant-ID` to that branch first (RLS's `get_all_descendant_ids()`
    # grant already makes the descendant's rows visible/writable). Using
    # `current.company_id` would silently stamp this Bill with the
    # PARENT's id instead of its own Project's/Subcontractor's, producing
    # a row whose `company_id` disagrees with the resource it's actually
    # billed against — a session later scoped directly to the descendant
    # branch would then find its own Project's/Subcontractor's Bill
    # invisible under RLS. Same bug class already fixed in
    # change_orders.py/expenses.py/subcontractor_assignments.py,
    # projects.py's upload_document/create_daily_log, tasks.py's
    # create_phase/create_task, and estimates.py's create_estimate — this
    # route was missed by all of those.
    #
    # Project takes precedence over Subcontractor when both are given
    # (matches subcontractor_assignments.py's own project.company_id
    # precedent for the identical Project+Subcontractor combination); a
    # free-text `vendor_name`-only Bill (no Project, no Subcontractor —
    # a genuine "company overhead bill") has no other resource to derive
    # from, so `current.company_id` is the only correct source there.
    resolved_company_id = current.company_id
    project_id: uuid.UUID | None = None
    project = None
    if body.project_id is not None:
        project = await _get_project_or_404(current, body.project_id)
        project_id = project.id
        resolved_company_id = project.company_id

    vendor_name = body.vendor_name
    if body.subcontractor_id is not None:
        subcontractor = await _get_subcontractor_or_404(current, body.subcontractor_id)
        vendor_name = vendor_name or subcontractor.name
        if project is None:
            resolved_company_id = subcontractor.company_id

    # Quantized to 2 decimal places (ROUND_HALF_UP, matching Postgres's own
    # NUMERIC rounding — app/core/money.py's own module comment) BEFORE
    # constructing the row: without this, a client-supplied amount with
    # more than 2 decimal places (e.g. "100.005") gets silently rounded by
    # Postgres's NUMERIC(12,2) column on INSERT, but this handler's own
    # response (built from the in-memory ORM object, never re-queried
    # after flush) would still show the UNROUNDED value — a real
    # create-response-disagrees-with-a-later-GET bug, not just cosmetic.
    quantized_amount = body.amount.quantize(CENTS, rounding=ROUND_HALF_UP)

    bill = Bill(
        id=uuid.uuid4(),
        company_id=resolved_company_id,
        project_id=project_id,
        subcontractor_id=body.subcontractor_id,
        vendor_name=vendor_name,
        bill_number=body.bill_number,
        amount=quantized_amount,
        status="unpaid",
        due_date=body.due_date,
    )
    current.session.add(bill)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    await publish(
        "BILL_CREATED",
        session=current.session,
        entity_type="bill",
        entity_id=bill.id,
        company_id=bill.company_id,
    )

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
    paid_by_bill_id: dict[uuid.UUID, Decimal] = dict(paid_result.tuples().all())

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


@router.post(
    "/{bill_id}/payments", response_model=BillPaymentResponse, status_code=status.HTTP_201_CREATED
)
async def record_bill_payment(
    bill_id: uuid.UUID,
    body: BillPaymentCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> BillPaymentResponse:
    bill = await _get_bill_or_404(current, bill_id)

    # Row-lock the bill for the rest of this transaction BEFORE reading its
    # status or computing the cumulative paid amount. Without this, two
    # concurrent payments against the SAME bill can each compute
    # _paid_amount() before either commits — under READ COMMITTED, a SELECT
    # SUM() never sees another transaction's still-uncommitted insert — so
    # neither request's "cumulative >= amount" check ever passes, even
    # though together the payments fully cover the bill. The bill would
    # then sit at "unpaid" forever with no later event to re-check it.
    # SELECT ... FOR UPDATE forces the second concurrent request to block
    # until the first commits, so its own _paid_amount() call correctly
    # sees the first payment already applied. Same pattern this codebase
    # already uses for an equivalent risk elsewhere (invitations.py's
    # accept_invitation uses .with_for_update() for the identical reason;
    # next_invoice_number uses pg_advisory_xact_lock; invoices.py's own
    # record_invoice_payment uses this exact fix for the AR-side mirror of
    # this route).
    #
    # Fetches the full row (not just Bill.id) and reassigns `bill` from
    # it — not merely acquiring the lock and trusting the earlier unlocked
    # read — because a concurrent void_bill call could have changed status
    # between the fetch above and the lock being granted here (e.g. a
    # payment and a void racing each other); the status check right below
    # must see the current, locked value.
    locked = await current.session.execute(
        select(Bill).where(Bill.id == bill.id).with_for_update()
    )
    bill = locked.scalar_one()

    # "paid" is blocked alongside void: without it, a second payment
    # against an already-fully-paid bill was silently accepted (only void
    # was rejected), stacking unlimited further "payments" on a settled
    # bill — same gap fixed in invoices.py's record_invoice_payment.
    if bill.status in ("void", "paid"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot record a payment against a {bill.status} bill")

    # Reads the already-committed payment total BEFORE this payment is
    # inserted, under the row lock acquired above — same remaining-balance
    # guard as invoices.py's own record_invoice_payment, and for the same
    # reason: without it, a payment could exceed the bill's remaining
    # balance, producing a negative outstanding_balance (bill.amount -
    # paid) for every reader after.
    already_paid = await _paid_amount(current, bill.id)
    remaining = bill.amount - already_paid
    if body.amount > remaining:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Payment amount {body.amount} exceeds the bill's remaining balance {remaining}",
        )

    payment = BillPayment(
        id=uuid.uuid4(),
        bill_id=bill.id,
        company_id=bill.company_id,
        amount=body.amount,
        paid_date=body.paid_date,
        recorded_by=current.user.id,
    )
    current.session.add(payment)
    await current.session.flush()

    paid = await _paid_amount(current, bill.id)
    if paid >= bill.amount:
        bill.status = "paid"
        await current.session.flush()

    # docs/07-security-compliance.md Section 5 lists "Bill payment/void"
    # among the state changes requiring an audit_log row. paid_date is
    # included (not just payment_id) because it's user-supplied and has
    # real financial meaning — same rationale as invoices.py's own
    # record_invoice_payment audit entry.
    await write_audit_log(
        current.session,
        company_id=bill.company_id,
        actor_id=current.user.id,
        action="bill.payment_recorded",
        entity_type="bill",
        entity_id=bill.id,
        metadata={
            "payment_id": str(payment.id),
            "amount": str(body.amount),
            "paid_date": body.paid_date.isoformat(),
        },
    )

    return BillPaymentResponse.model_validate(payment)


@router.post("/{bill_id}/void", response_model=BillResponse)
async def void_bill(
    bill_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> BillResponse:
    bill = await _get_bill_or_404(current, bill_id)

    # Row-lock before reading status, same fix and same reason as
    # record_bill_payment's own lock (see that route's comment for the
    # full explanation): without this, a void request racing a concurrent
    # payment that pushes the bill to "paid" could read a stale ("unpaid")
    # status and void a bill a payment just settled.
    # Refreshes `bill` from the locked row (not just lock-and-trust-the-
    # earlier-read), since a concurrent transaction may have changed
    # status between the initial fetch above and the lock being granted
    # here.
    locked = await current.session.execute(
        select(Bill).where(Bill.id == bill.id).with_for_update()
    )
    bill = locked.scalar_one()

    if bill.status in ("paid", "void"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot void a {bill.status} bill")

    bill.status = "void"
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=bill.company_id,
        actor_id=current.user.id,
        action="bill.voided",
        entity_type="bill",
        entity_id=bill.id,
    )

    return await _bill_response(current, bill)
