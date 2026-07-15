"""INVOICE_CREATED/EXPENSE_CREATED/BILL_CREATED event handler (Task 4.11,
design spec Section 4): enqueues one sync_financial_record Dramatiq
message per active integration connection. Does NO sync work itself — US-
6.2 requires syncing to be asynchronous, so this handler's only job is
deciding WHAT needs syncing and enqueuing it, the same "thin wrapper
enqueues, worker does the real work" split app/scheduler.py's own job
wrappers already establish for scheduled jobs.

Registered against app.core.events by app.core.event_handlers's
register_event_handlers(), same is_registered() re-registration guard
every other handler in this codebase uses.

Reuses the caller's session (the same AsyncSession the create_invoice/
create_bill/create_expense route handler is using) for its own read of
integration_connections — Inherited Invariant #4: MUST NEVER call
session.commit()/rollback() itself, only flush() if it ever writes
(currently it doesn't write anything at all, only reads). Enqueuing one
Dramatiq message per connection found (not one message that internally
loops over every connection) matters for retry correctness: if a
QuickBooks push fails but a FreshBooks push for the same record succeeds,
Dramatiq only retries the failed QuickBooks message — a successful
FreshBooks sync is never redundantly redone (design spec Section 4).
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IntegrationConnection
from app.tasks.accounting_sync import sync_financial_record


async def handle_financial_record_created(
    *,
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    company_id: uuid.UUID,
    **_ignored: object,
) -> None:
    connections_result = await session.execute(
        select(IntegrationConnection).where(IntegrationConnection.company_id == company_id)
    )
    connections = connections_result.scalars().all()

    for connection in connections:
        sync_financial_record.send(
            connection_id=str(connection.id),
            entity_type=entity_type,
            entity_id=str(entity_id),
        )
