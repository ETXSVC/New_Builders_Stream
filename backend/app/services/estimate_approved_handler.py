"""ESTIMATE_APPROVED event handler (Task 3.39, design spec Section 2): drafts
a deposit Invoice the moment an Estimate is approved.

Registered against app.core.events by app.core.event_handlers's
register_event_handlers(), same is_registered() re-registration guard
handle_lead_won uses — see that module's own docstring for the full
rationale (the module-global handler registry is cleared before/after every
test by tests/conftest.py's autouse _clean_event_registry fixture).

Inherited Invariant #4: reuses the caller's session (the exact AsyncSession
approve_estimate's own route handler is using) and MUST NEVER call
session.commit()/rollback() itself — only flush().

project_id may be None (an Estimate approved against a bare Lead, no
Project yet) — this handler no-ops silently in that case (design spec
Section 2): invoices.project_id is NOT NULL, so there's nothing to create.
No retroactive invoice generation if that Estimate's Project is drafted
later — out of scope, would need its own trigger on project-creation.

actor_id investigation (Task 3.39's own instruction to verify, not assume):
`POST /estimates/{id}/approve` (app/routers/estimates.py) IS gated
`require_role("client")` — an authenticated in-app client user, per design
decision #3 — and that route's OWN `estimate.approved` audit entry uses
`actor_id=current.user.id`, not `None`. So the premise that "there is no
actor at all" is false. But that route's `publish("ESTIMATE_APPROVED", ...)`
call (same file, right after the audit write) does NOT include `actor_id`
in the payload it publishes — only `estimate_id`, `project_id`, `company_id`,
`approved_total`. Since `app.core.events.publish()` calls
`await handler(**payload)`, a handler parameter can only ever be populated
from what that publish() call actually sends; there is no way for this
handler to learn who approved the Estimate without a change to the router's
publish() call, which Task 3.39's file list (create this handler, register
it, test it) does not include. `actor_id=None` here is therefore not a
blind guess at "no actor exists" — it reflects "this handler was not GIVEN
an actor," a narrower and more accurate reason, worth distinguishing from
LEAD_WON's handle_lead_won, whose payload DOES carry `actor_id` (leads.py's
own `publish("LEAD_WON", ..., actor_id=current.user.id)` call) and whose
handler signature accordingly declares `actor_id` as a required parameter.
Widening ESTIMATE_APPROVED's payload to also carry `actor_id` is a
reasonable follow-up, flagged in this task's implementation report rather
than done silently here as a scope-creeping router edit.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import publish
from app.core.money import CENTS
from app.models import Invoice
from app.services.audit import write_audit_log
from app.services.invoicing import DEFAULT_DEPOSIT_PERCENTAGE, next_invoice_number


async def handle_estimate_approved(
    *,
    session: AsyncSession,
    estimate_id: uuid.UUID,
    project_id: uuid.UUID | None,
    company_id: uuid.UUID,
    approved_total: Decimal,
    **_ignored: object,
) -> None:
    if project_id is None:
        return

    # Quantized to CENTS/ROUND_HALF_UP before it ever reaches the Numeric
    # (12,2) column — same rule every other monetary write in this codebase
    # follows (see app/core/money.py's own docstring). Without this,
    # approved_total * DEFAULT_DEPOSIT_PERCENTAGE can carry more than 2
    # decimal places (e.g. 999.99 * 0.10 = 99.9990); Postgres would round on
    # INSERT, but the in-memory invoice.amount on this ORM object would stay
    # unquantized after flush() (SQLAlchemy doesn't re-fetch plain columns
    # post-insert), diverging from what's actually persisted.
    deposit_amount = (approved_total * DEFAULT_DEPOSIT_PERCENTAGE).quantize(
        CENTS, rounding=ROUND_HALF_UP
    )

    invoice_number = await next_invoice_number(session, company_id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=project_id,
        company_id=company_id,
        estimate_id=estimate_id,
        invoice_number=invoice_number,
        amount=deposit_amount,
        status="draft",
        due_date=None,
    )
    session.add(invoice)
    await session.flush()

    # actor_id=None: not a stand-in for "nobody acted" (a Client DID act —
    # see this module's own docstring above) but for "this handler's
    # payload carries no actor_id to record," since ESTIMATE_APPROVED's
    # publish() call doesn't forward current.user.id the way LEAD_WON's
    # does.
    await write_audit_log(
        session,
        company_id=company_id,
        actor_id=None,
        action="invoice.auto_generated",
        entity_type="invoice",
        entity_id=invoice.id,
        metadata={"estimate_id": str(estimate_id)},
    )

    # This handler is the SECOND place Invoices are created (the first is
    # create_invoice in app/routers/invoices.py) — both must publish
    # INVOICE_CREATED, or auto-drafted deposit invoices silently bypass
    # accounting sync (Task 4.11's handle_financial_record_created), which
    # is precisely the flagship US-6.2 flow: client signs an Estimate, the
    # deposit invoice lands in the accountant's platform. Found by external
    # design review after Task 4.13 wired only the three create routes.
    # publish() dispatching is a plain sequential loop, so this nested
    # publish (we are ourselves running inside ESTIMATE_APPROVED's
    # dispatch) is just a recursive call, not a re-entrancy hazard.
    await publish(
        "INVOICE_CREATED",
        session=session,
        entity_type="invoice",
        entity_id=invoice.id,
        company_id=company_id,
    )
