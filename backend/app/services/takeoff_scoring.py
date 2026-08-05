"""Scoring a proposed takeoff against the one a human actually produced.

`docs/superpowers/specs/2026-08-04-ai-blueprint-takeoff-scoping.md` §5.3 says
the feature is blocked on an accuracy bar stated as a NUMBER, and that
answering it needs a corpus of real plan sets with the takeoffs humans
produced from them. This module is the other half of that: the arithmetic
that turns a corpus into a verdict.

It is deliberately separate from `scripts/score_takeoff.py`, which is the
operator command that reads a corpus off disk and prints a report. The
scoring itself lives here so it can be unit-tested without a filesystem, the
same split `pdf_export` and its Dramatiq actor already use.

## What is being measured, and why these numbers

The bar the scoping doc proposes is *"an estimator accepts ≥70% of proposed
lines unchanged, and the error on accepted quantities is within ±5%."* Two
separate things, and both are needed:

* **Acceptance rate** alone rewards a provider that proposes one line it is
  sure of and stays quiet — high precision, useless coverage.
* **Quantity error** alone rewards a provider that proposes a hundred lines
  and happens to get the few it is scored on right.

So this reports precision and recall separately, keeps quantity error only
over lines that matched at all (an error figure over a line the human never
had is meaningless), and never collapses them into a single score. A single
number would hide exactly the tradeoff a person choosing between providers
needs to see — and under §5.2's provider-agnostic decision, comparing
providers is what this exists for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from app.services.takeoff_client import TakeoffProposal


@dataclass(frozen=True)
class ExpectedLine:
    """One line of the takeoff a human produced — the ground truth."""

    cost_catalog_item_id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True)
class LineComparison:
    cost_catalog_item_id: uuid.UUID
    expected_quantity: Decimal
    proposed_quantity: Decimal
    #: Signed relative error, proposed vs expected. 0.02 is 2% over.
    relative_error: Decimal
    within_tolerance: bool


@dataclass(frozen=True)
class CaseScore:
    """One plan set's result."""

    name: str
    #: Proposed AND expected — the lines a provider got onto the right item.
    matched: list[LineComparison]
    #: Expected but never proposed. The provider missed these entirely.
    missed: list[ExpectedLine]
    #: Proposed but not in the human's takeoff. Each is a line an estimator
    #: has to notice and delete, which is a real cost even though it prices
    #: nothing on its own.
    spurious: list[uuid.UUID]
    #: Measurements the provider surfaced as having no catalog match. Not an
    #: error — this is the honest third outcome — but a high count means the
    #: catalog or the prompt needs work.
    unmatched_count: int
    #: Lines this codebase threw out before scoring (`reject_incompatible_units`).
    rejected_count: int

    @property
    def acceptance_rate(self) -> Decimal:
        """Of everything proposed, the share an estimator could keep as-is.

        The denominator is every proposal that survived rejection — matched
        plus spurious — because a spurious line is one the estimator has to
        find and remove. Excluding them would score a provider on the
        proposals it happened to get right, which is the flattering question
        rather than the useful one.
        """
        proposed = len(self.matched) + len(self.spurious)
        if proposed == 0:
            return Decimal("0")
        usable = sum(1 for line in self.matched if line.within_tolerance)
        return (Decimal(usable) / Decimal(proposed)).quantize(Decimal("0.0001"))

    @property
    def recall(self) -> Decimal:
        """Of the human's takeoff, the share the provider found at all."""
        expected = len(self.matched) + len(self.missed)
        if expected == 0:
            return Decimal("0")
        return (Decimal(len(self.matched)) / Decimal(expected)).quantize(Decimal("0.0001"))


def score_case(
    *,
    name: str,
    proposal: TakeoffProposal,
    expected: list[ExpectedLine],
    quantity_tolerance: Decimal = Decimal("0.05"),
) -> CaseScore:
    """Compare one proposal against one human takeoff.

    Matching is by catalog item id, and lines are compared at most once each:
    a provider that proposes the same item twice gets one match and one
    spurious line, rather than two matches for one piece of ground truth.
    Both sides are folded to a single entry per item first — an estimate can
    legitimately carry the same catalog item on two lines, and for scoring
    purposes what matters is the total quantity proposed for it.
    """
    expected_by_id: dict[uuid.UUID, Decimal] = {}
    for line in expected:
        expected_by_id[line.cost_catalog_item_id] = (
            expected_by_id.get(line.cost_catalog_item_id, Decimal("0")) + line.quantity
        )

    proposed_by_id: dict[uuid.UUID, Decimal] = {}
    for proposed in proposal.lines:
        proposed_by_id[proposed.cost_catalog_item_id] = (
            proposed_by_id.get(proposed.cost_catalog_item_id, Decimal("0")) + proposed.quantity
        )

    matched: list[LineComparison] = []
    missed: list[ExpectedLine] = []

    for item_id, expected_quantity in expected_by_id.items():
        if item_id not in proposed_by_id:
            missed.append(ExpectedLine(cost_catalog_item_id=item_id, quantity=expected_quantity))
            continue
        proposed_quantity = proposed_by_id[item_id]
        if expected_quantity == 0:
            # A ground-truth quantity of zero has no meaningful relative
            # error; treat any proposal against it as exact only if it is
            # also zero, rather than dividing by it.
            relative_error = Decimal("0") if proposed_quantity == 0 else Decimal("1")
        else:
            relative_error = (
                (proposed_quantity - expected_quantity) / expected_quantity
            ).quantize(Decimal("0.0001"))
        matched.append(
            LineComparison(
                cost_catalog_item_id=item_id,
                expected_quantity=expected_quantity,
                proposed_quantity=proposed_quantity,
                relative_error=relative_error,
                within_tolerance=abs(relative_error) <= quantity_tolerance,
            )
        )

    spurious = [item_id for item_id in proposed_by_id if item_id not in expected_by_id]

    return CaseScore(
        name=name,
        matched=matched,
        missed=missed,
        spurious=spurious,
        unmatched_count=len(proposal.unmatched),
        rejected_count=len(proposal.rejected),
    )


@dataclass(frozen=True)
class CorpusScore:
    cases: list[CaseScore]

    @property
    def acceptance_rate(self) -> Decimal:
        """Pooled across every case, not the mean of per-case rates.

        A mean of rates lets a tiny case with two lines weigh as much as one
        with sixty, which would make the headline number move with the shape
        of the corpus rather than the quality of the provider.
        """
        proposed = sum(len(case.matched) + len(case.spurious) for case in self.cases)
        if proposed == 0:
            return Decimal("0")
        usable = sum(
            1 for case in self.cases for line in case.matched if line.within_tolerance
        )
        return (Decimal(usable) / Decimal(proposed)).quantize(Decimal("0.0001"))

    @property
    def recall(self) -> Decimal:
        expected = sum(len(case.matched) + len(case.missed) for case in self.cases)
        if expected == 0:
            return Decimal("0")
        matched = sum(len(case.matched) for case in self.cases)
        return (Decimal(matched) / Decimal(expected)).quantize(Decimal("0.0001"))

    def clears_bar(self, *, minimum_acceptance: Decimal = Decimal("0.70")) -> bool:
        """The go/no-go the whole exercise exists to answer.

        Deliberately a single explicit threshold with an explicit default
        rather than a blended score: §5.3's bar is a number somebody chose,
        and it should stay visible and arguable rather than disappearing into
        a formula.
        """
        return self.acceptance_rate >= minimum_acceptance
