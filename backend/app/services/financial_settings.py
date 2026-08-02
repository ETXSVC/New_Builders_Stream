"""Resolving a tenant's deposit percentage and tax rate (migration 0033).

The two numbers this answers for used to be module constants in
`app/services/invoicing.py`, documented as placeholders. They are now
per-tenant, because neither was ever really one number: a deposit
percentage is a commercial policy that differs per builder, and a sales-tax
rate differs by jurisdiction, so two branches of the same company in
different states genuinely disagree.

## The resolution order, and why root fallback exists

For each value independently: **the company's own setting, else its root
company's, else the code default.**

Independently per value, not per row, because a tenant may want to state a
deposit policy and leave tax alone — resolving the whole row would make
setting one silently adopt the other's default.

Root fallback rather than plain per-company, because a head office should
be able to set a policy once and have branches follow it, while a branch in
another state can still override. This is deliberately NOT the
root-only resolution `subscriptions` uses (`get_root_company_id` and
nothing else): a subscription genuinely belongs to the root and a branch
has no business having its own, whereas a tax rate is exactly the thing a
branch does need to differ on.

One query resolves both companies. `ORDER BY (company_id = root) ` puts the
company's OWN row first, so `next(...)` takes the more specific value
without a second round trip.

## What this deliberately does not do

Change history. A deposit invoice's amount is computed once, at approval,
and stored — editing the percentage afterwards does not rewrite invoices
already raised, which is correct: they were agreed at the old rate. The
report's tax figure IS recomputed live, and that is also correct, because
it is labelled an estimate of current liability rather than a record of
anything.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CompanyFinancialSettings

# What a tenant that has stated nothing gets. Unchanged from the constants
# these replaced: a 10% deposit, and a 0% tax rate so a company with no
# configured tax obligation sees a $0 estimated liability rather than an
# invented nonzero one.
DEFAULT_DEPOSIT_PERCENTAGE = Decimal("0.10")
DEFAULT_TAX_RATE = Decimal("0.00")


@dataclass(frozen=True)
class FinancialSettings:
    deposit_percentage: Decimal
    tax_rate: Decimal


async def resolve_financial_settings(
    session: AsyncSession, company_id: uuid.UUID
) -> FinancialSettings:
    """Both numbers for one company, each falling back independently."""
    root_id = await session.scalar(select(func.get_root_company_id(company_id)))

    result = await session.execute(
        select(
            CompanyFinancialSettings.company_id,
            CompanyFinancialSettings.deposit_percentage,
            CompanyFinancialSettings.tax_rate,
        ).where(CompanyFinancialSettings.company_id.in_({company_id, root_id}))
    )
    rows = result.all()

    # Own row first, so it wins over the root's for each value it states.
    ordered = sorted(rows, key=lambda row: row.company_id != company_id)

    return FinancialSettings(
        deposit_percentage=_first(
            (row.deposit_percentage for row in ordered), DEFAULT_DEPOSIT_PERCENTAGE
        ),
        tax_rate=_first((row.tax_rate for row in ordered), DEFAULT_TAX_RATE),
    )


def _first(values, default: Decimal) -> Decimal:
    for value in values:
        if value is not None:
            return value
    return default
