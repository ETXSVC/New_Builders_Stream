"""Task 2.13: Estimate PDF export — template rendering + HTML-to-PDF
conversion.

Substitution note (mirrors the comment in `backend/pyproject.toml`'s
`dependencies` list, echoed here so this deviation reads as one coherent
decision rather than two independently-invented explanations): the plan
document (`docs/superpowers/plans/2026-07-09-phase-2-estimation-esignature.md`,
Task 2.13) names WeasyPrint for HTML-to-PDF conversion. WeasyPrint requires
the GTK3/Pango/Cairo native runtime, which has no simple pip-installable
path on this project's Windows development machine (current WeasyPrint docs
require a full MSYS2 toolchain install, not a small runtime installer) —
confirmed by actually attempting the install (`pip install weasyprint`
succeeded, but `import weasyprint` failed with
`OSError: cannot load library 'libgobject-2.0-0'`). With the user's explicit
approval, this module uses `xhtml2pdf` instead: pure Python (built on
reportlab/pypdf, zero native dependencies), the same "render HTML via
Jinja2, convert HTML to PDF" architecture the task spec calls for, and the
same `%PDF-` magic-byte output contract this task's own tests check
against. CSS support is a real, accepted tradeoff versus WeasyPrint (a
practical CSS 2.1-ish subset, not full modern CSS) — acceptable given this
task's explicitly simple, no-logo, no-custom-styling layout requirement
(see `app/templates/estimate_pdf.html.jinja`'s own docstring for the exact
"Explicitly Out of Scope" wording this cites).

Both functions here are pure: no DB access, no filesystem writes, no
`async`. This keeps them trivially unit-testable (see
`tests/test_estimate_pdf_export.py`) and reusable, unmodified, from both a
future synchronous code path and the Task 2.15 Dramatiq worker — neither
caller needs to know about the other, and neither this module needs to know
which one is calling it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from xhtml2pdf import pisa

from app.core.money import CENTS
from app.models import Estimate, EstimateLineItem, MarkupProfile

# app/templates/, resolved relative to this file (app/services/pdf_export.py)
# rather than the process's current working directory — matches this
# codebase's established convention for locating non-Python asset files
# relative to source location (see app/config.py's `ROOT_ENV_FILE =
# Path(__file__).resolve().parent.parent.parent / ".env"`, the closest
# existing precedent for a `Path(__file__)`-relative asset lookup in this
# project). `app/templates/` is the first template directory in this
# codebase (Task 2.13 is the first task to render anything via Jinja2).
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    # autoescape on for .jinja/.html: this template interpolates
    # user-controlled values (company_name, catalog item category/name),
    # never trusted to be pre-sanitized HTML — Jinja2's default autoescaping
    # for HTML template extensions prevents a catalog item name like
    # "<script>..." from being rendered as live markup in the exported PDF.
    autoescape=select_autoescape(["html", "jinja"]),
)


class PdfRenderError(RuntimeError):
    """Raised when xhtml2pdf's `pisa.CreatePDF` reports a non-zero error
    count (`result.err`). xhtml2pdf does not raise Python exceptions for
    malformed-but-parseable HTML; it reports failure via this return-value
    flag instead, so this module translates that into a normal Python
    exception rather than silently returning whatever (possibly empty or
    truncated) bytes ended up in the output buffer.
    """


@dataclass(frozen=True)
class EstimateLineItemDisplay:
    """One `EstimateLineItem` plus the two display fields this module
    cannot look up itself: `category` and `name` both live on
    `CostCatalogItem` (joined via `EstimateLineItem.cost_catalog_item_id`),
    not on `EstimateLineItem` — and this module has no DB access (this
    task's own "no DB access" requirement), so it cannot perform that join
    itself.

    This extends resolved judgment call #2's suggested
    `list[tuple[EstimateLineItem, str]]` (line item + category) shape to
    also carry `name`: `category` alone was the shape `EstimateLineItem`+
    `CostCatalogItem.category` needed for Task 2.12's calculation-response
    breakdown, but a line-items table with no item name would be useless.
    `name` has the exact same "lives on CostCatalogItem, not on
    EstimateLineItem" constraint as `category`, so it arrives pre-attached
    the same way. A small frozen dataclass (rather than a bare 3-tuple) is
    used so callers don't have to remember positional tuple ordering for
    three same-typed-adjacent fields (`EstimateLineItem`, `str`, `str`).

    Whatever future caller assembles these (Task 2.15's Dramatiq worker, or
    a synchronous route handler) is expected to build this list from its
    own already-executed join query — the exact same join shape
    `app/services/estimate_calculation.py`'s `calculate_estimate` already
    runs (`select(EstimateLineItem, CostCatalogItem.category)`, extended
    here to also select `CostCatalogItem.name`).
    """

    line_item: EstimateLineItem
    category: str
    name: str


def _format_currency(value: Decimal) -> str:
    """Pre-formats a `Decimal` into a currency string in Python, never
    handing a raw `Decimal` to Jinja2 for template-side formatting (this
    task's own "keeps currency formatting logic in Python, testable" rule).

    Explicitly quantizes to `CENTS` with `ROUND_HALF_UP` before formatting
    — NOT a bare `f"${value:,.2f}"` (this task's own literal example),
    which silently rounds via Python's Decimal-context default
    (`ROUND_HALF_EVEN`, "banker's rounding") instead. That default diverges
    from `ROUND_HALF_UP` at an exact `.xx5` tie (e.g. `Decimal("1.005")`
    formats as `"1.00"` under the bare f-string, `"1.01"` under this
    function) — the wrong rounding mode for this codebase, which
    established `ROUND_HALF_UP` everywhere else money gets rounded
    (`app/core/money.py`) specifically because it matches PostgreSQL's own
    `NUMERIC` rounding behavior (empirically verified, Task 2.11's review).
    Every value already quantized to 2 decimal places elsewhere (line
    totals, `estimate.subtotal`/`total`) passes through this unchanged; the
    quantize only matters for values computed fresh in THIS module (the
    overhead/profit display breakdown below), which are not yet rounded."""
    return f"${value.quantize(CENTS, rounding=ROUND_HALF_UP):,.2f}"


def _format_quantity(value: Decimal) -> str:
    """Quantity is displayed plainly (not currency-prefixed) but still
    pre-formatted to a fixed 2 decimal places in Python, matching
    `EstimateLineItem.quantity`'s `Numeric(12, 2)` column precision."""
    return f"{value:,.2f}"


def _format_percent(value: Decimal) -> str:
    """`MarkupProfile.overhead_pct`/`profit_pct` are `Numeric(5, 2)`
    percentage values (e.g. `Decimal("10.00")` means 10%, never a
    pre-divided fraction — same convention `estimate_calculation.py`
    documents) — formatted here as a plain percentage string, e.g.
    `"10.00%"`."""
    return f"{value:.2f}%"


def render_estimate_html(
    estimate: Estimate,
    line_items: list[EstimateLineItemDisplay],
    markup_profile: MarkupProfile,
    company_name: str,
) -> str:
    """Renders `estimate_pdf.html.jinja` to an HTML string. Exposed
    separately from `render_estimate_pdf` (rather than inlined) because
    asserting against an HTML string is far easier than asserting against
    PDF bytes — this task's own test list checks field presence against
    this function's output directly, not against the PDF conversion result.

    Pure function: no DB access (see module docstring), no exceptions raised
    for any valid input shape, including zero line items (handled explicitly
    below rather than left to fail inside the template) and an
    un-calculated estimate (`estimate.subtotal`/`estimate.total` both
    `None` — a real, reachable state per resolved judgment call #3: an
    Estimate can be exported before its first `POST
    /estimates/{id}/calculate` call; this function has no opinion on
    whether that SHOULD be allowed, only that it must not crash if it
    happens).
    """
    line_item_rows = [
        {
            "category": entry.category,
            "name": entry.name,
            "quantity_display": _format_quantity(entry.line_item.quantity),
            "unit_rate_display": _format_currency(entry.line_item.unit_rate_snapshot),
            "line_total_display": _format_currency(entry.line_item.line_total),
        }
        for entry in line_items
    ]

    # Category subtotals: same grouping shape as
    # `app/services/estimate_calculation.py`'s `calculate_estimate` (group
    # by category, sum `line_total` per group, sort alphabetically for
    # deterministic output) — but computed here from the pre-attached
    # `entry.category` on each `EstimateLineItemDisplay`, never from a join
    # this module performs itself (resolved judgment call #2).
    category_sums: dict[str, Decimal] = {}
    for entry in line_items:
        category_sums[entry.category] = (
            category_sums.get(entry.category, Decimal("0")) + entry.line_item.line_total
        )
    category_breakdown = [
        {"category": category, "subtotal_display": _format_currency(category_sums[category])}
        for category in sorted(category_sums)
    ]

    # Resolved judgment call #3: trust `estimate.subtotal`/`estimate.total`
    # as authoritative (Task 2.12's own "server-side recompute is the only
    # source of truth" principle) — the FINAL total rendered below is
    # always `estimate.total` verbatim, never re-derived. Render a clear
    # placeholder instead of crashing when the estimate has never been
    # calculated (`subtotal`/`total` both `None`).
    subtotal_display = (
        _format_currency(estimate.subtotal) if estimate.subtotal is not None else "Not yet calculated"
    )
    total_display = (
        _format_currency(estimate.total) if estimate.total is not None else "Not yet calculated"
    )

    # Overhead/profit DOLLAR amounts, display-only: a label showing just
    # "Overhead (10.00%)" next to a blank money column reads as a broken
    # export to a client reviewing this proposal, and every value needed to
    # show the dollar figure (subtotal, overhead_pct, profit_pct) is already
    # available here — so this recomputes the SAME overhead/profit pipeline
    # `app/services/estimate_calculation.py` already runs, purely to LABEL
    # this one row; it is never written back to `estimate` or treated as
    # authoritative anywhere (the final total on the page is still
    # `estimate.total` verbatim, per the paragraph above — this is not a
    # second source of truth, just a display breakdown of the one that
    # already exists). "Not yet calculated" propagates here too when there
    # is no subtotal to compute a breakdown from.
    #
    # Each row is independently rounded to 2 decimal places for display
    # (via `_format_currency`), while `estimate.total` was rounded ONCE, at
    # the very end of its own pipeline (Task 2.12's deliberate "round only
    # at the end, to avoid compounding error" choice). The displayed
    # subtotal + overhead + profit + tax can therefore land a cent away
    # from the displayed total in rare cases — the same harmless rounding
    # artifact real paper invoices routinely show, not a bug, and not
    # something this display-only breakdown should try to force-reconcile
    # by borrowing a cent from one row to balance another.
    if estimate.subtotal is not None:
        overhead_amount = estimate.subtotal * (markup_profile.overhead_pct / Decimal(100))
        subtotal_with_overhead = estimate.subtotal + overhead_amount
        profit_amount = subtotal_with_overhead * (markup_profile.profit_pct / Decimal(100))
        overhead_display = _format_currency(overhead_amount)
        profit_display = _format_currency(profit_amount)
    else:
        overhead_display = "Not yet calculated"
        profit_display = "Not yet calculated"

    # Tax is always a documented no-op per Task 2.12 (no tax
    # rate/jurisdiction concept exists in this codebase's schema or
    # functional requirements for Phase 2) — always displayed as $0.00/0%,
    # never computed from anything.
    tax_label = f"Tax ({_format_percent(Decimal('0'))})"
    tax_display = _format_currency(Decimal("0"))

    template = _jinja_env.get_template("estimate_pdf.html.jinja")
    return template.render(
        company_name=company_name,
        line_items=line_item_rows,
        category_breakdown=category_breakdown,
        subtotal_display=subtotal_display,
        overhead_label=f"Overhead ({_format_percent(markup_profile.overhead_pct)})",
        overhead_display=overhead_display,
        profit_label=f"Profit ({_format_percent(markup_profile.profit_pct)})",
        profit_display=profit_display,
        tax_label=tax_label,
        tax_display=tax_display,
        total_display=total_display,
    )


def render_estimate_pdf(
    estimate: Estimate,
    line_items: list[EstimateLineItemDisplay],
    markup_profile: MarkupProfile,
    company_name: str,
) -> bytes:
    """Renders `estimate_pdf.html.jinja` via `render_estimate_html`, then
    converts the resulting HTML string to PDF bytes via `xhtml2pdf`
    (substituted for the plan doc's named WeasyPrint — see module
    docstring). Pure function: no DB access, no filesystem writes, no
    `async` (this task's own requirement) — the caller (a future
    synchronous code path, or the Task 2.15 Dramatiq worker) owns deciding
    what to do with the returned bytes (persist to disk, stream in a
    response, etc.).
    """
    html = render_estimate_html(estimate, line_items, markup_profile, company_name)

    buffer = BytesIO()
    result = pisa.CreatePDF(html, dest=buffer)
    if result.err:
        raise PdfRenderError(
            f"xhtml2pdf reported {result.err} error(s) converting the rendered "
            f"estimate HTML to PDF"
        )

    return buffer.getvalue()
