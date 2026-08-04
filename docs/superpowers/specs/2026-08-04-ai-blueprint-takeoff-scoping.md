# AI-assisted blueprint takeoff — scoping

**Status: scoping only. No implementation proposed.**

PRD §5.2 lists this as out of scope for v1 ("Estimating via AI-driven blueprint
takeoff — manual/assisted quantity entry only in v1") and the roadmap carries
it as an unscheduled Phase 5 item. This document works out what it would
actually take, and surfaces the decisions that have to be made before any of
it is worth building.

**The headline: the model is not the hard part, and neither is the cost.** A
frontier model reads a drawing well, and a full takeoff pass on a twenty-sheet
plan set costs well under a dollar (§4). The hard parts are that a measured
quantity has to land on *this company's* catalog item (§3.2), and that a wrong
number ends up on a document a customer signs (§3.3).

**Two things are already decided, 2026-08-04, and they shape everything
below:**

- **Per-tenant opt-in.** Takeoff is off until a tenant turns it on. It is not
  a platform-wide property of the product. See §5.1.
- **Provider-agnostic.** The feature depends on a vision model, not on a
  specific vendor. Claude is an implementation, not the architecture. See §5.2
  — this is a real constraint on the design, not a preference, and it is why
  this document names capabilities rather than a product wherever it can.

---

## 1. What a takeoff has to produce

Not a report. Not a summary. The estimate builder already exists, and its unit
of work is an **estimate line item** — `app/models/estimate_line_item.py`:

| Column | Where it comes from |
|---|---|
| `cost_catalog_item_id` | Must resolve to a **visible** item in this company's catalog |
| `quantity` | `Numeric(12, 2)` — what the takeoff measures |
| `unit_rate_snapshot` | Copied from the catalog at write time; not the takeoff's business |
| `line_total` | `quantity × unit_rate_snapshot`, quantized |

So the deliverable of a takeoff is a list of `(cost_catalog_item_id, quantity)`
pairs, and everything else — pricing, markup, totals, the PDF, the signature —
is machinery that already works. That is the good news: **a takeoff is a
proposal step in front of `PUT /estimates/{id}/lines`, not a new estimate
path.** It should produce a draft the estimator reviews and saves through the
existing route, inheriting `expected_unit_rate`'s stale-rate guard (PR #128)
and the rate-conflict recovery (#131) for free.

The input already exists too. Blueprints are uploaded through
`POST /projects/{id}/documents` and stored on disk under `STORAGE_ROOT`
(`documents.storage_path`), and that table is **immutable** — migration 0004
revokes UPDATE and DELETE, and new versions are new rows. A takeoff can
therefore cite the exact document row it read, and that citation cannot later
be edited out from under it.

## 2. What a capable vision model can do today

Two capabilities the feature depends on. The specifics below are **one
provider's numbers, quoted because they are the ones that were to hand** —
they are here to establish that the capability exists and is adequate, not to
select a vendor. Every one of them moves, and every provider states them
differently; re-check at implementation time and per provider (§5.2):

- **High-resolution vision.** Claude Opus 5 and Sonnet 5 accept images up to
  **2576 px on the long edge** and return coordinates that map 1:1 to image
  pixels — no scale-factor arithmetic. That matters here specifically:
  architectural sheets are dense, and a model that has to work from a
  downsampled 1568 px image loses dimension strings and hatch patterns.
- **PDFs are a native input type.** A `document` content block takes a base64
  PDF directly (32 MB per request, 600 pages on a 1M-context model), or a
  `file_id` from the Files API (500 MB, upload once, reference across many
  calls). Blueprints are big and get read repeatedly — the Files API is the
  right shape, and it keeps the bytes out of every subsequent request body.

One piece of published guidance generalises beyond any single vendor and is
worth designing around: **give the model tools to crop and re-examine its own
work rather than more thinking budget.** A takeoff is exactly that shape —
find the scale, find the schedule, zoom the elevation, count the openings — so
the architecture should be a tool-using loop over a sheet rather than a single
one-shot prompt, whichever provider is behind it.

## 3. The three problems, in order of difficulty

### 3.1 Measurement — the part that mostly works

Counting fixtures, reading a door/window schedule, tracing a wall run against
a stated scale. This is what the model is good at, and it is the part everyone
imagines when they hear "AI takeoff." It is not where the risk is.

### 3.2 Catalog resolution — the part nobody pictures

A measurement is `"142 linear feet of interior partition, 2×4 @ 16 o.c."`. A
line item needs `cost_catalog_item_id`. Nothing about the drawing says which of
**this company's** catalog rows that is — and the catalog is per-tenant, freely
named, and inheritance-resolved (a branch may override its parent's item, per
`resolve_visible_catalog_items`). Two builders with the same drawing produce
different line items because they keep different catalogs.

This is the actual engineering problem, and it is a *matching* problem rather
than a vision one. The catalog is small enough to hand to the model
(`unit`, `category`, `name` for a few hundred items is a few thousand tokens,
and it is stable enough to sit behind a cache breakpoint), so the plausible
approach is to give the model the catalog and have it propose the mapping —
but that means **the model chooses which of the company's priced items a
measurement becomes**, and a wrong choice is a wrong price, not a wrong
number. The `unit` column is the one hard check available: a measurement in
linear feet must not land on an item priced per square foot, and that is
worth enforcing in code rather than trusting.

**A takeoff that measures perfectly and maps badly is worse than no takeoff**,
because the quantities look right.

### 3.3 The accuracy bar — the part that decides whether this ships

An estimate becomes a PDF, gets sent for signature, and is e-signed by a
customer. A quantity that is wrong by 20% is a number a builder is contractually
committed to.

This codebase has been here before and has an established answer. Twice now —
`expected_unit_rate` (#128) and the field crew's `expected_status` (#134) — the
rule has been: **when a value might have moved underneath a human, refuse to
apply it silently and show them both numbers.** The same discipline applies
here and is the thing to design around:

- A takeoff produces a **proposal**, never a saved estimate.
- Every proposed line carries what it was derived from — the sheet, and
  ideally the page — so the estimator can check it against the drawing rather
  than against their memory of the drawing.
- Nothing is written until a person saves it through the existing builder.

That is not a nice-to-have. It is the difference between a feature that
accelerates an estimator and one that quietly mis-bids a job.

**Provenance is a requirement of the interface, and how a provider earns it is
its own business.** Under §5.2 this is the clearest example of why the seam
belongs at the domain and not at the vendor: providers differ sharply in what
they offer here, and at least one has a constraint that would otherwise leak
into the whole design.

Concretely, on Claude, citations (`citations: {enabled: true}` on a document
block, returning a 1-indexed `page_location`) and structured outputs
(`output_config.format`, guaranteeing parseable JSON) are **mutually
exclusive** — requesting both returns a 400. So a Claude adapter must either
extract with citations and structure in a cheap second pass, or accept page
numbers the model merely asserts. Given §3.3 the two-pass form is the right
call there, because provenance the reviewer can trust is the point.

But that is a fact about one adapter, not about the feature. Another provider
may hand back both in one call, or neither. **The interface asks for a line
with its provenance; the adapter does whatever it must to produce one.**

## 4. What it costs

Worth doing plainly, because the intuition that "AI on every blueprint" is
expensive turns out to be wrong at these prices.

Worked with one provider's published prices, because a concrete number beats
an adjective. **The conclusion is what travels, not the arithmetic** — frontier
vision pricing across vendors is close enough that a different provider moves
these figures by tens of percent, not orders of magnitude, and the point below
survives either way.

A full-resolution sheet costs up to ~4,784 input tokens. Taking a
twenty-sheet plan set, a few hundred catalog items in the prompt, and a few
thousand output tokens:

| | Opus 5 ($5 / $25 per MTok) | Sonnet 5 ($3 / $15) |
|---|---|---|
| 20 sheets in | ~96K tokens ≈ $0.48 | ≈ $0.29 |
| Catalog + instructions | ~10K tokens ≈ $0.05 | ≈ $0.03 |
| Output (~4K tokens) | ≈ $0.10 | ≈ $0.06 |
| **Per takeoff pass** | **≈ $0.60** | **≈ $0.38** |

Three levers cut it further, and all three fit this workload:

- **Prompt caching** on the catalog and instructions — cache reads are ~0.1×,
  and the catalog is exactly the stable prefix caching is designed for.
- **The Batch API** — 50% off, and a takeoff is inherently asynchronous:
  nobody watches a twenty-sheet parse in real time.
- **Sheet triage** — most of a plan set is irrelevant to any one trade. A
  cheap first pass that picks the sheets worth reading in full is the largest
  saving available.

**So cost is not the constraint.** At well under a dollar a pass, the model
spend is noise next to an estimator's hour. That reframes the whole feature:
the question is not "can we afford to run this" but "is the output good enough
to be worth reviewing," and it means the design should spend tokens freely on
verification — re-reading, cross-checking, a second opinion on a suspicious
quantity — rather than economising toward a cheaper, shakier answer.

It also means **per-use metering is probably not worth building.** A flat tier
gate is simpler and honest at this price point.

## 5. Decisions

### 5.1 Per-tenant opt-in — decided 2026-08-04

**Takeoff is off until a tenant turns it on.** Not a platform-wide property of
the product, and not a term buried in a contract nobody reads.

The mechanic that forces the question: a vision model cannot read a drawing
that has not been sent to it. Today a blueprint sits on this deployment's disk
and never leaves; with takeoff it is transmitted to a third-party API,
processed, and returned. That is a narrow and boring fact — no person at the
vendor opens the file, and the major providers do not train on API inputs —
but it changes what a builder can say to *their* customer. A blueprint is
usually the architect's or the owner's document, and builders sometimes hold
agreements that speak to sharing drawings with third parties.

So the decision is who gets to say yes, and the answer is: the tenant, per
tenant, visibly. This is the same shape as the offline capture screen's
"Make available offline" (design §8.1) — an exposure the customer chose and
can see beats one that happens to them — and it means one cautious enterprise
customer cannot block the feature for everyone else.

Two consequences worth designing for rather than discovering:

- **The setting stores a provider, not a boolean.** Given §5.2, "yes to AI" is
  not the granularity a procurement review works in — "yes to *this* vendor"
  is. A tenant may permit one and refuse another.
- **Tenant-supplied credentials are the natural extension.** An enterprise
  tenant that wants the drawing to go to *their* vendor account under *their*
  agreement can supply their own API key. The pattern already exists: the
  email-server tab stores tenant-supplied credentials encrypted with the
  integrations key, never returned by any route
  (`company_email_settings`). Doing the same here dissolves most of the
  objection for exactly the customers most likely to raise it — and it moves
  the marginal cost to them, which changes the pricing question in §5.5.

### 5.2 Provider-agnostic — decided 2026-08-04

**The feature depends on a vision model, not on a vendor.** No provider's SDK,
model id, or response shape may reach outside the adapter that speaks to it.

This is a real design constraint, and the naive reading of it is a trap:
abstracting to the *intersection* of what every provider offers would strip
out exactly the capabilities that make a takeoff reviewable — high-resolution
input, cited pages, schema-guaranteed output. The seam has to sit at the
**domain**, not at the request:

> Given the sheets of a plan set and this company's resolved catalog, propose
> line items — each with a `cost_catalog_item_id`, a quantity, the unit it was
> measured in, where in the document it came from, and how confident the
> proposal is.

Everything above that line is the product; everything below it is one
provider's problem. Whether an adapter gets there in one call or three,
whether it uses native PDF input or rasterises sheets itself, whether it earns
provenance through a citations API or a second pass (§3.3) — all of that is
adapter-local.

This codebase already has the pattern twice, and it should be copied rather
than reinvented: `app/services/accounting_client.py` defines an
`AccountingProviderClient` Protocol with `RealQuickBooksClient` /
`RealFreshBooksClient` / `FakeAccountingProviderClient`, selected **per
provider** by that provider's own credentials being configured, with the fake
as the default so tests, CI and a local `docker compose up` make no network
calls. `stripe_client.py` established the same shape before it. A
`TakeoffProviderClient` Protocol plus a fake is the third instance, and the
fake matters as much here as it does there: **the entire test suite should
exercise the fake**, or the suite becomes slow, expensive, and
non-deterministic.

Three things this decision costs, stated so they are not a surprise:

- **Quality is not portable.** The accuracy bar (§5.3) has to be measured per
  provider against the same corpus. "It works" on one vendor does not transfer,
  and the corpus becomes the instrument that decides which provider a tenant
  should choose — a second reason it is the real prerequisite.
- **The interface must not promise what a provider cannot deliver.** If one
  adapter cannot produce page-level provenance at all, that is a fact the
  interface has to be able to express (provenance present or absent), not one
  it can paper over.
- **More surface to maintain.** Every provider is an adapter, an eval run, and
  a set of credentials. That is the price of not being locked in, and it is
  worth paying only if a second adapter actually gets written — one Protocol
  with one implementation is a Protocol with extra steps.

### 5.3 What is the accuracy bar, stated as a number? — open, and blocking

Not "accurate." Something like: *a takeoff is useful if an estimator accepts
≥70% of proposed lines unchanged and the error on accepted quantities is
within ±5%.* Without a number there is no way to tell whether a build
succeeded, no way to decide when to stop tuning, and — given §5.2 — no way to
compare two providers.

**Measuring it needs a corpus**: a dozen real plan sets with the takeoffs a
human actually produced from them. Assembling that is the genuine prerequisite
to this feature, it cannot be done after the fact, and it is the one piece of
work that is worth starting before any of the rest is decided.

### 5.4 Which model, and where the effort dial sits — open

Recommendation: pick one capable provider, run at high effort with a
crop/re-examine tool, and measure against the corpus to establish a quality
ceiling. Only then try cheaper models and lower effort settings — in that
order, so a later regression is attributable to the thing that caused it.

### 5.5 Tier and gating — open

Estimation is `pro` today (`MODULE_MIN_TIER["estimation"]`). Takeoff is
plausibly `enterprise`, and it would be the first module whose gate protects a
**real marginal cost** rather than a feature boundary. §4 argues per-use
metering is not worth building at these prices — but note that §5.1's
tenant-supplied-key path moves the cost to the tenant entirely, which may make
the tier question smaller than it looks.

## 6. What a first version should be

Deliberately smaller than the phrase "AI takeoff" suggests:

- **One trade, not all of them.** Pick the one with the cleanest drawing
  convention — door/window schedules are tabular and explicitly enumerated,
  which makes them the obvious first target and a fair test of §3.2 without
  also betting on area tracing.
- **Proposal only.** Output lands in the existing builder as an unsaved draft.
  No new write path, no new approval flow.
- **Provenance on every line**, or the review step is theatre.
- **A unit-compatibility check in code**, not in the prompt.
- **An eval corpus before the feature**, because §5.3 cannot be answered
  afterwards.
- **One provider behind the Protocol, plus the fake** — and the Protocol
  written as if a second adapter exists, because it will. Writing the second
  adapter is not a first-version job; making the second adapter *possible*
  without reshaping the feature is.

And explicitly not, in a first version: area/volume tracing from scaled
geometry, revision comparison between drawing versions, anything that writes
an estimate without a human, any per-use billing, and a second provider
adapter.

---

## Sources

Codebase as of `5eaf230`: `app/models/estimate_line_item.py`,
`app/models/cost_catalog_item.py`, `app/models/document.py`,
`app/services/catalog_resolution.py`, `app/routers/estimates.py`
(`replace_estimate_line_items`), `app/core/tier_gating.py`, `app/config.py`
(`storage_root`), and migration 0004's REVOKE.
`app/services/accounting_client.py` and `app/services/stripe_client.py` are the
Protocol-plus-fake precedent §5.2 says to copy;
`app/routers/company_email_settings.py` is the tenant-supplied-credential
precedent §5.1 points at.

Model capabilities, limits and prices are **one provider's, quoted to
establish that the capability exists and to put a real number on the cost** —
vision resolution and image-token ceiling, PDF and Files API limits, the
citations/structured-outputs incompatibility, batch and caching economics, all
from the `claude-api` skill's current reference. **Re-verify at implementation
time, and separately per provider: every one of these numbers moves, and none
of them is a commitment to a vendor** (§5.2).

PRD §5.2 and §8 for the scope boundary, and
`2026-08-02-offline-capture-screen-design.md` §8.1 for the data-boundary
precedent §5.1 follows.
