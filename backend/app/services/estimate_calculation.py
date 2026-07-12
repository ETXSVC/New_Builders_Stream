"""Task 2.12: `POST /estimates/{id}/calculate` calculation engine.

Implements docs/03-technical-architecture.md Section 6's fixed calculation
order EXACTLY, using `decimal.Decimal` throughout (never `float`, Inherited
Invariant #9):

  1. Line item base cost = `quantity x unit_rate` — already computed and
     stored per-line by Task 2.11 (`PUT /estimates/{id}/lines`, which
     writes `EstimateLineItem.line_total`). This module RE-READS those
     already-stored values; it does not recompute them from
     `quantity`/`unit_rate_snapshot` again. Recomputing here would risk
     silently diverging from what `PUT /estimates/{id}/lines` already
     persisted and what `GET /estimates/{id}` already returned to the
     client for the exact same rows.
  2. Category subtotals — line items grouped by their `cost_catalog_item`'s
     `category` (not a column on `EstimateLineItem` itself — `category`
     lives on `CostCatalogItem`, per Task 2.7/2.1's models — so this
     requires a join, see the query in `calculate_estimate` below). This is
     purely a display/reporting artifact for this route's response
     (`category_breakdown` on `EstimateCalculationResponse`,
     `app/schemas/estimate.py`): it is computed and returned, but never
     persisted as a new column, and it is NOT itself part of the running
     subtotal -> total pipeline — step 2 sits beside step 1's sum, feeding
     only the response, not steps 3-4.
  3. Overhead markup applied: `subtotal * (1 + overhead_pct / 100)`.
  4. Profit margin applied: `* (1 + profit_pct / 100)`.
  5. Tax liability calculated, if applicable: no tax rate/jurisdiction
     concept exists anywhere in docs/04-database-schema.md or the Phase 2
     functional requirements (confirmed by reading the schema doc and
     Section 6's own text directly, not assumed from the plan doc's
     paraphrase alone). Treated as a documented no-op for Phase 2: tax is
     always `Decimal("0")`, added additively at the end of the pipeline
     (tax is conventionally a charge added on top of a taxable amount, not
     a multiplicative factor like overhead/profit) so a future tax feature
     can slot in here without reshaping steps 1-4.

Rounding: per this task's resolved judgment call, ONLY the final `total` is
quantized (to 2 decimal places, `ROUND_HALF_UP` — reusing `app/core/money.py`'s
`CENTS` constant rather than redefining a second, independent
`Decimal("0.01")` literal elsewhere in this codebase). `subtotal` needs no
quantization of its own: it is a plain
sum of `EstimateLineItem.line_total` values that Task 2.11 already
individually quantized to 2 decimal places, and Decimal addition never
adds decimal places the way multiplication does. The overhead- and
profit-applied INTERMEDIATE values are deliberately left unquantized —
Decimal division/multiplication never loses precision the way float does,
so there is no correctness reason to round at each pipeline stage, and
rounding early would only introduce compounding rounding error across
stages that rounding once, at the very end, avoids.

Division of responsibility mirrors `app/services/catalog_resolution.py`
(Task 2.4): this module receives an already-fetched Estimate (the
`is_snapshotted` 409 guard is the ROUTER's job, same as Task 2.11's
identical guard in `replace_estimate_line_items` — this module has no
opinion on HTTP status codes and never raises `HTTPException`) and does
its OWN querying for everything else the computation itself needs — line
items joined to their catalog item's category, and the Estimate's
MarkupProfile row. The router's remaining job is exactly: fetch + 409-check
the Estimate, call this module, then persist `subtotal`/`total` onto the
Estimate row and shape the HTTP response.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import CENTS
from app.models import CostCatalogItem, Estimate, EstimateLineItem, MarkupProfile


@dataclass(frozen=True)
class CategoryTotal:
    """One category's summed `line_total`s — the raw computation result the
    router turns into `app/schemas/estimate.py`'s `CategorySubtotal`
    response schema. Kept as a plain dataclass here (not the Pydantic
    schema itself) so this service module has no dependency on the API
    response shape, matching `resolve_visible_catalog_items` returning
    plain ORM objects rather than response schemas.
    """

    category: str
    subtotal: Decimal


@dataclass(frozen=True)
class EstimateCalculation:
    """The full result of running `calculate_estimate` — everything the
    router needs to both persist (`subtotal`/`total`) and shape the HTTP
    response (`category_breakdown`), without this module knowing about
    either concern.
    """

    subtotal: Decimal
    total: Decimal
    category_breakdown: list[CategoryTotal]


async def calculate_estimate(session: AsyncSession, estimate: Estimate) -> EstimateCalculation:
    """Computes `subtotal`/`total`/`category_breakdown` for `estimate` per
    docs/03-technical-architecture.md Section 6's fixed order.

    Pure computation only: does NOT check `estimate.is_snapshotted` (the
    router's job, before this is ever called — see this module's own
    docstring) and does NOT write `estimate.subtotal`/`estimate.total` back
    to the row (also the router's job).

    Handles zero line items cleanly: `subtotal` starts at `Decimal("0")`
    and the join query below simply returns no rows, so `category_breakdown`
    ends up `[]` and the overhead/profit pipeline runs as
    `0 * overhead_multiplier * profit_multiplier == 0` — no division
    anywhere in this pipeline, so there is no division-by-zero case to
    special-case at all.
    """
    # Step 1 (re-read, not recompute) + Step 2 (category grouping) in one
    # query: `EstimateLineItem` has no `category` column of its own — only
    # `cost_catalog_item_id` — so a join to `CostCatalogItem` is required
    # to learn each line's category (resolved judgment call #6). An INNER
    # JOIN is safe here: every `EstimateLineItem.cost_catalog_item_id` is a
    # NOT NULL FK into `cost_catalog_items` (see
    # `app/models/estimate_line_item.py`), so no line item row can ever
    # lack a matching catalog item row.
    result = await session.execute(
        select(EstimateLineItem, CostCatalogItem.category)
        .join(CostCatalogItem, EstimateLineItem.cost_catalog_item_id == CostCatalogItem.id)
        .where(EstimateLineItem.estimate_id == estimate.id)
    )
    rows = result.all()

    subtotal = Decimal("0")
    category_sums: dict[str, Decimal] = {}
    for line_item, category in rows:
        # `line_item.line_total` is already a 2-decimal-place Decimal
        # (Task 2.11 quantized it at write time) — pure Decimal addition
        # below, no rounding concern here (Decimal addition never adds
        # decimal places, unlike multiplication).
        subtotal += line_item.line_total
        category_sums[category] = category_sums.get(category, Decimal("0")) + line_item.line_total

    # Sorted by category name for a deterministic response ordering —
    # dict-insertion order would otherwise depend on the DB's unordered
    # row-return order for the SELECT above, which is not a guarantee this
    # module should lean on for an API response's list ordering.
    category_breakdown = [
        CategoryTotal(category=category, subtotal=category_sums[category])
        for category in sorted(category_sums)
    ]

    # MarkupProfile.id is looked up here (not handed in pre-fetched by the
    # router) for the same "service does its own querying" shape
    # `resolve_visible_catalog_items` established — `estimate` only carries
    # `markup_profile_id`, a foreign key, not a loaded relationship (no
    # `relationship()` is declared on `Estimate`, matching this codebase's
    # "no ORM relationships, only explicit FK columns + explicit queries"
    # convention, see `EstimateDetailResponse`'s own docstring). `scalar_one()`
    # (not `scalar_one_or_none()`) is deliberate: `markup_profile_id` is a
    # NOT NULL FK enforced at the DB level (`app/models/estimate.py`), so a
    # missing row here would mean the referenced MarkupProfile row itself
    # was deleted or is RLS-invisible to this tenant — a data-integrity
    # situation outside this task's scope (Task 2.10's own docstring
    # already documents `markup_profile_id` as unvalidated at Estimate-
    # creation time); this simply lets that edge case surface loudly as an
    # unhandled `NoResultFound` rather than silently computing against a
    # wrong/default profile.
    markup_result = await session.execute(
        select(MarkupProfile).where(MarkupProfile.id == estimate.markup_profile_id)
    )
    markup_profile = markup_result.scalar_one()

    # Percentage-to-multiplier conversion (resolved judgment call #3):
    # `overhead_pct`/`profit_pct` are `Numeric(5, 2)` Decimal values
    # representing a percentage (e.g. `Decimal("10.00")` means 10%), never
    # a pre-divided fraction — dividing by `Decimal(100)` here, never
    # `float`, keeps every intermediate value an exact Decimal.
    overhead_multiplier = Decimal(1) + (markup_profile.overhead_pct / Decimal(100))
    profit_multiplier = Decimal(1) + (markup_profile.profit_pct / Decimal(100))

    # Step 5 (tax): see this module's own docstring above — no tax
    # rate/jurisdiction concept exists anywhere in this codebase's schema
    # or functional requirements for Phase 2, so this is a documented
    # no-op, not an oversight. Added (not multiplied) so a future tax
    # feature can slot in without reshaping steps 3-4.
    tax = Decimal("0")

    unrounded_total = (subtotal * overhead_multiplier * profit_multiplier) + tax
    # Single, final rounding point for the whole pipeline — see this
    # module's own docstring for why intermediate values above are
    # deliberately left unquantized.
    total = unrounded_total.quantize(CENTS, rounding=ROUND_HALF_UP)

    return EstimateCalculation(subtotal=subtotal, total=total, category_breakdown=category_breakdown)
