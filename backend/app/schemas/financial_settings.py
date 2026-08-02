from decimal import Decimal

from pydantic import BaseModel, Field


class CompanyFinancialSettingsPutRequest(BaseModel):
    """Both values are FRACTIONS: 0.08875 for 8.875%, 0.10 for a 10%
    deposit. Every consumer multiplies directly, so accepting percentages
    would mean dividing by 100 at each call site with one eventually
    forgotten.

    Bounded to [0, 1] here as well as by CHECK constraints in migration
    0033 — the database is the backstop, but a 422 naming the field beats a
    500 from a constraint violation. `10` meaning "10 percent" is the
    mistake this catches, and it would otherwise bill ten times the
    contract value.

    Explicitly nullable rather than optional-and-absent: null means "clear
    this back to inherited", which a patch-style partial update could not
    express at all.
    """

    deposit_percentage: Decimal | None = Field(None, ge=0, le=1, decimal_places=5)
    tax_rate: Decimal | None = Field(None, ge=0, le=1, decimal_places=5)


class CompanyFinancialSettingsResponse(BaseModel):
    """What this company set, and what is actually in force.

    The two differ whenever a value is inherited: a branch with no row of
    its own uses its root's, and a branch that set only a deposit
    percentage still inherits the root's tax rate. Returning only the
    stored values would show an operator "tax rate: not set" while their
    reports were being taxed at the head office's rate.
    """

    deposit_percentage: Decimal | None
    tax_rate: Decimal | None
    effective_deposit_percentage: Decimal
    effective_tax_rate: Decimal
