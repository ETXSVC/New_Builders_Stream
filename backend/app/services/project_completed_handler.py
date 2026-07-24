"""PROJECT_COMPLETED event handler: drafts a final Invoice for the
project's uninvoiced remainder the moment a Project transitions to
`completed`.

Closes the requirements-audit gap where docs/03-technical-architecture.md's
event-bus table lists PROJECT_COMPLETED (published by Project Management,
consumed by Billing) but nothing ever published or consumed it — benign
while Billing didn't exist, a live gap once it did.

Registered against app.core.events by app.core.event_handlers's
register_event_handlers(), same is_registered() re-registration guard the
other handlers use (see that module's docstring; the registry is cleared
around every test by tests/conftest.py's autouse _clean_event_registry).

Inherited Invariant #4: reuses the caller's session (the exact AsyncSession
update_project_status's route handler is using) and MUST NEVER call
session.commit()/rollback() itself — only flush().

Amount formula — an explicit product placeholder, same status as
app/services/invoicing.py's DEFAULT_DEPOSIT_PERCENTAGE, not a validated
business decision:

    contracted = sum(approved Estimates' totals) + sum(approved Change
                 Orders' cost_deltas)
    invoiced   = sum(non-void Invoices' amounts)
    remainder  = contracted - invoiced

A final invoice is drafted only when the project has at least one approved
Estimate AND remainder > 0; otherwise this handler silently no-ops (same
silent-no-op shape as handle_estimate_approved's project_id/tier early
returns). The output is a DRAFT — an admin/accountant can void it before
sending, so a wrong placeholder formula costs one void click, never a
mis-sent invoice.

Double-invoicing: PROJECT_TRANSITIONS makes `completed` effectively
one-way (completed -> archived only), so this fires at most once per
project today; and because previously drafted invoices count into
`invoiced`, the remainder math is self-correcting even if a re-fire path
is ever added.

Unlike ESTIMATE_APPROVED (whose publish() call doesn't forward an actor —
see estimate_approved_handler's docstring lament), the publish site here
(update_project_status) has current.user.id in hand and forwards it, so
the audit row below records the real actor.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import publish
from app.core.money import CENTS
from app.core.tier_gating import tier_allows
from app.models import ChangeOrder, Estimate, Invoice
from app.services.audit import write_audit_log
from app.services.invoicing import next_invoice_number

_ZERO = Decimal("0.00")


async def handle_project_completed(
    *,
    session: AsyncSession,
    project_id: uuid.UUID,
    company_id: uuid.UUID,
    actor_id: uuid.UUID,
    **_ignored: object,
) -> None:
    # Tier gating (same rationale as handle_estimate_approved): this is an
    # ACCOUNTING-module write reached through the event bus, not that
    # module's routes — without this check a pro company would get invoices
    # auto-drafted into a module its plan doesn't include.
    if not await tier_allows(session, company_id, "accounting"):
        return

    # Approved estimates only; Estimate.total is nullable (NULL until first
    # calculation) but an approved estimate is always snapshotted with a
    # calculated total, so coalesce is belt-and-suspenders, not load-bearing.
    approved_count, approved_total = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Estimate.total), 0),
            ).where(Estimate.project_id == project_id, Estimate.status == "approved")
        )
    ).one()
    if not approved_count:
        # No approved contract value at all — nothing to reconcile a final
        # invoice against. (Distinct from "remainder <= 0" below: a project
        # with no approved estimate shouldn't get a change-order-only
        # invoice out of this placeholder formula.)
        return

    change_order_total = (
        await session.scalar(
            select(func.coalesce(func.sum(ChangeOrder.cost_delta), 0)).where(
                ChangeOrder.project_id == project_id, ChangeOrder.status == "approved"
            )
        )
    ) or _ZERO

    invoiced_total = (
        await session.scalar(
            select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                Invoice.project_id == project_id, Invoice.status != "void"
            )
        )
    ) or _ZERO

    remainder = (
        Decimal(approved_total) + Decimal(change_order_total) - Decimal(invoiced_total)
    ).quantize(CENTS, rounding=ROUND_HALF_UP)
    if remainder <= _ZERO:
        return

    invoice_number = await next_invoice_number(session, company_id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=project_id,
        company_id=company_id,
        estimate_id=None,
        invoice_number=invoice_number,
        amount=remainder,
        status="draft",
        due_date=None,
    )
    session.add(invoice)
    await session.flush()

    await write_audit_log(
        session,
        company_id=company_id,
        actor_id=actor_id,
        action="invoice.auto_generated",
        entity_type="invoice",
        entity_id=invoice.id,
        metadata={"trigger": "project_completed", "project_id": str(project_id)},
    )

    # Same reasoning as handle_estimate_approved's nested publish: every
    # place an Invoice is created must publish INVOICE_CREATED or the
    # auto-drafted invoice silently bypasses accounting-integration sync.
    # publish() dispatch is a plain sequential loop, so this nested publish
    # is just a recursive call, not a re-entrancy hazard.
    await publish(
        "INVOICE_CREATED",
        session=session,
        entity_type="invoice",
        entity_id=invoice.id,
        company_id=company_id,
    )
