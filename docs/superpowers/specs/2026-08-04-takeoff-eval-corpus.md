# The takeoff eval corpus: how to assemble one

**Status:** the prerequisite for AI blueprint takeoff. Nothing downstream of
it gets built until it produces a number.
**Companion:** `2026-08-04-ai-blueprint-takeoff-scoping.md` (why the feature
is shaped this way). **Code:** `backend/scripts/score_takeoff.py`,
`backend/app/services/takeoff_scoring.py`.

## 1. Why this document exists

The scoping doc's §5.3 ends on the one question the feature is actually
blocked on: *does any provider read a plan set well enough to be worth
putting in front of an estimator?* That is not answerable by reasoning about
it, by a demo on a vendor's own sample drawings, or by trying it once on a
plan set that happens to be open. It is answerable by measurement, and
measurement needs a corpus.

The code for the measurement is written. The corpus is not, and **cannot be
written by anyone but you** — it is real customers' drawings and the takeoffs
your estimators actually produced from them. This document is how to
assemble it.

Everything here is deliberately cheap. If assembling a corpus feels like a
project, the corpus is too big; see §6.

## 2. What one case is

One case is **one plan set, plus the takeoff a human produced from it, plus
the catalog that human was working against.** All three parts matter, and
the third is the one that gets forgotten:

```json
{
  "name": "riverside-duplex",
  "document": "sheets/riverside-duplex.pdf",
  "catalog": [
    {"id": "8f3c...", "category": "Framing", "name": "2x4 stud wall", "unit": "lf"},
    {"id": "b21e...", "category": "Interior", "name": "1/2in drywall", "unit": "sf"}
  ],
  "expected": [
    {"cost_catalog_item_id": "8f3c...", "quantity": "142.00"},
    {"cost_catalog_item_id": "b21e...", "quantity": "980.00"}
  ]
}
```

`document` is a path relative to the case file. `catalog` is the company's
catalog **as it stood when the human did the takeoff** — not today's. If the
catalog has been reorganised since, exporting today's version measures
catalog drift and reports it as provider error.

`expected` names catalog item ids, not descriptions. That is the point of
the whole exercise: §3.1 of the scoping doc argues the hard part of a
takeoff is not measuring a wall, it is deciding *which of this company's
catalog rows* the wall is. A corpus that scored on descriptions would grade
the easy half.

## 3. Where the ground truth comes from

**From estimates you already have.** Pick a completed estimate, find the plan
set it was built from, and export its lines. The estimator's own work is the
ground truth — you are not asking anyone to produce anything new.

Two things to check per case before including it:

- **Did the estimate actually come from that plan set?** An estimate padded
  from a walkthrough, a phone call, or a previous job for the same client
  is not a takeoff. Scoring against it grades the provider on information
  that was never in the document.
- **Are the free-form lines excluded?** Migration 0035 lines with no
  `cost_catalog_item_id` (permit fees, allowances, site cleanup) are not
  takeoff output — nothing in a drawing implies them. Leave them out of
  `expected` rather than scoring a provider for missing them.

## 4. What to include, and what a biased corpus costs you

Pick cases that span the work you actually sell, not the cases that
photograph well. Concretely, over a dozen cases, aim to cover:

- **Both ends of your size range.** A provider can be excellent on a
  single-family remodel and useless on a 40-sheet commercial set, and one
  number over a corpus of only small jobs will not say so.
- **At least two or three drawing qualities.** A clean architect's PDF, a
  scan of a marked-up print, and a set with hand annotations. Field
  drawings are not all CAD exports and the difference is likely to dominate.
- **A case whose catalog is genuinely messy.** Duplicate-ish rows, items
  named by supplier SKU, categories that overlap. A tidy catalog makes the
  matching problem easier than it is in production.
- **One case you expect the provider to fail.** If every case is winnable
  the corpus cannot distinguish a good provider from a lucky one.

A corpus of a dozen similar, clean, mid-size jobs will produce a confident
number that does not generalise, which is worse than no number, because it
gets acted on.

## 5. Where it lives

**Not in this repository.** These are customers' drawings. Keep the corpus on
a machine you control, and point `--corpus` at it:

```bash
cd backend
python scripts/score_takeoff.py --corpus /path/to/takeoff-corpus
```

With no `--provider`, that runs the fake, which proposes nothing. That looks
useless and is not: it proves the corpus parses, every document resolves, and
every catalog id is well-formed — and it establishes the 0% floor a real
provider has to beat. **Run it as soon as you have two cases**, long before
any vendor credential exists. Format errors are much cheaper to find then.

## 6. How big, and when to stop

**A dozen cases is enough to decide.** Not because twelve is statistically
comfortable, but because the decision this corpus feeds is coarse: a provider
that clears the bar will clear it visibly, and one that does not will fail
visibly. Spending three weeks assembling sixty cases to sharpen a number
whose two outcomes are "build it" and "don't" is the expensive way to learn
what four cases would have told you.

Start with **four**. Score them. If the result is unambiguous in either
direction, that is the answer, and the remaining eight cases are optional
confirmation. Grow the corpus only if the number lands near the bar — which
is the one situation where more evidence changes what you do.

## 7. Reading the report

The script prints two rates and never blends them, because they fail in
opposite directions:

```
Acceptance rate: 71.43%  (bar: 70.00%)
Recall:          38.10%
```

**Acceptance** is: of everything the provider proposed, how much could an
estimator keep untouched. Spurious lines count against it — a line the human
never had is one somebody has to notice and delete, which is real work even
though it prices nothing by itself.

**Recall** is: of the human's takeoff, how much did the provider find at all.

The example above clears the bar and is still a bad result. A provider that
proposes four lines it is certain of and stays silent on the other thirty-six
scores 100% acceptance while leaving the estimator to do the entire takeoff
by hand. **A high acceptance rate with low recall is not a product**, and
this is exactly why the two numbers are always printed together and there is
no combined score to quote instead.

Per-case detail is printed above the totals, and every out-of-tolerance line
is named with its error — an aggregate that says "71%" tells nobody which
sheet to go and look at.

## 8. What clearing the bar does and does not authorise

It authorises building the adapter, the route and the review UI described in
scoping §3.3 — a proposal a person edits and saves through the existing
estimate builder.

It does not authorise a takeoff writing an estimate. Nothing in this
measurement bears on that, and §3.3's constraint is unchanged by any number
this script prints: **a proposal becomes an estimate only when a person saves
it.** A 95% acceptance rate would mean one line in twenty is wrong on a
document a customer signs.
