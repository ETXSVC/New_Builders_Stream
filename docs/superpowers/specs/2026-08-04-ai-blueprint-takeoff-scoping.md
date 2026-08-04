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

## 2. What the model can do today

Two facts worth knowing before scoping the work, both current as of this
writing and both worth re-checking at implementation time:

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

The published guidance for vision work on Opus 5 is also directly relevant:
**give the model tools to crop and re-examine its own work rather than more
thinking budget.** A takeoff is exactly that shape — find the scale, find the
schedule, zoom the elevation, count the openings — so the architecture should
be a tool-using loop over a sheet, not a single one-shot prompt.

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

**An API constraint shapes this, and it is a genuine fork.** Citations
(`citations: {enabled: true}` on a document block, which returns
`page_location` with 1-indexed page numbers) and structured outputs
(`output_config.format`, which guarantees parseable JSON) are **mutually
exclusive** — requesting both returns a 400. So either:

- **(a) Structured JSON, unverified provenance.** Parseable line items; page
  numbers only as free-form fields the model asserts, with nothing checking
  them.
- **(b) Citations, then a second structuring pass.** The extraction call cites
  real pages; a second, cheap call turns that text into JSON. Two calls, and
  the provenance is real.

Given §3.3, (b) is the one that matches how this codebase treats numbers a
human has to trust. It should be a stated decision, not a side effect of
whichever one gets written first.

## 4. What it costs

Worth doing plainly, because the intuition that "AI on every blueprint" is
expensive turns out to be wrong at these prices.

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

## 5. What has to be decided first

In this order. The first two are product and security calls, not engineering
ones, and the rest do not matter until they are answered.

1. **May a tenant's blueprints be sent to a third party at all?** A blueprint
   is a customer's building. Sending it to Anthropic's API means it leaves the
   RLS boundary and this deployment entirely — the same class of decision as
   caching the cost catalog on a device (offline capture design §8.1), and it
   should be made the same way: explicitly, per tenant, and visibly.
   Anthropic's API does not train on API inputs, but "does not train on it" and
   "never left our infrastructure" are different promises, and some customers
   have contracts that care about the second. **If the answer is no, or
   "only for tenants who opt in," that shapes everything downstream.**

2. **What is the accuracy bar, stated as a number?** Not "accurate." Something
   like: *a takeoff is useful if an estimator accepts ≥70% of proposed lines
   unchanged and the error on accepted quantities is within ±5%.* Without a
   number there is no way to tell whether a build succeeded, and no way to
   decide when to stop tuning. **Measuring this needs a corpus** — a dozen real
   plan sets with the takeoffs a human actually produced from them — and
   getting that corpus is the real prerequisite, not any of the code.

3. **Citations or structured output** (§3.3). Recommendation: citations plus a
   structuring pass, because provenance is what makes the review step real.

4. **Which model, and where the effort dial sits.** Recommendation: start at
   Opus 5 with high effort and a crop/re-examine tool, measure against the
   corpus, then try Sonnet 5 and lower effort as cost/latency optimisations —
   in that order. Establishing the quality ceiling first means a later
   regression is attributable.

5. **Tier and gating.** Estimation is `pro` today
   (`MODULE_MIN_TIER["estimation"]`). Takeoff is plausibly `enterprise`, and it
   would be the first module whose gate protects a real marginal cost rather
   than just a feature boundary — worth naming as a new kind of gate rather
   than assuming the existing one transfers.

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
- **An eval corpus before the feature**, because §5.2 cannot be answered
  afterwards.

And explicitly not, in a first version: area/volume tracing from scaled
geometry, revision comparison between drawing versions, anything that writes
an estimate without a human, and any per-use billing.

---

## Sources

Codebase as of `5eaf230`: `app/models/estimate_line_item.py`,
`app/models/cost_catalog_item.py`, `app/models/document.py`,
`app/services/catalog_resolution.py`, `app/routers/estimates.py`
(`replace_estimate_line_items`), `app/core/tier_gating.py`, `app/config.py`
(`storage_root`), and migration 0004's REVOKE. Model capabilities, limits and
pricing from the `claude-api` skill's current reference (vision resolution and
image-token ceiling, PDF and Files API limits, the
citations/structured-outputs incompatibility, batch and caching economics) —
**re-verify these at implementation time; they move.** PRD §5.2 and §8 for the
scope boundary, and `2026-08-02-offline-capture-screen-design.md` §8.1 for the
data-boundary precedent this document's decision 1 follows.
