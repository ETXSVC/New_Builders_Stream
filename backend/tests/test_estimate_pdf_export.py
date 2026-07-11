"""Task 2.13: `app/services/pdf_export.py` rendering-only tests.

Unlike most test modules in this codebase, these tests do NOT go through
`client`/HTTP or touch the database at all — `render_estimate_html` and
`render_estimate_pdf` are pure functions (this task's own "no DB access, no
filesystem writes, no async" requirement), so they're exercised directly
with plain, in-memory ORM model instances (never `flush`ed/committed to any
session). The async job wiring that will actually fetch these objects from
the DB and call these functions is Task 2.15's job, not this one's.

Substitution note: the plan document names WeasyPrint for HTML-to-PDF
conversion; this codebase uses `xhtml2pdf` instead (pure Python, no native
GTK3/Pango/Cairo runtime dependency — see the substitution comment in
`backend/pyproject.toml` and the module docstring in
`app/services/pdf_export.py` for the full rationale). These tests check the
same `%PDF-` magic-byte output contract the plan doc's own test list
specifies, regardless of which library produced it.
"""

import uuid
from decimal import Decimal

from app.models import Estimate, EstimateLineItem, MarkupProfile
from app.services.pdf_export import (
    EstimateLineItemDisplay,
    render_estimate_html,
    render_estimate_pdf,
)


def _markup_profile(**overrides):
    payload = {
        "id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "name": "Standard Markup",
        "overhead_pct": Decimal("10.00"),
        "profit_pct": Decimal("15.00"),
    }
    payload.update(overrides)
    return MarkupProfile(**payload)


def _estimate(**overrides):
    payload = {
        "id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "markup_profile_id": uuid.uuid4(),
        "status": "draft",
        "subtotal": Decimal("649.99"),
        "total": Decimal("822.24"),
        "is_snapshotted": False,
    }
    payload.update(overrides)
    return Estimate(**payload)


def _line_item_display(*, category, name, quantity, unit_rate_snapshot, line_total=None):
    line_total = line_total if line_total is not None else (quantity * unit_rate_snapshot)
    line_item = EstimateLineItem(
        id=uuid.uuid4(),
        estimate_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        cost_catalog_item_id=uuid.uuid4(),
        quantity=quantity,
        unit_rate_snapshot=unit_rate_snapshot,
        line_total=line_total,
    )
    return EstimateLineItemDisplay(line_item=line_item, category=category, name=name)


def _representative_line_items():
    return [
        _line_item_display(
            category="framing",
            name="2x4 Lumber",
            quantity=Decimal("10.00"),
            unit_rate_snapshot=Decimal("45.00"),
            line_total=Decimal("450.00"),
        ),
        _line_item_display(
            category="framing",
            name="Drywall",
            quantity=Decimal("5.00"),
            unit_rate_snapshot=Decimal("20.00"),
            line_total=Decimal("100.00"),
        ),
        _line_item_display(
            category="electrical",
            name="Wiring",
            quantity=Decimal("3.00"),
            unit_rate_snapshot=Decimal("33.33"),
            line_total=Decimal("99.99"),
        ),
    ]


# =============================================================================
# render_estimate_pdf: PDF byte-level output
# =============================================================================


def test_render_estimate_pdf_produces_valid_nonempty_pdf_bytes():
    """A representative multi-line, multi-category estimate renders to
    non-empty bytes starting with the PDF magic-byte header `%PDF-` — full
    visual/pixel verification is disproportionate for this test layer, per
    this task's own test spec."""
    estimate = _estimate()
    markup_profile = _markup_profile()
    line_items = _representative_line_items()

    pdf_bytes = render_estimate_pdf(estimate, line_items, markup_profile, "Acme Construction")

    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert pdf_bytes.startswith(b"%PDF-")


def test_render_estimate_pdf_zero_line_items_does_not_crash():
    """Zero line items must render cleanly, not raise — no line-item loop,
    no category breakdown, no division of any kind in this pipeline."""
    estimate = _estimate(subtotal=Decimal("0.00"), total=Decimal("0.00"))
    markup_profile = _markup_profile()

    pdf_bytes = render_estimate_pdf(estimate, [], markup_profile, "Acme Construction")

    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert pdf_bytes.startswith(b"%PDF-")


def test_render_estimate_pdf_uncalculated_estimate_does_not_crash():
    """Resolved judgment call #3: `estimate.subtotal`/`estimate.total`
    being `None` (never calculated) is a real, reachable state this
    function must handle gracefully, not crash on."""
    estimate = _estimate(subtotal=None, total=None)
    markup_profile = _markup_profile()
    line_items = _representative_line_items()

    pdf_bytes = render_estimate_pdf(estimate, line_items, markup_profile, "Acme Construction")

    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert pdf_bytes.startswith(b"%PDF-")


# =============================================================================
# render_estimate_html: field-presence checks against the intermediate HTML
# =============================================================================


def test_render_estimate_html_contains_company_name_and_every_line_item_field():
    """Asserts against the intermediate HTML string (far easier to inspect
    than PDF bytes, per this task's own test spec): the company name and
    every line item's category/name/quantity/unit_rate_snapshot/line_total
    must appear somewhere in the rendered HTML."""
    estimate = _estimate()
    markup_profile = _markup_profile()
    line_items = _representative_line_items()

    html = render_estimate_html(estimate, line_items, markup_profile, "Acme Construction")

    assert "Acme Construction" in html

    for entry in line_items:
        assert entry.category in html
        assert entry.name in html
        # Quantity/unit_rate_snapshot/line_total are pre-formatted in
        # Python before reaching the template (this task's own
        # requirement) — check for their formatted representations, not
        # raw Decimal reprs, since that's what actually appears in the HTML.
        assert f"{entry.line_item.quantity:,.2f}" in html
        assert f"${entry.line_item.unit_rate_snapshot:,.2f}" in html
        assert f"${entry.line_item.line_total:,.2f}" in html


def test_render_estimate_html_zero_line_items_still_contains_company_name():
    estimate = _estimate(subtotal=Decimal("0.00"), total=Decimal("0.00"))
    markup_profile = _markup_profile()

    html = render_estimate_html(estimate, [], markup_profile, "Acme Construction")

    assert "Acme Construction" in html
    assert "<table class=\"line-items\">" not in html


def test_render_estimate_html_shows_category_subtotals():
    estimate = _estimate()
    markup_profile = _markup_profile()
    line_items = _representative_line_items()

    html = render_estimate_html(estimate, line_items, markup_profile, "Acme Construction")

    # framing: 450.00 + 100.00 = 550.00; electrical: 99.99 (unchanged).
    assert "$550.00" in html
    assert "$99.99" in html


def test_render_estimate_html_shows_overhead_profit_and_total():
    """Overhead/profit rows show a real dollar figure, not just a bare
    percentage label next to an empty cell (a display-only recompute of
    the same pipeline `estimate_calculation.py` runs — see
    `render_estimate_html`'s own comment for why this doesn't violate
    "estimate.total is the only source of truth" for the FINAL total).
    Hand-computed: overhead = 649.99 * 0.10 = 64.999 -> $65.00;
    subtotal-with-overhead = 714.989; profit = 714.989 * 0.15 = 107.24835
    -> $107.25 (both ROUND_HALF_UP, neither an exact tie)."""
    estimate = _estimate(subtotal=Decimal("649.99"), total=Decimal("822.24"))
    markup_profile = _markup_profile(overhead_pct=Decimal("10.00"), profit_pct=Decimal("15.00"))
    line_items = _representative_line_items()

    html = render_estimate_html(estimate, line_items, markup_profile, "Acme Construction")

    assert "Overhead (10.00%)" in html
    assert "Profit (15.00%)" in html
    assert "Tax (0.00%)" in html
    assert "$649.99" in html
    assert "$65.00" in html
    assert "$107.25" in html
    assert "$822.24" in html


def test_render_estimate_html_uncalculated_estimate_shows_placeholder():
    """Resolved judgment call #3's placeholder text, not a crash and not a
    misleading `$0.00` that would falsely read as "calculated as free" —
    all four dollar-bearing summary rows (subtotal, overhead, profit,
    total) show the placeholder when there's no subtotal to compute a
    breakdown from; only tax (always a fixed $0.00 no-op) does not."""
    estimate = _estimate(subtotal=None, total=None)
    markup_profile = _markup_profile()
    line_items = _representative_line_items()

    html = render_estimate_html(estimate, line_items, markup_profile, "Acme Construction")

    assert html.count("Not yet calculated") == 4


# =============================================================================
# _format_currency: ROUND_HALF_UP rounding, not the Decimal-context default
# =============================================================================


def test_render_estimate_html_currency_rounding_matches_round_half_up_not_bankers_rounding():
    """Regression test for a real bug caught during this task's review:
    `_format_currency`'s original implementation was a bare
    `f"${value:,.2f}"`, which rounds via Python's Decimal-context default
    (`ROUND_HALF_EVEN`, "banker's rounding") — inconsistent with
    `app/core/money.py`'s `CENTS`/`ROUND_HALF_UP`, which every other money
    value in this codebase uses specifically because it matches
    PostgreSQL's own `NUMERIC` rounding (empirically verified, Task 2.11's
    review). This only became OBSERVABLE once this module started computing
    fresh, not-yet-2-decimal-place values itself (the overhead/profit
    display breakdown) — every value `_format_currency` was called on
    before that fix already arrived pre-quantized to 2 decimal places, so
    the wrong rounding mode was latent, not yet exercised.

    subtotal=201.00, overhead_pct=0.00 (so subtotal-with-overhead stays
    201.00 exactly), profit_pct=0.50% -> profit = 201.00 * 0.005 = 1.005
    exactly, an exact `.xx5` cent tie (verified via a throwaway
    computation, not assumed). ROUND_HALF_UP rounds this to "$1.01"; the
    old buggy bare-f-string default (ROUND_HALF_EVEN, "round to even")
    would have produced "$1.00" instead, since 0 (the digit before the
    tie) is even."""
    estimate = _estimate(subtotal=Decimal("201.00"), total=Decimal("202.01"))
    markup_profile = _markup_profile(overhead_pct=Decimal("0.00"), profit_pct=Decimal("0.50"))

    html = render_estimate_html(estimate, [], markup_profile, "Acme Construction")

    assert "$1.01" in html
    assert "$1.00" not in html


# =============================================================================
# Autoescaping: user-controlled values must not inject live HTML/script
# =============================================================================


def test_render_estimate_html_escapes_user_controlled_values():
    """Regression test confirming Jinja2's autoescaping (`Environment(...,
    autoescape=select_autoescape(["html", "jinja"]))`) actually takes
    effect for this template's `.jinja` extension — company name and
    catalog-item category/name are user-controlled (a company's own name,
    or a Cost Catalog item's category/name, both caller-supplied text) and
    must never be rendered as live markup in an exported PDF."""
    estimate = _estimate()
    markup_profile = _markup_profile()
    line_items = [
        _line_item_display(
            category="<script>alert(1)</script>",
            name='Evil" onmouseover="alert(2)',
            quantity=Decimal("1.00"),
            unit_rate_snapshot=Decimal("1.00"),
            line_total=Decimal("1.00"),
        )
    ]

    html = render_estimate_html(
        estimate, line_items, markup_profile, "Acme<img src=x onerror=alert(3)>"
    )

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=" not in html
    assert "&lt;img" in html


def test_render_estimate_html_autoescapes_untrusted_string_fields():
    """`company_name` and every `EstimateLineItemDisplay.category`/`.name`
    are untrusted strings (company_name is user-editable Company data;
    category/name come from CostCatalogItem, editable by any admin/PM) that
    flow directly into Jinja2 `{{ }}` expressions in
    estimate_pdf.html.jinja. `_jinja_env`'s `select_autoescape(["html",
    "jinja"])` is supposed to autoescape because the loaded template's name
    ends in `.jinja` (`estimate_pdf.html.jinja`) — this test proves that
    empirically rather than trusting the config to behave as intended: a
    `<script>`/`<img onerror=...>` payload must appear HTML-entity-escaped
    in the rendered output, never as live, browser-executable markup. This
    matters even though the ultimate consumer is a PDF (not a browser) —
    xhtml2pdf itself parses HTML and would otherwise interpret unescaped
    tags/attributes as real markup in the rendered document, and
    `render_estimate_html`'s output is independently useful/inspectable
    before PDF conversion."""
    estimate = _estimate()
    markup_profile = _markup_profile()
    malicious_line_items = [
        _line_item_display(
            category="<script>alert('cat')</script>",
            name="<img src=x onerror=alert('name')>",
            quantity=Decimal("1.00"),
            unit_rate_snapshot=Decimal("1.00"),
            line_total=Decimal("1.00"),
        )
    ]

    html = render_estimate_html(
        estimate, malicious_line_items, markup_profile, "<b>Acme</b> & \"Sons\""
    )

    # Raw, unescaped markup must never appear.
    assert "<script>alert('cat')</script>" not in html
    assert "<img src=x onerror=alert('name')>" not in html
    assert "<b>Acme</b> & \"Sons\"" not in html

    # The HTML-entity-escaped forms must appear instead, proving Jinja2
    # actually ran the values through its escaper rather than silently
    # dropping them.
    assert "&lt;script&gt;alert(&#39;cat&#39;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(&#39;name&#39;)&gt;" in html
    assert "&lt;b&gt;Acme&lt;/b&gt; &amp; &#34;Sons&#34;" in html
