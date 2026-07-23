"""Task 3.35 (design spec Section 3): POST/GET /projects/{id}/invoices,
GET /invoices/{id}.

RBAC per docs/07-security-compliance.md Section 2's split "Accounting/Billing
(AR)" row: Admin/Accountant write, Admin/Accountant/Client read (Client
scoped to non-draft only — same `if current.role == "client":
query = query.where(...)` shape list_estimates already uses).
"""
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.events import publish
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import Invoice, InvoicePayment
from app.routers.projects import _get_project_or_404
from app.schemas.invoice import (
    InvoiceCreateRequest,
    InvoiceDetailResponse,
    InvoiceListResponse,
    InvoicePaymentCreateRequest,
    InvoicePaymentResponse,
    InvoiceResponse,
    InvoiceSendRequest,
)
from app.services.audit import write_audit_log
from app.services.invoicing import next_invoice_number

router = APIRouter(tags=["invoices"])

_WRITE_ROLES = ("admin", "accountant")
_READ_ROLES = ("admin", "accountant", "client")


async def _get_invoice_or_404(current: CurrentUser, invoice_id: uuid.UUID) -> Invoice:
    result = await current.session.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return invoice


async def _paid_amount(current: CurrentUser, invoice_id: uuid.UUID) -> Decimal:
    result = await current.session.execute(
        select(func.coalesce(func.sum(InvoicePayment.amount), 0)).where(
            InvoicePayment.invoice_id == invoice_id
        )
    )
    return result.scalar_one()


async def _invoice_response(current: CurrentUser, invoice: Invoice) -> InvoiceResponse:
    paid = await _paid_amount(current, invoice.id)
    return InvoiceResponse(
        id=invoice.id,
        project_id=invoice.project_id,
        company_id=invoice.company_id,
        estimate_id=invoice.estimate_id,
        invoice_number=invoice.invoice_number,
        amount=invoice.amount,
        status=invoice.status,
        due_date=invoice.due_date,
        created_at=invoice.created_at,
        outstanding_balance=invoice.amount - paid,
    )


@router.post(
    "/projects/{project_id}/invoices",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invoice(
    project_id: uuid.UUID,
    body: InvoiceCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> InvoiceResponse:
    project = await _get_project_or_404(current, project_id)

    invoice_number = await next_invoice_number(current.session, project.company_id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=project.id,
        company_id=project.company_id,
        estimate_id=None,
        invoice_number=invoice_number,
        amount=body.amount,
        status="draft",
        due_date=body.due_date,
    )
    current.session.add(invoice)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    await publish(
        "INVOICE_CREATED",
        session=current.session,
        entity_type="invoice",
        entity_id=invoice.id,
        company_id=invoice.company_id,
    )

    return await _invoice_response(current, invoice)


@router.post("/invoices/{invoice_id}/send", response_model=InvoiceResponse)
async def send_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceSendRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> InvoiceResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)
    if invoice.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft invoice can be sent")

    due_date = body.due_date or invoice.due_date
    if due_date is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "due_date is required (either already set, or provided in this request)",
        )

    invoice.status = "sent"
    invoice.due_date = due_date
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=invoice.company_id,
        actor_id=current.user.id,
        action="invoice.sent",
        entity_type="invoice",
        entity_id=invoice.id,
    )

    return await _invoice_response(current, invoice)


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=InvoicePaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_invoice_payment(
    invoice_id: uuid.UUID,
    body: InvoicePaymentCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> InvoicePaymentResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)

    # Row-lock the invoice for the rest of this transaction BEFORE reading
    # its status or computing the cumulative paid amount. Without this, two
    # concurrent payments against the SAME invoice can each compute
    # _paid_amount() before either commits — under READ COMMITTED, a SELECT
    # SUM() never sees another transaction's still-uncommitted insert — so
    # neither request's "cumulative >= amount" check ever passes, even
    # though together the payments fully cover the invoice. The invoice
    # would then sit at "sent" forever with no later event to re-check it.
    # SELECT ... FOR UPDATE forces the second concurrent request to block
    # until the first commits, so its own _paid_amount() call correctly
    # sees the first payment already applied. Same pattern this codebase
    # already uses for an equivalent risk elsewhere (invitations.py's
    # accept_invitation uses .with_for_update() for the identical reason;
    # next_invoice_number uses pg_advisory_xact_lock).
    #
    # Fetches the full row (not just Invoice.id) and reassigns `invoice`
    # from it — not merely acquiring the lock and trusting the earlier
    # unlocked read — because a concurrent void_invoice call could have
    # changed status between the fetch above and the lock being granted
    # here (e.g. a payment and a void racing each other); the status check
    # right below must see the current, locked value.
    locked = await current.session.execute(
        select(Invoice).where(Invoice.id == invoice.id).with_for_update()
    )
    invoice = locked.scalar_one()

    # "paid" is blocked alongside draft/void: without it, a second payment
    # against an already-fully-paid invoice was silently accepted (only
    # draft/void were rejected), stacking unlimited further "payments" on a
    # settled invoice.
    if invoice.status in ("draft", "void", "paid"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot record a payment against a {invoice.status} invoice"
        )

    # Reads the already-committed payment total BEFORE this payment is
    # inserted, under the row lock acquired above — the same lock that
    # makes concurrent payments serialize (see the comment above) also
    # makes this remaining-balance read safe against a second request
    # racing in. Rejecting an over-the-remaining-balance payment here,
    # before the row is inserted, keeps a bad request from ever landing —
    # not silently accepted and then producing a negative
    # outstanding_balance (invoice.amount - paid) for every reader after.
    already_paid = await _paid_amount(current, invoice.id)
    remaining = invoice.amount - already_paid
    if body.amount > remaining:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Payment amount {body.amount} exceeds the invoice's remaining balance {remaining}",
        )

    payment = InvoicePayment(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        company_id=invoice.company_id,
        amount=body.amount,
        paid_date=body.paid_date,
        recorded_by=current.user.id,
    )
    current.session.add(payment)
    await current.session.flush()

    paid = await _paid_amount(current, invoice.id)
    if paid >= invoice.amount:
        invoice.status = "paid"
        await current.session.flush()

    # docs/07-security-compliance.md Section 5 lists "Invoice send/payment/
    # void" among the state changes requiring an audit_log row — same
    # requirement Task 3.36's send_invoice already satisfies. paid_date is
    # included (not just payment_id) because it's user-supplied and has
    # real financial meaning (which accounting period the payment lands
    # in) — the audit trail should show it directly, not require a join.
    await write_audit_log(
        current.session,
        company_id=invoice.company_id,
        actor_id=current.user.id,
        action="invoice.payment_recorded",
        entity_type="invoice",
        entity_id=invoice.id,
        metadata={
            "payment_id": str(payment.id),
            "amount": str(body.amount),
            "paid_date": body.paid_date.isoformat(),
        },
    )

    return InvoicePaymentResponse.model_validate(payment)


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceResponse)
async def void_invoice(
    invoice_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> InvoiceResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)

    # Row-lock before reading status, same fix and same reason as
    # record_invoice_payment's own lock (see that route's comment for the
    # full explanation): without this, a void request racing a concurrent
    # payment that pushes the invoice to "paid" could read a stale
    # ("sent") status and void an invoice a payment just settled.
    # Refreshes `invoice` from the locked row (not just lock-and-trust-the-
    # earlier-read), since a concurrent transaction may have changed
    # status between the initial fetch above and the lock being granted
    # here.
    locked = await current.session.execute(
        select(Invoice).where(Invoice.id == invoice.id).with_for_update()
    )
    invoice = locked.scalar_one()

    if invoice.status in ("paid", "void"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot void a {invoice.status} invoice")

    invoice.status = "void"
    await current.session.flush()

    # docs/07-security-compliance.md Section 5 lists "Invoice send/payment/
    # void" among the state changes requiring an audit_log row.
    await write_audit_log(
        current.session,
        company_id=invoice.company_id,
        actor_id=current.user.id,
        action="invoice.voided",
        entity_type="invoice",
        entity_id=invoice.id,
    )

    return await _invoice_response(current, invoice)


@router.get("/projects/{project_id}/invoices", response_model=InvoiceListResponse)
async def list_invoices(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> InvoiceListResponse:
    project = await _get_project_or_404(current, project_id)

    query = select(Invoice).where(Invoice.project_id == project.id)
    if current.role == "client":
        query = query.where(Invoice.status != "draft")

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Invoice.created_at,
        id_col=Invoice.id,
        cursor=cursor,
        limit=limit,
    )

    if not rows:
        return InvoiceListResponse(items=[], next_cursor=next_cursor)

    # One bounded query over the whole page's invoice_ids, not one
    # _paid_amount() call per row — same "second query scoped to the
    # current page, not an N+1" shape compliance.py's own
    # list_compliance_notifications already establishes for the identical
    # trade-off.
    invoice_ids = [row.id for row in rows]
    paid_result = await current.session.execute(
        select(InvoicePayment.invoice_id, func.coalesce(func.sum(InvoicePayment.amount), 0))
        .where(InvoicePayment.invoice_id.in_(invoice_ids))
        .group_by(InvoicePayment.invoice_id)
    )
    paid_by_invoice_id: dict[uuid.UUID, Decimal] = dict(paid_result.all())

    items = [
        InvoiceResponse(
            id=row.id,
            project_id=row.project_id,
            company_id=row.company_id,
            estimate_id=row.estimate_id,
            invoice_number=row.invoice_number,
            amount=row.amount,
            status=row.status,
            due_date=row.due_date,
            created_at=row.created_at,
            outstanding_balance=row.amount - paid_by_invoice_id.get(row.id, Decimal("0")),
        )
        for row in rows
    ]
    return InvoiceListResponse(items=items, next_cursor=next_cursor)


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> InvoiceDetailResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)
    if current.role == "client" and invoice.status == "draft":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")

    payments_result = await current.session.execute(
        select(InvoicePayment)
        .where(InvoicePayment.invoice_id == invoice.id)
        .order_by(InvoicePayment.paid_date.asc(), InvoicePayment.id.asc())
    )
    payments = [
        InvoicePaymentResponse.model_validate(payment) for payment in payments_result.scalars().all()
    ]

    base = await _invoice_response(current, invoice)
    return InvoiceDetailResponse(**base.model_dump(), payments=payments)
