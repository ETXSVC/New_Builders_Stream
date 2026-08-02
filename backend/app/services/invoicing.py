"""Task 3.33 (design spec Section 1). Deposit/tax-rate placeholders and the
per-company invoice-numbering helper, mirroring app/services/billing.py's
own module-level-constants pattern.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice

# Moved to app/services/financial_settings.py (migration 0033): both are
# now per-tenant configuration rather than constants, because neither was
# ever really one number — a deposit percentage is a commercial policy per
# builder, and a tax rate differs by jurisdiction.
#
# Re-exported here only so this module's own name keeps working for
# importers; `resolve_financial_settings` is what call sites should use, and
# these are the fallback for a tenant that has stated nothing.
from app.services.financial_settings import (  # noqa: F401  (re-export)
    DEFAULT_DEPOSIT_PERCENTAGE,
    DEFAULT_TAX_RATE,
)


async def next_invoice_number(session: AsyncSession, company_id: uuid.UUID) -> str:
    """Per-company sequential, formatted INV-{creation_year}-{counter}. The
    year is cosmetic (when this invoice was created), not an annual reset
    boundary — the counter itself never resets (design spec Section 2).

    pg_advisory_xact_lock, keyed on hashtext(company_id), serializes
    concurrent number generation for the SAME company within the current
    transaction (auto-released at commit/rollback) — a bare `SELECT
    COUNT(*) + 1` with no lock would race under concurrent invoice creation
    for the same company (two transactions both counting before either
    inserts). This codebase has no existing per-tenant DB sequence to reuse,
    so a locked-count approach is used rather than introducing new sequence
    infrastructure for one column.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:company_id)::bigint)"),
        {"company_id": str(company_id)},
    )
    result = await session.execute(
        select(func.count()).select_from(Invoice).where(Invoice.company_id == company_id)
    )
    count = result.scalar_one()
    year = datetime.now(timezone.utc).year
    return f"INV-{year}-{count + 1:04d}"
