"""Shared monetary rounding constant.

Hoisted out of `app/routers/estimates.py` during Task 2.12's review:
`app/services/estimate_calculation.py` needed this same constant, but a
top-level `from app.routers.estimates import CENTS` there would have formed
an import cycle (`estimates.py` imports `calculate_estimate` from that
service module at its own module scope). Services never import from routers
elsewhere in this codebase (`catalog_resolution.py`, `lead_transitions.py`
don't) — a shared, dependency-free home for this constant is the correct
fix, not a deferred/local import papering over the cycle.

`CENTS` is every `Numeric(12, 2)` monetary column's quantization target
(`EstimateLineItem.line_total`, `Estimate.total`, ...). `ROUND_HALF_UP`
(ties away from zero) matches PostgreSQL's own `NUMERIC` rounding behavior
for positive amounts, which every quantity/rate/total in this domain is —
empirically verified against a live Postgres `numeric(12,2)` column during
Task 2.11's review (inserting values at exact `.xx5` cent boundaries and
comparing against `Decimal.quantize(CENTS, rounding=ROUND_HALF_UP)`).
"""

from decimal import Decimal

CENTS = Decimal("0.01")
