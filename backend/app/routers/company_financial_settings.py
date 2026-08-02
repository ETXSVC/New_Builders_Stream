"""A tenant's deposit percentage and tax rate (migration 0033).

Same shape as `company_email_settings`: one row per company, a GET that
returns null when nothing has been set, and a PUT that upserts.

RBAC is admin **and accountant**, unlike email settings' admin-only. These
are accounting figures — a tax rate is exactly the thing a bookkeeper is
hired to get right — and the accountant role already reads the
profitability report that consumes one of them.

The GET returns both the row's own values (which may be null) and the
EFFECTIVE values actually in use, because those differ in a way a caller
cannot work out for itself: a branch with no row of its own inherits its
root's, and a branch that has stated only a deposit percentage still
inherits the root's tax rate. A screen showing only the stored row would
tell an operator their tax rate was unset while invoices were being taxed.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.models import CompanyFinancialSettings
from app.schemas.financial_settings import (
    CompanyFinancialSettingsPutRequest,
    CompanyFinancialSettingsResponse,
)
from app.services.audit import write_audit_log
from app.services.financial_settings import resolve_financial_settings

router = APIRouter(prefix="/companies/financial-settings", tags=["financial-settings"])

_ROLES = ("admin", "accountant")


async def _response(current: CurrentUser) -> CompanyFinancialSettingsResponse:
    row = (
        await current.session.execute(
            select(CompanyFinancialSettings).where(
                CompanyFinancialSettings.company_id == current.company_id
            )
        )
    ).scalar_one_or_none()
    effective = await resolve_financial_settings(current.session, current.company_id)
    return CompanyFinancialSettingsResponse(
        deposit_percentage=row.deposit_percentage if row else None,
        tax_rate=row.tax_rate if row else None,
        effective_deposit_percentage=effective.deposit_percentage,
        effective_tax_rate=effective.tax_rate,
    )


@router.get("", response_model=CompanyFinancialSettingsResponse)
async def get_financial_settings(
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> CompanyFinancialSettingsResponse:
    return await _response(current)


@router.put("", response_model=CompanyFinancialSettingsResponse)
async def put_financial_settings(
    payload: CompanyFinancialSettingsPutRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CompanyFinancialSettingsResponse:
    """Set either value, or clear one back to inherited by sending null.

    Both fields are explicit in the request rather than patch-style, so
    "clear this back to the default" is expressible at all — a partial
    update where absent means unchanged has no way to say it.
    """
    stmt = (
        pg_insert(CompanyFinancialSettings)
        .values(
            id=uuid.uuid4(),
            company_id=current.company_id,
            deposit_percentage=payload.deposit_percentage,
            tax_rate=payload.tax_rate,
        )
        .on_conflict_do_update(
            index_elements=["company_id"],
            set_={
                "deposit_percentage": payload.deposit_percentage,
                "tax_rate": payload.tax_rate,
            },
        )
    )
    await current.session.execute(stmt)
    await current.session.flush()

    # Audited: these change what customers are billed and what the tax
    # figure on the profitability report says, so "who set this, and when"
    # is a question someone will eventually ask.
    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="company.financial_settings_updated",
        entity_type="company",
        entity_id=current.company_id,
        metadata={
            "deposit_percentage": (
                str(payload.deposit_percentage) if payload.deposit_percentage is not None else None
            ),
            "tax_rate": str(payload.tax_rate) if payload.tax_rate is not None else None,
        },
    )

    return await _response(current)
