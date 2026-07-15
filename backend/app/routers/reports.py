"""Task 3.46 (design spec Section 7): GET /reports/profitability.

Per-project figures are date-range filtered on invoices.created_at/
bills.created_at; billed_revenue is what's actually been BILLED (status in
sent/paid/overdue), not the full Estimate total — a project with only a 10%
deposit invoiced shows that 10% as recognized revenue, matching accrual-
basis practice rather than overstating profitability on unbilled work.

AR/AP aging is point-in-time (today), not date-range filtered — an aging
report is inherently "as of now." tax_liability_estimate, by contrast, IS
date-range filtered — it's DEFAULT_TAX_RATE applied to the same
billed_revenue sum computed above for [start_date, end_date], not a
snapshot of anything "as of now."

Date-boundary fix (empirically verified before writing this file — see
Task 3.46 investigation): `Invoice.created_at`/`Bill.created_at` are
`DateTime(timezone=True)` columns (app/models/base.py's TimestampMixin,
`default=utcnow`, i.e. real wall-clock time, not midnight). Comparing them
directly against a bare `end_date` (a `date`, e.g.
`Invoice.created_at <= end_date`) silently EXCLUDES any row created later in
the day on `end_date` itself, because Postgres casts the bare date to
midnight of that day for the comparison. Reproduced directly: an Invoice
created at 2026-07-15 02:51:38 UTC was excluded by
`Invoice.created_at <= date(2026, 7, 15)` but included once the upper bound
was changed to `Invoice.created_at < date(2026, 7, 15) + timedelta(days=1)`.
That exclusive-upper-bound-on-the-day-after form is used below for both the
Invoice and Bill date-range queries. `start_date`'s `>=` comparison is left
as-is — it naturally includes everything from midnight of `start_date`
onward, which is already correct.
"""
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, require_role
from app.core.money import CENTS
from app.models import Bill, BillPayment, Expense, Invoice, InvoicePayment
from app.schemas.profitability import AgingEntry, ProfitabilityReportResponse, ProjectProfitability
from app.services.invoicing import DEFAULT_TAX_RATE

router = APIRouter(prefix="/reports", tags=["reports"])

_ROLES = ("admin", "accountant")

_BILLED_STATUSES = ("sent", "paid", "overdue")
_AR_OUTSTANDING_STATUSES = ("sent", "overdue")
_AP_OUTSTANDING_STATUSES = ("unpaid", "overdue")


def _aging_bucket(due_date: date | None, today: date) -> str:
    if due_date is None:
        return "current"
    days_overdue = (today - due_date).days
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


@router.get("/profitability", response_model=ProfitabilityReportResponse)
async def get_profitability_report(
    current: CurrentUser = Depends(require_role(*_ROLES)),
    start_date: date = Query(...),
    end_date: date = Query(...),
) -> ProfitabilityReportResponse:
    if start_date > end_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "start_date must not be after end_date"
        )

    # Exclusive upper bound of "the day after end_date" — see module
    # docstring. Both the Invoice and Bill queries below use this same
    # `< _end_date_exclusive` upper bound instead of `<= end_date`.
    _end_date_exclusive = end_date + timedelta(days=1)

    revenue_result = await current.session.execute(
        select(Invoice.project_id, func.coalesce(func.sum(Invoice.amount), 0))
        .where(
            Invoice.status.in_(_BILLED_STATUSES),
            Invoice.created_at >= start_date,
            Invoice.created_at < _end_date_exclusive,
        )
        .group_by(Invoice.project_id)
    )
    revenue_by_project: dict[uuid.UUID, Decimal] = dict(revenue_result.all())

    expense_result = await current.session.execute(
        select(Expense.project_id, func.coalesce(func.sum(Expense.amount), 0))
        .where(Expense.incurred_on >= start_date, Expense.incurred_on <= end_date)
        .group_by(Expense.project_id)
    )
    cost_by_project: dict[uuid.UUID, Decimal] = dict(expense_result.all())

    bill_result = await current.session.execute(
        select(Bill.project_id, func.coalesce(func.sum(Bill.amount), 0))
        .where(
            Bill.status != "void",
            Bill.project_id.isnot(None),
            Bill.created_at >= start_date,
            Bill.created_at < _end_date_exclusive,
        )
        .group_by(Bill.project_id)
    )
    for project_id, amount in bill_result.all():
        cost_by_project[project_id] = cost_by_project.get(project_id, Decimal("0.00")) + amount

    all_project_ids = set(revenue_by_project) | set(cost_by_project)
    projects = [
        ProjectProfitability(
            project_id=project_id,
            billed_revenue=revenue_by_project.get(project_id, Decimal("0.00")),
            actual_cost=cost_by_project.get(project_id, Decimal("0.00")),
            profitability=revenue_by_project.get(project_id, Decimal("0.00"))
            - cost_by_project.get(project_id, Decimal("0.00")),
        )
        for project_id in all_project_ids
    ]

    today = date.today()

    ar_result = await current.session.execute(
        select(
            Invoice.id,
            Invoice.amount,
            Invoice.due_date,
            func.coalesce(func.sum(InvoicePayment.amount), 0).label("paid"),
        )
        .outerjoin(InvoicePayment, InvoicePayment.invoice_id == Invoice.id)
        .where(Invoice.status.in_(_AR_OUTSTANDING_STATUSES))
        .group_by(Invoice.id)
    )
    ar_aging = [
        AgingEntry(
            id=row.id,
            outstanding_balance=row.amount - row.paid,
            due_date=row.due_date,
            bucket=_aging_bucket(row.due_date, today),
        )
        for row in ar_result.all()
    ]

    ap_result = await current.session.execute(
        select(
            Bill.id,
            Bill.amount,
            Bill.due_date,
            func.coalesce(func.sum(BillPayment.amount), 0).label("paid"),
        )
        .outerjoin(BillPayment, BillPayment.bill_id == Bill.id)
        .where(Bill.status.in_(_AP_OUTSTANDING_STATUSES))
        .group_by(Bill.id)
    )
    ap_aging = [
        AgingEntry(
            id=row.id,
            outstanding_balance=row.amount - row.paid,
            due_date=row.due_date,
            bucket=_aging_bucket(row.due_date, today),
        )
        for row in ap_result.all()
    ]

    # Quantized to CENTS/ROUND_HALF_UP before it reaches the response — same
    # rule every other monetary write/computation in this codebase follows
    # (app/core/money.py). Currently latent since DEFAULT_TAX_RATE is 0.00,
    # but the moment invoicing.py's own documented placeholder becomes a
    # real nonzero rate, an unquantized multiplication here would emit more
    # than 2 decimal places.
    tax_liability_estimate = (
        sum(revenue_by_project.values(), Decimal("0.00")) * DEFAULT_TAX_RATE
    ).quantize(CENTS, rounding=ROUND_HALF_UP)

    return ProfitabilityReportResponse(
        projects=projects,
        ar_aging=ar_aging,
        ap_aging=ap_aging,
        tax_liability_estimate=tax_liability_estimate,
    )
