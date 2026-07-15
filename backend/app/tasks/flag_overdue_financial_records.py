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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.tasks import broker  # noqa: F401 - import-time side effect

_owner_engine = create_async_engine(settings.migrations_database_url, pool_pre_ping=True)
_OwnerSessionLocal = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)


async def _flag_overdue_financial_records(
    session_factory: async_sessionmaker[AsyncSession] = _OwnerSessionLocal,
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
