"""The takeoff seam: the unit check, and the scoring arithmetic.

No database, no network, no `client` fixture — every one of these is pure
computation over the dataclasses in `app/services/takeoff_client.py` and
`app/services/takeoff_scoring.py`, the same shape `test_estimate_pdf_export.py`
uses for the render layer.

There is no vendor adapter to test yet, deliberately (see
`takeoff_client`'s own docstring). What CAN be got wrong today is the rule
this codebase enforces over any adapter's output, and the arithmetic that
decides whether a provider is good enough to build on — so that is what these
cover.
"""

import uuid
from decimal import Decimal

import pytest

from app.services.takeoff_client import (
    CatalogEntry,
    FakeTakeoffProviderClient,
    ProposedLine,
    SheetRef,
    TakeoffProposal,
    TakeoffRequest,
    UnmatchedMeasurement,
    get_takeoff_client,
    reject_incompatible_units,
    units_are_compatible,
)
from app.services.takeoff_scoring import CorpusScore, ExpectedLine, score_case

STUD_WALL = uuid.uuid4()
DRYWALL = uuid.uuid4()


def _catalog() -> list[CatalogEntry]:
    return [
        CatalogEntry(id=STUD_WALL, category="Framing", name="2x4 stud wall", unit="lf"),
        CatalogEntry(id=DRYWALL, category="Interior", name="1/2in drywall", unit="sf"),
    ]


def _line(item_id, quantity, unit, *, confidence=0.9, provenance=None) -> ProposedLine:
    return ProposedLine(
        cost_catalog_item_id=item_id,
        quantity=Decimal(quantity),
        measured_unit=unit,
        confidence=confidence,
        provenance=provenance,
    )


def _proposal(lines, unmatched=None) -> TakeoffProposal:
    return TakeoffProposal(lines=lines, unmatched=unmatched or [], rejected=[])


# --- Unit compatibility ------------------------------------------------------


@pytest.mark.parametrize(
    "measured,catalogued",
    [
        ("lf", "lf"),
        ("LF", "lf"),  # case never carries meaning in a unit
        ("  lf  ", "lf"),  # nor does surrounding whitespace
        ("lin ft", "lf"),  # documented synonyms fold together
        ("linear feet", "LF"),
        ("ea.", "each"),  # a trailing period is an abbreviation, not a unit
    ],
)
def test_units_that_mean_the_same_thing_are_compatible(measured, catalogued):
    assert units_are_compatible(measured, catalogued)


@pytest.mark.parametrize(
    "measured,catalogued",
    [
        ("lf", "sf"),  # the case the whole check exists for
        ("sf", "cy"),
        ("ea", "lf"),
        ("bf", "lf"),  # board feet is not in the synonym table and must not be guessed at
        ("widgets", "gadgets"),  # two unknown units are equal only if written identically
    ],
)
def test_units_that_differ_are_not_compatible(measured, catalogued):
    assert not units_are_compatible(measured, catalogued)


def test_unknown_units_compare_literally_rather_than_being_guessed_at():
    """The synonym table is conservative on purpose. Erring toward literal
    comparison occasionally rejects a good line, which a reviewer sees and can
    act on; the opposite error puts a mispriced line in front of a customer."""
    assert units_are_compatible("board foot", "board foot")
    assert not units_are_compatible("board foot", "bf")


def test_a_line_measured_in_the_wrong_unit_is_rejected_with_its_reason():
    """142 linear feet cannot become an item sold by the square foot. This is
    the one machine-checkable form of the mapping error described in the
    scoping doc §3.2 — and a mapping error is a wrong PRICE, which is far
    harder for a reviewer to spot than a wrong quantity, because the number
    looks right."""
    proposal = _proposal([_line(STUD_WALL, "142", "sf")])

    checked = reject_incompatible_units(proposal, _catalog())

    assert checked.lines == []
    assert len(checked.rejected) == 1
    # The reason travels with it: a takeoff that quietly discards its own
    # output teaches an estimator to trust numbers they cannot trace.
    assert "sf" in checked.rejected[0].reason
    assert "lf" in checked.rejected[0].reason


def test_a_line_naming_an_item_outside_this_catalog_is_rejected():
    """Not paranoia about adapters: the catalog handed to a provider is
    resolved for one company at one moment, so a proposal naming something
    outside it is either stale or invented."""
    proposal = _proposal([_line(uuid.uuid4(), "10", "lf")])

    checked = reject_incompatible_units(proposal, _catalog())

    assert checked.lines == []
    assert "not in this company's resolved catalog" in checked.rejected[0].reason


def test_compatible_lines_survive_the_check_unchanged():
    """The non-vacuity floor: a check that rejected everything would pass
    every test above."""
    proposal = _proposal([_line(STUD_WALL, "142", "lf"), _line(DRYWALL, "980", "sf")])

    checked = reject_incompatible_units(proposal, _catalog())

    assert len(checked.lines) == 2
    assert checked.rejected == []


async def test_the_fake_applies_the_unit_check_to_its_own_canned_proposal():
    """Otherwise the fake would be a way to smuggle a proposal past a rule
    every real adapter's output is held to, and tests written against it would
    prove something the production path does not do."""
    fake = FakeTakeoffProviderClient(_proposal([_line(STUD_WALL, "142", "sf")]))

    result = await fake.propose_takeoff(
        TakeoffRequest(
            document_id=uuid.uuid4(), pdf_bytes=None, sheet_images=[], catalog=_catalog()
        )
    )

    assert result.lines == []
    assert len(result.rejected) == 1


def test_no_configured_provider_yields_the_fake():
    """A deployment that has configured nothing makes no network calls and
    costs nothing — the same default `get_accounting_client` and
    `stripe_client` establish."""
    assert isinstance(get_takeoff_client(None), FakeTakeoffProviderClient)
    assert isinstance(get_takeoff_client("some-unconfigured-vendor"), FakeTakeoffProviderClient)


def test_provenance_may_be_absent_and_that_is_expressible():
    """Providers differ in whether they can cite the page a measurement came
    from. The interface has to be able to SAY a proposal has no provenance
    rather than paper over it — an estimator should be able to tell "page 4"
    from "somewhere in this document"."""
    with_page = _line(STUD_WALL, "142", "lf", provenance=SheetRef(uuid.uuid4(), 4))
    without = _line(DRYWALL, "980", "sf")

    assert with_page.provenance is not None
    assert with_page.provenance.page == 4
    assert without.provenance is None


# --- Scoring -----------------------------------------------------------------


def test_a_quantity_inside_tolerance_counts_as_accepted():
    proposal = _proposal([_line(STUD_WALL, "144", "lf")])

    score = score_case(
        name="case",
        proposal=proposal,
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )

    assert len(score.matched) == 1
    # 2/142 is about 1.4% — inside the 5% default.
    assert score.matched[0].within_tolerance
    assert score.acceptance_rate == Decimal("1.0000")


def test_a_quantity_outside_tolerance_is_matched_but_not_accepted():
    """The distinction the report leans on: the provider found the right item
    (so recall counts it) and got the number wrong (so acceptance does not).
    Collapsing the two would hide which of the two problems a provider has."""
    proposal = _proposal([_line(STUD_WALL, "200", "lf")])

    score = score_case(
        name="case",
        proposal=proposal,
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )

    assert len(score.matched) == 1
    assert not score.matched[0].within_tolerance
    assert score.recall == Decimal("1.0000")
    assert score.acceptance_rate == Decimal("0")


def test_a_spurious_line_lowers_acceptance_even_though_it_prices_nothing_by_itself():
    """A line the human's takeoff never had is one an estimator must notice
    and delete. Excluding those from the denominator would score a provider on
    the proposals it happened to get right — the flattering question rather
    than the useful one."""
    proposal = _proposal([_line(STUD_WALL, "142", "lf"), _line(DRYWALL, "980", "sf")])

    score = score_case(
        name="case",
        proposal=proposal,
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )

    assert score.spurious == [DRYWALL]
    # One usable proposal out of two made.
    assert score.acceptance_rate == Decimal("0.5000")
    # ...but it did find everything the human had.
    assert score.recall == Decimal("1.0000")


def test_a_missed_line_lowers_recall_without_touching_acceptance():
    """The mirror image, and the reason both numbers are always reported: a
    provider that proposes one line it is sure of and stays quiet scores
    perfectly on acceptance."""
    proposal = _proposal([_line(STUD_WALL, "142", "lf")])

    score = score_case(
        name="case",
        proposal=proposal,
        expected=[
            ExpectedLine(STUD_WALL, Decimal("142")),
            ExpectedLine(DRYWALL, Decimal("980")),
        ],
    )

    assert score.acceptance_rate == Decimal("1.0000")
    assert score.recall == Decimal("0.5000")
    assert len(score.missed) == 1


def test_the_same_item_proposed_twice_is_summed_rather_than_double_counted():
    """An estimate can legitimately carry one catalog item on two lines, so
    for scoring what matters is the total quantity proposed for it. Counting
    them separately would let a provider earn two matches from one piece of
    ground truth."""
    proposal = _proposal([_line(STUD_WALL, "100", "lf"), _line(STUD_WALL, "42", "lf")])

    score = score_case(
        name="case",
        proposal=proposal,
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )

    assert len(score.matched) == 1
    assert score.matched[0].proposed_quantity == Decimal("142")
    assert score.matched[0].within_tolerance


def test_unmatched_measurements_are_reported_and_are_not_scored_as_errors():
    """The honest third outcome: measured something real, found nothing in
    this company's catalog for it. Not a failure — but a high count means the
    catalog or the prompt needs work, so it is surfaced rather than dropped."""
    proposal = _proposal(
        [_line(STUD_WALL, "142", "lf")],
        unmatched=[
            UnmatchedMeasurement(
                description="Temporary site fencing",
                quantity=Decimal("120"),
                measured_unit="lf",
                confidence=0.8,
                provenance=None,
            )
        ],
    )

    score = score_case(
        name="case",
        proposal=proposal,
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )

    assert score.unmatched_count == 1
    assert score.acceptance_rate == Decimal("1.0000")
    assert score.spurious == []


def test_a_corpus_pools_across_cases_rather_than_averaging_their_rates():
    """A mean of per-case rates lets a two-line case weigh as much as a
    sixty-line one, which makes the headline number move with the shape of the
    corpus rather than the quality of the provider."""
    tiny = score_case(
        name="tiny",
        proposal=_proposal([_line(STUD_WALL, "142", "lf")]),
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )
    big = score_case(
        name="big",
        # Both wildly out: 999 against 142, and 1500 against 980. Picking a
        # number that merely LOOKS wrong is not enough — 999 against 980 is
        # +1.9%, inside the tolerance, which is how this test first claimed a
        # failure the code had not made.
        proposal=_proposal([_line(STUD_WALL, "999", "lf"), _line(DRYWALL, "1500", "sf")]),
        expected=[
            ExpectedLine(STUD_WALL, Decimal("142")),
            ExpectedLine(DRYWALL, Decimal("980")),
        ],
    )

    corpus = CorpusScore(cases=[tiny, big])

    # Pooled: 1 usable proposal out of 3 made. A mean of the per-case rates
    # (100% and 0%) would have said 50%.
    assert corpus.acceptance_rate == Decimal("0.3333")


def test_clears_bar_is_an_explicit_threshold_the_caller_can_argue_with():
    """§5.3's bar is a number somebody chose. It stays visible and
    overridable rather than disappearing into a blended score."""
    corpus = CorpusScore(
        cases=[
            score_case(
                name="case",
                # One exact, one far out (1500 against 980) — not 999, which
                # is within tolerance of 980 and would score 100%.
                proposal=_proposal([_line(STUD_WALL, "142", "lf"), _line(DRYWALL, "1500", "sf")]),
                expected=[
                    ExpectedLine(STUD_WALL, Decimal("142")),
                    ExpectedLine(DRYWALL, Decimal("980")),
                ],
            )
        ]
    )

    assert corpus.acceptance_rate == Decimal("0.5000")
    assert not corpus.clears_bar(minimum_acceptance=Decimal("0.70"))
    assert corpus.clears_bar(minimum_acceptance=Decimal("0.50"))


def test_an_empty_proposal_scores_zero_rather_than_dividing_by_zero():
    """The floor the fake produces against a real corpus, and the number any
    provider has to beat."""
    score = score_case(
        name="case",
        proposal=_proposal([]),
        expected=[ExpectedLine(STUD_WALL, Decimal("142"))],
    )

    assert score.acceptance_rate == Decimal("0")
    assert score.recall == Decimal("0")
    assert len(score.missed) == 1
