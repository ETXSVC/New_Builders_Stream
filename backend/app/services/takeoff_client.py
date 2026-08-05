"""The seam an AI blueprint takeoff plugs into — stated in this product's
terms, not any vendor's.

Design and the decisions behind it:
`docs/superpowers/specs/2026-08-04-ai-blueprint-takeoff-scoping.md`. Two of
those decisions shape this module directly:

* **Provider-agnostic** (§5.2). No vendor's SDK, model id, request shape or
  response format may appear outside the adapter that speaks to it. The
  naive way to honour that would be to abstract over the *intersection* of
  what every provider offers, which would strip out exactly the capabilities
  that make a takeoff reviewable. So the seam sits at the DOMAIN instead —
  the Protocol below asks for a proposal and says nothing about how one is
  obtained. One adapter may need three round trips and another one; one may
  cite pages natively and another may have to earn provenance a different
  way. All of that is adapter-local.

* **Per-tenant opt-in** (§5.1). Nothing here runs unless a tenant has said
  yes, and the setting that records that says *which provider* rather than
  merely "yes to AI" — a procurement review works in vendors, not in
  categories. `get_takeoff_client` takes the provider as an argument for
  that reason; resolving it per tenant is the caller's job.

Same Protocol-plus-fake shape as `app/services/accounting_client.py` and
`app/services/stripe_client.py`, including the part that matters most: the
**fake is the default and is what the entire test suite exercises**. A suite
that reached a real vision model would be slow, expensive and
non-deterministic, and a takeoff's whole difficulty is that its output
varies.

## What is deliberately NOT here

No vendor adapter, no route, no UI. Those are downstream of the question
this feature is actually blocked on — whether any provider clears an
accuracy bar stated as a number (§5.3) — and that is answered by
`scripts/score_takeoff.py` against a corpus, not by writing more of the
feature. One Protocol with one implementation is a Protocol with extra
steps; this exists so the measurement can happen at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol, Sequence


@dataclass(frozen=True)
class SheetRef:
    """Where in a document a measurement came from.

    `page` is 1-indexed to match how a person reads a plan set and how the
    document-citation APIs that can produce it report page locations.
    """

    document_id: uuid.UUID
    page: int | None


@dataclass(frozen=True)
class CatalogEntry:
    """One row of the company's resolved catalog, as an adapter sees it.

    Deliberately not the `CostCatalogItem` ORM object: an adapter has no
    business holding a database row, and `unit_rate` is absent because a
    takeoff measures quantities and never prices them — pricing is the
    estimate builder's job, from the catalog, at save time.
    """

    id: uuid.UUID
    category: str
    name: str
    unit: str


@dataclass(frozen=True)
class ProposedLine:
    """A measurement the provider believes maps to a catalog item.

    `provenance` is **optional, and its absence is meaningful** rather than a
    detail to paper over: providers differ in whether they can cite the page
    a measurement came from, and an estimator reviewing a proposal should be
    able to tell "page 4 of A-201" from "somewhere in this document". A
    proposal without provenance is still usable; it is just harder to check,
    and the reviewer deserves to know which they have.

    `measured_unit` is what the provider says it measured in, kept separate
    from the catalog item's own unit so the two can be COMPARED — see
    `reject_incompatible_units`. Collapsing them would destroy the only hard
    check available.
    """

    cost_catalog_item_id: uuid.UUID
    quantity: Decimal
    measured_unit: str
    confidence: float
    provenance: SheetRef | None


@dataclass(frozen=True)
class UnmatchedMeasurement:
    """Something measured that nothing in this company's catalog prices.

    **The third outcome, and the one that is easy to leave out.** A catalog
    is per-tenant and freely named, so a takeoff will regularly measure real
    work a given company has no line for. The wrong answers are to drop it
    silently (the estimator never learns the takeoff saw it) or to invent a
    free-form line for it — migration 0035 made those possible, and a price a
    model made up on a document a customer signs is a strictly worse failure
    than the one it dodges. So it is surfaced, described, and left for a
    person to price or ignore.
    """

    description: str
    quantity: Decimal
    measured_unit: str
    confidence: float
    provenance: SheetRef | None


@dataclass(frozen=True)
class RejectedLine:
    """A proposal this codebase threw out, and why.

    Kept rather than filtered away so the reason can be shown. A takeoff that
    quietly discards its own output teaches an estimator to trust a number
    they cannot see the derivation of.
    """

    line: ProposedLine
    reason: str


@dataclass(frozen=True)
class TakeoffProposal:
    """Everything one pass produced. Never written anywhere by itself — a
    proposal becomes an estimate only when a person saves it through the
    existing builder (§3.3)."""

    lines: list[ProposedLine]
    unmatched: list[UnmatchedMeasurement]
    rejected: list[RejectedLine]


@dataclass(frozen=True)
class TakeoffRequest:
    """One document to read, and the catalog to map it onto.

    `sheet_images` is a list of raw page images; `pdf_bytes` is the document
    itself. An adapter takes whichever suits it — some providers accept a PDF
    natively, others want rasterised pages — and that choice is exactly the
    kind of thing the domain seam exists to keep local to an adapter.
    """

    document_id: uuid.UUID
    pdf_bytes: bytes | None
    sheet_images: list[bytes]
    catalog: Sequence[CatalogEntry]


class TakeoffProviderClient(Protocol):
    """Given a document and a catalog, propose line items.

    That sentence is the whole contract. Everything a provider does to honour
    it — how many calls, whether it rasterises, how it earns provenance — is
    below this line and stays there.
    """

    async def propose_takeoff(self, request: TakeoffRequest) -> TakeoffProposal: ...


# --- Unit compatibility ------------------------------------------------------
#
# The one hard check available on a proposal (§3.2). A takeoff can measure
# beautifully and still map a measurement onto the wrong catalog row, and a
# wrong mapping is a wrong PRICE rather than a wrong number — which is far
# harder for a reviewer to spot, because the quantity looks right.
#
# Units are the one place that mismatch becomes machine-checkable: 142 linear
# feet cannot become an item sold by the square foot. Enforced here, over
# whatever any adapter returns, rather than asked for in a prompt — a prompt
# is a request, and this is a rule.

# Conservative on purpose. Only spellings that unambiguously mean the same
# measure are grouped; anything not listed compares literally, so an unknown
# unit is never silently treated as equivalent to another. Erring toward
# literal comparison means the check occasionally rejects a good line, which
# a reviewer sees and can act on — the opposite error puts a mispriced line
# in front of a customer.
_UNIT_SYNONYMS: dict[str, str] = {
    "lf": "length", "lin ft": "length", "linear ft": "length", "linear feet": "length",
    "lineal ft": "length", "ft": "length", "feet": "length", "foot": "length",
    "sf": "area", "sq ft": "area", "square ft": "area", "square feet": "area",
    "sqft": "area",
    "cy": "volume", "cu yd": "volume", "cubic yard": "volume", "cubic yards": "volume",
    "ea": "count", "each": "count", "eaches": "count", "pc": "count", "piece": "count",
    "hr": "time", "hour": "time", "hours": "time",
}


def _normalise_unit(unit: str) -> str:
    """Fold a unit to whatever it can be compared as.

    Case and surrounding whitespace never carry meaning here ("SF" and "sf"
    are the same unit), and a trailing period is a common way to write an
    abbreviation. Anything the synonym table does not know keeps its own
    literal spelling, so two unrecognised units are equal only if they are
    written identically.
    """
    cleaned = unit.strip().lower().rstrip(".")
    return _UNIT_SYNONYMS.get(cleaned, cleaned)


def units_are_compatible(measured: str, catalogued: str) -> bool:
    return _normalise_unit(measured) == _normalise_unit(catalogued)


def reject_incompatible_units(
    proposal: TakeoffProposal, catalog: Sequence[CatalogEntry]
) -> TakeoffProposal:
    """Move every line whose measured unit disagrees with its catalog item's
    into `rejected`, with the reason attached.

    A line naming a catalog item that is not in this catalog is rejected the
    same way. That is not paranoia about adapters: the catalog handed to a
    provider is resolved for one company at one moment, and a proposal that
    comes back naming something outside it is either stale or invented —
    either way it must not reach an estimate.
    """
    by_id = {entry.id: entry for entry in catalog}
    kept: list[ProposedLine] = []
    rejected = list(proposal.rejected)

    for line in proposal.lines:
        entry = by_id.get(line.cost_catalog_item_id)
        if entry is None:
            rejected.append(
                RejectedLine(
                    line=line,
                    reason=(
                        f"proposed catalog item {line.cost_catalog_item_id} is not in this "
                        "company's resolved catalog"
                    ),
                )
            )
            continue
        if not units_are_compatible(line.measured_unit, entry.unit):
            rejected.append(
                RejectedLine(
                    line=line,
                    reason=(
                        f"measured in {line.measured_unit!r} but {entry.name!r} is priced "
                        f"per {entry.unit!r}"
                    ),
                )
            )
            continue
        kept.append(line)

    return replace(proposal, lines=kept, rejected=rejected)


# --- The fake, and the selector ---------------------------------------------


class FakeTakeoffProviderClient:
    """The default, and what the whole test suite runs against.

    Returns a proposal handed to it at construction, or an empty one. That is
    the useful behaviour for a fake here: a takeoff's output is exactly the
    thing that varies, so a test or a harness run needs to state the output it
    is reasoning about rather than have one generated for it.
    """

    def __init__(self, proposal: TakeoffProposal | None = None) -> None:
        self._proposal = proposal or TakeoffProposal(lines=[], unmatched=[], rejected=[])

    async def propose_takeoff(self, request: TakeoffRequest) -> TakeoffProposal:
        # The unit check runs here too, so the fake cannot be used to smuggle
        # a proposal past a rule every real adapter's output is held to.
        return reject_incompatible_units(self._proposal, request.catalog)


def get_takeoff_client(provider: str | None) -> TakeoffProviderClient:
    """The one seam a real provider plugs into.

    `provider` comes from the tenant's own opt-in (§5.1) — this function does
    not decide, it dispatches. `None`, or a provider with no credentials
    configured, yields the fake: a deployment that has configured nothing
    makes no network calls and costs nothing, exactly as
    `get_accounting_client` and `stripe_client` behave.

    There is no real adapter yet, and that is deliberate rather than
    unfinished — see this module's docstring. When one is added it goes here,
    behind a function-local import so a deployment running the fake never
    imports a vendor SDK.
    """
    return FakeTakeoffProviderClient()
