"""Task 3.45 (design spec Section 5). Same undecorated-function/decorated-
actor split as app/tasks/compliance_expiry.py and app/tasks/seat_usage.py,
and the same owner-role engine pattern (legitimately cross-tenant — scans
every company's invoices/bills).

Two single, set-based UPDATEs in one transaction, not a per-row loop — no
per-row try/except like seat_usage.py's cross-subscription loop needs, since
neither UPDATE calls an external service that can fail mid-batch.

paid/void rows are excluded by each UPDATE's own WHERE clause status set — a
settled or voided record never gets flagged overdue regardless of due_date.
"""
from __future__ import annotations

import dramatiq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


from app.tasks import broker  # noqa: F401 - import-time side effect
from app.tasks.scanner_db import ScannerSessionLocal

# The shared `scanner` connection (app/tasks/scanner_db.py), not a
# per-module owner-role engine. This job is genuinely cross-tenant, so it
# needs a role that sees every company — but it does not need the role that
# OWNS every table and can rewrite the RLS policies protecting them. See
# migration 0020.


async def _flag_overdue_financial_records(
    session_factory: async_sessionmaker[AsyncSession] = ScannerSessionLocal,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE invoices SET status = 'overdue' "
                "WHERE status = 'sent' AND due_date < CURRENT_DATE"
            )
        )
        await session.execute(
            text(
                "UPDATE bills SET status = 'overdue' "
                "WHERE status = 'unpaid' AND due_date < CURRENT_DATE"
            )
        )
        await session.commit()


flag_overdue_financial_records = dramatiq.actor(
    max_retries=3, actor_name="flag_overdue_financial_records"
)(_flag_overdue_financial_records)
